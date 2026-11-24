"""Écriture dans l'inventaire : le seul module du service web qui en soit capable.

Tout le reste de `web/` ne connaît que l'instantané en lecture. Ce module concentre ce qui
écrit — depuis l'interface, l'API et le serveur MCP, qui passent tous les trois par ici —
pour deux raisons.

La première est une raison de sûreté. Un champ que le collecteur réécrit n'est pas
modifiable : une saisie y disparaîtrait au passage suivant, sans avertissement ni trace,
et une CMDB qui perd silencieusement ce qu'on lui confie ne vaut pas la peine d'être
tenue. La liste des champs modifiables est donc appliquée ici, côté serveur, et non dans
le gabarit : l'interface décide ce qu'elle affiche, elle ne décide pas ce qui est permis.

La seconde est une raison de cohérence. L'interface, l'API et le MCP appliquent les mêmes
règles parce qu'ils appellent le même code : un agent ne peut pas écrire ce qu'un
formulaire refuserait.

Les créations sont ouvertes là où l'ajout est un geste courant : une adresse à réserver,
une application à déclarer, un équipement que le collecteur ne peut pas découvrir — un
portable, une imprimante, une caméra —, un VLAN. Conteneurs et images restent des faits
Docker, que seul le collecteur crée. La suppression d'une ligne est possible depuis
l'interface, sur toutes les tables, et journalisée en entier ; l'API et le MCP ne
suppriment rien.

Une ligne Node créée ici porte `Source = Manual`, ce qui la protège intégralement : le
collecteur ne la met pas à jour et ne la supprimera jamais, alors même qu'il ne la voit
sur aucun hyperviseur. Sans cette clause, un portable saisi le matin disparaîtrait à la
passe suivante.
"""

import ipaddress
import logging
import re
import secrets
from collections import namedtuple

from cmdb.depot import ErreurDonnee

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
    """La demande était recevable ; la base l'a refusée (verrou, disque plein…)."""


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
        ("Parent_host", "lien1:node"),
        ("vmid", "entier"),
        ("OS", "texte"),
        ("vCPU", "entier"),
        ("RAM_Gb", "entier"),
        ("Disk_Gb", "entier"),
        ("Backup", "bool"),
        ("Guest_agent", "bool"),
        ("Roles", "multi"),
        ("Criticality", "select"),
        ("Application", "lien:application"),
        ("notes", "long"),
        ("Arch", "select"),
        ("CPU", "lien1:cpu"),
        ("Sockets", "entier"),
        ("CPU_type", "texte"),
        ("CPU_level", "select"),
        ("Machine", "texte"),
        ("Kernel", "texte"),
        ("Motherboard", "texte"),
        ("BIOS", "texte"),
        ("RAM_type", "select"),
        ("RAM_ECC", "select"),
        ("RAM_MHz", "entier"),
        ("RAM_modules", "texte"),
        ("RAM_max_Gb", "entier"),
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
    "cpu": [
        ("Name", "texte"),
        ("Vendor", "select"),
        ("Arch", "select"),
        ("Generation", "texte"),
        ("Cores", "entier"),
        ("Threads", "entier"),
        ("Base_MHz", "entier"),
        ("Boost_MHz", "entier"),
        ("TDP_W", "entier"),
        ("L3_MB", "entier"),
        ("x86_level", "select"),
        ("iGPU", "texte"),
        ("Virtualization", "texte"),
        ("Max_RAM_Gb", "entier"),
        ("Doc", "url"),
        ("Notes", "long"),
    ],
    "composant": [
        ("Name", "texte"),
        ("Category", "select"),
        ("Bus", "select"),
        ("Host", "lien1:node"),
        ("Assigned_to", "lien1:node"),
        ("Vendor", "texte"),
        ("Model", "texte"),
        ("Reference", "texte"),
        ("Chipset", "texte"),
        ("Address", "texte"),
        ("Serial", "texte"),
        ("Ports", "entier"),
        ("Speed_Mbps", "entier"),
        ("Capacity_Gb", "entier"),
        ("Standard", "select"),
        ("VRAM_Gb", "entier"),
        ("Shared_memory", "bool"),
        ("GPU_cores", "entier"),
        ("Compute_API", "multi"),
        ("Firmware", "texte"),
        ("Doc", "url"),
        ("Notes", "long"),
    ],
}

