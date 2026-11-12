"""Administration : tout ce qui se faisait dans Baserow et que la console ne couvrait pas.

- **Listes** : créer, renommer, colorer, ordonner et supprimer les options des listes
  déroulantes — celles des tables comme celles des champs personnalisés ;
- **Champs** : ajouter des champs à une table, sans migration ;
- **Données** : exporter chaque table en CSV, et importer un CSV collé — modification des
  lignes existantes et création des nouvelles, après un aperçu de ce qui changera ;
- **Journal** : qui a écrit quoi, quand, depuis où ;
- **Sauvegardes** : les copies de la base, leur cadence, leur conservation ;
- **Jetons** : les accès à l'API et au serveur MCP.

Aucune authentification propre, comme le reste de l'interface : l'accès est filtré en
amont, par le reverse proxy.
"""

import csv
import io
import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from cmdb import modele, sauvegarde
from cmdb.depot import ErreurDonnee

from . import ecriture, exposition, store

logger = logging.getLogger("cmdb.web.admin")

ACTEUR = "interface"
SEPARATEUR_MULTI = " | "
TAILLE_MAX_IMPORT = 4 * 1024 * 1024


def _retour(chemin, ok=None, erreur=None):
    """Redirection après POST, avec le compte rendu en paramètre d'URL."""
    message = ("ok", ok) if ok else (("erreur", erreur) if erreur else None)
    if message:
        chemin += ("&" if "?" in chemin else "?") + f"{message[0]}={quote(message[1])}"
    return RedirectResponse(chemin, status_code=303)


def _libelle_champ(nom):
    return ecriture.LIBELLES.get(nom, nom)


# --------------------------------------------------------------------------- CSV

def _cellule(valeur):
    """Valeur de ligne -> texte de cellule. Les listes multiples et les liens multiples
    sont joints par « | » : un séparateur qu'aucun libellé ne contient en pratique."""
    if valeur is None:
        return ""
    if isinstance(valeur, bool):
        return "oui" if valeur else "non"
    if isinstance(valeur, dict):
        return valeur.get("value") or ""
    if isinstance(valeur, list):
        return SEPARATEUR_MULTI.join(str(v.get("value") if isinstance(v, dict) else v)
                                     for v in valeur)
    return str(valeur)


def colonnes_export(snap, kind):
    t = modele.table(kind)
    noms = [c.nom for c in t.champs if c.type != "inverse"]
    return ["id"] + noms + [p["nom"] for p in snap.perso.get(kind, [])]


def exporter_csv(snap, kind):
    tampon = io.StringIO()
    # Point-virgule et BOM : c'est ce qu'un tableur réglé en français ouvre sans assistant.
    tampon.write("﻿")
    ecrivain = csv.writer(tampon, delimiter=";", lineterminator="\r\n")
    colonnes = colonnes_export(snap, kind)
    ecrivain.writerow(colonnes)
    objets = list(snap.by_kind.get(kind, {}).values())
    if kind == "image":
        objets = list(snap.images) + list(snap.images_hors_service)
    for obj in sorted(objets, key=lambda o: o.id):
        ecrivain.writerow([obj.id] + [_cellule(obj.get(c)) for c in colonnes[1:]])
    return tampon.getvalue()


def _lire_csv(texte):
    texte = texte.lstrip("﻿").strip()
    if not texte:
        raise ErreurDonnee("Le CSV est vide.")
    premiere = texte.splitlines()[0]
    delimiteur = max((";", ",", "\t"), key=premiere.count)
    lecteur = csv.reader(io.StringIO(texte), delimiter=delimiteur)
    lignes = [l for l in lecteur if any(c.strip() for c in l)]
    entete = [c.strip() for c in lignes[0]]
    if len(set(entete)) != len(entete):
        raise ErreurDonnee("Deux colonnes portent le même nom.")
    return entete, [dict(zip(entete, l + [""] * (len(entete) - len(l)))) for l in lignes[1:]]


