"""Lecture du matériel et des configurations Proxmox : des fonctions pures, sans réseau.

`proxmox.py` interroge l'API ; ce module traduit ce qu'elle rend en champs de la CMDB.
Séparé pour une raison simple : tout ce qui suit est de l'interprétation (une classe PCI
devient une catégorie, une liste de flags un niveau x86-64), et une interprétation se
vérifie sur des exemples, pas en interrogeant un hyperviseur.

Rien ici ne va chercher d'information ailleurs que dans ce que la machine dit d'elle-même :
les caractéristiques commerciales d'un processeur (fréquence turbo, TDP…) se saisissent
dans le référentiel, une fois par modèle.
"""

import json
import re

# --------------------------------------------------------------------------- architecture

# Vocabulaire de Docker (`linux/amd64`…), celui des plateformes d'une image : c'est ce qui
# permet de rapprocher un hôte de ce qu'il peut exécuter. Les clés sont ce que rendent
# `uname -m` (hyperviseur, VM) et la configuration d'un LXC.
_ARCH = {
    "x86_64": "amd64", "amd64": "amd64",
    "aarch64": "arm64", "arm64": "arm64",
    "i386": "386", "i686": "386",
    "armhf": "arm/v7", "armv7l": "arm/v7", "armv7": "arm/v7",
    "riscv64": "riscv64",
}


def arch(valeur):
    return _ARCH.get((valeur or "").strip().lower())


# --------------------------------------------------------------------------- processeur

# Niveaux de la micro-architecture x86-64 (psABI). C'est le résumé qui sert en dépannage :
# une image compilée pour x86-64-v3 (AVX2) ne démarre pas sur un invité exposé en v2, et
# c'est la question qu'on finit par se poser devant un « Illegal instruction ».
_V1 = {"lm", "cmov", "cx8", "fxsr", "mmx", "sse2"}
_V2 = {"cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3"}
_V3 = {"avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave"}
_V4 = {"avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"}


def niveau_x86(flags):
    """`flags` : la ligne de /proc/cpuinfo. None si ce n'est pas un x86-64."""
    f = set(flags.split()) if isinstance(flags, str) else set(flags or ())
    if not _V1 <= f:
        return None
    niveau = 1
    for n, requis in ((2, _V2), (3, _V3), (4, _V4)):
        if not requis <= f:
            break
        niveau = n
    return f"x86-64-v{niveau}"


# Modèle de processeur virtuel QEMU -> niveau exposé à l'invité. `host` et `max` exposent
# celui de l'hyperviseur. Les modèles nommés sont rapprochés de leur génération ; les
# suffixes (`-IBRS`, `-noTSX`, `-v2`…) ne changent pas le niveau.
_NIVEAU_TYPE = {
    "kvm64": 1, "qemu64": 1,
    "x86-64-v2": 2, "x86-64-v2-aes": 2, "x86-64-v3": 3, "x86-64-v4": 4,
    "nehalem": 2, "westmere": 2, "sandybridge": 2, "ivybridge": 2,
    "haswell": 3, "broadwell": 3, "skylake-client": 3,
    "skylake-server": 4, "cascadelake-server": 4, "cooperlake": 4,
    "icelake-client": 4, "icelake-server": 4, "sapphirerapids": 4,
    "epyc-genoa": 4, "epyc": 3,
}


def type_cpu(config):
    """Type de processeur déclaré dans la configuration d'une VM : (texte, modèle).

    Sans option `cpu`, qemu-server expose `kvm64` — c'est ce qui est rendu, avec la mention
    du défaut : un invité qu'on croit en `host` et qui ne l'est pas est une cause classique
    de binaire qui refuse de démarrer.
    """
    brut = (config or {}).get("cpu")
    if not brut:
        return "kvm64 (défaut)", "kvm64"
    premier = str(brut).split(",")[0]
    modele = premier.split("=", 1)[1] if premier.startswith("cputype=") else premier
    return str(brut), modele


