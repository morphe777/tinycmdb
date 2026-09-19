"""Ordonnancement : deux passes à des périodes distinctes, dans une boucle du
programme (pas de cron dans le conteneur, pas de `docker run --rm` depuis l'hôte).
"""

import ipaddress
import logging
import socket
import time
from datetime import datetime, timezone

import signaux
import version

from . import config
from . import netcheck
from . import oui
from .baserow import BaserowClient
from .sources import cup as cup_source
from .sources import docker as docker_source
from .sources import proxmox as proxmox_source
from .sources import trivy as trivy_source

logger = logging.getLogger("cmdb.main")


def _drop_none(fields):
    return {k: v for k, v in fields.items() if v is not None}


def _multiselect_values(row, field):
    """Un multiple_select revient de Baserow comme une liste d'objets {id, value, color} —
    extrait juste les valeurs, casse d'origine préservée (nécessaire pour la fusion/écriture)."""
    return {opt.get("value") for opt in (row.get(field) or [])}


def _has_role(row, role_name):
    """Comparaison insensible à la casse : le libellé d'une option Baserow est éditable
    (ex. 'hypervisor' renommé en 'Hypervisor'), la détection d'un rôle ne doit pas en dépendre —
    contrairement à _multiselect_values, dont le résultat sert à réécrire `Roles` et doit donc
    garder la casse exacte."""
    target = role_name.lower()
    return any((v or "").lower() == target for v in _multiselect_values(row, "Roles"))


def run_node_pass(client, cfg):
    """Proxmox -> Node. Toujours appelée : même sans Proxmox, on a besoin du cache
    Node à jour pour résoudre les hôtes de la passe Docker.

    Renvoie (node_cache, guest_ip_data) : le second sert à la passe IPAM juste après
    (IP/MAC ne sont pas des champs Node, il n'y a nulle part où les garder sinon).
    """
    node_cache = client.build_cache(cfg.TABLE_NODE, "Name")
    guest_ip_data = {}
    if not cfg.PROXMOX_URL:
        return node_cache, guest_ip_data

    try:
        nodes = proxmox_source.collect(cfg)
    except Exception:
        # une source en échec n'arrête pas le collecteur (règle 8), mais ne bloque plus non
        # plus la suppression : `seen` reste vide, l'âge s'accumule normalement sur chaque
        # ligne. Une panne Proxmox de quelques minutes ne fait rien (délai de grâce) ; une
        # panne qui dure vraiment finit par être traitée comme une disparition, assumé.
        logger.exception("Passe Proxmox en échec, on continue sans elle")
        nodes = []

    seen = set()
    # hyperviseurs d'abord : parent_host doit être résolvable pour les invités (règle 2)
    hypervisors = [n for n in nodes if n["type"] == "Physical"]
    guests = [n for n in nodes if n["type"] != "Physical"]

    for n in hypervisors + guests:
        try:
            fields = _drop_none(dict(n["fields"]))
            parent_name = n["parent_name"]
            if parent_name:
                # insensible à la casse : un hyperviseur peut avoir été renommé à la main
                # dans Baserow ("biggy" -> "Biggy") sans que le nom réel côté Proxmox (le
                # hostname Linux, non modifiable) ne suive.
                parent_match = _ci_node_index(node_cache).get(parent_name.lower())
                parent_row = parent_match[1] if parent_match else None
                fields["Parent_host"] = [parent_row["id"]] if parent_row else []
                if not parent_row:
                    logger.warning("%s : hyperviseur parent '%s' introuvable dans Node", n["name"], parent_name)

            # résout le nom déjà stocké (insensible à la casse) pour ne pas dupliquer une
            # ligne simplement renommée à la main plutôt que de mettre à jour la bonne.
            self_match = _ci_node_index(node_cache).get(n["name"].lower())
            canonical_name, existing_row = self_match if self_match else (n["name"], None)

            if n["type"] == "Physical":
                # Proxmox ne possède que l'option "Hypervisor" dans `Roles` : les autres tags
                # (Docker, Database, ...) sont posés à la main dans Baserow. `Roles` étant un
                # multiple_select, Baserow remplace la liste entière au PATCH — il faut donc
                # fusionner avec l'existant plutôt qu'écraser, sous peine d'effacer un tag manuel.
                # Le libellé exact de l'option doit matcher Baserow ("Hypervisor", H majuscule).
                existing_roles = _multiselect_values(existing_row, "Roles") if existing_row else set()
                fields["Roles"] = sorted(existing_roles | {"Hypervisor"})

            client.upsert(
                node_cache, cfg.TABLE_NODE, "Name", canonical_name, fields,
                manual_gate_field="Source", last_seen_field="Last_seen",
            )
            seen.add(canonical_name)

            if n.get("ip"):
                guest_ip_data[canonical_name] = {
                    "ip": n["ip"],
                    "mac": n.get("mac"),
                    "fqdn_hint": n.get("fqdn_hint"),
                    "ip_type": n.get("ip_type"),
                }
        except Exception:
            # une ligne en échec (ex. champ Baserow manquant) ne doit pas bloquer les autres
            logger.exception("Écriture Node '%s' en échec, passage au suivant", n["name"])

    client.delete_missing(
        node_cache, cfg.TABLE_NODE, seen, cfg.RETIRE_GRACE_HOURS,
        last_seen_field="Last_seen", manual_gate_field="Source",
    )
    return node_cache, guest_ip_data


