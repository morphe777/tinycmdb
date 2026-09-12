"""Source Proxmox VE -> table Node (+ IP/MAC exposés à main.py pour la passe IPAM).

Ne fait qu'une chose : interroger l'API Proxmox et renvoyer des dicts normalisés.
Aucun appel Baserow ici (l'orchestration et la réconciliation vivent dans main.py
et baserow.py) : ajouter une source ne doit jamais dupliquer cette logique.
"""

import logging
import socket

import requests

logger = logging.getLogger("cmdb.proxmox")

_GUEST_TYPE_MAP = {"qemu": "VM", "lxc": "LXC"}


def collect(cfg):
    """Renvoie une liste de dicts : {"name", "type", "parent_name", "fields", "ip", "mac"}.

    `fields` ne contient que ce que Proxmox possède réellement (règle 3) : jamais
    `criticality` (manuel). `os`, `guest_agent` et `backup` en font désormais partie (choix
    explicite : la valeur "manuel" du prompt d'origine était jugée excessive pour ces trois
    champs, l'objectif étant justement d'avoir une vue automatique de l'OS/agent/couverture
    backup en place — pas quelque chose que l'utilisateur va saisir à la main).

    `ip`/`mac` ne sont pas des champs Node (pas de colonne pour ça) : ce sont des données
    de passage pour que main.py alimente la table IPAM séparément.
    """
    session = requests.Session()
    session.headers.update({"Authorization": f"PVEAPIToken={cfg.PROXMOX_TOKEN}"})
    session.verify = cfg.PROXMOX_VERIFY_TLS

    resp = session.get(f"{cfg.PROXMOX_URL}/api2/json/nodes", timeout=15)
    resp.raise_for_status()
    hypervisors = resp.json()["data"]

    resp = session.get(f"{cfg.PROXMOX_URL}/api2/json/cluster/resources", params={"type": "vm"}, timeout=15)
    resp.raise_for_status()
    guests = resp.json()["data"]

    if not hypervisors and not guests:
        logger.warning(
            "Proxmox a répondu 200 mais sans aucune donnée (ni nœud, ni VM/LXC). Cause la plus "
            "fréquente : le token API a été créé sans --privsep 0. Avec la séparation de "
            "privilèges activée, le token n'hérite d'aucun droit et l'API renvoie une liste "
            "vide sans erreur explicite. Voir README.md."
        )

    backup_coverage = _load_backup_coverage(session, cfg)

    nodes = []

    for hv in hypervisors:
        nodes.append({
            "name": hv["node"],
            "type": "Physical",
            "parent_name": None,
            "fields": {
                "Type": "Physical",
                "Status": "Running" if hv.get("status") == "online" else "Stopped",
                "vCPU": hv.get("maxcpu"),
                "RAM_Gb": _bytes_to_gb(hv.get("maxmem")),
                "Disk_Gb": _bytes_to_gb(hv.get("maxdisk")),
                "Roles": ["Hypervisor"],
            },
            "ip": None,
            "mac": None,
            "fqdn_hint": None,
            "ip_type": None,
        })

    for g in guests:
        guest_type = _GUEST_TYPE_MAP.get(g.get("type"))
        if not guest_type:
            continue

        node, vmid = g.get("node"), g.get("vmid")
        status = "running" if g.get("status") == "running" else "stopped"

        if guest_type == "LXC":
            probe = _probe_lxc(session, cfg, node, vmid)
        else:
            probe = _probe_vm(session, cfg, node, vmid, running=(status == "running"))

        fields = {
            "Type": guest_type,
            "vmid": vmid,
            "Status": "Running" if status == "running" else "Stopped",
            "vCPU": g.get("maxcpu"),
            "RAM_Gb": _bytes_to_gb(g.get("maxmem")),
            "Disk_Gb": _bytes_to_gb(g.get("maxdisk")),
        }
        if probe.get("os"):
            fields["OS"] = probe["os"]
        if guest_type == "VM" and probe.get("guest_agent") is not None:
            fields["Guest_agent"] = probe["guest_agent"]

        is_backed_up = _is_backed_up(vmid, backup_coverage)
        if is_backed_up is not None:
            fields["Backup"] = is_backed_up

        name = g.get("name") or f"vmid-{vmid}"
        fqdn = None
        if not probe.get("ip"):
            # repli : la plupart du temps le nom Proxmox == le nom DNS. Moins fiable qu'une
            # lecture directe (LXC interfaces / Guest Agent), donc utilisé en dernier recours.
            dns_ip, fqdn = _resolve_dns(name, cfg.DNS_SUFFIX)
            if dns_ip:
                probe["ip"] = dns_ip
                logger.debug("%s : IP trouvée par repli DNS (%s)", name, fqdn)

        nodes.append({
            "name": name,
            "type": guest_type,
            "parent_name": node,
            "fields": fields,
            "ip": probe.get("ip"),
            "mac": probe.get("mac"),
            "fqdn_hint": fqdn,
            "ip_type": probe.get("ip_type"),
        })

    return nodes