def niveau_type(modele, niveau_hote):
    m = (modele or "").strip().lower()
    if m in ("host", "max"):
        return niveau_hote
    if m in _NIVEAU_TYPE:
        return f"x86-64-v{_NIVEAU_TYPE[m]}"
    # Préfixe le plus long : `Skylake-Server-IBRS` est un Skylake-Server.
    candidats = [k for k in _NIVEAU_TYPE if m.startswith(k + "-")]
    if candidats:
        return f"x86-64-v{_NIVEAU_TYPE[max(candidats, key=len)]}"
    return None


_BRUIT_MODELE = re.compile(r"\((?:R|TM|tm|r)\)|\bCPU\b|\bProcessor\b|\b\d+-Core\b|@\s*[\d.]+\s*GHz")
_FREQ = re.compile(r"@\s*([\d.]+)\s*GHz")


def nom_cpu(modele):
    """Nom stable d'un modèle de processeur, tiré de /proc/cpuinfo.

    `Intel(R) Core(TM) i5-8500T CPU @ 2.10GHz` devient `Intel Core i5-8500T` : c'est la clé
    du référentiel, elle doit être la même d'une machine à l'autre et se lire sans effort.
    """
    if not modele:
        return None
    nom = _BRUIT_MODELE.sub(" ", modele)
    return re.sub(r"\s+", " ", nom).strip() or None


def amorce_cpu(cpuinfo, machine):
    """Ce que l'hyperviseur dit de son processeur, pour amorcer une ligne du référentiel.

    Posé une fois, à la création : la ligne appartient ensuite à la saisie. Seule la
    fréquence de base se lit dans le nom (`@ 2.10GHz`) ; la fréquence courante de
    /proc/cpuinfo varie d'une seconde à l'autre et ne dit rien du modèle.
    """
    cpuinfo = cpuinfo or {}
    nom = nom_cpu(cpuinfo.get("model"))
    if not nom or nom.lower() == "unknown":
        return None
    sockets = max(int(cpuinfo.get("sockets") or 1), 1)
    flags = cpuinfo.get("flags") or ""
    champs = {"Name": nom, "Arch": arch(machine)}
    if cpuinfo.get("cores"):
        champs["Cores"] = int(cpuinfo["cores"]) // sockets
    if cpuinfo.get("cpus"):
        champs["Threads"] = int(cpuinfo["cpus"]) // sockets
    bas = nom.lower()
    if "intel" in bas:
        champs["Vendor"] = "Intel"
    elif "amd" in bas:
        champs["Vendor"] = "AMD"
    freq = _FREQ.search(cpuinfo.get("model") or "")
    if freq:
        champs["Base_MHz"] = round(float(freq.group(1)) * 1000)
    champs["x86_level"] = niveau_x86(flags)
    virt = [v for v, f in (("VT-x", "vmx"), ("AMD-V", "svm")) if f in flags.split()]
    if virt:
        champs["Virtualization"] = ", ".join(virt)
    return {k: v for k, v in champs.items() if v is not None}


def version_pve(pveversion):
    """`pve-manager/8.2.4/faa83925c9641325` -> `Proxmox VE 8.2.4`."""
    m = re.match(r"pve-manager/([\w.\-~]+)", pveversion or "")
    return f"Proxmox VE {m.group(1)}" if m else None


def demarrage(boot_info):
    """Mode de démarrage d'un hyperviseur : `UEFI · Secure Boot`, `BIOS`."""
    if not boot_info:
        return None
    mode = {"efi": "UEFI", "legacy-bios": "BIOS"}.get(boot_info.get("mode"), boot_info.get("mode"))
    if not mode:
        return None
    return f"{mode} · Secure Boot" if boot_info.get("secureboot") else mode


def machine_vm(config):
    """Chipset et micrologiciel d'une VM : `q35 · ovmf`, `i440fx · seabios`."""
    config = config or {}
    return f"{config.get('machine') or 'i440fx'} · {config.get('bios') or 'seabios'}"


# --------------------------------------------------------------------------- configuration

# Retirés du texte de configuration : l'empreinte change à chaque écriture, le verrou à
# chaque sauvegarde nocturne (le journal s'en remplirait), et les deux derniers sont des
# secrets cloud-init.
_CONFIG_EXCLUS = {"digest", "lock", "cipassword", "sshkeys"}