def _load_vlan_networks(client, table_vlan):
    """Lecture seule : les VLAN sont une référence déjà maintenue à la main dans Baserow."""
    networks = []
    for row in client.fetch_all(table_vlan):
        subnet = row.get("subnet")
        if not subnet:
            continue
        try:
            networks.append((ipaddress.ip_network(subnet, strict=False), row["id"]))
        except ValueError:
            logger.warning("VLAN '%s' : subnet illisible ('%s')", row.get("name"), subnet)
    return networks


def _match_vlan(ip, networks):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for network, row_id in networks:
        if addr in network:
            return row_id
    return None


def _resolve_fqdn(ip, hint):
    """`hint` vient du repli DNS (nom -> IP) déjà fait côté Proxmox : pas besoin de reposer
    la question. Sinon, tentative de résolution inverse (PTR) sur l'IP. Toujours renvoyé en
    minuscules par convention (peu importe la casse du nom Proxmox ou du PTR)."""
    if hint:
        return hint.lower()
    try:
        return socket.gethostbyaddr(ip)[0].lower()
    except OSError:
        return None


# Les noms de champs IPAM sont ceux affichés dans Baserow, pas une convention à nous : ils ont
# déjà changé une fois (tout est passé en casse capitalisée). Un futur renommage de colonne
# (pas juste d'option) cassera ces écritures silencieusement — Baserow ignore un champ inconnu
# plutôt que de renvoyer une erreur.
def run_ipam_pass(client, cfg, node_cache, guest_ip_data):
    """Proxmox (IP/MAC) -> IPAM. Toujours après Node (règle 2 : `IPAM.Node` doit pouvoir
    résoudre vers une ligne Node déjà en cache). Ne s'exécute qu'aux passages Proxmox : pas
    de sens à la reconstruire à chaque passe conteneurs, l'IP ne vient que de là.

    Tourne même si `guest_ip_data` est vide (ex. panne Proxmox) : la suppression de ce qui
    n'est plus vu doit quand même s'évaluer, sinon une panne prolongée ne serait jamais
    traitée comme une disparition (voir run_node_pass)."""
    if not cfg.TABLE_IPAM:
        return

    networks = _load_vlan_networks(client, cfg.TABLE_VLAN)
    ipam_cache = client.build_cache(cfg.TABLE_IPAM, "Address")
    seen_addresses = set()

    for name, data in guest_ip_data.items():
        ip = data["ip"]
        node_row = node_cache.get(name)
        vlan_id = _match_vlan(ip, networks)
        if vlan_id is None:
            logger.warning(
                "IPAM %s (%s) : aucun VLAN correspondant parmi les %d subnets connus, ligne créée sans vlan",
                ip, name, len(networks),
            )

        if data.get("ip_type"):
            # confirmation directe par Proxmox (agent/LXC/net0) : le signal le plus fort possible
            status = "Used"
        elif netcheck.is_alive(ip):
            # pas de confirmation Proxmox, mais l'IP répond au ping : probablement toujours là,
            # sans garantie totale (un hôte peut bloquer l'ICMP sans être inutilisé)
            status = "Reserved"
        else:
            # ni Proxmox ni ping : dérive possible dans le temps (le nom DNS existe encore mais
            # plus personne ne répond), à vérifier à la main plutôt que de trancher à sa place
            status = "To check"

        fields = _drop_none({
            "Node": [node_row["id"]] if node_row else [],
            "MAC": data.get("mac"),
            "Hostname": name,
            "VLAN": [vlan_id] if vlan_id else [],
            "FQDN": _resolve_fqdn(ip, data.get("fqdn_hint")),
            # "Free" reste toujours manuel : le collecteur ne scanne jamais une plage entière,
            # il ne voit que ce qui est assigné à un guest qu'il connaît déjà.
            "Status": status,
            "Type": data.get("ip_type"),
            # forme entière de l'adresse : tri/filtrage numérique correct dans Baserow
            # ("192.168.10.9" < "192.168.10.10" trie mal en texte)
            "IP_int": int(ipaddress.ip_address(ip)),
            "Vendor": oui.lookup(data.get("mac")),
        })
        try:
            client.upsert(
                ipam_cache, cfg.TABLE_IPAM, "Address", ip, fields,
                manual_gate_field="Source", auto_label="Auto", last_seen_field="Last_seen",
            )
            seen_addresses.add(ip)
        except Exception:
            logger.exception("Écriture IPAM '%s' (%s) en échec, IP suivante", ip, name)

    client.delete_missing(
        ipam_cache, cfg.TABLE_IPAM, seen_addresses, cfg.RETIRE_GRACE_HOURS,
        last_seen_field="Last_seen", manual_gate_field="Source",
    )

    _enrich_manual_ipam(client, cfg, networks)