# Champs du nœud qui n'ont de sens que pour une machine réelle, ou que pour un invité.
# Ni affichés ni proposés à la saisie ailleurs : la carte mère d'un LXC ou la configuration
# Proxmox d'une caméra ne sont pas des champs vides, ce sont des questions sans objet.
PHYSIQUE_SEULEMENT = {"CPU", "Sockets", "Kernel", "Motherboard", "BIOS", "RAM_type", "RAM_ECC",
                      "RAM_MHz", "RAM_modules", "RAM_max_Gb"}
# L'identifiant Proxmox, l'agent invité et la sauvegarde ne se rapportent qu'à un invité :
# proposés sur une machine réelle, ils laissaient croire qu'il fallait les renseigner.
INVITE_SEULEMENT = {"CPU_type", "Config", "vmid", "Guest_agent", "Backup"}
TYPES_VIRTUELS = ("VM", "LXC")


# Les champs propres au calcul (mémoire graphique, interfaces) n'ont de sens que sur un GPU
# ou un accélérateur : la mémoire graphique d'une carte réseau est une question sans objet.
CALCUL_SEULEMENT = {"VRAM_Gb", "Shared_memory", "GPU_cores", "Compute_API"}
CATEGORIES_CALCUL = ("GPU", "Accelerator")

# Pour chaque table : le champ qui décide, et pour chaque portée les valeurs de ce champ
# où elle s'applique (« dans ») ou ne s'applique pas (« hors »). Une seule déclaration,
# lue par le serveur (`applicable`) et transmise telle quelle à la page, qui masque les
# champs pendant la saisie — à la création, ou quand on change le type d'une ligne
# manuelle, le serveur ne connaît pas encore la valeur retenue.
PILOTES = {
    "node": ("Type", {"physique": ("hors", TYPES_VIRTUELS), "invite": ("dans", TYPES_VIRTUELS)}),
    "composant": ("Category", {"calcul": ("dans", CATEGORIES_CALCUL)}),
}
_PORTEES = {
    "node": {**{c: "physique" for c in PHYSIQUE_SEULEMENT},
             **{c: "invite" for c in INVITE_SEULEMENT}},
    "composant": {c: "calcul" for c in CALCUL_SEULEMENT},
}


def portee(kind, champ):
    """Nom de la portée d'un champ (« physique », « invite », « calcul »), ou None s'il
    vaut pour toute ligne de sa table."""
    return _PORTEES.get(kind, {}).get(champ)


def regles_pilote(kind, champ):
    """Les règles à poser sur le champ qui décide, pour la page ; None sur les autres."""
    pilote = PILOTES.get(kind)
    if not pilote or pilote[0] != champ:
        return None
    return {nom: [mode, list(valeurs)] for nom, (mode, valeurs) in pilote[1].items()}


COURANTE = object()


def applicable(obj, champ, pilote=COURANTE):
    """Le champ a-t-il un sens sur cette ligne ? `pilote` est la valeur du champ qui décide
    quand elle est en train de changer — « ce nœud devient une VM » ouvre son vmid dans la
    même saisie ; sinon, celle qui est enregistrée."""
    if pilote is COURANTE and obj.kind in PILOTES:
        pilote = obj.sel(PILOTES[obj.kind][0])
    return applicable_pour(obj.kind, champ, pilote)


def applicable_pour(kind, champ, pilote):
    """Même question pour une ligne qui n'existe pas encore : `pilote` est la valeur
    retenue pour le type (ou la catégorie)."""
    nom = portee(kind, champ)
    if nom is None:
        return True
    mode, valeurs = PILOTES[kind][1][nom]
    dedans = pilote in valeurs
    return dedans if mode == "dans" else not dedans


