"""Écriture dans Baserow : le seul module du service web qui en soit capable.

Tout le reste de `web/` ne connaît que `fetch_all`. Ce module concentre les trois
appels qui écrivent, pour deux raisons.

La première est une raison de sûreté. Un champ que le collecteur réécrit n'est pas
modifiable : une saisie y disparaîtrait au passage suivant, sans avertissement ni trace,
et une CMDB qui perd silencieusement ce qu'on lui confie ne vaut pas la peine d'être
tenue. La liste des champs modifiables est donc appliquée ici, côté serveur, et non dans
le gabarit : l'interface décide ce qu'elle affiche, elle ne décide pas ce qui est permis.

La seconde est une raison d'architecture. Baserow rend aujourd'hui trois services —
stocker, offrir une porte de secours quand cette console ne suffit pas, et porter le
schéma sans migration. Le premier, n'importe quoi le rend. Si un jour les deux autres ne
justifient plus un Django, un Postgres et un Redis pour quelques centaines de lignes,
changer de stockage ne devra pas être une réécriture : il faudra remplacer ce fichier, et
lui seul.

Ce module ne supprime aucune ligne, et n'en crée que dans les deux tables où l'ajout est
un geste courant : une adresse à réserver, une application à déclarer. Le token de la
console n'a donc besoin ni de `delete`, ni de `create` ailleurs que sur Ipam et
Application — la compromission de ce service permet de fausser l'inventaire, pas de
l'effacer.
"""

import ipaddress
import logging
from collections import namedtuple

from . import schema

logger = logging.getLogger("cmdb.web.ecriture")

MAX_TEXTE = 255
MAX_LONG = 4000

# Une valeur d'URL finit dans un `href`. Jinja échappe les guillemets, ce qui empêche de
# sortir de l'attribut, mais pas d'y placer un schéma exécutable : `javascript:` dans un
# lien sur lequel on clique soi-même reste du code qu'on exécute soi-même.
SCHEMES_URL = ("http://", "https://")

Changement = namedtuple("Changement", "champ avant apres")


class EcritureRefusee(Exception):
    """La demande est invalide : champ non modifiable, option inconnue, format incorrect.

    C'est une erreur de l'appelant, et elle se répare à l'écran."""


class EcritureImpossible(Exception):
    """La demande était recevable ; Baserow l'a refusée ou n'a pas répondu."""


# Champs proposés à la saisie, par type d'objet et dans cet ordre.
#
# Cette liste ne fait que *restreindre* : elle n'autorise rien. Chaque champ est ensuite
# confronté à `schema.editable()`, qui seul décide — et dont la réponse dépend de la
# ligne. Un nœud découvert sur Proxmox n'expose donc que ses quatre champs manuels, tandis
# que la même déclaration donne onze champs sur une caméra saisie à la main, protégée dans
# son intégralité. Les deux règles vivent au même endroit qu'avant, aucune ne se duplique.
#
# Ce qui n'y figure volontairement pas : `Last_seen` et `Source`, qui décrivent le
# fonctionnement du collecteur et non l'équipement ; `Container` et `IPAM`, liens posés par
# les autres passes et qui seraient réécrits ; les champs des tables Conteneur et Image,
# intégralement automatiques hormis leurs notes.
CHAMPS = {
    "node": [
        ("Name", "texte"),
        ("Type", "select"),
        ("Status", "select"),
        ("OS", "texte"),
        ("vCPU", "entier"),
        ("RAM_Gb", "entier"),
        ("Disk_Gb", "entier"),
        ("Roles", "multi"),
        ("Criticality", "select"),
        ("Application", "lien:application"),
        ("notes", "long"),
    ],
    "container": [
        ("Application - Stack", "lien:application"),
        ("Notes", "long"),
    ],
    "image": [
        ("Notes", "long"),
    ],
    "ipam": [
        ("Address", "ip"),
        ("MAC", "texte"),
        ("Node", "lien:node"),
        ("Type", "select"),
        ("Status", "select"),
        ("Notes", "long"),
    ],
    "application": [
        ("Name", "texte"),
        ("Status", "select"),
        ("Criticality", "select"),
        ("Capability", "multi"),
        ("URL", "url"),
        ("URL 2", "url"),
        ("Doc", "url"),
        ("Notes", "long"),
    ],
    "vlan": [
        ("name", "texte"),
        ("vlan_id", "entier"),
        ("subnet", "texte"),
        ("gateway", "texte"),
        ("zone", "select"),
        ("dhcp", "bool"),
        ("dhcp_range", "texte"),
        ("dns", "texte"),
        ("notes", "long"),
    ],
}