def _enrich_manual_ipam(client, cfg, networks):
    """Lignes IPAM `Source = Manual` : recalcule VLAN/Vendor/IP_int/FQDN/Hostname/Last_seen
    à partir de la seule Address/MAC déjà saisies à la main. Ce sont des faits déductibles
    de l'adresse elle-même (VLAN par subnet, Vendor par OUI...), pas des corrections
    métier — contrairement à Type/Status/Notes, jamais touchés ici.

    Ne dépend d'aucune source (Proxmox/Docker) : une IP sur un Node Physical, par exemple,
    n'est jamais détectée par ailleurs (aucun code ne sonde l'IP d'un serveur physique),
    donc sans cette passe une ligne manuelle resterait figée pour toujours après création.
    """
    for row in client.fetch_all(cfg.TABLE_IPAM):
        source = client._select_value(row, "Source")
        if (source or "").lower() != "manual":
            continue
        address = row.get("Address")
        if not address:
            continue

        vlan_id = _match_vlan(address, networks)
        if vlan_id is None:
            logger.warning(
                "IPAM (manuel) %s : aucun VLAN correspondant parmi les %d subnets connus",
                address, len(networks),
            )

        fields = _drop_none({
            "VLAN": [vlan_id] if vlan_id else [],
            "Vendor": oui.lookup(row.get("MAC")),
            "IP_int": int(ipaddress.ip_address(address)),
            "FQDN": _resolve_fqdn(address, None),
        })

        # DNS d'abord : fiable et à jour dans cet environnement. Le nom du Node lié ne sert
        # qu'en dernier recours (si le PTR n'existe pas pour cette adresse).
        try:
            fields["Hostname"] = socket.gethostbyaddr(address)[0].split(".")[0]
        except OSError:
            node_links = row.get("Node") or []
            if node_links:
                fields["Hostname"] = node_links[0]["value"]

        fields["Last_seen"] = datetime.now(timezone.utc).isoformat()

        try:
            client.update_row(cfg.TABLE_IPAM, row["id"], fields)
            logger.debug("~ IPAM (manuel) enrichi : %s", address)
        except Exception:
            logger.exception("Enrichissement IPAM manuel '%s' en échec", address)