def sans_objet(kind, valeurs):
    """Champs de `valeurs` ({champ: valeur}) sans objet pour la ligne qu'elles décrivent."""
    pilote = PILOTES.get(kind)
    if not pilote:
        return set()
    return {c for c in valeurs if not applicable_pour(kind, c, valeurs.get(pilote[0]) or None)}


def pilote_modifiable(obj):
    """Le type (ou la catégorie) de cette ligne peut-il changer depuis un formulaire ? Sinon
    rien ne révélera un champ sans objet : inutile de l'écrire dans la page."""
    pilote = PILOTES.get(obj.kind)
    return bool(pilote) and schema.editable(obj.kind, pilote[0], obj.source)


def pilote_soumis(kind, valeurs):
    """Valeur du champ qui décide, si `valeurs` ({champ: valeur}) la change ; COURANTE
    sinon."""
    pilote = PILOTES.get(kind)
    if not pilote or pilote[0] not in valeurs:
        return COURANTE
    return valeurs[pilote[0]] or None


# Libellés de saisie. Le nom du champ est exact mais parfois sec : `notes` en minuscules sur
# une table et `Notes` sur une autre, `vmid`, `RAM_Gb`. La fiche en consultation affiche le
# nom du champ tel qu'il est dans la base — le même que dans l'API et les exports CSV ; le
# formulaire, lui, s'adresse à quelqu'un qui saisit.
LIBELLES = {
    "Name": "Nom", "name": "Nom", "notes": "Notes", "Notes": "Notes",
    "Type": "Type", "Status": "Statut", "Roles": "Rôles", "Criticality": "Criticité",
    "Application": "Applications", "Application - Stack": "Application",
    "OS": "Système", "vCPU": "vCPU", "vmid": "Identifiant Proxmox (vmid)",
    "Parent_host": "Hébergé par", "Backup": "Sauvegardé", "Guest_agent": "Agent invité", "RAM_Gb": "Mémoire (Go)", "Disk_Gb": "Disque (Go)",
    "Address": "Adresse", "MAC": "Adresse MAC", "Node": "Nœud",
    "Capability": "Fonctions", "URL": "URL", "URL 2": "URL secondaire", "Doc": "Documentation",
    "vlan_id": "Identifiant 802.1Q", "subnet": "Sous-réseau", "gateway": "Passerelle",
    "zone": "Zone", "dhcp": "DHCP", "dhcp_range": "Plage DHCP", "dns": "DNS",
    "Arch": "Architecture", "CPU": "Processeur", "Sockets": "Sockets",
    "CPU_type": "Type de processeur (VM)", "CPU_level": "Niveau x86-64", "Machine": "Machine",
    "Kernel": "Noyau", "Motherboard": "Carte mère", "BIOS": "BIOS / UEFI",
    "RAM_type": "Type de mémoire", "RAM_ECC": "ECC", "RAM_MHz": "Fréquence mémoire (MHz)",
    "RAM_modules": "Barrettes", "RAM_max_Gb": "Mémoire maximale (Go)",
    "Vendor": "Marque", "Generation": "Génération", "Cores": "Cœurs", "Threads": "Threads",
    "Base_MHz": "Fréquence de base (MHz)", "Boost_MHz": "Fréquence turbo (MHz)",
    "TDP_W": "TDP (W)", "L3_MB": "Cache L3 (Mo)", "x86_level": "Niveau x86-64",
    "iGPU": "Graphique intégré", "Virtualization": "Virtualisation",
    "Max_RAM_Gb": "Mémoire maximale supportée (Go)",
    "Category": "Catégorie", "Bus": "Bus", "Host": "Installé dans",
    "Assigned_to": "Utilisé par (VM en passthrough)", "Model": "Modèle", "Reference": "Référence",
    "Chipset": "Chipset", "Serial": "Numéro de série", "Ports": "Ports",
    "Speed_Mbps": "Vitesse (Mbit/s)", "Capacity_Gb": "Capacité (Go)", "Standard": "Norme",
    "Firmware": "Micrologiciel",
    "VRAM_Gb": "Mémoire graphique (Go)", "Shared_memory": "Mémoire partagée",
    "GPU_cores": "Cœurs graphiques", "Compute_API": "Interfaces de calcul",
}

