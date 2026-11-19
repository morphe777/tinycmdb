"""Le modèle de données : huit tables, et la forme de chacun de leurs champs.

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
        # Matériel et configuration (migration 002). Les premiers sont relevés sur
        # l'hyperviseur ou dans la configuration Proxmox d'un invité ; carte mère, BIOS et
        # mémoire ne sont visibles d'aucune API sans droits root, ils se saisissent.
        Champ("Arch", "select", "arch"),
        Champ("CPU", "lien", "cpu_id", "cpu"),
        Champ("Sockets", "entier", "sockets"),
        Champ("CPU_type", "texte", "cpu_type"),
        Champ("CPU_level", "select", "cpu_level"),
        Champ("Machine", "texte", "machine"),
        Champ("Kernel", "texte", "kernel"),
        Champ("Motherboard", "texte", "motherboard"),
        Champ("BIOS", "texte", "bios"),
        Champ("RAM_type", "select", "ram_type"),
        Champ("RAM_ECC", "select", "ram_ecc"),
        Champ("RAM_MHz", "entier", "ram_mhz"),
        Champ("RAM_modules", "texte", "ram_modules"),
        Champ("RAM_max_Gb", "entier", "ram_max_gb"),
        Champ("Config", "long", "config"),
        Champ("Application", "liens", None, "application",
              ("node_application", "node_id", "application_id")),
        Champ("Container", "inverse", None, "container", ("container", "Host")),
        Champ("IPAM", "inverse", None, "ipam", ("ipam", "Node")),
        Champ("Components", "inverse", None, "composant", ("composant", "Host")),
        Champ("Devices", "inverse", None, "composant", ("composant", "Assigned_to")),
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
    # Référentiel des modèles de processeur. Un modèle a des caractéristiques fixes : elles
    # se saisissent une fois, à la main ou par un agent, et valent pour toutes les machines
    # qui le portent. Le collecteur crée la ligne quand il rencontre un modèle inconnu, avec
    # ce que la machine dit d'elle-même, puis n'y revient jamais.
    "cpu": _t("cpu", "cpu", "Processeurs", "processeur", "Name", [
        Champ("Name", "texte", "name"),
        Champ("Vendor", "select", "vendor"),
        Champ("Arch", "select", "arch"),
        Champ("Generation", "texte", "generation"),
        Champ("Cores", "entier", "cores"),
        Champ("Threads", "entier", "threads"),
        Champ("Base_MHz", "entier", "base_mhz"),
        Champ("Boost_MHz", "entier", "boost_mhz"),
        Champ("TDP_W", "entier", "tdp_w"),
        Champ("L3_MB", "entier", "l3_mb"),
        Champ("x86_level", "select", "x86_level"),
        Champ("iGPU", "texte", "igpu"),
        Champ("Virtualization", "texte", "virtualization"),
        Champ("Max_RAM_Gb", "entier", "max_ram_gb"),
        Champ("Doc", "url", "doc"),
        Champ("Notes", "long", "notes"),
        Champ("Nodes", "inverse", None, "node", ("node", "CPU")),
    ]),
    # Cartes PCIe, disques, périphériques USB. Découverts sur les hyperviseurs Proxmox
    # (Source = Auto) ou saisis à la main pour le reste du parc (Source = Manual).
    "composant": _t("composant", "composant", "Composants", "composant", "UID", [
        Champ("UID", "texte", "uid"),
        Champ("Name", "texte", "name"),
        Champ("Category", "select", "category"),
        Champ("Bus", "select", "bus"),
        Champ("Host", "lien", "host_id", "node"),
        Champ("Assigned_to", "lien", "assigned_id", "node"),
        Champ("Vendor", "texte", "vendor"),
        Champ("Model", "texte", "model"),
        Champ("Reference", "texte", "reference"),
        Champ("Chipset", "texte", "chipset"),
        Champ("Address", "texte", "address"),
        Champ("IOMMU_group", "entier", "iommu_group"),
        Champ("Serial", "texte", "serial"),
        Champ("Ports", "entier", "ports"),
        Champ("Speed_Mbps", "entier", "speed_mbps"),
        Champ("Capacity_Gb", "entier", "capacity_gb"),
        Champ("Standard", "select", "standard"),
        Champ("Usage", "texte", "usage"),
        Champ("Health", "texte", "health"),
        Champ("Wearout", "entier", "wearout"),
        Champ("Firmware", "texte", "firmware"),
        Champ("Doc", "url", "doc"),
        Champ("Source", "select", "source"),
        Champ("Last_seen", "horodatage", "last_seen"),
        Champ("Notes", "long", "notes"),
    ]),
}

# Ordre d'écriture et de lecture : une table avant celles qui la référencent.
ORDRE = ("vlan", "application", "cpu", "node", "image", "container", "ipam", "composant")


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
    ("node", "Arch"): {"amd64", "arm64", "arm/v7", "386", "riscv64"},
    ("node", "CPU_level"): {"x86-64-v1", "x86-64-v2", "x86-64-v3", "x86-64-v4"},
    ("cpu", "Vendor"): {"Intel", "AMD"},
    ("cpu", "Arch"): {"amd64", "arm64", "arm/v7", "386", "riscv64"},
    ("cpu", "x86_level"): {"x86-64-v1", "x86-64-v2", "x86-64-v3", "x86-64-v4"},
    ("composant", "Source"): {"Auto", "Manual"},
    ("composant", "Category"): {"Network", "Wireless", "Disk", "Storage controller", "GPU",
                                "Accelerator", "USB controller", "USB device", "Other"},
    ("composant", "Bus"): {"PCIe", "NVMe", "SATA", "USB", "Other"},
}


def option_protegee(kind, nom_champ, valeur):
    return valeur in OPTIONS_PROTEGEES.get((kind, nom_champ), set())