def _ci_node_index(node_cache):
    """Index insensible à la casse : DNS ne différencie pas 'myTools' de 'mytools', la
    comparaison de noms d'hôtes ne devrait donc pas en dépendre non plus."""
    return {name.lower(): (name, row) for name, row in node_cache.items()}


def _resolve_docker_targets(cfg, node_cache):
    """Liste des hôtes Docker à scanner : DOCKER_HOSTS (explicite, prioritaire) fusionné
    avec tout Node dont `roles` contient "docker" (tag posé à la main dans Baserow).

    Convention d'URL pour les hôtes découverts : http://{Node.name}{SUFFIX}:{PORT}. Un hôte
    hors Proxmox (pas de Node correspondant) doit passer par DOCKER_HOSTS, seule façon de lui
    donner une URL.
    """
    targets = dict(cfg.DOCKER_HOSTS)
    seen_lower = {name.lower() for name in targets}
    for name, row in node_cache.items():
        if name.lower() in seen_lower:
            continue
        if _has_role(row, "docker"):
            targets[name] = f"http://{name}{cfg.DNS_SUFFIX}:{cfg.DOCKER_SOCKET_PROXY_PORT}"
    return targets


def run_container_pass(client, cfg, node_cache):
    """Docker -> Images, Container (règle 2). `Application` n'est plus touchée : 100%
    manuelle, un projet Compose est un fait sur le conteneur (`Container.Stack`), pas une
    Application au sens métier.

    Renvoie `cup_targets` ({image_reference: {"mode", "reference", "host_url"}}) pour
    run_cup_pass juste après : c'est ici qu'on sait, pour chaque image, sur quel hôte (donc
    quel socket-proxy) elle tourne — inutile de le redemander à part.
    """
    cup_targets = {}
    docker_targets = _resolve_docker_targets(cfg, node_cache)
    if not docker_targets:
        return cup_targets

    image_cache = client.build_cache(cfg.TABLE_IMAGES, "Reference")
    container_cache = client.build_cache(cfg.TABLE_CONTAINER, "UID")

    ci_nodes = _ci_node_index(node_cache)
    seen_containers = set()
    seen_images = set()
    unresolved_hosts = set()

    for host_name, host_url in docker_targets.items():
        match = ci_nodes.get(host_name.lower())
        canonical_name, host_row = match if match else (host_name, None)
        if not host_row:
            unresolved_hosts.add(host_name)

        try:
            containers = docker_source.collect(host_url, cfg)
        except Exception:
            # une source en échec n'arrête pas le collecteur (règle 8), mais ne bloque plus la
            # suppression : ce host n'alimente simplement pas `seen_*` ce passage, l'âge de ses
            # lignes s'accumule normalement (voir run_node_pass pour le raisonnement complet).
            logger.exception("Hôte Docker '%s' injoignable, passage ignoré", host_name)
            continue

        for c in containers:
            try:
                image_row, _ = client.upsert(
                    image_cache, cfg.TABLE_IMAGES, "Reference", c["image_reference"], c["image_fields"],
                    last_seen_field="Last_seen",
                )
                seen_images.add(c["image_reference"])
                cup_targets.setdefault(c["image_reference"], {
                    "mode": c["cup_mode"], "reference": c["cup_reference"], "host_url": host_url,
                })

                # nom canonique (Node.name) pour la clé, pas la casse locale de DOCKER_HOSTS :
                # évite deux préfixes différents pour le même hôte selon d'où vient la config
                uid = f"{canonical_name}/{c['name']}"
                container_fields = dict(c["fields"])
                container_fields["Host"] = [host_row["id"]] if host_row else []
                container_fields["Image"] = [image_row["id"]]
                # `Application` n'est jamais touchée ici : lien 100% manuel désormais.
                client.upsert(
                    container_cache, cfg.TABLE_CONTAINER, "UID", uid, container_fields,
                    last_seen_field="Last_seen",
                )
                seen_containers.add(uid)
            except Exception:
                # un conteneur en échec (ex. valeur d'option Baserow inconnue) ne doit pas
                # empêcher les autres d'être traités, ni faire planter tout le collecteur
                logger.exception("Écriture conteneur '%s' (%s) en échec, conteneur suivant", c["name"], host_name)

    if unresolved_hosts:
        logger.warning(
            "Nom(s) DOCKER_HOSTS sans Node correspondant (vérifier l'orthographe exacte vs "
            "Node.name ; un hôte découvert via le tag 'docker' ne peut pas produire ce cas) : "
            "%s -> leurs conteneurs ont un champ 'host' vide",
            ", ".join(sorted(unresolved_hosts)),
        )

    # Container/Images n'ont pas de champ Source : rien n'y est jamais manuel par design
    # (Container est du pur factuel Docker ; Images, un miroir de ce que les conteneurs
    # référencent). Application n'est plus touchée du tout par le collecteur.
    client.delete_missing(container_cache, cfg.TABLE_CONTAINER, seen_containers, cfg.RETIRE_GRACE_HOURS)
    client.delete_missing(image_cache, cfg.TABLE_IMAGES, seen_images, cfg.RETIRE_GRACE_HOURS)
    return cup_targets