def _load_backup_coverage(session, cfg):
    """Jobs vzdump actifs (/cluster/backup) : la liste est réputée complète, donc un vmid
    absent de tous les jobs veut dire "non sauvegardé" — une information positive, pas une
    inconnue (contrairement à `os` sans agent). Renvoie None si l'appel échoue (on ne sait
    vraiment rien, mieux vaut ne pas toucher au champ plutôt que d'écrire un faux `false`)."""
    try:
        resp = session.get(f"{cfg.PROXMOX_URL}/api2/json/cluster/backup", timeout=10)
        resp.raise_for_status()
        jobs = resp.json().get("data", [])
    except requests.RequestException:
        logger.warning("Jobs de backup Proxmox indisponibles, champ 'backup' non touché ce passage")
        return None

    covers_all = False
    included, excluded = set(), set()
    for job in jobs:
        if str(job.get("enabled", "1")) not in ("1", "true", "True"):
            continue
        if str(job.get("all", "0")) == "1":
            covers_all = True
            excluded |= _parse_vmid_list(job.get("exclude"))
        else:
            included |= _parse_vmid_list(job.get("vmid"))

    return {"covers_all": covers_all, "included": included, "excluded": excluded}


def _parse_vmid_list(raw):
    if not raw:
        return set()
    return {int(x) for x in str(raw).split(",") if x.strip().isdigit()}


def _is_backed_up(vmid, coverage):
    if coverage is None:
        return None
    if vmid in coverage["excluded"]:
        return False
    if vmid in coverage["included"]:
        return True
    return coverage["covers_all"]


def _probe_lxc(session, cfg, node, vmid):
    """IP/MAC en live via /lxc/{vmid}/interfaces (marche même en DHCP, pas d'agent requis) ;
    OS via `ostype`, un champ de config Proxmox natif, toujours disponible. `ip_type` vient
    de la déclaration `net0` elle-même : ip=<CIDR> -> static, ip=dhcp -> dhcp-reservation
    (convention de l'infra : le DHCP y est toujours une réservation par MAC, jamais un pool)."""
    result = {"os": None, "ip": None, "mac": None, "ip_type": None}

    try:
        resp = session.get(f"{cfg.PROXMOX_URL}/api2/json/nodes/{node}/lxc/{vmid}/config", timeout=10)
        resp.raise_for_status()
        cfg_data = resp.json().get("data", {})
        result["os"] = cfg_data.get("ostype")
        result["ip_type"] = _net0_ip_type(cfg_data.get("net0"))
    except requests.RequestException:
        logger.debug("Config LXC indisponible pour vmid %s", vmid)

    try:
        resp = session.get(f"{cfg.PROXMOX_URL}/api2/json/nodes/{node}/lxc/{vmid}/interfaces", timeout=10)
        resp.raise_for_status()
        for iface in resp.json().get("data", []):
            if iface.get("name") == "lo":
                continue
            ip = _first_ipv4(iface.get("ip-addresses"))
            if ip:
                result["ip"] = ip
                result["mac"] = iface.get("hardware-address")
                break
    except requests.RequestException:
        logger.debug("Interfaces LXC indisponibles pour vmid %s", vmid)

    return result


