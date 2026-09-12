"""Source Docker -> tables Container, Images.

Interroge un docker-socket-proxy en lecture seule (jamais le socket Docker en
direct : c'est équivalent à root). Renvoie des dicts normalisés, un par
conteneur. Aucun appel Baserow ici.

`Application` n'est plus alimentée par cette source (choix explicite : un projet
Docker Compose est un fait technique sur le conteneur, pas la même chose qu'une
Application au sens métier — qui reste 100% manuelle). `Container.Stack` porte
ce fait, exactement comme `Container.Host` : jamais interprété, juste rapporté.
"""

import logging

import requests

logger = logging.getLogger("cmdb.docker")

_STATUS_MAP = {
    "running": "Running",
    "exited": "Exited",
    "paused": "Paused",
    "restarting": "Restarting",
}

_RESTART_POLICY_MAP = {
    "no": "No",
    "always": "Always",
    "unless-stopped": "Unless-Stopped",
    "on-failure": "On-Failure",
}

_TYPE_COMPOSE = "Docker_Compose"
_TYPE_STANDALONE = "Container"


def collect(host_url, cfg):
    session = requests.Session()

    resp = session.get(f"{host_url}/containers/json", params={"all": "true"}, timeout=15)
    resp.raise_for_status()
    raw_containers = resp.json()

    results = []
    for c in raw_containers:
        name = c["Names"][0].lstrip("/")
        labels = c.get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        working_dir = labels.get("com.docker.compose.project.working_dir")
        service = labels.get("com.docker.compose.service")

        restart_policy = _fetch_restart_policy(session, host_url, c["Id"], name)

        image_ref = c.get("Image")
        parsed = parse_image_reference(image_ref)
        digest_local, resolved_version = _fetch_image_metadata(session, host_url, image_ref)

        image_fields = {
            "Registry": parsed["registry"],
            "Repository": parsed["repository"],
        }
        if parsed["tag"]:
            image_fields["Tag"] = parsed["tag"]
        if digest_local:
            image_fields["Digest_local"] = digest_local

        # Comment vérifier les mises à jour avec Cup pour cette image (voir main.py:run_cup_pass) :
        # - tag déjà versionné ("15-alpine") -> Cup s'en sert directement, aucun accès Docker requis.
        # - tag "latest" mais un label OCI donne la vraie version -> on la substitue au tag, même
        #   comparaison fiable que ci-dessus, sans avoir besoin d'interroger le registre nous-mêmes.
        # - "latest" sans label -> seule solution : comparaison par empreinte via le socket-proxy
        #   de CET hôte (Cup a besoin d'accéder à l'image réellement présente, peu importe comment).
        repo = repo_from_reference(image_ref)
        if parsed["tag"] and parsed["tag"] != "latest":
            cup_mode, cup_reference = "version", image_ref
        elif resolved_version and repo:
            cup_mode, cup_reference = "version", f"{repo}:{resolved_version}"
        else:
            cup_mode, cup_reference = "digest", image_ref

        status = _STATUS_MAP.get(c.get("State"))
        if c.get("State") and status is None:
            logger.warning("Conteneur %s : état Docker '%s' non mappé, status omis", name, c.get("State"))

        container_fields = _drop_none({
            "Name": name,
            "Service": service,
            "Status": status,
            "Ports": _format_ports(c.get("Ports")),
            "Restart_policy": restart_policy,
            # écrit explicitement "" quand on SAIT qu'il n'y a pas de projet Compose (règle 3) :
            # révèle ce qui tourne hors de tout `docker compose`, donc hors reproductibilité.
            "Stack": project or "",
            "Type": _TYPE_COMPOSE if project else _TYPE_STANDALONE,
            # connu seulement si le label working_dir est présent (rare qu'il manque avec un
            # projet) ; absent de tout compose -> "" explicite, comme Stack.
            "Compose_path": working_dir if project else "",
        })

        results.append({
            "name": name,
            "fields": container_fields,
            "image_reference": image_ref,
            "image_fields": image_fields,
            "cup_mode": cup_mode,
            "cup_reference": cup_reference,
        })

    return results


def _fetch_restart_policy(session, host_url, container_id, name):
    try:
        resp = session.get(f"{host_url}/containers/{container_id}/json", timeout=15)
        resp.raise_for_status()
        raw = resp.json().get("HostConfig", {}).get("RestartPolicy", {}).get("Name") or "no"
        policy = _RESTART_POLICY_MAP.get(raw)
        if policy is None:
            logger.warning("Conteneur %s : restart_policy Docker '%s' non mappée, omise", name, raw)
        return policy
    except requests.RequestException:
        logger.warning("Détail indisponible pour le conteneur %s, restart_policy omis", name)
        return None


def _fetch_image_metadata(session, host_url, image_ref):
    """Renvoie (digest_local, resolved_version). `resolved_version` vient du label OCI
    standard `org.opencontainers.image.version` (ou l'équivalent label-schema) quand il est
    posé dans l'image — donne la vraie version même sous un tag `latest`, sans appel réseau
    supplémentaire (même requête que pour le digest)."""
    if not image_ref:
        return None, None
    try:
        resp = session.get(f"{host_url}/images/{image_ref}/json", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        digest_local = None
        repo_digests = data.get("RepoDigests") or []
        if repo_digests:
            digest_local = repo_digests[0].split("@", 1)[-1]
        labels = data.get("Config", {}).get("Labels") or {}
        resolved_version = labels.get("org.opencontainers.image.version") or labels.get("org.label-schema.version")
        return digest_local, resolved_version
    except requests.RequestException:
        logger.debug("Détail image indisponible pour %s", image_ref)
        return None, None


def _drop_none(fields):
    """Ne jamais envoyer une clé à None : ça effacerait un champ dont on ne connaît
    en fait pas la valeur à ce passage (règle 3, distincte d'une valeur réellement vide)."""
    return {k: v for k, v in fields.items() if v is not None}


def _format_ports(ports):
    if not ports:
        return ""
    parts = []
    for p in ports:
        private_port = p.get("PrivatePort")
        public_port = p.get("PublicPort")
        proto = p.get("Type", "tcp")
        if public_port:
            parts.append(f"{public_port}:{private_port}/{proto}")
        else:
            parts.append(f"{private_port}/{proto}")
    return ", ".join(sorted(set(parts)))


def repo_from_reference(reference):
    """'ghcr.io/org/app:1.2' -> 'ghcr.io/org/app' (retire le tag et/ou le digest). Réutilisé
    par main.py:run_trivy_pass pour reconstruire une référence pointée par digest."""
    if not reference:
        return None
    return reference.split("@", 1)[0].rsplit(":", 1)[0]


def parse_image_reference(reference):
    """'ghcr.io/org/app:1.2' -> {registry, repository, tag, digest}.

    La première composante n'est un registry que si elle contient un point, un
    deux-points, ou vaut "localhost" ; sinon c'est le début du namespace Docker Hub
    (`docker.io` implicite). Gère aussi la forme par digest (`@sha256:...`).
    """
    ref = reference
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)

    tag = None
    name_part = ref
    last_segment = ref.rsplit("/", 1)[-1]
    if ":" in last_segment:
        name_part, tag = ref.rsplit(":", 1)

    parts = name_part.split("/", 1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, repository = parts
    else:
        registry, repository = "docker.io", name_part

    if tag is None and digest is None:
        tag = "latest"

    return {"registry": registry, "repository": repository, "tag": tag, "digest": digest}