# Libellés de saisie. Le nom Baserow est exact mais parfois sec : `notes` en minuscules sur
# une table et `Notes` sur une autre, `vmid`, `RAM_Gb`. La fiche en consultation affiche le
# nom du champ tel qu'il est dans Baserow — c'est ce qui permet de s'y retrouver quand on y
# passe ; le formulaire, lui, s'adresse à quelqu'un qui saisit.
LIBELLES = {
    "Name": "Nom", "name": "Nom", "notes": "Notes", "Notes": "Notes",
    "Type": "Type", "Status": "Statut", "Roles": "Rôles", "Criticality": "Criticité",
    "Application": "Applications", "Application - Stack": "Application",
    "OS": "Système", "vCPU": "vCPU", "RAM_Gb": "Mémoire (Go)", "Disk_Gb": "Disque (Go)",
    "Address": "Adresse", "MAC": "Adresse MAC", "Node": "Nœud",
    "Capability": "Fonctions", "URL": "URL", "URL 2": "URL secondaire", "Doc": "Documentation",
    "vlan_id": "Identifiant 802.1Q", "subnet": "Sous-réseau", "gateway": "Passerelle",
    "zone": "Zone", "dhcp": "DHCP", "dhcp_range": "Plage DHCP", "dns": "DNS",
}

# Précisions affichées sous un champ, là où la saisie a une conséquence non évidente.
NOTICES = {
    ("node", "Roles"): "Le rôle « docker » déclenche la collecte des conteneurs sur cet hôte.",
    ("ipam", "Address"): "Modifier l'adresse d'une ligne manuelle revient à déclarer un autre équipement.",
    ("container", "Application - Stack"): "Rattachement métier : c'est lui qui donne un sens aux vulnérabilités de l'image.",
    ("node", "Application"): "Pour un équipement qui rend un service sans passer par un conteneur.",
}

# Certaines précisions n'ont de sens qu'à la modification : prévenir qu'on change
# d'équipement en changeant l'adresse n'a aucun sens sur une ligne qui n'existe pas encore.
NOTICES_CREATION = {
    ("ipam", "Address"): "Ligne marquée « saisie manuelle » : le collecteur ne la supprimera jamais.",
    ("ipam", "Node"): "L'équipement qui répond à cette adresse, s'il est déjà inventorié.",
    ("application", "Name"): "Le regroupement fonctionnel, pas le projet Compose — celui-ci est déjà porté par la stack.",
}

LIENS = {"lien:node": "node", "lien:application": "application"}

# Tables où la console sait créer une ligne. Les quatre autres sont soit alimentées par le
# collecteur (Node hors saisie manuelle, Container, Images), soit un référentiel qu'on
# n'étend qu'une fois par an (VLAN) — Baserow y suffit.
CREABLES = ("ipam", "application")

# Valeur imposée au champ `Source` de la ligne créée. Sur l'IPAM, c'est la clause qui
# protège la saisie : sans `Manual`, le collecteur supprimerait cette adresse dès qu'il
# constaterait ne pas la voir sur le réseau. Elle n'est pas proposée au formulaire — c'est
# une conséquence du geste, pas une option.
SOURCE_CREATION = {"ipam": "Manual"}

# Champ portant le nom, par table : sert au contrôle de doublon et au message qui
# l'accompagne.
CLE_NATURELLE = {"ipam": "Address", "application": "Name"}