def _probe_vm(session, cfg, node, vmid, running):
    """MAC toujours dispo (config, pas d'agent). OS et IP nécessitent le QEMU Guest Agent,
    et seulement si la VM tourne — sinon l'appel échoue de toute façon, ce qui ne dirait
    rien sur la présence réelle de l'agent (`guest_agent` resterait donc inconnu, pas False).
    """
    result = {"os": None, "ip": None, "mac": None, "guest_agent": None, "ip_type": None}

    try:
        resp = session.get(f"{cfg.PROXMOX_URL}/api2/json/nodes/{node}/qemu/{vmid}/config", timeout=10)
        resp.raise_for_status()
        result["mac"] = _parse_net0_mac(resp.json().get("data", {}).get("net0"))
    except requests.RequestException:
        logger.debug("Config VM indisponible pour vmid %s", vmid)

    if not running:
        return result

    try:
        resp = session.get(
            f"{cfg.PROXMOX_URL}/api2/json/nodes/{node}/qemu/{vmid}/agent/get-osinfo", timeout=10
        )
        resp.raise_for_status()
        osinfo = resp.json().get("data", {}).get("result", {})
        result["os"] = osinfo.get("pretty-name") or osinfo.get("name")
        result["guest_agent"] = True
    except requests.RequestException:
        result["guest_agent"] = False
        return result

    try:
        resp = session.get(
            f"{cfg.PROXMOX_URL}/api2/json/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces", timeout=10
        )
        resp.raise_for_status()
        for iface in resp.json().get("data", {}).get("result", []):
            if iface.get("name") in ("lo", "lo0"):
                continue
            ip = _first_ipv4(iface.get("ip-addresses"))
            if ip:
                result["ip"] = ip
                # Proxmox ne déclare jamais l'IP d'une VM (contrairement au net0 LXC) : on ne
                # sait que ce que l'agent rapporte à l'instant T, jamais si c'est "static" côté
                # invité. Convention de l'infra : toujours une réservation DHCP par MAC.
                result["ip_type"] = "DHCP-reservation"
                break
    except requests.RequestException:
        logger.debug("network-get-interfaces indisponible pour vmid %s", vmid)

    return result


def _resolve_dns(name, dns_suffix):
    """Repli quand rien n'a été trouvé côté Proxmox : le nom Proxmox est souvent identique
    au nom DNS. Moins fiable qu'une lecture directe (pas de garantie que ce soit la même
    machine), donc seulement utilisé en dernier recours. Renvoie (ip, fqdn) ou (None, None)."""
    fqdn = f"{name}{dns_suffix}"
    try:
        return socket.gethostbyname(fqdn), fqdn
    except OSError:
        return None, None


def _net0_ip_type(net0):
    """LXC uniquement : net0 déclare explicitement ip=dhcp ou ip=<CIDR>."""
    if not net0:
        return None
    for part in net0.split(","):
        if part.startswith("ip="):
            return "DHCP-reservation" if part[3:] == "dhcp" else "Static"
    return None


def _first_ipv4(ip_addresses):
    """Première IPv4 non link-local d'une liste renvoyée par Proxmox. Convention différente
    selon l'endpoint : "inet" pour /lxc/{vmid}/interfaces, "ipv4" pour le Guest Agent QEMU
    (network-get-interfaces) — les deux doivent être acceptés."""
    for entry in ip_addresses or []:
        if entry.get("ip-address-type") not in ("inet", "ipv4"):
            continue
        ip = entry.get("ip-address")
        if ip and not ip.startswith("169.254."):
            return ip
    return None


def _parse_net0_mac(net0):
    if not net0:
        return None
    for part in net0.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key in ("virtio", "e1000", "vmxnet3", "rtl8139"):
            return value.split(",")[0]
    return None


def _bytes_to_gb(value):
    """Le champ Baserow ram_gb/disk_gb est configuré sans décimale : arrondi à l'entier."""
    if value is None:
        return None
    return round(value / (1024 ** 3))
