"""Ordonnancement : les passes de collecte à des périodes distinctes, et la sauvegarde
de la base, dans une boucle du programme (pas de cron dans le conteneur, pas de
`docker run --rm` depuis l'hôte).
"""

import ipaddress
import logging
import socket
import time
from collections import deque
from datetime import datetime, timezone

import signaux
import version
from cmdb import sauvegarde
from cmdb.depot import Depot

from . import config
from . import netcheck
from . import oui
from .stockage import Stockage
from .sources import cup as cup_source
from .sources import docker as docker_source
from .sources import materiel
from .sources import proxmox as proxmox_source
from .sources import trivy as trivy_source

logger = logging.getLogger("cmdb.main")


def _drop_none(fields):
    return {k: v for k, v in fields.items() if v is not None}


def _multiselect_values(row, field):
    """Un multiple_select revient comme une liste d'objets {id, value, color} —
    extrait juste les valeurs, casse d'origine préservée (nécessaire pour la fusion/écriture)."""
    return {opt.get("value") for opt in (row.get(field) or [])}


def _has_role(row, role_name):
    """Comparaison insensible à la casse : le libellé d'une option est éditable
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
    cpu_cache = {name.lower(): row for name, row in client.build_cache("cpu", "Name").items()}

    for n in hypervisors + guests:
        try:
            fields = _drop_none(dict(n["fields"]))
            if n.get("cpu"):
                cpu_row = _ensure_cpu(client, cpu_cache, n["cpu"])
                if cpu_row:
                    fields["CPU"] = [cpu_row["id"]]
            parent_name = n["parent_name"]
            if parent_name:
                # insensible à la casse : un hyperviseur peut avoir été renommé à la main
                # depuis la console ("biggy" -> "Biggy") sans que le nom réel côté Proxmox (le
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
                # (Docker, Database, ...) sont posés à la main depuis la console. `Roles` étant
                # une liste multiple, l'écriture remplace la liste entière — il faut donc
                # fusionner avec l'existant plutôt qu'écraser, sous peine d'effacer un tag manuel.
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
            # une ligne en échec (ex. valeur refusée par la base) ne doit pas bloquer les autres
            logger.exception("Écriture Node '%s' en échec, passage au suivant", n["name"])

    client.delete_missing(
        node_cache, cfg.TABLE_NODE, seen, cfg.RETIRE_GRACE_HOURS,
        last_seen_field="Last_seen", manual_gate_field="Source",
    )
    run_component_pass(client, cfg, node_cache, hypervisors)
    return node_cache, guest_ip_data


def _ensure_cpu(client, cpu_cache, seed):
    """La ligne du référentiel pour ce modèle de processeur, créée s'il est inconnu.

    Le collecteur ne fait qu'amorcer : ce que la machine dit d'elle-même (cœurs, threads,
    fréquence de base lue dans le nom, niveau x86-64), à la création seulement. La ligne
    appartient ensuite à la saisie — c'est là qu'on complète une fois pour toutes ce qui
    ne se mesure pas (turbo, TDP, cache, iGPU) — et n'est jamais supprimée : un modèle qui
    n'équipe plus rien reste une information.
    """
    cle = seed["Name"].lower()
    if cle in cpu_cache:
        return cpu_cache[cle]
    try:
        row = client.create_row("cpu", seed)
    except Exception:
        logger.exception("Création du processeur '%s' en échec", seed["Name"])
        return None
    cpu_cache[cle] = row
    logger.info("+ cpu créé : %s", seed["Name"])
    return row


def run_component_pass(client, cfg, node_cache, hypervisors):
    """Proxmox (PCI, disques, USB) -> Composant. Après Node : l'hôte et la VM qui reçoit
    un périphérique doivent être résolvables.

    Ce que Proxmox devine (nom, catégorie, marque, modèle) n'est posé qu'à la création :
    c'est une proposition, que la saisie corrige et que le passage suivant ne défait pas.

    Une famille qu'un hyperviseur n'a pas pu lister (droits, hôte éteint) garde ses lignes
    telles quelles : ne pas voir n'est pas constater une absence.

    Une carte PCI qu'on déplace change de clé (`hôte/pci/emplacement`). Plutôt que d'en
    créer une nouvelle et de laisser l'ancienne s'éteindre avec tout ce qui y avait été
    saisi, la passe reprend la ligne d'une carte de même identité (`PCI_id`) qui vient de
    disparaître — d'un autre hôte ou d'un autre emplacement. Les nouvelles cartes sont donc
    écrites en dernier, une fois connu tout ce que les hyperviseurs ont montré.
    """
    cache = client.build_cache("composant", "UID")
    ci_nodes = _ci_node_index(node_cache)
    seen = set()
    nouvelles = []

    def ecrire(comp, fields):
        try:
            client.upsert(
                cache, "composant", "UID", comp["uid"], fields,
                manual_gate_field="Source", auto_label="Auto", last_seen_field="Last_seen",
                create_only_fields=_drop_none(dict(comp.get("seed") or {})),
            )
            seen.add(comp["uid"])
        except Exception:
            logger.exception("Écriture composant '%s' en échec, composant suivant", comp.get("uid"))

    for hv in hypervisors:
        match = ci_nodes.get(hv["name"].lower())
        host_row = match[1] if match else None
        for comp in hv.get("components") or []:
            fields = _drop_none(dict(comp["fields"]))
            fields["Host"] = [host_row["id"]] if host_row else []
            cible = ci_nodes.get((comp.get("assigned_to") or "").lower())
            fields["Assigned_to"] = [cible[1]["id"]] if cible else []
            if comp["uid"] not in cache and fields.get("PCI_id"):
                nouvelles.append((comp, fields))
                continue
            ecrire(comp, fields)

        muettes = {f for f, ok in (hv.get("hardware_ok") or {}).items() if not ok}
        if muettes and host_row:
            for uid, row in cache.items():
                hotes = {lien.get("id") for lien in row.get("Host") or []}
                if host_row["id"] in hotes and materiel.famille_uid(uid) in muettes:
                    seen.add(uid)

    for comp, fields in nouvelles:
        ancienne = _carte_deplacee(client, cache, seen, fields["PCI_id"])
        if ancienne is not None:
            uid_avant, row = ancienne
            # La ligne change de clé : elle est réindexée sous la nouvelle, et `UID` part
            # avec le reste des champs — le journal note le déplacement (UID, Host).
            del cache[uid_avant]
            cache[comp["uid"]] = row
            fields = {**fields, "UID": comp["uid"]}
            logger.info("~ composant déplacé : %s -> %s (%s)", uid_avant, comp["uid"],
                        row.get("Name") or fields["PCI_id"])
        ecrire(comp, fields)

    client.delete_missing(
        cache, "composant", seen, cfg.RETIRE_GRACE_HOURS,
        last_seen_field="Last_seen", manual_gate_field="Source",
    )


def _carte_deplacee(client, cache, seen, pci_id):
    """(uid, ligne) de la carte de même identité que le collecteur ne voit plus nulle
    part, ou None.

    Une seule candidate, sinon rien : deux cartes identiques disparues en même temps ne
    disent pas laquelle est arrivée ici, et rattacher les saisies de l'une à l'autre serait
    pire que de recommencer. Une ligne manuelle n'est jamais reprise — le collecteur n'y
    touche pas. Une carte d'un hôte éteint est comptée comme vue (famille muette), elle
    n'est donc pas candidate : elle n'a pas disparu, on ne la voit simplement pas.
    """
    candidates = [
        (uid, row) for uid, row in cache.items()
        if uid not in seen and materiel.famille_uid(uid) == "pci"
        and row.get("PCI_id") == pci_id
        and (client._select_value(row, "Source") or "").lower() != "manual"
    ]
    return candidates[0] if len(candidates) == 1 else None


def _load_vlan_networks(client, table_vlan):
    """Lecture seule : les VLAN sont une référence maintenue à la main depuis la console."""
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


# Les noms de champs IPAM sont ceux du modèle (cmdb/modele.py). Un champ inconnu y est refusé
# avec une erreur, ligne par ligne — il ne disparaît plus en silence comme avec Baserow.
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
            # forme entière de l'adresse : tri/filtrage numérique correct
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
            client.update_row(cfg.TABLE_IPAM, row["id"], fields, avant=row)
            logger.debug("~ IPAM (manuel) enrichi : %s", address)
        except Exception:
            logger.exception("Enrichissement IPAM manuel '%s' en échec", address)


def _ci_node_index(node_cache):
    """Index insensible à la casse : DNS ne différencie pas 'myTools' de 'mytools', la
    comparaison de noms d'hôtes ne devrait donc pas en dépendre non plus."""
    return {name.lower(): (name, row) for name, row in node_cache.items()}


