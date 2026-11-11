"""Le modèle de données : six tables, et la forme de chacun de leurs champs.

Les noms de champ sont ceux que la CMDB emploie partout — gabarits, collecteur, règles de
provenance (`web/schema.py`), CSV, API et MCP. Ils viennent de Baserow et sont gardés
tels quels, majuscules et espaces compris (`URL 2`, `Application - Stack`) : un seul
vocabulaire d'un bout à l'autre, plutôt qu'une table de traduction à tenir à jour entre le
stockage et le reste.

Le nom de colonne SQL, lui, est libre : c'est une affaire interne à `cmdb/`.

Types de champ :

- `texte`, `long`, `url` : chaîne ; `entier` ; `bool` ; `date` (AAAA-MM-JJ) ;
  `horodatage` (ISO 8601, UTC) ;
- `select` : une option de la table `option`, rendue `{"id", "value", "color"}` ;
- `multi` : plusieurs options, rendues en liste de la même forme ;
- `lien` : une ligne d'une autre table, colonne de clé étrangère, rendue
  `[{"id", "value"}]` — une liste, comme Baserow, pour que le graphe ne distingue pas un
  lien simple d'un lien multiple ;
- `liens` : plusieurs lignes, par une table de liaison ;
- `inverse` : l'autre extrémité d'un `lien` ou de `liens`, calculée à la lecture et jamais
  écrite. Elle n'existe que pour garder la symétrie que Baserow offrait.

Les champs personnalisés, créés depuis l'administration, prennent les mêmes types sauf
`lien`, `liens` et `inverse`.
"""

from collections import namedtuple

Champ = namedtuple("Champ", "nom type colonne cible via", defaults=(None, None, None))
Table = namedtuple("Table", "nom sql libelle singulier cle champs")

TYPES_PERSO = {
    "texte": "Texte court",
    "long": "Texte long",
    "url": "Adresse web",
    "entier": "Nombre entier",
    "bool": "Case à cocher",
    "date": "Date",
    "select": "Liste (un choix)",
    "multi": "Liste (plusieurs choix)",
}

# Familles de couleur proposées pour une option. Ce sont celles que la console sait
# afficher (`web/store.py:_TEINTES`) ; les déclinaisons de Baserow (`light-blue`,
# `darker-red`) restent lisibles, elles se ramènent à leur famille.
COULEURS = ("blue", "cyan", "green", "yellow", "orange", "red", "brown", "purple",
            "pink", "gray")


def _t(nom, sql, libelle, singulier, cle, champs):
    return Table(nom, sql, libelle, singulier, cle, tuple(champs))