def config_texte(config):
    """La configuration Proxmox d'un invité, en texte `clé: valeur` trié.

    Gardée entière plutôt que découpée en champs : réseau, disques, démarrage automatique,
    ballooning, étiquettes, notes Proxmox… Un agent lit tout, et la liste de ce qui compte
    pour une question donnée n'est pas connue d'avance.
    """
    if not config:
        return None
    lignes = []
    for cle in sorted(config):
        if cle in _CONFIG_EXCLUS:
            continue
        valeur = config[cle]
        if cle == "lxc" and isinstance(valeur, list):
            for paire in valeur:
                if isinstance(paire, (list, tuple)) and len(paire) == 2:
                    lignes.append(f"{paire[0]}: {paire[1]}")
            continue
        if isinstance(valeur, (dict, list)):
            valeur = json.dumps(valeur, ensure_ascii=False)
        texte = str(valeur).rstrip()
        lignes.append(f"{cle}: " + texte.replace("\n", "\n  "))
    return "\n".join(lignes) or None


# --------------------------------------------------------------------------- passthrough

def emplacement_pci(ident):
    """`01:00.0`, `0000:01:00`, `0000:01:00.1` -> `0000:01:00` (domaine:bus:périphérique).

    Une carte s'identifie à son emplacement : ses fonctions (les ports d'une carte réseau,
    la sortie audio d'une carte graphique) sont des parties d'elle, pas d'autres cartes.
    """
    ident = (ident or "").strip().lower()
    if not ident:
        return None
    ident = ident.split(".")[0]
    parties = ident.split(":")
    if len(parties) == 2:
        parties = ["0000"] + parties
    if len(parties) != 3:
        return None
    return ":".join(parties)


def passthrough(config):
    """Périphériques attribués à une VM : emplacements PCI, spécifications USB, et
    correspondances (`mapping=`) à résoudre au niveau du cluster."""
    pci, usb, correspondances = set(), set(), []
    for cle, valeur in (config or {}).items():
        texte = str(valeur)
        if re.fullmatch(r"hostpci\d+", cle):
            parties = texte.split(",")
            for p in parties:
                if p.startswith("mapping="):
                    correspondances.append(("pci", p[len("mapping="):]))
            premier = parties[0]
            if premier.startswith("host="):
                premier = premier[len("host="):]
            if "=" in premier:
                continue
            for ident in premier.split(";"):
                e = emplacement_pci(ident)
                if e:
                    pci.add(e)
        elif re.fullmatch(r"usb\d+", cle):
            for p in texte.split(","):
                if p.startswith("host="):
                    usb.add(p[len("host="):].strip().lower())
                elif p.startswith("mapping="):
                    correspondances.append(("usb", p[len("mapping="):]))
    return pci, usb, correspondances


def lire_correspondance(entree):
    """Une ligne `map` d'une correspondance de ressources : `node=pve1,path=…,id=…`."""
    sortie = {}
    for p in str(entree).split(","):
        if "=" in p:
            k, v = p.split("=", 1)
            sortie[k.strip()] = v.strip()
    return sortie


# --------------------------------------------------------------------------- PCI

# Entreprises : ce qui suit le nom de marque sans rien ajouter à l'identification.
_SUFFIXES = re.compile(
    r"[\s,]+(corporation|corp\.?|inc\.?|co\.?,? ?ltd\.?|co\.?|ltd\.?|limited|gmbh|ag|s\.?a\.?|"
    r"technology|technologies|semiconductor|electronics|international|systems|"
    r"communications|microelectronics|computer)$", re.IGNORECASE)
_MARQUES = {
    "advanced micro devices, inc. [amd/ati]": "AMD", "advanced micro devices, inc. [amd]": "AMD",
    "advanced micro devices, inc.": "AMD", "nvidia corporation": "NVIDIA",
    "wdc": "Western Digital", "western digital": "Western Digital", "hgst": "HGST",
    "toshiba": "Toshiba", "samsung": "Samsung", "kingston": "Kingston", "crucial": "Crucial",
    "seagate": "Seagate", "sandisk": "SanDisk", "intel": "Intel",
}