# Précisions affichées sous un champ, là où la saisie a une conséquence non évidente.
NOTICES = {
    ("node", "Roles"): "Le rôle « docker » déclenche la collecte des conteneurs sur cet hôte.",
    ("ipam", "Address"): "Modifier l'adresse d'une ligne manuelle revient à déclarer un autre équipement.",
    ("container", "Application - Stack"): "Rattachement métier : c'est lui qui donne un sens aux vulnérabilités de l'image.",
    ("node", "Application"): "Pour un équipement qui rend un service sans passer par un conteneur.",
    ("node", "CPU"): "Le modèle, dans le référentiel des processeurs : ses caractéristiques s'y saisissent une fois pour toutes les machines qui le portent.",
    ("node", "RAM_modules"): "Nombre, taille et format des barrettes, et emplacements libres : « 2 × 16 Go SO-DIMM, 2/2 occupés ».",
    ("node", "BIOS"): "Version et date : c'est la première chose qu'on demande devant un problème matériel.",
    ("cpu", "Name"): "Le modèle tel que la machine le nomme, sans les (R) et (TM).",
    ("cpu", "Boost_MHz"): "Fréquence maximale sur un cœur.",
    ("composant", "Speed_Mbps"): "En Mbit/s : 2500 pour 2,5 Gb/s, 10000 pour 10 Gb/s.",
    ("composant", "Capacity_Gb"): "En Go décimaux, ceux de l'étiquette : un disque de 1 To vaut 1000.",
    ("composant", "Assigned_to"): "La VM qui reçoit ce périphérique en passthrough, s'il y en a une.",
    ("composant", "VRAM_Gb"): "Ce dont il dispose pour charger un modèle. Mémoire partagée : la part utilisable de la mémoire de la machine (sur un Mac, environ les deux tiers).",
    ("composant", "Shared_memory"): "Coché pour un GPU intégré ou une puce Apple : sa mémoire est celle de la machine, et ce qu'il prend manque aux applications.",
    ("composant", "GPU_cores"): "Cœurs graphiques, ou unités d'exécution : ce qui distingue un M2 à 8 cœurs d'un M2 à 10.",
    ("composant", "Compute_API"): "Ce qu'une application peut lui demander : CUDA pour la plupart des outils d'IA, VA-API ou QSV pour un transcodage Jellyfin, Edge TPU pour Frigate.",
}

# Certaines précisions n'ont de sens qu'à la modification : prévenir qu'on change
# d'équipement en changeant l'adresse n'a aucun sens sur une ligne qui n'existe pas encore.
NOTICES_CREATION = {
    ("ipam", "Address"): "Ligne marquée « saisie manuelle » : le collecteur ne la supprimera jamais.",
    ("ipam", "Node"): "L'équipement qui répond à cette adresse, s'il est déjà inventorié.",
    ("application", "Name"): "Le regroupement fonctionnel, pas le projet Compose — celui-ci est déjà porté par la stack.",
    ("node", "Name"): "Ce que le réseau en dit : le nom d'hôte, pas l'étiquette collée dessus.",
    ("node", "Type"): "Physical pour un équipement réel. VM et LXC appartiennent au collecteur.",
    ("node", "Roles"): "À quoi il sert. Ne pas poser « docker » ici : ce rôle déclenche une interrogation du socket-proxy, qui n'existe pas sur un portable.",
    ("cpu", "Name"): "Le modèle exact, sans les (R) et (TM) : « Intel Core i5-8500T », « Broadcom BCM2711 ».",
    ("composant", "Name"): "Ce qui le désigne pour toi : « Carte 4 × 2,5 GbE », « Clé Zigbee ».",
    ("composant", "Host"): "La machine dans laquelle il est installé ou branché.",
    ("composant", "Category"): "GPU ou Accelerator : une ligne par unité. Un Mac Mini en a deux, son GPU et son Neural Engine.",
}

