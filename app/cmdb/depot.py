"""Lecture et écriture de l'inventaire.

Tout ce qui touche SQL passe par ici. Les appelants manipulent des *lignes* : un
dictionnaire par objet, indexé par nom de champ, où une liste déroulante vaut
`{"id", "value", "color"}` et un lien `[{"id", "value"}]`. Les écritures acceptent la
forme simple — un libellé d'option, une liste d'identifiants.

Trois usages, une seule implémentation : le collecteur (sans journal pour ses mises à jour
de routine), la console et l'API (journalisées, avec leur auteur).
"""

import hashlib
import json
import logging
import secrets
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import db, modele

logger = logging.getLogger("cmdb.depot")

PREFIXE_JETON = "tcmdb_"


class ErreurDonnee(Exception):
    """Écriture refusée par le modèle : champ inconnu, option inexistante, doublon…"""


class BaseInaccessible(Exception):
    """Le fichier de la base ne s'ouvre pas : répertoire absent, droits, disque plein."""


def maintenant():
    return datetime.now(timezone.utc).isoformat()


def _vrai(valeur):
    if isinstance(valeur, str):
        return valeur.strip().lower() in ("1", "true", "oui", "yes", "on", "vrai", "x")
    return bool(valeur)


def _texte(valeur):
    if valeur is None:
        return None
    texte = str(valeur).strip()
    return texte or None


def _entier(valeur, nom):
    if valeur is None or (isinstance(valeur, str) and not valeur.strip()):
        return None
    if isinstance(valeur, bool):
        return int(valeur)
    try:
        return int(float(str(valeur).replace(",", ".")))
    except ValueError:
        raise ErreurDonnee(f"{nom} : nombre attendu, « {valeur} » reçu") from None


def _ids(valeur, nom):
    """Un lien s'écrit comme Baserow l'acceptait : liste d'identifiants, liste de
    `{"id": …}`, identifiant seul, ou rien."""
    if valeur is None or valeur == "":
        return []
    if not isinstance(valeur, (list, tuple, set)):
        valeur = [valeur]
    ids = []
    for v in valeur:
        if isinstance(v, dict):
            v = v.get("id")
        if v is None or v == "":
            continue
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            raise ErreurDonnee(f"{nom} : identifiant invalide (« {v} »)") from None
    return sorted(set(ids))


def affichage(valeur):
    """Une valeur de ligne, en texte, pour le journal et l'export."""
    if valeur is None:
        return ""
    if isinstance(valeur, bool):
        return "oui" if valeur else "non"
    if isinstance(valeur, dict):
        return str(valeur.get("value") or "")
    if isinstance(valeur, list):
        return ", ".join(str(v.get("value") if isinstance(v, dict) else v) for v in valeur)
    return str(valeur)


def simple(valeur):
    """Une valeur de ligne, sous la forme qu'on écrirait : libellés et identifiants."""
    if isinstance(valeur, dict):
        return valeur.get("value")
    if isinstance(valeur, list):
        if valeur and isinstance(valeur[0], dict) and "color" in valeur[0]:
            return [v.get("value") for v in valeur]
        return [v.get("id") if isinstance(v, dict) else v for v in valeur]
    return valeur