def formulaire_creation(snap, kind):
    """Champs d'un formulaire de création.

    Rigoureusement les mêmes règles qu'en modification, appliquées à la ligne *telle
    qu'elle sera créée* : sur l'IPAM, `schema.editable()` interrogé avec `Source = Manual`
    rend Address, MAC, Node, Type, Status et Notes — et écarte le VLAN, le constructeur et
    le nom DNS, que le collecteur recalculera. Une déclaration, trois usages : la mention
    de provenance en consultation, le formulaire de modification, celui-ci.
    """
    source = SOURCE_CREATION.get(kind)
    champs = []
    for champ, widget in CHAMPS.get(kind, []):
        if not schema.editable(kind, champ, source):
            continue
        champs.append({
            "champ": champ,
            "widget": widget,
            "libelle": LIBELLES.get(champ, champ),
            "notice": NOTICES_CREATION.get((kind, champ)),
            "valeur": [] if (widget == "multi" or widget in LIENS) else (False if widget == "bool" else None),
            "options": options(snap, kind, champ) if widget in ("select", "multi") else [],
            "cibles": cibles(snap, LIENS[widget]) if widget in LIENS else [],
        })
    return champs


# --------------------------------------------------------------------------- options

def options(snap, kind, field):
    """Options proposées pour une liste déroulante.

    Baserow ne donne pas la définition de ses champs à un *database token* : la liste des
    options d'un `single_select` n'est lisible que par l'API d'administration, avec un
    jeton de session. Elles sont donc déduites de ce qui est employé dans la table.

    Conséquence assumée : une option définie mais encore jamais utilisée n'apparaît pas.
    Créer une option est un changement de schéma ; c'est précisément ce que Baserow reste
    là pour faire, et le formulaire le dit.
    """
    vues = []
    for obj in snap.by_kind.get(kind, {}).values():
        valeurs = obj.multi(field) if isinstance(obj.get(field), list) else [obj.sel(field)]
        for v in valeurs:
            if v and v not in vues:
                vues.append(v)
    return sorted(vues, key=str.lower)


def cibles(snap, kind):
    """Objets proposés pour un champ de liaison, triés par nom."""
    return sorted(snap.by_kind.get(kind, {}).values(), key=lambda o: o.name.lower())


def formulaire(snap, obj):
    """Champs réellement modifiables sur cet objet, prêts à être rendus.

    Le filtre est `schema.editable()`, et lui seul : c'est la même fonction qui décide de
    la mention « manuel » affichée en consultation. Les deux écrans ne peuvent donc pas se
    contredire.
    """
    champs = []
    for champ, widget in CHAMPS.get(obj.kind, []):
        if not schema.editable(obj.kind, champ, obj.source):
            continue
        champs.append({
            "champ": champ,
            "widget": widget,
            "libelle": LIBELLES.get(champ, champ),
            "notice": NOTICES.get((obj.kind, champ)),
            "valeur": _actuel(obj, champ, widget),
            "options": options(snap, obj.kind, champ) if widget in ("select", "multi") else [],
            "cibles": cibles(snap, LIENS[widget]) if widget in LIENS else [],
        })
    return champs


def saisie_brute(snap, descripteurs, donnees):
    """Ce qui vient d'être tapé, sans validation, dans la forme qu'attend le gabarit.

    Sert au seul cas du refus : réafficher le formulaire vidé de la saisie obligerait à
    retaper une note de deux mille caractères pour une option mal choisie ailleurs.
    """
    saisie = {}
    portee = set(donnees.getlist("soumis"))
    for descr in descripteurs:
        champ, widget = descr["champ"], descr["widget"]
        if champ not in portee:
            continue
        clef = f"champ.{champ}"
        if widget == "multi":
            saisie[champ] = [v for v in donnees.getlist(clef) if v]
        elif widget in LIENS:
            ids = []
            for v in donnees.getlist(clef):
                try:
                    ids.append(int(v))
                except (TypeError, ValueError):
                    pass
            saisie[champ] = ids
        elif widget == "bool":
            saisie[champ] = clef in donnees
        elif widget == "entier":
            saisie[champ] = (donnees.get(clef) or "").strip() or None
        else:
            saisie[champ] = (donnees.get(clef) or "").strip() or None
    return saisie