LIENS = {"lien:node": "node", "lien:application": "application", "lien1:node": "node",
         "lien1:cpu": "cpu"}


def _simple(widget):
    """Lien vers une seule ligne (`lien1:…`) : un sélecteur, pas une liste à cocher."""
    return widget.startswith("lien1:")


# Tables où la console sait créer une ligne. Les deux autres sont alimentées par le
# collecteur (Container, Images), et lui seul sait ce qu'elles doivent contenir.
#
# Node y figure pour ce que Proxmox ne peut pas voir : un portable, une imprimante, une
# caméra, un NAS. Sans cela, la moitié du parc réel n'a aucun moyen d'entrer dans la CMDB,
# et l'IPAM renvoie des adresses rattachées à rien.
CREABLES = ("ipam", "application", "node", "vlan", "cpu", "composant")

# Widget de saisie d'un champ personnalisé, par type.
WIDGET_PERSO = {"texte": "texte", "long": "long", "url": "url", "entier": "entier",
                "bool": "bool", "date": "date", "select": "select", "multi": "multi"}

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def champs_de(snap, kind):
    """Champs saisissables d'une table : ceux du modèle, puis les champs personnalisés
    créés dans l'administration, dans leur ordre."""
    return list(CHAMPS.get(kind, [])) + [
        (p["nom"], WIDGET_PERSO.get(p["type"], "texte"))
        for p in snap.perso.get(kind, [])]


def aide_perso(snap, kind, champ):
    for p in snap.perso.get(kind, []):
        if p["nom"] == champ:
            return p.get("aide")
    return None

# Valeur imposée au champ `Source` de la ligne créée. Sur l'IPAM, c'est la clause qui
# protège la saisie : sans `Manual`, le collecteur supprimerait cette adresse dès qu'il
# constaterait ne pas la voir sur le réseau. Elle n'est pas proposée au formulaire — c'est
# une conséquence du geste, pas une option.
SOURCE_CREATION = {"ipam": "Manual", "node": "Manual", "composant": "Manual"}

# Champ portant le nom, par table : sert au contrôle de doublon et au message qui
# l'accompagne.
CLE_NATURELLE = {"ipam": "Address", "application": "Name", "node": "Name", "vlan": "name",
                 "cpu": "Name", "composant": "Name"}

# Tables où le nom saisi n'a pas à être unique : deux barrettes, deux disques du même modèle
# sont deux lignes. Leur clé technique (`UID`) est posée par la console.
NOMS_LIBRES = ("composant",)


# Champs du nœud laissés hors de la fenêtre de création de la console : on déclare d'abord
# un équipement, on le décrit ensuite depuis sa fiche. L'API et le MCP, eux, acceptent tout
# en une fois — un agent qui vient d'identifier une machine n'a pas à s'y prendre à deux fois.
DETAIL_CREATION = {"node": {"Arch", "CPU", "Sockets", "CPU_type", "CPU_level", "Machine",
                            "Kernel", "Motherboard", "BIOS", "RAM_type", "RAM_ECC", "RAM_MHz",
                            "RAM_modules", "RAM_max_Gb"}}


def formulaire_creation(snap, kind, essentiels=False):
    """Champs d'un formulaire de création.

    Rigoureusement les mêmes règles qu'en modification, appliquées à la ligne *telle
    qu'elle sera créée* : sur l'IPAM, `schema.editable()` interrogé avec `Source = Manual`
    rend Address, MAC, Node, Type, Status et Notes — et écarte le VLAN, le constructeur et
    le nom DNS, que le collecteur recalculera. Une déclaration, trois usages : la mention
    de provenance en consultation, le formulaire de modification, celui-ci.
    """
    source = SOURCE_CREATION.get(kind)
    exclus = DETAIL_CREATION.get(kind, set()) if essentiels else set()
    champs = []
    for champ, widget in champs_de(snap, kind):
        if not schema.editable(kind, champ, source) or champ in exclus:
            continue
        champs.append({
            "champ": champ,
            "widget": widget,
            "libelle": LIBELLES.get(champ, champ),
            "notice": NOTICES_CREATION.get((kind, champ)) or aide_perso(snap, kind, champ),
            "portee": portee(kind, champ),
            "regles": regles_pilote(kind, champ),
            "valeur": [] if (widget == "multi" or widget in LIENS and not _simple(widget))
                      else (False if widget == "bool" else None),
            "options": options(snap, kind, champ) if widget in ("select", "multi") else [],
            "cibles": cibles(snap, LIENS[widget]) if widget in LIENS else [],
        })
    return champs