def _vers_formulaire(snap, kind, descripteurs, cellules):
    """Cellules d'une ligne CSV -> formulaire, pour les seuls champs saisissables. Les
    liens s'écrivent par le nom de la ligne visée, et non par un identifiant illisible."""
    paires = []
    for d in descripteurs:
        nom, widget = d["champ"], d["widget"]
        if nom not in cellules:
            continue
        brut = (cellules[nom] or "").strip()
        paires.append(("soumis", nom))
        cle = f"champ.{nom}"
        if widget == "bool":
            if brut.lower() in ("oui", "1", "true", "vrai", "x", "yes"):
                paires.append((cle, "1"))
        elif widget == "multi":
            for v in brut.split("|"):
                if v.strip():
                    paires.append((cle, v.strip()))
        elif widget in ecriture.LIENS:
            cible = ecriture.LIENS[widget]
            par_nom = {o.name.lower(): o.id for o in snap.by_kind.get(cible, {}).values()}
            for v in brut.split("|"):
                v = v.strip()
                if not v:
                    continue
                ident = par_nom.get(v.lower())
                if ident is None and v.isdigit():
                    ident = int(v)
                if ident is None:
                    raise ecriture.EcritureRefusee(
                        f"{nom} : « {v} » introuvable dans {modele.table(cible).libelle}")
                paires.append((cle, str(ident)))
        else:
            paires.append((cle, brut))
    return FormData(paires)


def traiter_import(snap, redacteur, kind, texte, simulation):
    """Rapport ligne par ligne : création, modification, rien, ou refus motivé."""
    entete, lignes = _lire_csv(texte)
    t = modele.table(kind)
    if "id" not in entete and t.cle not in entete:
        raise ErreurDonnee(f"Il faut une colonne « id » ou « {t.cle} » pour reconnaître les lignes.")
    connues = set(colonnes_export(snap, kind))
    inconnues = [c for c in entete if c not in connues]
    par_cle = {(o.get(t.cle) or "").strip().lower(): o for o in snap.by_kind.get(kind, {}).values()}
    rapport = []
    for n, cellules in enumerate(lignes, start=2):
        entree = {"ligne": n, "nom": cellules.get(t.cle) or cellules.get("id"), "action": None,
                  "details": [], "obj": None}
        try:
            obj = None
            ident = (cellules.get("id") or "").strip()
            if ident:
                if not ident.isdigit():
                    raise ecriture.EcritureRefusee(f"id « {ident} » invalide")
                obj = snap.get(kind, int(ident))
                if obj is None:
                    raise ecriture.EcritureRefusee(f"aucune ligne n°{ident} — vider la colonne id "
                                                   "pour créer la ligne")
            elif (cellules.get(t.cle) or "").strip():
                obj = par_cle.get(cellules[t.cle].strip().lower())
            if obj is not None:
                entree["obj"] = obj
                descripteurs = ecriture.formulaire(snap, obj)
                formulaire = _vers_formulaire(snap, kind, descripteurs, cellules)
                changements = redacteur.appliquer(snap, obj, formulaire, acteur=ACTEUR,
                                                  simulation=simulation)
                entree["action"] = "modification" if changements else "inchangée"
                entree["details"] = [f"{_libelle_champ(c.champ)} : {_cellule(c.avant)} → "
                                     f"{_cellule(c.apres)}" for c in changements]
                ignores = [c for c in entete if c in connues and c not in ("id",)
                           and c not in {d["champ"] for d in descripteurs}
                           and (cellules.get(c) or "").strip() != _cellule(obj.get(c)).strip()]
                if ignores:
                    entree["details"].append("ignorés, non modifiables sur cette ligne : "
                                             + ", ".join(ignores))
            else:
                if kind not in ecriture.CREABLES:
                    raise ecriture.EcritureRefusee(f"création impossible sur {t.libelle} "
                                                   "(lignes écrites par le collecteur)")
                descripteurs = ecriture.formulaire_creation(snap, kind)
                formulaire = _vers_formulaire(snap, kind, descripteurs, cellules)
                nouvel = redacteur.creer(snap, kind, formulaire, acteur=ACTEUR,
                                         simulation=simulation)
                entree["action"] = "création"
                if nouvel:
                    entree["id"] = nouvel
        except (ecriture.EcritureRefusee, ecriture.EcritureImpossible) as exc:
            entree["action"] = "refus"
            entree["details"] = [str(exc)]
        rapport.append(entree)
    return {"colonnes_inconnues": inconnues, "lignes": rapport,
            "creations": sum(1 for e in rapport if e["action"] == "création"),
            "modifications": sum(1 for e in rapport if e["action"] == "modification"),
            "refus": sum(1 for e in rapport if e["action"] == "refus")}