# --------------------------------------------------------------------------- lecture

def _actuel(obj, champ, widget):
    """Valeur actuelle, dans la forme que le formulaire renvoie — donc comparable."""
    if widget == "multi":
        return sorted(obj.multi(champ))
    if widget in LIENS:
        return sorted(v["id"] for v in (obj.get(champ) or []) if isinstance(v, dict) and "id" in v)
    if widget == "select":
        return obj.sel(champ) or None
    if widget == "bool":
        return bool(obj.get(champ))
    if widget == "entier":
        return obj.num(champ)
    valeur = obj.get(champ)
    return (valeur or "").strip() or None


# --------------------------------------------------------------------------- validation

def _valider(snap, kind, champ, widget, brut, multiples):
    """Traduit la saisie en valeur comparable. Lève EcritureRefusee si elle ne tient pas."""
    libelle = LIBELLES.get(champ, champ)

    if widget == "bool":
        return brut is not None

    if widget == "multi":
        choix = [v for v in multiples if v]
        connues = set(options(snap, kind, champ))
        inconnues = [v for v in choix if v not in connues]
        if inconnues:
            raise EcritureRefusee(f"{libelle} : option inconnue ({', '.join(inconnues)})")
        return sorted(set(choix))

    if widget in LIENS:
        table = snap.by_kind.get(LIENS[widget], {})
        ids = []
        for v in multiples:
            if not v:
                continue
            try:
                ident = int(v)
            except (TypeError, ValueError):
                raise EcritureRefusee(f"{libelle} : référence invalide")
            if ident not in table:
                raise EcritureRefusee(f"{libelle} : référence inconnue ({ident})")
            ids.append(ident)
        return sorted(set(ids))

    texte = (brut or "").strip()

    if widget == "select":
        if not texte:
            return None
        if texte not in options(snap, kind, champ):
            raise EcritureRefusee(f"{libelle} : option inconnue ({texte})")
        return texte

    if widget == "ip":
        if not texte:
            raise EcritureRefusee(f"{libelle} : ce champ ne peut pas être vide")
        try:
            ipaddress.ip_address(texte)
        except ValueError:
            raise EcritureRefusee(f"{libelle} : « {texte} » n'est pas une adresse IP valide")
        return texte

    if widget == "entier":
        if not texte:
            return None
        try:
            valeur = int(float(texte.replace(",", ".")))
        except ValueError:
            raise EcritureRefusee(f"{libelle} : nombre attendu")
        if valeur < 0:
            raise EcritureRefusee(f"{libelle} : valeur négative")
        return valeur

    if widget == "url":
        if not texte:
            return None
        if not texte.lower().startswith(SCHEMES_URL):
            raise EcritureRefusee(f"{libelle} : l'adresse doit commencer par http:// ou https://")
        if len(texte) > MAX_TEXTE:
            raise EcritureRefusee(f"{libelle} : adresse trop longue")
        return texte

    limite = MAX_LONG if widget == "long" else MAX_TEXTE
    if len(texte) > limite:
        raise EcritureRefusee(f"{libelle} : {len(texte)} caractères pour {limite} au maximum")
    if widget == "texte" and not texte and champ in ("Name", "name", "Address"):
        raise EcritureRefusee(f"{libelle} : ce champ ne peut pas être vide")
    return texte or None


def _payload(widget, valeur):
    """Forme attendue par Baserow pour effacer ou poser la valeur."""
    if widget in LIENS or widget == "multi":
        return valeur
    if widget in ("texte", "long", "url"):
        return valeur if valeur is not None else ""
    return valeur


# --------------------------------------------------------------------------- écriture