def run_cup_pass(client, cfg, cup_targets):
    """Images -> Cup (sous-processus) -> Images (Available_version, Update_available).

    `cup_targets` vient de run_container_pass (règle 2 : Images doit déjà être en cache,
    donc après la passe conteneurs). Deux lots distincts (voir sources/cup.py) :
    - "version" : un seul appel `-s none` pour toutes ces références d'un coup (rapide,
      aucun accès Docker, marche pour tag versionné ou latest+label OCI substitué).
    - "digest" : un appel par hôte, `-s tcp://<host>:<port>` vers SON socket-proxy en
      lecture seule — seul moyen de vérifier un `latest` sans label.

    Ce qu'on pense être une "version" n'en est pas toujours une pour Cup (tags comme
    "ihm-latest" ou un hash de build) : plutôt que de deviner parfaitement à l'avance,
    tout ce qui est rejeté en mode rapide est retenté en comparaison par empreinte, sur
    l'hôte où l'image tourne.
    """
    if not cfg.CUP_ENABLED or not cup_targets:
        return

    version_mode = {}  # cup_reference -> image_reference d'origine
    digest_mode_by_host = {}  # host_url -> {cup_reference: image_reference d'origine}

    for image_reference, info in cup_targets.items():
        if info["mode"] == "version":
            version_mode[info["reference"]] = image_reference
        else:
            digest_mode_by_host.setdefault(info["host_url"], {})[info["reference"]] = image_reference

    all_results = {}  # image_reference d'origine -> fields

    if version_mode:
        try:
            raw, rejected = cup_source.collect(version_mode.keys())
            for cup_reference, fields in raw.items():
                original = version_mode.get(cup_reference)
                if original:
                    all_results[original] = fields
            for cup_reference in rejected:
                image_reference = version_mode.get(cup_reference)
                info = cup_targets.get(image_reference) if image_reference else None
                if not info:
                    continue
                # repli : Cup n'a pas su interpréter ce tag comme une version -> on retente
                # par empreinte, sur l'hôte réel de l'image, avec la référence d'origine
                # (le tag substitué via le label OCI n'a aucun sens pour une comparaison
                # par empreinte, qui compare l'image telle qu'elle tourne réellement).
                digest_mode_by_host.setdefault(info["host_url"], {})[image_reference] = image_reference
        except Exception:
            # règle 8 : une source en échec n'arrête pas le collecteur
            logger.exception("Passe Cup (version) en échec, on continue sans elle")

    for host_url, mapping in digest_mode_by_host.items():
        socket_addr = host_url.replace("http://", "tcp://").replace("https://", "tcp://")
        try:
            raw, rejected = cup_source.collect(mapping.keys(), socket=socket_addr)
            for cup_reference, fields in raw.items():
                original = mapping.get(cup_reference)
                if original:
                    all_results[original] = fields
            if rejected:
                logger.debug("Cup (empreinte, %s) : références non vérifiables : %s", host_url, rejected)
        except Exception:
            logger.exception("Passe Cup (empreinte, %s) en échec, hôte suivant", host_url)

    if not all_results:
        return

    rows = client.fetch_all(cfg.TABLE_IMAGES)
    image_cache = {r["Reference"]: r for r in rows if r.get("Reference")}

    for reference, fields in all_results.items():
        row = image_cache.get(reference)
        if not row:
            continue
        mapped = _drop_none({
            "Available_version": fields.get("available_version"),
            "Update_available": fields.get("update_available"),
        })
        if not mapped:
            continue
        try:
            client.update_row(cfg.TABLE_IMAGES, row["id"], mapped)
        except Exception:
            logger.exception("Mise à jour Cup de l'image '%s' en échec", reference)