# --------------------------------------------------------------------------- options

def options(snap, kind, field):
    """Options proposées pour une liste, dans l'ordre réglé dans l'administration.

    Ce sont les options *définies*, employées ou non : c'est là qu'on les crée, qu'on les
    renomme et qu'on choisit leur couleur (Administration → Listes).
    """
    return [o["valeur"] for o in snap.options.get((kind, field), [])]


def cibles(snap, kind):
    """Objets proposés pour un champ de liaison, triés par nom."""
    return sorted(snap.by_kind.get(kind, {}).values(), key=lambda o: o.name.lower())


def formulaire(snap, obj, pilote=COURANTE, tous=False):
    """Champs réellement modifiables sur cet objet, prêts à être rendus.

    Le filtre est `schema.editable()`, et lui seul : c'est la même fonction qui décide de
    la mention « manuel » affichée en consultation. Les deux écrans ne peuvent donc pas se
    contredire.

    `tous` garde aussi les champs sans objet pour la valeur actuelle du type (ou de la
    catégorie) : c'est le formulaire affiché, où la page les masque et les révèle selon le
    choix fait. `pilote` évalue l'applicabilité pour une valeur qui est en train de changer.
    """
    champs = []
    for champ, widget in champs_de(snap, obj.kind):
        if not schema.editable(obj.kind, champ, obj.source):
            continue
        if not (tous and pilote_modifiable(obj)) and not applicable(obj, champ, pilote):
            continue
        champs.append({
            "champ": champ,
            "widget": widget,
            "libelle": LIBELLES.get(champ, champ),
            "notice": NOTICES.get((obj.kind, champ)) or aide_perso(snap, obj.kind, champ),
            "portee": portee(obj.kind, champ),
            "regles": regles_pilote(obj.kind, champ),
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
        elif _simple(widget):
            try:
                saisie[champ] = int(donnees.get(clef) or 0) or None
            except (TypeError, ValueError):
                saisie[champ] = None
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
    if _simple(widget):
        liens = [v["id"] for v in (obj.get(champ) or []) if isinstance(v, dict) and "id" in v]
        return liens[0] if liens else None
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

    if _simple(widget):
        texte = (brut or "").strip()
        if not texte:
            return None
        try:
            ident = int(texte)
        except ValueError:
            raise EcritureRefusee(f"{libelle} : référence invalide")
        if ident not in snap.by_kind.get(LIENS[widget], {}):
            raise EcritureRefusee(f"{libelle} : référence inconnue ({ident})")
        return ident

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

    if widget == "date":
        if not texte:
            return None
        if not DATE.match(texte):
            raise EcritureRefusee(f"{libelle} : date attendue au format AAAA-MM-JJ")
        return texte

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
    if widget == "texte" and not texte and champ in ("Name", "name", "Address", "UID", "Reference"):
        raise EcritureRefusee(f"{libelle} : ce champ ne peut pas être vide")
    return texte or None


def _payload(widget, valeur):
    """Forme attendue par la base pour effacer ou poser la valeur."""
    if _simple(widget):
        return [valeur] if valeur else []
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
        if widget == "multi" or (widget in LIENS and not _simple(widget)):
            brut, multiples = None, donnees.getlist(clef)
        else:
            brut, multiples = donnees.get(clef), []
        valeurs[champ] = _valider(snap, kind, champ, widget, brut, multiples)
    return valeurs


def _doublon(snap, kind, champ, valeur):
    """Ligne existante portant déjà cette clé. La base l'interdit aussi ; le vérifier ici
    donne un message qui désigne la ligne existante plutôt qu'une contrainte violée."""
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
    if kind == "composant":
        # Clé technique d'un composant saisi : unique, stable, sans prétention à dire quoi
        # que ce soit. Celles du collecteur (`hôte/pci/…`, `disk/<série>`) ne peuvent pas
        # la rencontrer.
        forces["UID"] = "manuel/" + secrets.token_hex(6)
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

    `id` porte ici la clé de la stack — hôte et nom — et non un identifiant de ligne : une
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
    """Applique une saisie à l'inventaire. `acteur` nomme l'auteur au journal :
    `interface`, `api:<jeton>` ou `mcp:<jeton>`.

    `simulation` valide tout sans rien écrire et rend ce qui serait fait : c'est ce qui
    sert l'aperçu d'un import CSV.
    """

    def __init__(self, depot):
        self.depot = depot

    def _ecrire(self, kind, ident, payload, acteur):
        try:
            if ident is None:
                return self.depot.creer(kind, payload, acteur=acteur)
            return self.depot.modifier(kind, ident, payload, acteur=acteur)
        except ErreurDonnee as exc:
            raise EcritureRefusee(str(exc)) from exc
        except Exception as exc:
            logger.exception("Écriture refusée par la base sur %s#%s", kind, ident)
            raise EcritureImpossible(_motif(exc)) from exc

    # -- création ------------------------------------------------------------
    def creer(self, snap, kind, donnees, acteur="interface", simulation=False):
        """Crée une ligne et renvoie son identifiant (None en simulation)."""
        if kind not in CREABLES:
            raise EcritureRefusee("La création n'est pas ouverte sur cette table.")

        descripteurs = formulaire_creation(snap, kind)
        valeurs = _lire(snap, kind, descripteurs, donnees)
        # Un formulaire affiché sans script envoie tous les champs, vmid d'un portable ou
        # mémoire graphique d'une carte réseau compris : laissés de côté, pas refusés.
        for champ in sans_objet(kind, valeurs):
            del valeurs[champ]

        cle = CLE_NATURELLE[kind]
        if not valeurs.get(cle):
            raise EcritureRefusee(f"{LIBELLES.get(cle, cle)} : ce champ est obligatoire.")
        existant = None if kind in NOMS_LIBRES else _doublon(snap, kind, cle, valeurs[cle])
        if existant is not None:
            raise EcritureRefusee(
                f"« {valeurs[cle]} » figure déjà dans l'inventaire. Modifier la ligne "
                "existante plutôt que d'en créer une seconde.")
        if simulation:
            return None

        widgets = {d["champ"]: d["widget"] for d in descripteurs}
        payload = {champ: _payload(widgets[champ], v) for champ, v in valeurs.items()}
        payload.update(_derives(snap, kind, valeurs))

        ligne = self._ecrire(kind, None, payload, acteur)
        logger.info("création %s#%s %s=%r par %s", kind, ligne.get("id"), cle, valeurs[cle], acteur)
        return ligne["id"]

    # -- suppression ---------------------------------------------------------
    def supprimer(self, obj, acteur="interface"):
        """Supprime la ligne pour de bon. Le journal en garde le contenu intégral."""
        try:
            self.depot.supprimer(obj.kind, obj.id, acteur=acteur)
        except ErreurDonnee as exc:
            raise EcritureRefusee(str(exc)) from exc
        logger.info("suppression %s#%s %s par %s", obj.kind, obj.id, obj.name, acteur)

    # -- provenance ----------------------------------------------------------
    def basculer_source(self, obj, acteur="interface"):
        """Passe une ligne Node ou IPAM de Auto à Manual, ou l'inverse.

        Manual protège la ligne du collecteur (il ne la met plus à jour et ne la supprime
        plus) ; Auto la lui rend. C'est le geste qu'on faisait dans Baserow en changeant
        la colonne Source.
        """
        if obj.kind not in ("node", "ipam", "composant"):
            raise EcritureRefusee("Seuls les nœuds, les adresses et les composants portent "
                                  "une provenance.")
        nouvelle = "Auto" if obj.is_manual else "Manual"
        self._ecrire(obj.kind, obj.id, {"Source": nouvelle}, acteur)
        return nouvelle

    # -- rattachement applicatif --------------------------------------------

    def _poser_rattachement(self, conteneur, ids, acteur):
        self._ecrire("container", conteneur.id, {CHAMP_APPLICATION: ids}, acteur)
        logger.info("rattachement conteneur#%s %s -> %r", conteneur.id, conteneur.name, ids)

    def rattacher_stack(self, snap, stack, donnees, acteur="interface"):
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
            self._poser_rattachement(conteneur, ids, acteur)
            ecrits += 1
        return ecrits

    def rattacher_application(self, snap, app, donnees, acteur="interface"):
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
                self._poser_rattachement(conteneur, nouveau, acteur)
                ecrits += 1
        return ecrits

    # -- modification --------------------------------------------------------

    def appliquer(self, snap, obj, donnees, hors_ligne=(), acteur="interface",
                  simulation=False):
        """`donnees` est le formulaire reçu. Renvoie la liste des changements écrits.

        `hors_ligne` nomme les champs que ce formulaire porte sans qu'ils appartiennent à
        la ligne : sur une application, le rattachement aux stacks s'écrit sur des
        conteneurs. Ils sont enregistrés par ailleurs ; ici il suffit de ne pas les
        prendre pour des intrus.

        Seuls les champs effectivement modifiés sont écrits : un champ absent reste
        intact, et le journal ne note que ce qui a réellement changé.
        """
        # Un champ sans objet pour la ligne n'est pas un intrus : un formulaire affiché sans
        # script les envoie tous. Il est simplement laissé de côté, selon le type (ou la
        # catégorie) qui sort de cette même saisie.
        modifiables = {c["champ"]: c for c in formulaire(snap, obj, tous=True)}
        pilote = PILOTES.get(obj.kind, (None,))[0]
        portes = set(donnees.getlist("soumis"))
        valeur_pilote = pilote_soumis(obj.kind, {pilote: (donnees.get(f"champ.{pilote}") or "").strip()}
                                      if pilote in portes else {})
        soumis = {clef[6:] for clef in donnees if clef.startswith("champ.")}
        intrus = soumis - set(modifiables) - set(hors_ligne)
        if intrus:
            # Ni ignoré en silence, ni traité comme une attaque : le cas normal est une
            # page laissée ouverte pendant que le collecteur reprenait la main sur la
            # ligne. Le dire permet de recharger et de recommencer.
            raise EcritureRefusee(
                "Champs non modifiables sur cette ligne : " + ", ".join(sorted(intrus))
                + ". La fiche a peut-être changé depuis l'ouverture du formulaire.")

        retenus = [d for d in modifiables.values()
                   if applicable(obj, d["champ"], valeur_pilote)]
        changements, payload = [], {}
        for champ, nouveau in _lire(snap, obj.kind, retenus, donnees).items():
            descr = modifiables[champ]
            if nouveau == descr["valeur"]:
                continue
            changements.append(Changement(champ, descr["valeur"], nouveau))
            payload[champ] = _payload(descr["widget"], nouveau)

        if not payload or simulation:
            return changements

        self._ecrire(obj.kind, obj.id, payload, acteur)
        for c in changements:
            logger.info("écriture %s#%s %s : %r -> %r par %s", obj.kind, obj.id, c.champ,
                        c.avant, c.apres, acteur)
        return changements


def _motif(exc):
    """Message de la base plutôt qu'une trace : « database is locked » désigne la cause
    réelle là où une erreur 500 ne désigne rien."""
    return str(exc) or exc.__class__.__name__