class _Lecture:
    """Une passe de lecture : les options et les noms des lignes visées sont lus une fois."""

    def __init__(self, conn):
        self.conn = conn
        self.options = {r["id"]: {"id": r["id"], "value": r["valeur"], "color": r["couleur"],
                                  "_ordre": (r["ordre"], r["valeur"].lower())}
                        for r in conn.execute("SELECT * FROM option")}
        self._noms = {}
        self.perso = defaultdict(list)
        for r in conn.execute("SELECT * FROM champ_perso ORDER BY ordre, id"):
            self.perso[r["tab"]].append(dict(r))

    def option(self, ident):
        o = self.options.get(ident)
        return {k: v for k, v in o.items() if not k.startswith("_")} if o else None

    def nom(self, kind, ident):
        if kind not in self._noms:
            t = modele.table(kind)
            col = modele.champ(kind, t.cle).colonne
            self._noms[kind] = dict(self.conn.execute(f"SELECT id, {col} FROM {t.sql}").fetchall())
        return self._noms[kind].get(ident)

    def lien(self, kind, ident):
        return {"id": ident, "value": self.nom(kind, ident)}

    def lignes(self, kind, ids=None):
        t = modele.table(kind)
        filtre, params = "", []
        if ids is not None:
            ids = list(ids)
            if not ids:
                return []
            filtre = f" WHERE id IN ({','.join('?' * len(ids))})"
            params = ids
        base = self.conn.execute(f"SELECT * FROM {t.sql}{filtre} ORDER BY id", params).fetchall()
        if not base:
            return []
        voulus = {r["id"] for r in base}

        choix = defaultdict(list)
        for r in self.conn.execute("SELECT champ, ligne_id, option_id FROM choix WHERE tab = ?",
                                   (kind,)):
            if r["ligne_id"] in voulus and r["option_id"] in self.options:
                choix[(r["champ"], r["ligne_id"])].append(self.options[r["option_id"]])

        relations = {}
        for c in t.champs:
            if c.type == "liens":
                jonction, src, dst = c.via
                paires = defaultdict(list)
                for a, b in self.conn.execute(f"SELECT {src}, {dst} FROM {jonction} ORDER BY {dst}"):
                    paires[a].append(b)
                relations[c.nom] = paires
            elif c.type == "inverse":
                kind_source, nom_source = c.via
                source = modele.champ(kind_source, nom_source)
                paires = defaultdict(list)
                if source.type == "lien":
                    requete = (f"SELECT {source.colonne}, id FROM {modele.table(kind_source).sql} "
                               f"WHERE {source.colonne} IS NOT NULL ORDER BY id")
                else:
                    jonction, src, dst = source.via
                    requete = f"SELECT {dst}, {src} FROM {jonction} ORDER BY {src}"
                for a, b in self.conn.execute(requete):
                    paires[a].append(b)
                relations[c.nom] = paires

        persos = defaultdict(dict)
        champs_perso = {p["id"]: p for p in self.perso.get(kind, [])}
        if champs_perso:
            for r in self.conn.execute(
                    "SELECT champ_id, ligne_id, valeur FROM valeur_perso WHERE champ_id IN "
                    f"({','.join('?' * len(champs_perso))})", list(champs_perso)):
                if r["ligne_id"] in voulus:
                    persos[r["ligne_id"]][r["champ_id"]] = r["valeur"]

        sortie = []
        for r in base:
            ligne = {"id": r["id"]}
            for c in t.champs:
                if c.type in ("texte", "long", "url", "date", "horodatage"):
                    ligne[c.nom] = r[c.colonne]
                elif c.type == "entier":
                    ligne[c.nom] = r[c.colonne]
                elif c.type == "bool":
                    ligne[c.nom] = bool(r[c.colonne])
                elif c.type == "select":
                    ligne[c.nom] = self.option(r[c.colonne]) if r[c.colonne] is not None else None
                elif c.type == "multi":
                    ligne[c.nom] = self._multi(choix.get((c.nom, r["id"]), []))
                elif c.type == "lien":
                    cible = r[c.colonne]
                    ligne[c.nom] = [self.lien(c.cible, cible)] if cible is not None else []
                else:  # liens, inverse
                    ligne[c.nom] = [self.lien(c.cible, i) for i in relations[c.nom].get(r["id"], [])]
            for p in self.perso.get(kind, []):
                ligne[p["nom"]] = self._perso(p, persos[r["id"]].get(p["id"]),
                                              choix.get((p["nom"], r["id"]), []))
            sortie.append(ligne)
        return sortie

    def _multi(self, options):
        return [{k: v for k, v in o.items() if not k.startswith("_")}
                for o in sorted(options, key=lambda o: o["_ordre"])]

    def _perso(self, p, brut, choix):
        if p["type"] == "multi":
            return self._multi(choix)
        if p["type"] == "bool":
            return brut == "1"
        if brut is None:
            return None
        if p["type"] == "entier":
            try:
                return int(brut)
            except ValueError:
                return None
        if p["type"] == "select":
            try:
                return self.option(int(brut))
            except ValueError:
                return None
        return brut


