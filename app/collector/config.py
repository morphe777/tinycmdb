"""Lecture et validation de la configuration : env -> constantes.

Aucune valeur par défaut secrète ici. Une config invalide doit faire échouer le
démarrage tout de suite (fail-fast), pas au milieu d'une passe.
"""

import os
from pathlib import Path


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

    # La base est un fichier, partagé avec la console par le volume /data (voir
    # compose.yaml). Les copies de sauvegarde vont dans un dossier voisin, pour que l'hôte
    # qui sauvegarde /data les emporte avec.
    cfg.DB_PATH = os.environ.get("TINYCMDB_DB", "/data/tinycmdb.db")
    cfg.BACKUP_DIR = os.environ.get("TINYCMDB_BACKUP_DIR", "") or str(
        Path(cfg.DB_PATH).resolve().parent / "backups")

    # Les tables sont désignées par leur nom. Elles existent toutes, toujours : c'est la
    # disponibilité d'une source qui décide si une passe les alimente, d'où les None
    # plus bas, qui gardent aux passes leur comportement d'origine.
    cfg.TABLE_NODE = "node"

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
    # taguant un Node "docker" depuis la console — impossible à savoir avant la première passe
    # Proxmox, donc dès que Proxmox est actif la passe Docker est armée par précaution.
    docker_possible = bool(cfg.DOCKER_HOSTS) or bool(cfg.PROXMOX_URL)

    cfg.TABLE_CONTAINER = "container" if docker_possible else None
    cfg.TABLE_IMAGES = "image" if docker_possible else None

    # IPAM alimenté uniquement à partir des données Proxmox (IP/MAC des guests).
    cfg.TABLE_VLAN = "vlan" if cfg.PROXMOX_URL else None
    cfg.TABLE_IPAM = "ipam" if cfg.PROXMOX_URL else None

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
        raise ConfigError("CUP_ENABLED est actif mais aucune source Docker n'est active : il n'y a aucune image à examiner")

    # Trivy : même principe que Cup (sous-processus, pas de serveur). Désactivé par défaut.
    # Scan lent (dizaines de secondes par image la première fois) : intervalle bien plus
    # long que les autres passes, les CVE ne changent pas d'heure en heure.
    cfg.TRIVY_ENABLED = _get_bool("TRIVY_ENABLED", False)
    cfg.TRIVY_CACHE_DIR = os.environ.get("TRIVY_CACHE_DIR", "/cache")
    if cfg.TRIVY_ENABLED and not docker_possible:
        raise ConfigError("TRIVY_ENABLED est actif mais aucune source Docker n'est active : il n'y a aucune image à examiner")

    cfg.INTERVAL_CONTAINERS = _get_int("INTERVAL_CONTAINERS", 900)
    cfg.INTERVAL_NODES = _get_int("INTERVAL_NODES", 3600)
    cfg.INTERVAL_CUP = _get_int("INTERVAL_CUP", 21600)
    cfg.INTERVAL_TRIVY = _get_int("INTERVAL_TRIVY", 86400)
    cfg.RETIRE_GRACE_HOURS = _get_int("RETIRE_GRACE_HOURS", 72)

    cfg.RUN_ONCE = _get_bool("RUN_ONCE", False)
    cfg.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

    return cfg