def marque(nom):
    """`Intel Corporation` -> `Intel`, `Realtek Semiconductor Co., Ltd.` -> `Realtek`."""
    nom = (nom or "").strip()
    if not nom:
        return None
    connu = _MARQUES.get(nom.lower())
    if connu:
        return connu
    precedent = None
    while precedent != nom:
        precedent = nom
        nom = _SUFFIXES.sub("", nom).strip()
    return nom or None


def _hex(valeur):
    try:
        return int(str(valeur), 16)
    except (TypeError, ValueError):
        return None


# Constructeurs d'accélérateurs déclarés « périphérique système » plutôt que coprocesseur :
# Coral (Global Unichip) et Hailo. Sans cette liste, la puce qui fait tourner Frigate
# passerait pour un contrôleur sans intérêt.
_ACCELERATEURS = {0x1AC1, 0x1E60}


def categorie_pci(classe, vendeur):
    """(catégorie, toujours) : `toujours` dit si la fonction mérite une ligne même quand
    aucune VM ne la reçoit. Un pont, un contrôleur SMBus ou l'audio intégré ne disent rien
    d'utile ; le contrôleur SATA de la carte mère non plus, sauf le jour où on le passe à
    une VM — Unraid, typiquement."""
    c = _hex(classe)
    if _hex(vendeur) in _ACCELERATEURS:
        return "Accelerator", True
    if c is None:
        return "Other", False
    base, sous = c >> 16, (c >> 8) & 0xFF
    if base == 0x02:
        return ("Wireless" if sous == 0x80 else "Network"), True
    if base == 0x03:
        return "GPU", True
    if base == 0x01:
        if sous in (0x00, 0x04, 0x07):  # SCSI, RAID, SAS : les HBA
            return "Storage controller", True
        if sous == 0x08:  # NVMe : la liste des disques le décrit mieux, sauf s'il est passé
            return "Disk", False
        return "Storage controller", False
    if base == 0x12:
        return "Accelerator", True
    if base == 0x0D:
        return "Wireless", True
    if base == 0x0C and sous == 0x03:
        return "USB controller", False
    return "Other", False


def composants_pci(hote, appareils, passes):
    """Les cartes d'un hyperviseur, une par emplacement, à partir de /hardware/pci.

    `passes` : les emplacements attribués à une VM. Ceux-là sont toujours gardés, quelle
    que soit leur classe.
    """
    par_emplacement = {}
    for a in appareils or []:
        e = emplacement_pci(a.get("id"))
        if e:
            par_emplacement.setdefault(e, []).append(a)

    sortie = []
    for e, fonctions in sorted(par_emplacement.items()):
        fonctions.sort(key=lambda a: a.get("id") or "")
        categories = [categorie_pci(a.get("class"), a.get("vendor")) for a in fonctions]
        retenues = [i for i, (_, toujours) in enumerate(categories) if toujours]
        if not retenues and e not in passes:
            continue
        principale = fonctions[retenues[0] if retenues else 0]
        categorie = categories[retenues[0] if retenues else 0][0]
        fabricant = marque(principale.get("vendor_name")) or principale.get("vendor")
        puce = principale.get("device_name") or principale.get("device")
        sous_fab = marque(principale.get("subsystem_vendor_name"))
        sous_modele = principale.get("subsystem_device_name")
        if sous_modele and re.fullmatch(r"(device|subsystem)\s+[0-9a-fx]+", sous_modele, re.I):
            sous_modele = None
        groupe = principale.get("iommugroup")
        amorce = {
            "Name": " ".join(x for x in (fabricant, puce) if x) or e,
            "Category": categorie,
            "Vendor": sous_fab or fabricant,
            "Model": sous_modele or puce,
        }
        if categorie == "Network":
            ports = sum(1 for a in fonctions if (_hex(a.get("class")) or 0) >> 16 == 0x02)
            if ports > 1:
                amorce["Ports"] = ports
        sortie.append({
            "uid": f"{hote}/pci/{e}",
            "famille": "pci",
            "slot": e,
            "fields": {
                "Bus": "NVMe" if categorie == "Disk" else "PCIe",
                "Chipset": " ".join(x for x in (fabricant, puce) if x) or None,
                "Address": e,
                "IOMMU_group": groupe if isinstance(groupe, int) and groupe >= 0 else None,
            },
            "seed": amorce,
        })
    return sortie


