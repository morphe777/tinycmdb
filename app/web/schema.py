"""Qui possède quel champ.

C'est la raison d'être de cette console. Baserow affiche les 19 champs d'un nœud comme
s'ils étaient tous à toi ; en réalité le collecteur en réécrit la majorité à chaque
passage, et une saisie faite dans l'un d'eux disparaît sans avertissement ni trace.

Trois provenances, et non deux :

- AUTO      : écrit par le collecteur sur les lignes `Source = Auto`. Une saisie manuelle
              y est perdue au passage suivant.
- ENRICHI   : recalculé par le collecteur MÊME sur les lignes `Source = Manual`. Ne
              concerne aujourd'hui que l'IPAM : tu saisis l'adresse, la MAC et le nœud,
              le collecteur en déduit le VLAN, le constructeur, le FQDN, le hostname.
- MANUEL    : jamais touché par le collecteur, dans aucun cas.

Les lignes `Source = Manual` des tables Nœud et IPAM sont protégées intégralement (le
collecteur ne les met pas à jour et ne les supprime jamais), à l'exception des champs
ENRICHI ci-dessus. Container, Images et VLAN n'ont pas de champ `Source` : les deux
premières sont intégralement automatiques, la dernière intégralement manuelle.

Ce module est la seule source de vérité sur le sujet. Toute évolution du collecteur qui
change ce qu'il écrit doit se refléter ici, sinon la console ment — ce qui serait pire
que de ne rien afficher du tout.
"""

AUTO = "auto"
ENRICHI = "enrichi"
MANUEL = "manuel"

ORIGIN_LABELS = {
    AUTO: "écrasé à chaque passage du collecteur",
    ENRICHI: "recalculé par le collecteur, même sur une ligne manuelle",
    MANUEL: "à toi — le collecteur n'y touche jamais",
}

ORIGIN_SHORT = {AUTO: "auto", ENRICHI: "enrichi", MANUEL: "manuel"}

# Par table : {champ: provenance}. Les champs absents de ces tables sont traités comme
# MANUEL — le défaut prudent, puisque prétendre à tort qu'un champ est manuel fait perdre
# une saisie, tandis que l'inverse fait seulement rater une occasion d'éditer.
_NODE = {
    "Name": AUTO, "Type": AUTO, "Parent_host": AUTO, "vmid": AUTO, "Status": AUTO,
    "OS": AUTO, "vCPU": AUTO, "RAM_Gb": AUTO, "Disk_Gb": AUTO, "Backup": AUTO,
    "Guest_agent": AUTO, "Source": AUTO, "Last_seen": AUTO,
    # Liens posés par les autres passes (côté Container et IPAM), pas éditables ici.
    "Container": AUTO, "IPAM": AUTO,
    "Roles": MANUEL, "Criticality": MANUEL, "notes": MANUEL, "Application": MANUEL,
}

_CONTAINER = {
    "UID": AUTO, "Name": AUTO, "Host": AUTO, "Image": AUTO, "Service": AUTO,
    "Status": AUTO, "Ports": AUTO, "Restart_policy": AUTO, "Last_seen": AUTO,
    "Stack": AUTO, "Compose_path": AUTO, "Type": AUTO,
    "Notes": MANUEL, "Application - Stack": MANUEL,
}

_IMAGE = {
    "Reference": AUTO, "Registry": AUTO, "Repository": AUTO, "Tag": AUTO,
    "Digest_local": AUTO, "Available_version": AUTO, "Update_available": AUTO,
    "CVE_critical": AUTO, "CVE_high": AUTO, "Last_scan": AUTO, "Last_seen": AUTO,
    "Container": AUTO,
    "Notes": MANUEL,
}

# Cas particulier : sur une ligne Source = Manual, Address / MAC / Node / Type / Status
# restent à toi, mais le reste est recalculé à chaque passage à partir de ces trois-là.
_IPAM_AUTO_ROW = {
    "Address": AUTO, "IP_int": AUTO, "VLAN": AUTO, "Type": AUTO, "Node": AUTO,
    "FQDN": AUTO, "MAC": AUTO, "Vendor": AUTO, "Status": AUTO, "Source": AUTO,
    "Last_seen": AUTO, "Hostname": AUTO,
    "Notes": MANUEL,
}
_IPAM_MANUAL_ROW = {
    "Address": MANUEL, "MAC": MANUEL, "Node": MANUEL, "Type": MANUEL,
    "Status": MANUEL, "Notes": MANUEL, "Source": MANUEL,
    "IP_int": ENRICHI, "VLAN": ENRICHI, "FQDN": ENRICHI, "Vendor": ENRICHI,
    "Hostname": ENRICHI, "Last_seen": ENRICHI,
}

_BY_TABLE = {
    "node": _NODE,
    "container": _CONTAINER,
    "image": _IMAGE,
    "application": {},   # 100 % manuelle, par décision explicite
    "vlan": {},          # référentiel en lecture seule pour le collecteur
}


def origin(table, field, source=None):
    """Provenance d'un champ. `source` est la valeur de `Source` sur la ligne, quand la
    table en a un : sur Nœud et IPAM, elle change la réponse."""
    if table == "ipam":
        mapping = _IPAM_MANUAL_ROW if (source or "").lower() == "manual" else _IPAM_AUTO_ROW
        return mapping.get(field, MANUEL)
    if table == "node" and (source or "").lower() == "manual":
        # Une ligne manuelle est protégée intégralement : le collecteur ne la met pas à
        # jour et ne la supprime pas. Tout y est donc à toi, y compris Type et Status —
        # c'est le cas des 23 appliances, caméras et équipements IoT.
        return MANUEL
    return _BY_TABLE.get(table, {}).get(field, MANUEL)


def editable(table, field, source=None):
    """Vrai si une saisie dans ce champ survivra au passage suivant du collecteur.
    Utilisé par le lot 2 (édition) ; le lot 1 s'en sert seulement pour l'affichage."""
    return origin(table, field, source) == MANUEL