TABLES = {
    "node": _t("node", "node", "Nœuds", "nœud", "Name", [
        Champ("Name", "texte", "name"),
        Champ("Type", "select", "type"),
        Champ("Parent_host", "lien", "parent_id", "node"),
        Champ("vmid", "entier", "vmid"),
        Champ("Status", "select", "status"),
        Champ("OS", "texte", "os"),
        Champ("vCPU", "entier", "vcpu"),
        Champ("RAM_Gb", "entier", "ram_gb"),
        Champ("Disk_Gb", "entier", "disk_gb"),
        Champ("Roles", "multi"),
        Champ("Criticality", "select", "criticality"),
        Champ("Backup", "bool", "backup"),
        Champ("Guest_agent", "bool", "guest_agent"),
        Champ("Source", "select", "source"),
        Champ("Last_seen", "horodatage", "last_seen"),
        Champ("notes", "long", "notes"),
        Champ("Application", "liens", None, "application",
              ("node_application", "node_id", "application_id")),
        Champ("Container", "inverse", None, "container", ("container", "Host")),
        Champ("IPAM", "inverse", None, "ipam", ("ipam", "Node")),
    ]),
    "container": _t("container", "container", "Conteneurs", "conteneur", "UID", [
        Champ("UID", "texte", "uid"),
        Champ("Name", "texte", "name"),
        Champ("Host", "lien", "host_id", "node"),
        Champ("Image", "lien", "image_id", "image"),
        Champ("Service", "texte", "service"),
        Champ("Status", "select", "status"),
        Champ("Ports", "texte", "ports"),
        Champ("Restart_policy", "select", "restart_policy"),
        Champ("Stack", "texte", "stack"),
        Champ("Compose_path", "texte", "compose_path"),
        Champ("Type", "select", "type"),
        Champ("Last_seen", "horodatage", "last_seen"),
        Champ("Notes", "long", "notes"),
        Champ("Application - Stack", "liens", None, "application",
              ("container_application", "container_id", "application_id")),
    ]),
    "image": _t("image", "image", "Images", "image", "Reference", [
        Champ("Reference", "texte", "reference"),
        Champ("Registry", "texte", "registry"),
        Champ("Repository", "texte", "repository"),
        Champ("Tag", "texte", "tag"),
        Champ("Digest_local", "texte", "digest_local"),
        Champ("Available_version", "texte", "available_version"),
        Champ("Update_available", "bool", "update_available"),
        Champ("CVE_critical", "entier", "cve_critical"),
        Champ("CVE_high", "entier", "cve_high"),
        Champ("Last_scan", "date", "last_scan"),
        Champ("Last_seen", "horodatage", "last_seen"),
        Champ("Notes", "long", "notes"),
        Champ("Container", "inverse", None, "container", ("container", "Image")),
    ]),
    "ipam": _t("ipam", "ipam", "Adresses IP", "adresse", "Address", [
        Champ("Address", "texte", "address"),
        Champ("IP_int", "entier", "ip_int"),
        Champ("VLAN", "lien", "vlan_id", "vlan"),
        Champ("Type", "select", "type"),
        Champ("Node", "lien", "node_id", "node"),
        Champ("FQDN", "texte", "fqdn"),
        Champ("Hostname", "texte", "hostname"),
        Champ("MAC", "texte", "mac"),
        Champ("Vendor", "texte", "vendor"),
        Champ("Status", "select", "status"),
        Champ("Source", "select", "source"),
        Champ("Last_seen", "horodatage", "last_seen"),
        Champ("Notes", "long", "notes"),
    ]),
    "vlan": _t("vlan", "vlan", "VLAN", "VLAN", "name", [
        Champ("name", "texte", "name"),
        Champ("vlan_id", "entier", "numero"),
        Champ("subnet", "texte", "subnet"),
        Champ("gateway", "texte", "gateway"),
        Champ("zone", "select", "zone"),
        Champ("dhcp", "bool", "dhcp"),
        Champ("dhcp_range", "texte", "dhcp_range"),
        Champ("dns", "texte", "dns"),
        Champ("notes", "long", "notes"),
        Champ("IPAM", "inverse", None, "ipam", ("ipam", "VLAN")),
    ]),
    "application": _t("application", "application", "Applications", "application", "Name", [
        Champ("Name", "texte", "name"),
        Champ("Status", "select", "status"),
        Champ("Criticality", "select", "criticality"),
        Champ("Capability", "multi"),
        Champ("URL", "url", "url"),
        Champ("URL 2", "url", "url_2"),
        Champ("Doc", "url", "doc"),
        Champ("Notes", "long", "notes"),
        Champ("Containers", "inverse", None, "container", ("container", "Application - Stack")),
        Champ("VM", "inverse", None, "node", ("node", "Application")),
    ]),
}

# Ordre d'écriture et de lecture : une table avant celles qui la référencent.
ORDRE = ("vlan", "application", "node", "image", "container", "ipam")


def table(kind):
    try:
        return TABLES[kind]
    except KeyError:
        raise KeyError(f"table inconnue : {kind}") from None


def champ(kind, nom):
    for c in table(kind).champs:
        if c.nom == nom:
            return c
    return None


def champs_a_options(kind):
    return [c for c in table(kind).champs if c.type in ("select", "multi")]


# Options dont le code dépend : il les compare par leur libellé. Les renommer ou les
# supprimer casserait une règle sans un message — on peut en changer la couleur, pas plus.
OPTIONS_PROTEGEES = {
    ("node", "Source"): {"Auto", "Manual"},
    ("ipam", "Source"): {"Auto", "Manual"},
    ("node", "Type"): {"Physical", "VM", "LXC"},
    ("node", "Roles"): {"Hypervisor", "Docker"},
    ("node", "Status"): {"Running", "Stopped"},
    ("container", "Status"): {"Running", "Exited", "Restarting", "Paused"},
    ("container", "Restart_policy"): {"Always", "Unless-Stopped", "On-Failure", "No"},
    ("container", "Type"): {"Docker_Compose", "Container"},
    ("ipam", "Status"): {"Used", "Reserved", "To check"},
    ("ipam", "Type"): {"Static", "DHCP-reservation"},
}


def option_protegee(kind, nom_champ, valeur):
    return valeur in OPTIONS_PROTEGEES.get((kind, nom_champ), set())