def _resolve_docker_targets(cfg, node_cache):
    """Liste des hôtes Docker à scanner : DOCKER_HOSTS (explicite, prioritaire) fusionné
    avec tout Node dont `roles` contient "docker" (tag posé à la main depuis la console).

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
                # un conteneur en échec (ex. valeur refusée par la base) ne doit pas
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
            # Vidée quand Cup se prononce sans la nommer (pas de mise à jour, ou comparaison
            # par empreinte) : sans quoi une version annoncée par un passage précédent —
            # parfois fausse, voir sources/docker.py:VERSION_COMPARABLE — restait affichée.
            "Available_version": fields.get("available_version") or (
                "" if "update_available" in fields else None),
            "Update_available": fields.get("update_available"),
        })
        if not mapped:
            continue
        try:
            client.update_row(cfg.TABLE_IMAGES, row["id"], mapped, avant=row)
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
            # Pas de journal : les deux compteurs bougent au gré de la base de
            # vulnérabilités, pas de ce qui tourne (voir stockage.MUETS).
            client.update_row(cfg.TABLE_IMAGES, row["id"], {
                "CVE_critical": result["cve_critical"],
                "CVE_high": result["cve_high"],
                "Last_scan": today,
            })
        except Exception:
            # règle 8 : une image en échec n'arrête pas le collecteur
            logger.exception("Scan Trivy de l'image '%s' en échec, image suivante", reference)


def run_sauvegarde(depot, cfg):
    """Copie de la base si la précédente a dépassé l'intervalle réglé dans
    l'administration. Repérée par la date du fichier le plus récent, et non par une
    horloge du process : un redémarrage du collecteur ne déclenche pas une copie de plus.

    Profite du passage pour purger le journal au-delà de sa durée de conservation.
    """
    intervalle = depot.parametre_entier("sauvegarde_intervalle_heures", 24)
    if intervalle <= 0:
        return False
    derniere = sauvegarde.derniere(cfg.BACKUP_DIR)
    if derniere is not None:
        age = (datetime.now(timezone.utc) - derniere["date"]).total_seconds() / 3600
        if age < intervalle:
            return False
    try:
        sauvegarde.sauvegarder(cfg.DB_PATH, cfg.BACKUP_DIR,
                               depot.parametre_entier("sauvegarde_conservation", 14))
    except Exception:
        # Règle 8 : une sauvegarde en échec n'arrête pas la collecte. Elle se voit sur la
        # page État, où l'âge de la dernière copie passe en alerte.
        logger.exception("Sauvegarde de la base en échec (dossier %s)", cfg.BACKUP_DIR)
        return False
    jours = depot.parametre_entier("journal_conservation_jours", 365)
    if jours > 0:
        purgees = depot.purger_journal(jours)
        if purgees:
            logger.info("Journal : %d entrées de plus de %d jours supprimées", purgees, jours)
    return True


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

    if not getattr(cfg, "PROXMOX_VERIFY_TLS", True):
        # Sans ça, urllib3 écrit un avertissement PAR REQUÊTE : quelques centaines de
        # lignes par passe, qui noient les seuls messages qui comptent. Le journal
        # devient illisible, donc inutile, donc jamais lu le jour où il dit quelque
        # chose. La vérification reste désactivée, c'est un choix assumé de la
        # configuration — le répéter n'ajoute rien.
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    depot = Depot(cfg.DB_PATH)
    client = Stockage(depot)
    logger.info("Base : %s", cfg.DB_PATH)

    last_node_pass = 0.0
    last_cup_pass = 0.0
    last_trivy_pass = 0.0
    passes = {}
    demarre = time.time()
    # Le journal publié à la console. Le collecteur écrit déjà dans le journal du
    # conteneur, que personne n'ouvre : celui-ci est la même chose, à l'écran, pendant que
    # ça tourne. Borné, parce qu'il montre un cycle en cours et non un historique.
    journal = deque(maxlen=signaux.JOURNAL_MAX)
    # Vrai pendant toute la durée d'un cycle réclamé depuis la console, faux pour les
    # passages automatiques. C'est ce qui décide de l'affichage du journal à l'écran : on
    # ouvre un terminal parce qu'on vient de demander quelque chose, pas pour regarder une
    # tâche de fond qui tourne de toute façon toutes les quinze minutes.
    reclame = {"actif": False, "fin": 0.0}

    def etat(en_cours=None, prochain=None, note=None):
        if note:
            journal.append({"t": time.time(), "texte": note})
        signaux.publier(en_cours=en_cours, passes=passes, demarre=demarre,
                        prochain=prochain, version=version.VERSION,
                        demandee=reclame["actif"], demandee_fin=reclame["fin"],
                        journal=list(journal))

    etat(en_cours="démarrage", note=f"collecteur démarré — version {version.VERSION}")

    while True:
        cycle_start = time.monotonic()
        # Relevé AVANT la collecte, jamais après : une demande arrivée pendant le cycle
        # porte sur un état que celui-ci a déjà lu, elle doit en déclencher un autre.
        force_inventaire = signaux.consommer("inventaire")
        force_securite = signaux.consommer("securite")
        reclame["actif"] = bool(force_inventaire or force_securite)
        if force_inventaire or force_securite:
            demandees = ", ".join(n for n, v in (("inventaire", force_inventaire),
                                                 ("sécurité", force_securite)) if v)
            logger.info("Collecte demandée depuis la console (%s)", demandees)
            etat(en_cours="démarrage", note=f"collecte demandée depuis la console : {demandees}")

        due_for_nodes = (last_node_pass == 0.0 or force_inventaire
                         or (cycle_start - last_node_pass) >= cfg.INTERVAL_NODES)
        # Cup suit les deux demandes : il ne coûte qu'un appel pour tout le parc, et
        # « ma mise à jour a-t-elle été prise en compte » est la question qu'on se pose en
        # relançant une collecte. La faire attendre Trivy n'aurait servi personne.
        due_for_cup = (last_cup_pass == 0.0 or force_inventaire or force_securite
                       or (cycle_start - last_cup_pass) >= cfg.INTERVAL_CUP)
        due_for_trivy = (last_trivy_pass == 0.0 or force_securite
                         or (cycle_start - last_trivy_pass) >= cfg.INTERVAL_TRIVY)

        if due_for_nodes:
            logger.info("--- passe nodes/VMs ---")
            etat(en_cours="nœuds et machines virtuelles", note="nœuds et machines virtuelles…")
            node_cache, guest_ip_data = run_node_pass(client, cfg)
            passes["noeuds"] = time.time()
            etat(en_cours="nœuds et machines virtuelles",
                 note=f"{len(node_cache)} nœuds inventoriés")
            logger.info("--- passe IPAM ---")
            etat(en_cours="adresses IP", note="adresses IP…")
            run_ipam_pass(client, cfg, node_cache, guest_ip_data)
            passes["ipam"] = time.time()
            last_node_pass = cycle_start
        else:
            node_cache = client.build_cache(cfg.TABLE_NODE, "Name")

        logger.info("--- passe conteneurs ---")
        etat(en_cours="conteneurs", note="conteneurs et images…")
        cup_targets = run_container_pass(client, cfg, node_cache)
        passes["conteneurs"] = time.time()
        etat(en_cours="conteneurs", note=f"{len(cup_targets)} images distinctes en service")

        if due_for_cup:
            logger.info("--- passe Cup ---")
            etat(en_cours="versions disponibles", note="comparaison des versions publiées…")
            run_cup_pass(client, cfg, cup_targets)
            passes["cup"] = time.time()
            last_cup_pass = cycle_start

        if due_for_trivy:
            logger.info("--- passe Trivy ---")
            etat(en_cours="vulnérabilités des images",
                 note="analyse des vulnérabilités, image par image…")
            run_trivy_pass(client, cfg)
            passes["trivy"] = time.time()
            last_trivy_pass = cycle_start
            etat(en_cours="vulnérabilités des images", note="analyse terminée")

        if run_sauvegarde(depot, cfg):
            passes["sauvegarde"] = time.time()
            etat(note="base sauvegardée")

        if cfg.RUN_ONCE:
            logger.info("RUN_ONCE=true : une seule passe effectuée, sortie")
            etat()
            break

        elapsed = time.monotonic() - cycle_start
        attente = max(cfg.INTERVAL_CONTAINERS - elapsed, 1)
        # La fin du cycle réclamé est horodatée plutôt que simplement effacée : la
        # console garde le journal quelques secondes de plus, sans quoi la dernière ligne —
        # celle qui annonce la durée — disparaîtrait dans l'instant où elle est écrite.
        if reclame["actif"]:
            reclame["fin"] = time.time()
        reclame["actif"] = False
        etat(prochain=time.time() + attente,
             note=f"cycle terminé en {elapsed:.0f} s — prochain dans {attente / 60:.0f} min")
        if _dormir(attente):
            logger.info("Sommeil écourté : une collecte est demandée")


if __name__ == "__main__":
    main()