def _lire(snap, kind, descripteurs, donnees):
    """Traduit le formulaire en {champ: valeur validée}, restreint à ce qu'il déclare porter.

    Le formulaire déclare les champs qu'il porte (`soumis`). Sans cette déclaration, une
    case décochée et un champ absent de la requête sont indiscernables : une soumission
    partielle viderait les rôles et les rattachements au lieu de les laisser tels quels.
    Le cas ne se produit pas dans un navigateur, qui envoie toujours le formulaire entier —
    il se produirait au premier script.
    """
    portee = set(donnees.getlist("soumis"))
    valeurs = {}
    for descr in descripteurs:
        champ, widget = descr["champ"], descr["widget"]
        if champ not in portee:
            continue
        clef = f"champ.{champ}"
        if widget == "multi" or widget in LIENS:
            brut, multiples = None, donnees.getlist(clef)
        else:
            brut, multiples = donnees.get(clef), []
        valeurs[champ] = _valider(snap, kind, champ, widget, brut, multiples)
    return valeurs


def _doublon(snap, kind, champ, valeur):
    """Ligne existante portant déjà cette clé. Baserow ne contraint pas l'unicité, et deux
    lignes pour une même adresse rendent l'inventaire faux plutôt qu'incomplet."""
    reference = (valeur or "").strip().lower()
    for obj in snap.by_kind.get(kind, {}).values():
        if (obj.get(champ) or "").strip().lower() == reference:
            return obj
    return None


def _derives(snap, kind, valeurs):
    """Champs posés par la console elle-même à la création, en plus de la saisie.

    `Source = Manual` est ce qui protège la ligne : sans lui, le collecteur supprimerait
    l'adresse dès qu'il constaterait ne pas la voir sur le réseau.

    VLAN et IP_int sont ENRICHI — le collecteur les recalcule à chaque passage, et les
    poser ici ne devance que le prochain. Sans eux, une adresse tout juste réservée
    n'apparaîtrait pas dans la grille de son VLAN avant l'heure suivante, ce qui donnerait
    à croire que la création a échoué.
    """
    forces = {}
    source = SOURCE_CREATION.get(kind)
    if source:
        forces["Source"] = source
    if kind == "ipam" and valeurs.get("Address"):
        adresse = ipaddress.ip_address(valeurs["Address"])
        forces["IP_int"] = int(adresse)
        for vlan in snap.vlans:
            if vlan.network is not None and adresse in vlan.network:
                forces["VLAN"] = [vlan.id]
                break
    return forces


# ------------------------------------------------- rattachement applicatif, par lot
#
# Le lien entre une application et une stack n'existe nulle part en tant que tel : il est
# porté par chacun des conteneurs (`Application - Stack`), et une stack est dite rattachée
# dès qu'un seul des siens l'est. Corriger cela conteneur par conteneur est exact et
# pénible — trois gestes pour une stack de trois services, et rien à l'écran ne dit que
# les trois devraient s'accorder.
#
# D'où ces deux opérations. Elles écrivent le même champ, dans les deux sens où l'on
# raisonne : depuis la stack on désigne ce qu'elle sert, depuis l'application on coche où
# elle tourne. Aucune notion nouvelle, aucune règle relâchée — le champ reste soumis à
# `schema.editable`, seul change le nombre de lignes touchées d'un coup.

CHAMP_APPLICATION = "Application - Stack"

Cible = namedtuple("Cible", "id name")


def rattachement_ouvert():
    """Le rattachement est-il modifiable ? Même question, même réponse que partout
    ailleurs : c'est `schema` qui décide, pas ce module."""
    return schema.editable("container", CHAMP_APPLICATION)


def champ_applications(snap, stack):
    """Descripteur prêt pour le sélecteur : toutes les applications, celles de la stack
    cochées."""
    return {
        "champ": CHAMP_APPLICATION,
        "widget": "lien:application",
        "libelle": "Applications servies",
        "notice": None,
        "valeur": sorted(a.id for a in stack.apps),
        "options": [],
        "cibles": cibles(snap, "application"),
    }