def run_trivy_pass(client, cfg):
    """Images -> Trivy (sous-processus, scan par digest exact) -> Images (CVE_critical,
    CVE_high, Last_scan). Indépendante des autres passes, comme Cup : lit directement
    Images, pas besoin d'une passe conteneurs récente. Seules les images avec un
    Digest_local connu sont scannées — sans lui, pas de référence exacte à pointer."""
    if not cfg.TRIVY_ENABLED:
        return

    rows = client.fetch_all(cfg.TABLE_IMAGES)
    today = datetime.now(timezone.utc).date().isoformat()

    for row in rows:
        reference = row.get("Reference")
        digest = row.get("Digest_local")
        if not reference or not digest:
            continue
        try:
            repo = docker_source.repo_from_reference(reference)
            result = trivy_source.collect(f"{repo}@{digest}", cfg.TRIVY_CACHE_DIR)
            if result is None:
                continue
            client.update_row(cfg.TABLE_IMAGES, row["id"], {
                "CVE_critical": result["cve_critical"],
                "CVE_high": result["cve_high"],
                "Last_scan": today,
            })
        except Exception:
            # règle 8 : une image en échec n'arrête pas le collecteur
            logger.exception("Scan Trivy de l'image '%s' en échec, image suivante", reference)


# Découpage du sommeil entre deux cycles. Le collecteur dort jusqu'à quinze minutes ;
# une demande venue de la console ne doit pas attendre la fin de ce sommeil, sans quoi le
# bouton ne vaudrait pas mieux que de patienter. Deux secondes de latence au pire, contre
# un réveil toutes les deux secondes pour rien le reste du temps — un `stat` sur un
# fichier absent, c'est le prix qu'on accepte de payer.
PAS_DE_VEILLE = 2.0


def _dormir(duree):
    fin = time.monotonic() + duree
    while True:
        reste = fin - time.monotonic()
        if reste <= 0:
            return False
        if signaux.en_attente():
            return True
        time.sleep(min(reste, PAS_DE_VEILLE))


def _setup_logging(level):
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")