class Depot:
    def __init__(self, chemin, paresseux=False):
        """`paresseux` : n'ouvrir la base qu'au premier usage, et réessayer à chaque usage
        tant qu'elle ne s'ouvre pas. C'est le cas de la console : une base illisible
        (répertoire mal monté, droits) ne doit pas l'empêcher de démarrer, mais lui laisser
        afficher la page qui dit quoi corriger. Le collecteur, lui, s'arrête net."""
        self.chemin = str(chemin)
        self._cx = db.Connexions(self.chemin)
        self._veille = None
        self._verrou_veille = threading.Lock()
        if not paresseux:
            self._pret()

    def _pret(self):
        if self._veille is not None:
            return
        with self._verrou_veille:
            if self._veille is None:
                try:
                    db.initialiser(self.chemin)
                except Exception as exc:
                    raise BaseInaccessible(str(exc) or exc.__class__.__name__) from exc
                # Connexion réservée à la surveillance : `PRAGMA data_version` ne change
                # que pour les écritures faites par *une autre* connexion, et ne se compare
                # qu'à lui-même. Une connexion qui n'écrit jamais voit donc passer toutes
                # les écritures, celles du collecteur comme celles des autres threads de la
                # console.
                self._veille = db.connecter(self.chemin, partagee=True)

    @property
    def conn(self):
        self._pret()
        return self._cx.conn

    def transaction(self):
        self._pret()
        return self._cx.transaction()

    def version_donnees(self):
        self._pret()
        with self._verrou_veille:
            return self._veille.execute("PRAGMA data_version").fetchone()[0]

    # ------------------------------------------------------------------ lecture

    def lire_tout(self):
        """Les six tables, les options et les champs personnalisés, lus dans une même
        transaction de lecture : un instantané cohérent, même si le collecteur écrit."""
        conn = self.conn
        conn.execute("BEGIN")
        try:
            lecture = _Lecture(conn)
            lignes = {kind: lecture.lignes(kind) for kind in modele.TABLES}
            options = self._options_par_champ(conn)
            perso = {k: list(v) for k, v in lecture.perso.items()}
        finally:
            conn.execute("COMMIT")
        return lignes, options, perso

    def lignes(self, kind, ids=None):
        return _Lecture(self.conn).lignes(kind, ids)

    def ligne(self, kind, ident):
        trouvees = self.lignes(kind, [ident])
        return trouvees[0] if trouvees else None

    def compter(self, kind):
        return self.conn.execute(f"SELECT COUNT(*) FROM {modele.table(kind).sql}").fetchone()[0]

    # ------------------------------------------------------------------ écriture

    def _option_id(self, conn, kind, nom, valeur, creer):
        if isinstance(valeur, dict):
            valeur = valeur.get("id") or valeur.get("value")
        if isinstance(valeur, int) and not isinstance(valeur, bool):
            ligne = conn.execute("SELECT id FROM option WHERE id = ? AND tab = ? AND champ = ?",
                                 (valeur, kind, nom)).fetchone()
            if not ligne:
                raise ErreurDonnee(f"{nom} : option {valeur} inconnue")
            return valeur
        texte = _texte(valeur)
        if texte is None:
            return None
        ligne = conn.execute("SELECT id FROM option WHERE tab = ? AND champ = ? AND valeur = ?",
                             (kind, nom, texte)).fetchone()
        if ligne:
            return ligne["id"]
        # Repli insensible à la casse : un libellé écrit « auto » par une source qui ne
        # connaît que la convention, et l'option « Auto » de la base, sont la même chose.
        ligne = conn.execute("SELECT id FROM option WHERE tab = ? AND champ = ? "
                             "AND lower(valeur) = lower(?)", (kind, nom, texte)).fetchone()
        if ligne:
            return ligne["id"]
        if not creer:
            raise ErreurDonnee(f"{nom} : option inconnue (« {texte} »). Elle se crée dans "
                               "Administration → Listes.")
        ordre = conn.execute("SELECT COALESCE(MAX(ordre), 0) + 1 FROM option WHERE tab = ? "
                             "AND champ = ?", (kind, nom)).fetchone()[0]
        cur = conn.execute("INSERT INTO option (tab, champ, valeur, couleur, ordre) "
                           "VALUES (?, ?, ?, 'gray', ?)", (kind, nom, texte, ordre))
        logger.warning("Option créée à la volée : %s.%s = %r", kind, nom, texte)
        return cur.lastrowid

    def _verifier_cible(self, conn, kind, ident, nom):
        t = modele.table(kind)
        if not conn.execute(f"SELECT 1 FROM {t.sql} WHERE id = ?", (ident,)).fetchone():
            raise ErreurDonnee(f"{nom} : aucune ligne {t.singulier} n°{ident}")

    def _perso_par_nom(self, conn, kind):
        return {r["nom"]: dict(r) for r in
                conn.execute("SELECT * FROM champ_perso WHERE tab = ?", (kind,))}

    def ecrire(self, kind, ident, champs, acteur=None, creer_options=False, id_impose=None,
               verifier=True):
        """Crée (`ident` None) ou met à jour une ligne. Seuls les champs présents dans
        `champs` sont touchés. Renvoie la ligne telle qu'elle est après écriture.

        `acteur` nomme l'auteur au journal ; sans lui, rien n'y est écrit pour une mise à
        jour — c'est le cas des passes du collecteur, qui réécrivent `Last_seen` sur deux
        cents lignes à chaque cycle.
        """
        t = modele.table(kind)
        with self.transaction() as conn:
            perso = self._perso_par_nom(conn, kind)
            colonnes, multis, liens, persos = {}, {}, {}, {}
            for nom, valeur in champs.items():
                c = modele.champ(kind, nom)
                if c is None:
                    if nom in perso:
                        persos[nom] = (perso[nom], valeur)
                        continue
                    raise ErreurDonnee(f"champ inconnu sur {t.libelle} : {nom}")
                if c.type == "inverse":
                    raise ErreurDonnee(f"{nom} : champ calculé, il se modifie depuis "
                                       f"{modele.table(c.cible).libelle}")
                if c.type in ("texte", "long", "url", "date", "horodatage"):
                    colonnes[c.colonne] = _texte(valeur)
                elif c.type == "entier":
                    colonnes[c.colonne] = _entier(valeur, nom)
                elif c.type == "bool":
                    colonnes[c.colonne] = 1 if _vrai(valeur) else 0
                elif c.type == "select":
                    colonnes[c.colonne] = self._option_id(conn, kind, nom, valeur, creer_options)
                elif c.type == "multi":
                    valeurs = valeur if isinstance(valeur, (list, tuple, set)) else (
                        [] if valeur in (None, "") else [valeur])
                    multis[nom] = sorted({self._option_id(conn, kind, nom, v, creer_options)
                                          for v in valeurs} - {None})
                elif c.type == "lien":
                    ids = _ids(valeur, nom)
                    if verifier:
                        for i in ids:
                            self._verifier_cible(conn, c.cible, i, nom)
                    colonnes[c.colonne] = ids[0] if ids else None
                elif c.type == "liens":
                    ids = _ids(valeur, nom)
                    if verifier:
                        for i in ids:
                            self._verifier_cible(conn, c.cible, i, nom)
                    liens[c] = ids

            avant = self.ligne(kind, ident) if (ident is not None and acteur) else None
            if ident is not None and avant is None and acteur:
                raise ErreurDonnee(f"aucune ligne {t.singulier} n°{ident}")

            try:
                if ident is None:
                    if id_impose is not None:
                        colonnes["id"] = id_impose
                    noms = list(colonnes)
                    if noms:
                        cur = conn.execute(
                            f"INSERT INTO {t.sql} ({', '.join(noms)}) VALUES "
                            f"({', '.join('?' * len(noms))})", [colonnes[n] for n in noms])
                    else:
                        cur = conn.execute(f"INSERT INTO {t.sql} DEFAULT VALUES")
                    ident = cur.lastrowid
                elif colonnes:
                    affectations = ", ".join(f"{n} = ?" for n in colonnes)
                    cur = conn.execute(f"UPDATE {t.sql} SET {affectations} WHERE id = ?",
                                       [*colonnes.values(), ident])
                    if cur.rowcount == 0:
                        raise ErreurDonnee(f"aucune ligne {t.singulier} n°{ident}")
            except sqlite3.IntegrityError as exc:
                raise ErreurDonnee(self._motif_integrite(kind, exc, champs)) from exc

            for nom, ids in multis.items():
                conn.execute("DELETE FROM choix WHERE tab = ? AND champ = ? AND ligne_id = ?",
                             (kind, nom, ident))
                conn.executemany("INSERT INTO choix (tab, champ, ligne_id, option_id) "
                                 "VALUES (?, ?, ?, ?)", [(kind, nom, ident, i) for i in ids])
            for c, ids in liens.items():
                jonction, src, dst = c.via
                conn.execute(f"DELETE FROM {jonction} WHERE {src} = ?", (ident,))
                conn.executemany(f"INSERT INTO {jonction} ({src}, {dst}) VALUES (?, ?)",
                                 [(ident, i) for i in ids])
            for nom, (p, valeur) in persos.items():
                self._ecrire_perso(conn, kind, ident, p, valeur, creer_options)

            apres = self.ligne(kind, ident)
            if acteur:
                libelle = apres.get(t.cle)
                if avant is None:
                    self._journaliser(conn, acteur, "création", kind, ident, libelle)
                else:
                    for nom in champs:
                        a, b = affichage(avant.get(nom)), affichage(apres.get(nom))
                        if a != b:
                            self._journaliser(conn, acteur, "modification", kind, ident,
                                              libelle, nom, a, b)
            return apres

    def creer(self, kind, champs, acteur=None, creer_options=False):
        ligne = self.ecrire(kind, None, champs, acteur=acteur, creer_options=creer_options)
        return ligne

    def modifier(self, kind, ident, champs, acteur=None, creer_options=False):
        return self.ecrire(kind, ident, champs, acteur=acteur, creer_options=creer_options)

    def _ecrire_perso(self, conn, kind, ident, p, valeur, creer_options):
        if p["type"] == "multi":
            valeurs = valeur if isinstance(valeur, (list, tuple, set)) else (
                [] if valeur in (None, "") else [valeur])
            ids = sorted({self._option_id(conn, kind, p["nom"], v, creer_options)
                          for v in valeurs} - {None})
            conn.execute("DELETE FROM choix WHERE tab = ? AND champ = ? AND ligne_id = ?",
                         (kind, p["nom"], ident))
            conn.executemany("INSERT INTO choix (tab, champ, ligne_id, option_id) VALUES "
                             "(?, ?, ?, ?)", [(kind, p["nom"], ident, i) for i in ids])
            return
        if p["type"] == "bool":
            brut = "1" if _vrai(valeur) else "0"
        elif p["type"] == "entier":
            n = _entier(valeur, p["nom"])
            brut = None if n is None else str(n)
        elif p["type"] == "select":
            i = self._option_id(conn, kind, p["nom"], valeur, creer_options)
            brut = None if i is None else str(i)
        else:
            brut = _texte(valeur)
        if brut is None:
            conn.execute("DELETE FROM valeur_perso WHERE champ_id = ? AND ligne_id = ?",
                         (p["id"], ident))
        else:
            conn.execute("INSERT INTO valeur_perso (champ_id, ligne_id, valeur) VALUES (?, ?, ?) "
                         "ON CONFLICT (champ_id, ligne_id) DO UPDATE SET valeur = excluded.valeur",
                         (p["id"], ident, brut))

    @staticmethod
    def _motif_integrite(kind, exc, champs):
        message = str(exc)
        t = modele.table(kind)
        if "UNIQUE" in message:
            return f"{t.cle} « {champs.get(t.cle)} » existe déjà dans {t.libelle}"
        if "NOT NULL" in message:
            return f"{t.cle} : ce champ est obligatoire"
        if "FOREIGN KEY" in message:
            return "référence vers une ligne qui n'existe pas"
        return message

    def supprimer(self, kind, ident, acteur=None):
        t = modele.table(kind)
        with self.transaction() as conn:
            avant = self.ligne(kind, ident)
            if avant is None:
                raise ErreurDonnee(f"aucune ligne {t.singulier} n°{ident}")
            conn.execute(f"DELETE FROM {t.sql} WHERE id = ?", (ident,))
            if acteur:
                # La ligne entière part au journal : sans porte de secours, c'est ce qui
                # permet de la retrouver — et de la ressaisir — après une erreur.
                contenu = {k: simple(v) for k, v in avant.items()
                           if k != "id" and v not in (None, "", [], False)}
                self._journaliser(conn, acteur, "suppression", kind, ident, avant.get(t.cle),
                                  avant=json.dumps(contenu, ensure_ascii=False, default=str))
        return avant

    # ------------------------------------------------------------------ journal

    def _journaliser(self, conn, acteur, action, kind=None, ident=None, libelle=None,
                     champ=None, avant=None, apres=None):
        conn.execute("INSERT INTO journal (quand, acteur, action, tab, ligne_id, libelle, champ, "
                     "avant, apres) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (maintenant(), acteur, action, kind, ident,
                      None if libelle is None else str(libelle), champ, avant, apres))

    def journaliser(self, acteur, action, **kw):
        with self.transaction() as conn:
            self._journaliser(conn, acteur, action, **kw)

    def journal(self, limite=200, kind=None, ident=None, acteur=None, avant_id=None):
        clauses, params = [], []
        if kind:
            clauses.append("tab = ?")
            params.append(kind)
        if ident is not None:
            clauses.append("ligne_id = ?")
            params.append(ident)
        if acteur:
            clauses.append("acteur LIKE ?")
            params.append(f"%{acteur}%")
        if avant_id:
            clauses.append("id < ?")
            params.append(avant_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return [dict(r) for r in self.conn.execute(
            f"SELECT * FROM journal {where} ORDER BY id DESC LIMIT ?", [*params, limite])]

    def purger_journal(self, jours):
        limite = (datetime.now(timezone.utc) - timedelta(days=jours)).isoformat()
        with self.transaction() as conn:
            return conn.execute("DELETE FROM journal WHERE quand < ?", (limite,)).rowcount

    # ------------------------------------------------------------------ options

    def _options_par_champ(self, conn):
        sortie = defaultdict(list)
        for r in conn.execute("SELECT * FROM option ORDER BY ordre, lower(valeur)"):
            sortie[(r["tab"], r["champ"])].append(dict(r))
        return dict(sortie)

    def options(self):
        return self._options_par_champ(self.conn)

    def option(self, ident):
        r = self.conn.execute("SELECT * FROM option WHERE id = ?", (ident,)).fetchone()
        return dict(r) if r else None

    def champs_a_options(self):
        """(table, champ, type, personnalisé) pour toutes les listes, dans l'ordre des
        tables puis des champs."""
        sortie = []
        conn = self.conn
        for kind in modele.TABLES:
            for c in modele.champs_a_options(kind):
                sortie.append({"tab": kind, "champ": c.nom, "type": c.type, "perso": False})
            for p in conn.execute("SELECT * FROM champ_perso WHERE tab = ? AND type IN "
                                  "('select', 'multi') ORDER BY ordre, id", (kind,)):
                sortie.append({"tab": kind, "champ": p["nom"], "type": p["type"], "perso": True})
        return sortie

    def _type_liste(self, conn, kind, nom):
        c = modele.champ(kind, nom)
        if c is not None:
            return c.type if c.type in ("select", "multi") else None
        p = conn.execute("SELECT type FROM champ_perso WHERE tab = ? AND nom = ?",
                         (kind, nom)).fetchone()
        return p["type"] if p and p["type"] in ("select", "multi") else None

    def usage_option(self, ident):
        conn = self.conn
        o = self.option(ident)
        if o is None:
            return 0
        c = modele.champ(o["tab"], o["champ"])
        if c is not None and c.type == "select":
            return conn.execute(f"SELECT COUNT(*) FROM {modele.table(o['tab']).sql} "
                                f"WHERE {c.colonne} = ?", (ident,)).fetchone()[0]
        if c is None:
            p = conn.execute("SELECT * FROM champ_perso WHERE tab = ? AND nom = ?",
                             (o["tab"], o["champ"])).fetchone()
            if p and p["type"] == "select":
                return conn.execute("SELECT COUNT(*) FROM valeur_perso WHERE champ_id = ? AND "
                                    "valeur = ?", (p["id"], str(ident))).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM choix WHERE option_id = ?",
                            (ident,)).fetchone()[0]

    def ajouter_option(self, kind, nom, valeur, couleur, acteur):
        valeur = _texte(valeur)
        if not valeur:
            raise ErreurDonnee("Le libellé de l'option est obligatoire.")
        if len(valeur) > 80:
            raise ErreurDonnee("Libellé trop long (80 caractères au plus).")
        with self.transaction() as conn:
            if self._type_liste(conn, kind, nom) is None:
                raise ErreurDonnee(f"{nom} n'est pas une liste sur {modele.table(kind).libelle}")
            if conn.execute("SELECT 1 FROM option WHERE tab = ? AND champ = ? AND "
                            "lower(valeur) = lower(?)", (kind, nom, valeur)).fetchone():
                raise ErreurDonnee(f"L'option « {valeur} » existe déjà.")
            ordre = conn.execute("SELECT COALESCE(MAX(ordre), 0) + 1 FROM option WHERE tab = ? "
                                 "AND champ = ?", (kind, nom)).fetchone()[0]
            cur = conn.execute("INSERT INTO option (tab, champ, valeur, couleur, ordre) VALUES "
                               "(?, ?, ?, ?, ?)", (kind, nom, valeur, couleur or "gray", ordre))
            self._journaliser(conn, acteur, "option ajoutée", kind, None, valeur, nom)
            return cur.lastrowid

    def modifier_option(self, ident, valeur, couleur, acteur):
        with self.transaction() as conn:
            o = self.option(ident)
            if o is None:
                raise ErreurDonnee("Option introuvable.")
            valeur = _texte(valeur) or o["valeur"]
            if valeur != o["valeur"]:
                if modele.option_protegee(o["tab"], o["champ"], o["valeur"]):
                    raise ErreurDonnee(f"« {o['valeur']} » est utilisée par le code de la CMDB : "
                                       "sa couleur se change, pas son libellé.")
                if conn.execute("SELECT 1 FROM option WHERE tab = ? AND champ = ? AND "
                                "lower(valeur) = lower(?) AND id <> ?",
                                (o["tab"], o["champ"], valeur, ident)).fetchone():
                    raise ErreurDonnee(f"L'option « {valeur} » existe déjà.")
            conn.execute("UPDATE option SET valeur = ?, couleur = ? WHERE id = ?",
                         (valeur, couleur or o["couleur"], ident))
            if valeur != o["valeur"]:
                self._journaliser(conn, acteur, "option renommée", o["tab"], None, valeur,
                                  o["champ"], o["valeur"], valeur)
            if (couleur or o["couleur"]) != o["couleur"]:
                self._journaliser(conn, acteur, "option recolorée", o["tab"], None, valeur,
                                  o["champ"], o["couleur"], couleur)

    def deplacer_option(self, ident, sens):
        with self.transaction() as conn:
            o = self.option(ident)
            if o is None:
                raise ErreurDonnee("Option introuvable.")
            freres = [r["id"] for r in conn.execute(
                "SELECT id FROM option WHERE tab = ? AND champ = ? ORDER BY ordre, lower(valeur)",
                (o["tab"], o["champ"]))]
            i = freres.index(ident)
            j = i - 1 if sens < 0 else i + 1
            if 0 <= j < len(freres):
                freres[i], freres[j] = freres[j], freres[i]
            conn.executemany("UPDATE option SET ordre = ? WHERE id = ?",
                             [(n + 1, f) for n, f in enumerate(freres)])

    def supprimer_option(self, ident, remplacement, acteur):
        """Supprime une option. Si elle est employée, `remplacement` (une autre option du
        même champ) prend sa place partout ; sans remplacement, les lignes la perdent."""
        with self.transaction() as conn:
            o = self.option(ident)
            if o is None:
                raise ErreurDonnee("Option introuvable.")
            if modele.option_protegee(o["tab"], o["champ"], o["valeur"]):
                raise ErreurDonnee(f"« {o['valeur']} » est utilisée par le code de la CMDB et ne "
                                   "peut pas être supprimée.")
            usage = self.usage_option(ident)
            r = None
            if remplacement:
                r = self.option(int(remplacement))
                if r is None or (r["tab"], r["champ"]) != (o["tab"], o["champ"]) or r["id"] == ident:
                    raise ErreurDonnee("Option de remplacement invalide.")
            c = modele.champ(o["tab"], o["champ"])
            if r is not None:
                if c is not None and c.type == "select":
                    conn.execute(f"UPDATE {modele.table(o['tab']).sql} SET {c.colonne} = ? "
                                 f"WHERE {c.colonne} = ?", (r["id"], ident))
                else:
                    conn.execute("INSERT OR IGNORE INTO choix (tab, champ, ligne_id, option_id) "
                                 "SELECT tab, champ, ligne_id, ? FROM choix WHERE option_id = ?",
                                 (r["id"], ident))
                    p = conn.execute("SELECT id FROM champ_perso WHERE tab = ? AND nom = ? AND "
                                     "type = 'select'", (o["tab"], o["champ"])).fetchone()
                    if p:
                        conn.execute("UPDATE valeur_perso SET valeur = ? WHERE champ_id = ? AND "
                                     "valeur = ?", (str(r["id"]), p["id"], str(ident)))
            p = conn.execute("SELECT id FROM champ_perso WHERE tab = ? AND nom = ? AND "
                             "type = 'select'", (o["tab"], o["champ"])).fetchone()
            if p:
                conn.execute("DELETE FROM valeur_perso WHERE champ_id = ? AND valeur = ?",
                             (p["id"], str(ident)))
            conn.execute("DELETE FROM option WHERE id = ?", (ident,))
            self._journaliser(conn, acteur, "option supprimée", o["tab"], None, o["valeur"],
                              o["champ"], o["valeur"],
                              f"remplacée par {r['valeur']} ({usage} lignes)" if r
                              else f"{usage} lignes vidées")
            return usage

    # ------------------------------------------------------------------ champs personnalisés

    def champs_perso(self, kind=None):
        if kind:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM champ_perso WHERE tab = ? ORDER BY ordre, id", (kind,))]
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM champ_perso ORDER BY tab, ordre, id")]

    def _valider_nom_champ(self, conn, kind, nom, sauf=None):
        nom = _texte(nom)
        if not nom:
            raise ErreurDonnee("Le nom du champ est obligatoire.")
        if len(nom) > 60:
            raise ErreurDonnee("Nom trop long (60 caractères au plus).")
        if any(ch in nom for ch in "\"'<>&\n\r\t"):
            raise ErreurDonnee("Le nom ne peut contenir ni guillemets, ni chevrons, ni &.")
        existants = {c.nom.lower() for c in modele.table(kind).champs} | {"id"}
        if nom.lower() in existants:
            raise ErreurDonnee(f"« {nom} » est déjà un champ de {modele.table(kind).libelle}.")
        if conn.execute("SELECT 1 FROM champ_perso WHERE tab = ? AND lower(nom) = lower(?) "
                        "AND id IS NOT ?", (kind, nom, sauf)).fetchone():
            raise ErreurDonnee(f"Un champ « {nom} » existe déjà sur {modele.table(kind).libelle}.")
        return nom

    def ajouter_champ(self, kind, nom, type_, aide, acteur):
        modele.table(kind)
        if type_ not in modele.TYPES_PERSO:
            raise ErreurDonnee("Type de champ inconnu.")
        with self.transaction() as conn:
            nom = self._valider_nom_champ(conn, kind, nom)
            ordre = conn.execute("SELECT COALESCE(MAX(ordre), 0) + 1 FROM champ_perso WHERE "
                                 "tab = ?", (kind,)).fetchone()[0]
            cur = conn.execute("INSERT INTO champ_perso (tab, nom, type, aide, ordre, cree) VALUES "
                               "(?, ?, ?, ?, ?, ?)",
                               (kind, nom, type_, _texte(aide), ordre, maintenant()))
            self._journaliser(conn, acteur, "champ ajouté", kind, None, nom, nom, None,
                              modele.TYPES_PERSO[type_])
            return cur.lastrowid

    def modifier_champ(self, ident, nom, aide, acteur):
        with self.transaction() as conn:
            p = conn.execute("SELECT * FROM champ_perso WHERE id = ?", (ident,)).fetchone()
            if p is None:
                raise ErreurDonnee("Champ introuvable.")
            nom = self._valider_nom_champ(conn, p["tab"], nom, sauf=ident)
            conn.execute("UPDATE champ_perso SET nom = ?, aide = ? WHERE id = ?",
                         (nom, _texte(aide), ident))
            if nom != p["nom"]:
                conn.execute("UPDATE option SET champ = ? WHERE tab = ? AND champ = ?",
                             (nom, p["tab"], p["nom"]))
                conn.execute("UPDATE choix SET champ = ? WHERE tab = ? AND champ = ?",
                             (nom, p["tab"], p["nom"]))
                self._journaliser(conn, acteur, "champ renommé", p["tab"], None, nom, nom,
                                  p["nom"], nom)

    def deplacer_champ(self, ident, sens):
        with self.transaction() as conn:
            p = conn.execute("SELECT * FROM champ_perso WHERE id = ?", (ident,)).fetchone()
            if p is None:
                raise ErreurDonnee("Champ introuvable.")
            freres = [r["id"] for r in conn.execute(
                "SELECT id FROM champ_perso WHERE tab = ? ORDER BY ordre, id", (p["tab"],))]
            i = freres.index(ident)
            j = i - 1 if sens < 0 else i + 1
            if 0 <= j < len(freres):
                freres[i], freres[j] = freres[j], freres[i]
            conn.executemany("UPDATE champ_perso SET ordre = ? WHERE id = ?",
                             [(n + 1, f) for n, f in enumerate(freres)])

    def supprimer_champ(self, ident, acteur):
        with self.transaction() as conn:
            p = conn.execute("SELECT * FROM champ_perso WHERE id = ?", (ident,)).fetchone()
            if p is None:
                raise ErreurDonnee("Champ introuvable.")
            n = conn.execute("SELECT COUNT(*) FROM valeur_perso WHERE champ_id = ?",
                             (ident,)).fetchone()[0]
            n += conn.execute("SELECT COUNT(DISTINCT ligne_id) FROM choix WHERE tab = ? AND "
                              "champ = ?", (p["tab"], p["nom"])).fetchone()[0]
            conn.execute("DELETE FROM choix WHERE tab = ? AND champ = ?", (p["tab"], p["nom"]))
            conn.execute("DELETE FROM option WHERE tab = ? AND champ = ?", (p["tab"], p["nom"]))
            conn.execute("DELETE FROM champ_perso WHERE id = ?", (ident,))
            self._journaliser(conn, acteur, "champ supprimé", p["tab"], None, p["nom"], p["nom"],
                              f"{n} valeur{'s' if n > 1 else ''}", None)
            return n

    # ------------------------------------------------------------------ jetons

    @staticmethod
    def _empreinte(secret):
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    def creer_jeton(self, nom, droits, expire_jours, acteur):
        nom = _texte(nom)
        if not nom:
            raise ErreurDonnee("Donner un nom au jeton : celui de l'application ou de l'agent "
                               "qui l'utilisera.")
        if droits not in ("lecture", "ecriture"):
            raise ErreurDonnee("Droits inconnus.")
        expire = None
        if expire_jours:
            jours = _entier(expire_jours, "Expiration")
            if jours and jours > 0:
                expire = (datetime.now(timezone.utc) + timedelta(days=jours)).isoformat()
        secret = PREFIXE_JETON + secrets.token_urlsafe(32)
        with self.transaction() as conn:
            cur = conn.execute("INSERT INTO jeton (nom, empreinte, prefixe, droits, cree, expire) "
                               "VALUES (?, ?, ?, ?, ?, ?)",
                               (nom, self._empreinte(secret), secret[:len(PREFIXE_JETON) + 6],
                                droits, maintenant(), expire))
            self._journaliser(conn, acteur, "jeton créé", None, None, nom, None, None, droits)
            return cur.lastrowid, secret

    def jetons(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM jeton ORDER BY revoque IS NOT "
                                                   "NULL, id DESC")]

    def revoquer_jeton(self, ident, acteur):
        with self.transaction() as conn:
            j = conn.execute("SELECT * FROM jeton WHERE id = ?", (ident,)).fetchone()
            if j is None:
                raise ErreurDonnee("Jeton introuvable.")
            if j["revoque"] is None:
                conn.execute("UPDATE jeton SET revoque = ? WHERE id = ?", (maintenant(), ident))
                self._journaliser(conn, acteur, "jeton révoqué", None, None, j["nom"])

    def supprimer_jeton(self, ident, acteur):
        with self.transaction() as conn:
            j = conn.execute("SELECT * FROM jeton WHERE id = ?", (ident,)).fetchone()
            if j is None:
                raise ErreurDonnee("Jeton introuvable.")
            conn.execute("DELETE FROM jeton WHERE id = ?", (ident,))
            self._journaliser(conn, acteur, "jeton supprimé", None, None, j["nom"])

    def verifier_jeton(self, secret):
        """Le jeton s'il est valide, None sinon. La date de dernière utilisation est tenue à
        la minute près : l'écrire à chaque requête ferait d'une lecture une écriture."""
        if not secret or not secret.startswith(PREFIXE_JETON):
            return None
        j = self.conn.execute("SELECT * FROM jeton WHERE empreinte = ?",
                              (self._empreinte(secret),)).fetchone()
        if j is None or j["revoque"]:
            return None
        now = datetime.now(timezone.utc)
        if j["expire"] and datetime.fromisoformat(j["expire"]) < now:
            return None
        dernier = j["dernier_usage"]
        if not dernier or (now - datetime.fromisoformat(dernier)).total_seconds() > 60:
            try:
                with self.transaction() as conn:
                    conn.execute("UPDATE jeton SET dernier_usage = ? WHERE id = ?",
                                 (now.isoformat(), j["id"]))
            except sqlite3.OperationalError:
                logger.debug("Date d'usage du jeton %s non enregistrée (base occupée)", j["id"])
        return dict(j)

    # ------------------------------------------------------------------ paramètres

    def parametre(self, cle, defaut=None):
        r = self.conn.execute("SELECT valeur FROM parametre WHERE cle = ?", (cle,)).fetchone()
        return r["valeur"] if r else defaut

    def parametre_entier(self, cle, defaut):
        try:
            return int(self.parametre(cle, defaut))
        except (TypeError, ValueError):
            return defaut

    def definir_parametre(self, cle, valeur, acteur):
        with self.transaction() as conn:
            avant = self.parametre(cle)
            conn.execute("INSERT INTO parametre (cle, valeur) VALUES (?, ?) ON CONFLICT (cle) "
                         "DO UPDATE SET valeur = excluded.valeur", (cle, str(valeur)))
            if avant != str(valeur):
                self._journaliser(conn, acteur, "paramètre modifié", None, None, cle, cle,
                                  avant, str(valeur))