def champ_stacks(snap, app):
    """Descripteur prêt pour le sélecteur : toutes les stacks, celles de l'application
    cochées.

    `id` porte ici la clé de la stack — hôte et nom — et non un identifiant Baserow : une
    stack n'est pas une ligne, c'est un regroupement de conteneurs. Deux hôtes peuvent
    porter une stack de même nom, les trois socket-proxies en sont l'exemple, et seule la
    clé les distingue.
    """
    retenues = {s.key for s in snap.stacks if any(app.id == a.id for a in s.apps)}
    return {
        "champ": "stack",
        "widget": "lien:stack",
        "libelle": "Stacks de cette application",
        "notice": None,
        "valeur": sorted(retenues),
        "options": [],
        "cibles": [Cible(s.key, f"{s.name} · {s.host.name if s.host else '?'}")
                   for s in sorted(snap.stacks, key=lambda s: s.key.lower())],
    }


class Redacteur:
    """Applique une saisie à une ligne Baserow. Crée sur Ipam et Application, jamais
    ailleurs ; ne supprime nulle part."""

    def __init__(self, client, cfg):
        self.client = client
        self.cfg = cfg

    # -- création ------------------------------------------------------------
    def creer(self, snap, kind, donnees):
        """Crée une ligne et renvoie son identifiant."""
        if kind not in CREABLES:
            raise EcritureRefusee("La création n'est pas ouverte sur cette table.")

        descripteurs = formulaire_creation(snap, kind)
        valeurs = _lire(snap, kind, descripteurs, donnees)

        cle = CLE_NATURELLE[kind]
        if not valeurs.get(cle):
            raise EcritureRefusee(f"{LIBELLES.get(cle, cle)} : ce champ est obligatoire.")
        existant = _doublon(snap, kind, cle, valeurs[cle])
        if existant is not None:
            raise EcritureRefusee(
                f"« {valeurs[cle]} » figure déjà dans l'inventaire. Modifier la ligne "
                "existante plutôt que d'en créer une seconde.")

        widgets = {d["champ"]: d["widget"] for d in descripteurs}
        payload = {champ: _payload(widgets[champ], v) for champ, v in valeurs.items()}
        payload.update(_derives(snap, kind, valeurs))

        try:
            ligne = self.client.create_row(self.cfg.TABLES[kind], payload)
        except Exception as exc:
            logger.exception("Création refusée par Baserow sur %s", kind)
            raise EcritureImpossible(_motif(exc)) from exc

        logger.info("création %s#%s %s=%r", kind, ligne.get("id"), cle, valeurs[cle])
        return ligne["id"]

    # -- rattachement applicatif --------------------------------------------

    def _poser_rattachement(self, conteneur, ids):
        try:
            self.client.update_row(self.cfg.TABLES["container"], conteneur.id,
                                   {CHAMP_APPLICATION: ids})
        except Exception as exc:
            logger.exception("Rattachement refusé par Baserow sur conteneur#%s", conteneur.id)
            raise EcritureImpossible(_motif(exc)) from exc
        logger.info("rattachement conteneur#%s %s -> %r", conteneur.id, conteneur.name, ids)

    def rattacher_stack(self, snap, stack, donnees):
        """Depuis la stack : pose le même rattachement sur tous ses conteneurs.

        Remplacement et non ajout : dire ce que sert une stack, c'est dire ce qu'elle sert
        *entièrement*. Renvoie le nombre de conteneurs réellement écrits — les autres
        portaient déjà la bonne valeur, et les réécrire n'aurait produit que du bruit dans
        l'horodatage de modification.
        """
        if not rattachement_ouvert():
            raise EcritureRefusee("Le rattachement applicatif n'est pas modifiable.")
        if CHAMP_APPLICATION not in set(donnees.getlist("soumis")):
            raise EcritureRefusee("Formulaire incomplet.")

        ids = _valider(snap, "container", CHAMP_APPLICATION, "lien:application",
                       None, donnees.getlist("champ." + CHAMP_APPLICATION))
        ecrits = 0
        for conteneur in stack.containers:
            if _actuel(conteneur, CHAMP_APPLICATION, "lien:application") == ids:
                continue
            self._poser_rattachement(conteneur, ids)
            ecrits += 1
        return ecrits

    def rattacher_application(self, snap, app, donnees):
        """Depuis l'application : coche les stacks où elle tourne.

        Une stack décochée perd CETTE application, pas les autres qu'elle porterait. Un
        conteneur peut en servir plusieurs ; les effacer toutes parce qu'on en retire une
        serait une perte silencieuse — précisément ce contre quoi cette console existe.

        Les conteneurs hors stack ne sont pas concernés : cet écran parle de stacks, et ce
        qu'il ne montre pas, il ne le touche pas.
        """
        if not rattachement_ouvert():
            raise EcritureRefusee("Le rattachement applicatif n'est pas modifiable.")
        if "stack" not in set(donnees.getlist("soumis")):
            raise EcritureRefusee("Formulaire incomplet.")

        connues = {s.key: s for s in snap.stacks}
        choisies = {c for c in donnees.getlist("champ.stack") if c}
        inconnues = choisies - set(connues)
        if inconnues:
            raise EcritureRefusee(
                "Stacks inconnues : " + ", ".join(sorted(inconnues))
                + ". La page a peut-être changé depuis son ouverture.")

        ecrits = 0
        for cle, stack in connues.items():
            voulu = cle in choisies
            for conteneur in stack.containers:
                actuel = _actuel(conteneur, CHAMP_APPLICATION, "lien:application")
                if (app.id in actuel) == voulu:
                    continue
                nouveau = sorted(set(actuel) | {app.id}) if voulu \
                    else sorted(set(actuel) - {app.id})
                self._poser_rattachement(conteneur, nouveau)
                ecrits += 1
        return ecrits

    # -- modification --------------------------------------------------------

    def appliquer(self, snap, obj, donnees):
        """`donnees` est le formulaire reçu. Renvoie la liste des changements écrits.

        Seuls les champs effectivement modifiés partent dans la requête : Baserow laisse
        intact tout champ absent du payload, ce qui évite de réécrire dix champs — et dix
        horodatages de modification — pour une note corrigée.
        """
        modifiables = {c["champ"]: c for c in formulaire(snap, obj)}

        soumis = {clef[6:] for clef in donnees if clef.startswith("champ.")}
        intrus = soumis - set(modifiables)
        if intrus:
            # Ni ignoré en silence, ni traité comme une attaque : le cas normal est une
            # page laissée ouverte pendant que le collecteur reprenait la main sur la
            # ligne. Le dire permet de recharger et de recommencer.
            raise EcritureRefusee(
                "Champs non modifiables sur cette ligne : " + ", ".join(sorted(intrus))
                + ". La fiche a peut-être changé depuis l'ouverture du formulaire.")

        changements, payload = [], {}
        for champ, nouveau in _lire(snap, obj.kind, list(modifiables.values()), donnees).items():
            descr = modifiables[champ]
            if nouveau == descr["valeur"]:
                continue
            changements.append(Changement(champ, descr["valeur"], nouveau))
            payload[champ] = _payload(descr["widget"], nouveau)

        if not payload:
            return []

        table_id = self.cfg.TABLES[obj.kind]
        try:
            self.client.update_row(table_id, obj.id, payload)
        except Exception as exc:
            logger.exception("Écriture refusée par Baserow sur %s#%s", obj.kind, obj.id)
            raise EcritureImpossible(_motif(exc)) from exc

        for c in changements:
            logger.info("écriture %s#%s %s : %r -> %r", obj.kind, obj.id, c.champ, c.avant, c.apres)
        return changements


def _motif(exc):
    """Message de Baserow plutôt que trace HTTP : `ERROR_FIELD_NOT_IN_TABLE` désigne la
    cause réelle — un champ renommé — là où « 400 Bad Request » ne désigne rien."""
    reponse = getattr(exc, "response", None)
    if reponse is not None:
        try:
            corps = reponse.json()
        except ValueError:
            corps = {}
        erreur = corps.get("error") or ""
        detail = corps.get("detail")
        if isinstance(detail, dict):
            detail = "; ".join(f"{k} : {v}" for k, v in detail.items())
        if erreur or detail:
            return " — ".join(x for x in (erreur, str(detail) if detail else "") if x)
        return f"HTTP {reponse.status_code}"
    return str(exc) or exc.__class__.__name__