# --------------------------------------------------------------------------- routes

def creer_routeur(data, redacteur, templates, render, lire_formulaire, meme_origine, cfg):
    r = APIRouter(prefix="/admin")
    depot = data.depot

    async def formulaire(request, limite=None):
        if not meme_origine(request):
            raise HTTPException(status_code=403, detail="origine du formulaire non reconnue")
        if limite:
            return await lire_formulaire(request, limite)
        return await lire_formulaire(request)

    def page(request, gabarit, **contexte):
        snap = data.get()
        contexte.setdefault("ok", request.query_params.get("ok"))
        contexte.setdefault("erreur", request.query_params.get("erreur"))
        return render(request, f"admin/{gabarit}", snap, **contexte)

    def _int(valeur, defaut=None):
        try:
            return int(valeur)
        except (TypeError, ValueError):
            return defaut

    # -- accueil -------------------------------------------------------------
    @r.get("")
    def accueil(request: Request):
        snap = data.get()
        copie = sauvegarde.derniere(cfg.BACKUP_DIR)
        jetons = depot.jetons()
        return page(request, "accueil.html",
                    nb_options=sum(len(v) for v in snap.options.values()),
                    nb_listes=len(depot.champs_a_options()),
                    nb_perso=sum(len(v) for v in snap.perso.values()),
                    copie=copie, nb_copies=len(sauvegarde.lister(cfg.BACKUP_DIR)),
                    nb_jetons=sum(1 for j in jetons if not j["revoque"]),
                    journal=depot.journal(limite=8))

    # -- listes --------------------------------------------------------------
    @r.get("/listes")
    def listes(request: Request, table: str = Query("node")):
        toutes = depot.champs_a_options()
        # Un onglet par table qui A des listes, et seulement celles-là : un onglet qui
        # n'ouvre sur rien ne sert qu'à faire chercher ce qu'il devrait contenir. Un champ
        # personnalisé de type liste fait apparaître l'onglet de sa table.
        avec_listes = {liste["tab"] for liste in toutes}
        tables = [(k, modele.table(k).libelle) for k in modele.TABLES if k in avec_listes]
        if table not in avec_listes:
            table = tables[0][0] if tables else "node"
        snap = data.get()
        groupes = []
        for liste in toutes:
            if liste["tab"] != table:
                continue
            options = []
            for o in snap.options.get((table, liste["champ"]), []):
                options.append({**o, "usage": depot.usage_option(o["id"]),
                                "teinte": store._teinte(o["couleur"]),
                                "protegee": modele.option_protegee(table, liste["champ"],
                                                                   o["valeur"])})
            groupes.append({**liste, "libelle": _libelle_champ(liste["champ"]),
                            "options": options})
        return page(request, "listes.html", table=table, tables=tables, groupes=groupes,
                    couleurs=[(c, store._teinte(c)) for c in modele.COULEURS],
                    store_teinte=store._teinte)

    @r.post("/listes/ajouter")
    async def ajouter_option(request: Request):
        f = await formulaire(request)
        table, champ = f.get("table"), f.get("champ")
        retour = f"/admin/listes?table={quote(table or '')}"
        try:
            depot.ajouter_option(table, champ, f.get("valeur"), f.get("couleur"), ACTEUR)
        except (ErreurDonnee, KeyError) as exc:
            return _retour(retour, erreur=str(exc))
        return _retour(retour, ok=f"Option « {f.get('valeur')} » ajoutée à {champ}.")

    @r.post("/listes/{ident}/modifier")
    async def modifier_option(request: Request, ident: int):
        f = await formulaire(request)
        o = depot.option(ident)
        retour = f"/admin/listes?table={quote(o['tab'] if o else 'node')}"
        try:
            depot.modifier_option(ident, f.get("valeur"), f.get("couleur"), ACTEUR)
        except ErreurDonnee as exc:
            return _retour(retour, erreur=str(exc))
        return _retour(retour, ok="Option enregistrée.")

    @r.post("/listes/{ident}/deplacer")
    async def deplacer_option(request: Request, ident: int):
        f = await formulaire(request)
        o = depot.option(ident)
        try:
            depot.deplacer_option(ident, -1 if f.get("sens") == "haut" else 1)
        except ErreurDonnee as exc:
            return _retour("/admin/listes", erreur=str(exc))
        return _retour(f"/admin/listes?table={quote(o['tab'])}")

    @r.post("/listes/{ident}/supprimer")
    async def supprimer_option(request: Request, ident: int):
        f = await formulaire(request)
        o = depot.option(ident)
        retour = f"/admin/listes?table={quote(o['tab'] if o else 'node')}"
        try:
            n = depot.supprimer_option(ident, _int(f.get("remplacement")), ACTEUR)
        except ErreurDonnee as exc:
            return _retour(retour, erreur=str(exc))
        return _retour(retour, ok=f"Option « {o['valeur']} » supprimée "
                       + (f"({n} ligne{'s' if n > 1 else ''} concernée{'s' if n > 1 else ''})."
                          if n else "(aucune ligne concernée)."))

    # -- champs personnalisés ------------------------------------------------
    @r.get("/champs")
    def champs(request: Request):
        snap = data.get()
        par_table = []
        for kind, t in modele.TABLES.items():
            persos = []
            for p in snap.perso.get(kind, []):
                persos.append({**p, "type_libelle": modele.TYPES_PERSO.get(p["type"], p["type"])})
            par_table.append({"kind": kind, "libelle": t.libelle, "perso": persos,
                              "natifs": [c.nom for c in t.champs if c.type != "inverse"]})
        return page(request, "champs.html", par_table=par_table, types=modele.TYPES_PERSO)

    @r.post("/champs/ajouter")
    async def ajouter_champ(request: Request):
        f = await formulaire(request)
        try:
            depot.ajouter_champ(f.get("table"), f.get("nom"), f.get("type"), f.get("aide"), ACTEUR)
        except (ErreurDonnee, KeyError) as exc:
            return _retour("/admin/champs", erreur=str(exc))
        message = f"Champ « {f.get('nom')} » ajouté."
        if f.get("type") in ("select", "multi"):
            message += " Ses options se créent dans Listes."
        return _retour("/admin/champs", ok=message)

    @r.post("/champs/{ident}/modifier")
    async def modifier_champ(request: Request, ident: int):
        f = await formulaire(request)
        try:
            depot.modifier_champ(ident, f.get("nom"), f.get("aide"), ACTEUR)
        except ErreurDonnee as exc:
            return _retour("/admin/champs", erreur=str(exc))
        return _retour("/admin/champs", ok="Champ enregistré.")

    @r.post("/champs/{ident}/deplacer")
    async def deplacer_champ(request: Request, ident: int):
        f = await formulaire(request)
        try:
            depot.deplacer_champ(ident, -1 if f.get("sens") == "haut" else 1)
        except ErreurDonnee as exc:
            return _retour("/admin/champs", erreur=str(exc))
        return _retour("/admin/champs")

    @r.post("/champs/{ident}/supprimer")
    async def supprimer_champ(request: Request, ident: int):
        f = await formulaire(request)
        if f.get("confirme") != "1":
            return _retour("/admin/champs", erreur="Cocher la case de confirmation.")
        try:
            n = depot.supprimer_champ(ident, ACTEUR)
        except ErreurDonnee as exc:
            return _retour("/admin/champs", erreur=str(exc))
        return _retour("/admin/champs", ok=f"Champ supprimé, avec ses {n} valeurs.")

    # -- données : export / import -------------------------------------------
    @r.get("/donnees")
    def donnees(request: Request):
        snap = data.get()
        tables = [{"kind": k, "libelle": t.libelle, "prefixe": store.URL_PREFIX[k],
                   "nombre": len(snap.by_kind.get(k, {})), "cle": t.cle,
                   "creation": k in ecriture.CREABLES}
                  for k, t in modele.TABLES.items()]
        return page(request, "donnees.html", tables=tables, rapport=None, texte="",
                    table_import="node")

    @r.get("/export/{prefixe}.csv")
    def export(prefixe: str):
        kind = exposition.TYPES.get(prefixe)
        if kind is None:
            raise HTTPException(status_code=404, detail="table inconnue")
        contenu = exporter_csv(data.get(), kind)
        return Response(contenu.encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="tinycmdb-{prefixe}.csv"'})

    @r.post("/import")
    async def importer(request: Request):
        f = await formulaire(request, TAILLE_MAX_IMPORT)
        kind = f.get("table")
        texte = f.get("csv") or ""
        confirmer = f.get("confirmer") == "1"
        snap = data.get()
        tables = [{"kind": k, "libelle": t.libelle, "prefixe": store.URL_PREFIX[k],
                   "nombre": len(snap.by_kind.get(k, {})), "cle": t.cle,
                   "creation": k in ecriture.CREABLES}
                  for k, t in modele.TABLES.items()]
        if kind not in modele.TABLES:
            return page(request, "donnees.html", tables=tables, rapport=None, texte=texte,
                        table_import="node", erreur="Table inconnue.")
        try:
            rapport = traiter_import(snap, redacteur, kind, texte, simulation=not confirmer)
        except ErreurDonnee as exc:
            return page(request, "donnees.html", tables=tables, rapport=None, texte=texte,
                        table_import=kind, erreur=str(exc))
        if confirmer:
            data.get(force=True)
            depot.journaliser(ACTEUR, "import CSV", kind=kind,
                              libelle=f"{rapport['creations']} créations, "
                                      f"{rapport['modifications']} modifications, "
                                      f"{rapport['refus']} refus")
        return page(request, "donnees.html", tables=tables, rapport=rapport, texte=texte,
                    table_import=kind, applique=confirmer)

    # -- journal -------------------------------------------------------------
    @r.get("/journal")
    def journal(request: Request, table: str = Query(""), id: int | None = Query(None),
                acteur: str = Query(""), avant: int | None = Query(None)):
        kind = table if table in modele.TABLES else None
        entrees = depot.journal(limite=200, kind=kind, ident=id, acteur=acteur or None,
                                avant_id=avant)
        snap = data.get()
        for e in entrees:
            e["obj"] = snap.get(e["tab"], e["ligne_id"]) if e.get("tab") and e.get("ligne_id") else None
            e["table_libelle"] = modele.TABLES[e["tab"]].libelle if e.get("tab") in modele.TABLES else ""
        return page(request, "journal.html", entrees=entrees, table=kind or "", ident=id,
                    acteur=acteur, suite=entrees[-1]["id"] if len(entrees) == 200 else None)

    # -- sauvegardes ---------------------------------------------------------
    @r.get("/sauvegardes")
    def sauvegardes(request: Request):
        import os
        base = cfg.DB_PATH
        taille = os.path.getsize(base) if os.path.exists(base) else 0
        return page(request, "sauvegardes.html", copies=sauvegarde.lister(cfg.BACKUP_DIR),
                    dossier=cfg.BACKUP_DIR, base=base, taille_base=taille,
                    intervalle=depot.parametre_entier("sauvegarde_intervalle_heures", 24),
                    conservation=depot.parametre_entier("sauvegarde_conservation", 14),
                    journal_jours=depot.parametre_entier("journal_conservation_jours", 365))

    @r.post("/sauvegardes/maintenant")
    async def sauvegarder(request: Request):
        await formulaire(request)
        try:
            chemin = sauvegarde.sauvegarder(cfg.DB_PATH, cfg.BACKUP_DIR,
                                            depot.parametre_entier("sauvegarde_conservation", 14))
        except Exception as exc:
            logger.exception("Sauvegarde manuelle en échec")
            return _retour("/admin/sauvegardes", erreur=f"Sauvegarde impossible : {exc}")
        depot.journaliser(ACTEUR, "sauvegarde", libelle=chemin.name)
        return _retour("/admin/sauvegardes", ok=f"Copie écrite : {chemin.name}")

    @r.post("/sauvegardes/reglages")
    async def reglages(request: Request):
        f = await formulaire(request)
        valeurs = {}
        for cle, minimum in (("sauvegarde_intervalle_heures", 0), ("sauvegarde_conservation", 1),
                             ("journal_conservation_jours", 0)):
            v = _int(f.get(cle))
            if v is None or v < minimum:
                return _retour("/admin/sauvegardes", erreur=f"{cle} : entier ≥ {minimum} attendu.")
            valeurs[cle] = v
        for cle, v in valeurs.items():
            depot.definir_parametre(cle, v, ACTEUR)
        sauvegarde.rotation(cfg.BACKUP_DIR, valeurs["sauvegarde_conservation"])
        return _retour("/admin/sauvegardes", ok="Réglages enregistrés.")

    @r.get("/sauvegardes/fichier/{nom}")
    def telecharger(nom: str):
        chemin = sauvegarde.copie(cfg.BACKUP_DIR, nom)
        if chemin is None:
            raise HTTPException(status_code=404, detail="copie introuvable")
        return FileResponse(chemin, media_type="application/vnd.sqlite3", filename=nom)

    # -- jetons --------------------------------------------------------------
    @r.get("/jetons")
    def jetons(request: Request):
        return page(request, "jetons.html", jetons=depot.jetons(), nouveau=None,
                    base_url=str(request.base_url).rstrip("/"))

    @r.post("/jetons/creer")
    async def creer_jeton(request: Request):
        f = await formulaire(request)
        try:
            ident, secret = depot.creer_jeton(f.get("nom"), f.get("droits"), f.get("expire"), ACTEUR)
        except ErreurDonnee as exc:
            return _retour("/admin/jetons", erreur=str(exc))
        # Affiché une fois, dans la réponse même : jamais dans une URL, qui finirait dans
        # l'historique du navigateur et les journaux du reverse proxy.
        return page(request, "jetons.html", jetons=depot.jetons(),
                    nouveau={"id": ident, "secret": secret, "nom": f.get("nom")},
                    base_url=str(request.base_url).rstrip("/"))

    @r.post("/jetons/{ident}/revoquer")
    async def revoquer(request: Request, ident: int):
        await formulaire(request)
        try:
            depot.revoquer_jeton(ident, ACTEUR)
        except ErreurDonnee as exc:
            return _retour("/admin/jetons", erreur=str(exc))
        return _retour("/admin/jetons", ok="Jeton révoqué : il est refusé dès maintenant.")

    @r.post("/jetons/{ident}/supprimer")
    async def supprimer_jeton(request: Request, ident: int):
        await formulaire(request)
        try:
            depot.supprimer_jeton(ident, ACTEUR)
        except ErreurDonnee as exc:
            return _retour("/admin/jetons", erreur=str(exc))
        return _retour("/admin/jetons", ok="Jeton supprimé.")

    return r