def main():
    cfg = config.load()
    _setup_logging(cfg.LOG_LEVEL)
    logger.info("TinyCMDB %s", version.libelle(court=False))

    if not cfg.BASEROW_VERIFY_TLS or not getattr(cfg, "PROXMOX_VERIFY_TLS", True):
        # Sans ça, urllib3 écrit un avertissement PAR REQUÊTE : quelques centaines de
        # lignes par passe, qui noient les seuls messages qui comptent. Le journal
        # devient illisible, donc inutile, donc jamais lu le jour où il dit quelque
        # chose. La vérification reste désactivée, c'est un choix assumé de la
        # configuration — le répéter n'ajoute rien.
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    client = BaserowClient(cfg.BASEROW_URL, cfg.BASEROW_TOKEN, cfg.BASEROW_VERIFY_TLS)

    last_node_pass = 0.0
    last_cup_pass = 0.0
    last_trivy_pass = 0.0
    passes = {}
    demarre = time.time()

    def etat(en_cours=None, prochain=None):
        signaux.publier(en_cours=en_cours, passes=passes, demarre=demarre,
                        prochain=prochain, version=version.VERSION)

    etat(en_cours="démarrage")

    while True:
        cycle_start = time.monotonic()
        # Relevé AVANT la collecte, jamais après : une demande arrivée pendant le cycle
        # porte sur un état que celui-ci a déjà lu, elle doit en déclencher un autre.
        force_inventaire = signaux.consommer("inventaire")
        force_securite = signaux.consommer("securite")
        if force_inventaire or force_securite:
            logger.info("Collecte demandée depuis la console (%s)",
                        ", ".join(n for n, v in (("inventaire", force_inventaire),
                                                 ("sécurité", force_securite)) if v))

        due_for_nodes = (last_node_pass == 0.0 or force_inventaire
                         or (cycle_start - last_node_pass) >= cfg.INTERVAL_NODES)
        due_for_cup = (last_cup_pass == 0.0 or force_securite
                       or (cycle_start - last_cup_pass) >= cfg.INTERVAL_CUP)
        due_for_trivy = (last_trivy_pass == 0.0 or force_securite
                         or (cycle_start - last_trivy_pass) >= cfg.INTERVAL_TRIVY)

        if due_for_nodes:
            logger.info("--- passe nodes/VMs ---")
            etat(en_cours="nœuds et machines virtuelles")
            node_cache, guest_ip_data = run_node_pass(client, cfg)
            passes["noeuds"] = time.time()
            logger.info("--- passe IPAM ---")
            etat(en_cours="adresses IP")
            run_ipam_pass(client, cfg, node_cache, guest_ip_data)
            passes["ipam"] = time.time()
            last_node_pass = cycle_start
        else:
            node_cache = client.build_cache(cfg.TABLE_NODE, "Name")

        logger.info("--- passe conteneurs ---")
        etat(en_cours="conteneurs")
        cup_targets = run_container_pass(client, cfg, node_cache)
        passes["conteneurs"] = time.time()

        if due_for_cup:
            logger.info("--- passe Cup ---")
            etat(en_cours="versions disponibles")
            run_cup_pass(client, cfg, cup_targets)
            passes["cup"] = time.time()
            last_cup_pass = cycle_start

        if due_for_trivy:
            logger.info("--- passe Trivy ---")
            etat(en_cours="vulnérabilités des images")
            run_trivy_pass(client, cfg)
            passes["trivy"] = time.time()
            last_trivy_pass = cycle_start

        if cfg.RUN_ONCE:
            logger.info("RUN_ONCE=true : une seule passe effectuée, sortie")
            etat()
            break

        elapsed = time.monotonic() - cycle_start
        attente = max(cfg.INTERVAL_CONTAINERS - elapsed, 1)
        etat(prochain=time.time() + attente)
        if _dormir(attente):
            logger.info("Sommeil écourté : une collecte est demandée")


if __name__ == "__main__":
    main()