# --------------------------------------------------------------------------- disques

_BUS_DISQUE = {"nvme": "NVMe", "usb": "USB", "ssd": "SATA", "hdd": "SATA"}


def _marque_disque(vendeur, modele):
    vendeur = (vendeur or "").strip()
    if vendeur and vendeur.upper() not in ("ATA", "NVME", "USB"):
        return marque(vendeur)
    premier = (modele or "").split(" ")[0].split("_")[0]
    if premier.upper().startswith("ST") and premier[2:3].isdigit():
        return "Seagate"
    if premier.upper().startswith("CT") and premier[2:3].isdigit():
        return "Crucial"
    return marque(premier) if premier else None


def composants_disques(hote, disques):
    """Les disques vus par l'hyperviseur (/disks/list).

    La clé est le numéro de série quand il existe : un disque change parfois de machine, et
    c'est le même disque. Ceux qu'une VM reçoit derrière un contrôleur passé en entier
    (le HBA d'Unraid) n'apparaissent pas ici — l'hyperviseur ne les voit plus.
    """
    sortie = []
    for d in disques or []:
        devpath = d.get("devpath")
        if not devpath:
            continue
        serie = (d.get("serial") or "").strip()
        if serie.lower() in ("", "unknown", "none"):
            serie = None
        modele = (d.get("model") or "").strip() or None
        usure = d.get("wearout")
        try:
            usure = int(usure)
        except (TypeError, ValueError):
            usure = None
        taille = d.get("size")
        sortie.append({
            "uid": f"disk/{serie}" if serie else f"{hote}/disk/{devpath}",
            "famille": "disks",
            "fields": {
                "Bus": _BUS_DISQUE.get((d.get("type") or "").lower(), "Other"),
                "Address": devpath,
                "Serial": serie,
                # En gigaoctets décimaux : ceux de l'étiquette (un « 1 To » fait 1000 Go).
                "Capacity_Gb": round(taille / 1e9) if isinstance(taille, (int, float)) else None,
                "Health": d.get("health") or None,
                "Wearout": usure,
                "Usage": d.get("used") or None,
            },
            "seed": {
                "Name": modele or devpath,
                "Category": "Disk",
                "Vendor": _marque_disque(d.get("vendor"), modele),
                "Model": modele,
            },
        })
    return sortie


# --------------------------------------------------------------------------- USB

def _id_usb(valeur):
    texte = str(valeur or "").lower().removeprefix("0x")
    return texte.zfill(4) if texte else None


def composants_usb(hote, appareils):
    """Les périphériques USB branchés sur l'hyperviseur (/hardware/usb), concentrateurs
    exclus. La clé Zigbee, le Coral USB, l'onduleur : c'est ici qu'ils apparaissent."""
    sortie = []
    for a in appareils or []:
        vid, pid = _id_usb(a.get("vendid")), _id_usb(a.get("prodid"))
        if not vid or not pid or vid == "1d6b":  # concentrateurs racine du noyau
            continue
        if str(a.get("class") or "").lower().removeprefix("0x").lstrip("0") == "9":
            continue
        chemin = f"{a.get('busnum')}-{a.get('usbpath')}" if a.get("usbpath") else None
        serie = (a.get("serial") or "").strip() or None
        produit = (a.get("product") or "").strip() or None
        sortie.append({
            "uid": f"{hote}/usb/{vid}:{pid}/{serie or chemin or a.get('devnum')}",
            "famille": "usb",
            "usb": {f"{vid}:{pid}", chemin} - {None},
            "fields": {
                "Bus": "USB",
                "Chipset": f"{vid}:{pid}",
                "Address": chemin,
                "Serial": serie,
            },
            "seed": {
                "Name": produit or f"USB {vid}:{pid}",
                "Category": "USB device",
                "Vendor": marque(a.get("manufacturer")),
                "Model": produit,
            },
        })
    return sortie


def famille_uid(uid):
    if uid.startswith("disk/") or "/disk/" in uid:
        return "disks"
    if "/pci/" in uid:
        return "pci"
    if "/usb/" in uid:
        return "usb"
    return None
