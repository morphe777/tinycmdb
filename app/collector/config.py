"""Lecture et validation de la configuration : env -> constantes.

Aucune valeur par défaut secrète ici. Une config invalide doit faire échouer le
démarrage tout de suite (fail-fast), pas au milieu d'une passe.
"""

import os


class ConfigError(Exception):
    pass


class Config:
    """Sac de constantes, rempli une fois par load(). Pas de logique dedans."""


def _get_bool(name, default):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _get_int(name, default):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _parse_docker_hosts(raw):
    """'nom=url,nom=url' -> {nom: url}.

    Le nom doit correspondre exactement à Node.name (règle documentée dans le
    prompt d'origine) : c'est la source la plus fréquente de conteneurs orphelins.
    """
    hosts = {}
    if not raw:
        return hosts
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ConfigError(f"DOCKER_HOSTS mal formé, attendu 'nom=url' : '{pair}'")
        name, url = pair.split("=", 1)
        name = name.strip()
        url = url.strip().rstrip("/")
        if not name or not url:
            raise ConfigError(f"DOCKER_HOSTS mal formé, attendu 'nom=url' : '{pair}'")
        hosts[name] = url
    return hosts


def load():
    cfg = Config()

    cfg.BASEROW_URL = os.environ.get("BASEROW_URL", "").rstrip("/")
    cfg.BASEROW_TOKEN = os.environ.get("BASEROW_TOKEN", "")
    cfg.BASEROW_VERIFY_TLS = _get_bool("BASEROW_VERIFY_TLS", True)

    if not cfg.BASEROW_URL or not cfg.BASEROW_TOKEN:
        raise ConfigError(
            "BASEROW_URL et BASEROW_TOKEN sont obligatoires : Baserow est la seule destination du collecteur"
        )

    cfg.TABLE_NODE = os.environ.get("TABLE_NODE", "")
    if not cfg.TABLE_NODE:
        # Docker a besoin de Node pour résoudre le lien `host`, même si Proxmox est désactivé.
        raise ConfigError("TABLE_NODE est obligatoire, y compris si seule la source Docker est active")

    cfg.PROXMOX_URL = os.environ.get("PROXMOX_URL", "").rstrip("/")
    cfg.PROXMOX_TOKEN = os.environ.get("PROXMOX_TOKEN", "")
    cfg.PROXMOX_VERIFY_TLS = _get_bool("PROXMOX_VERIFY_TLS", False)

    if cfg.PROXMOX_URL and not cfg.PROXMOX_TOKEN:
        raise ConfigError("PROXMOX_URL est renseigné mais PROXMOX_TOKEN est vide")

    cfg.DOCKER_HOSTS = _parse_docker_hosts(os.environ.get("DOCKER_HOSTS", ""))
    # utilisé pour construire l'URL des hôtes Docker découverts par tag, et comme repli DNS
    # (nom Proxmox -> IP) quand aucune autre source ne donne l'adresse d'un guest
    cfg.DNS_SUFFIX = os.environ.get("DNS_SUFFIX", "")
    cfg.DOCKER_SOCKET_PROXY_PORT = _get_int("DOCKER_SOCKET_PROXY_PORT", 2375)

    # Un hôte Docker peut venir de DOCKER_HOSTS (explicite) ou être découvert après coup en
    # taguant un Node "docker" dans Baserow — impossible à savoir avant la première passe
    # Proxmox, donc dès que Proxmox est actif on exige les tables Docker par précaution.
    docker_possible = bool(cfg.DOCKER_HOSTS) or bool(cfg.PROXMOX_URL)

    # TABLE_APPLICATION n'est plus utilisée par le collecteur : Application est 100% manuelle
    # (un projet Compose est un fait sur Container.Stack, pas une Application au sens métier).
    if docker_possible:
        cfg.TABLE_CONTAINER = os.environ.get("TABLE_CONTAINER", "")
        cfg.TABLE_IMAGES = os.environ.get("TABLE_IMAGES", "")
        if not (cfg.TABLE_CONTAINER and cfg.TABLE_IMAGES):
            raise ConfigError(
                "DOCKER_HOSTS et/ou PROXMOX_URL sont renseignés (un Node Proxmox peut être tagué "
                "'docker' à tout moment) : TABLE_CONTAINER et TABLE_IMAGES sont obligatoires"
            )
    else:
        cfg.TABLE_CONTAINER = None
        cfg.TABLE_IMAGES = None

    # IPAM alimenté uniquement à partir des données Proxmox (IP/MAC des guests) : les tables
    # ne sont exigées que si PROXMOX_URL est actif.
    if cfg.PROXMOX_URL:
        cfg.TABLE_VLAN = os.environ.get("TABLE_VLAN", "")
        cfg.TABLE_IPAM = os.environ.get("TABLE_IPAM", "")
        if not (cfg.TABLE_VLAN and cfg.TABLE_IPAM):
            raise ConfigError("PROXMOX_URL est renseigné : TABLE_VLAN et TABLE_IPAM sont obligatoires")
    else:
        cfg.TABLE_VLAN = None
        cfg.TABLE_IPAM = None

    if not cfg.PROXMOX_URL and not cfg.DOCKER_HOSTS:
        raise ConfigError(
            "Aucune source configurée : renseigner PROXMOX_URL et/ou DOCKER_HOSTS (au moins une des deux)"
        )

    # Cup tourne en sous-processus dans ce conteneur (binaire installé au build, voir
    # Dockerfile) : pas de serveur séparé, pas d'URL à configurer. Désactivé par défaut
    # (appels réseau vers des registres publics, pas anodin) ; n'a de sens que si Images
    # existe déjà (docker_possible), sinon il n'y a jamais rien à vérifier.
    cfg.CUP_ENABLED = _get_bool("CUP_ENABLED", False)
    if cfg.CUP_ENABLED and not docker_possible:
        raise ConfigError("CUP_ENABLED est actif mais aucune source Docker n'est active : TABLE_IMAGES n'existe pas")

    # Trivy : même principe que Cup (sous-processus, pas de serveur). Désactivé par défaut.
    # Scan lent (dizaines de secondes par image la première fois) : intervalle bien plus
    # long que les autres passes, les CVE ne changent pas d'heure en heure.
    cfg.TRIVY_ENABLED = _get_bool("TRIVY_ENABLED", False)
    cfg.TRIVY_CACHE_DIR = os.environ.get("TRIVY_CACHE_DIR", "/cache")
    if cfg.TRIVY_ENABLED and not docker_possible:
        raise ConfigError("TRIVY_ENABLED est actif mais aucune source Docker n'est active : TABLE_IMAGES n'existe pas")

    cfg.INTERVAL_CONTAINERS = _get_int("INTERVAL_CONTAINERS", 900)
    cfg.INTERVAL_NODES = _get_int("INTERVAL_NODES", 3600)
    cfg.INTERVAL_CUP = _get_int("INTERVAL_CUP", 21600)
    cfg.INTERVAL_TRIVY = _get_int("INTERVAL_TRIVY", 86400)
    cfg.RETIRE_GRACE_HOURS = _get_int("RETIRE_GRACE_HOURS", 72)

    cfg.RUN_ONCE = _get_bool("RUN_ONCE", False)
    cfg.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

    return cfg
