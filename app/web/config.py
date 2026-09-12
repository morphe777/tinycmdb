"""Configuration du service web.

Volontairement séparée de `collector/config.py` plutôt que réutilisée : le service web
n'a pas besoin de Proxmox ni des sockets Docker, et exiger ces valeurs au démarrage
ferait échouer un service qui n'en a que faire. Les deux lisent le même `.env`, chacun
n'y prend que ce qui le concerne.

Périmètre du service web = Baserow, et rien d'autre. C'est une propriété de sécurité,
pas un détail d'implémentation : ce process est le seul des deux à être exposé à un
navigateur, donc le seul dont la compromission mène quelque part.
"""

import os


class ConfigError(Exception):
    pass


class Config:
    pass


def _get_int(name, default):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _get_bool(name, default):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def load():
    cfg = Config()

    cfg.BASEROW_URL = os.environ.get("BASEROW_URL", "").rstrip("/")
    # Token dédié si fourni : la console n'a besoin que de la lecture (lot 1). Repli sur
    # le token du collecteur pour démarrer sans configuration supplémentaire — pratique en
    # test, à ne pas garder en production (voir README.md).
    cfg.BASEROW_TOKEN = os.environ.get("WEB_BASEROW_TOKEN") or os.environ.get("BASEROW_TOKEN", "")
    cfg.BASEROW_VERIFY_TLS = _get_bool("BASEROW_VERIFY_TLS", True)

    if not cfg.BASEROW_URL or not cfg.BASEROW_TOKEN:
        raise ConfigError("BASEROW_URL et BASEROW_TOKEN (ou WEB_BASEROW_TOKEN) sont obligatoires")

    # La console lit les six tables sans exception : son intérêt est précisément de suivre
    # les liens de l'une à l'autre. Une table manquante n'est donc pas un mode dégradé,
    # c'est une configuration incomplète.
    cfg.TABLES = {}
    for key, env_name in (
        ("node", "TABLE_NODE"),
        ("container", "TABLE_CONTAINER"),
        ("image", "TABLE_IMAGES"),
        ("ipam", "TABLE_IPAM"),
        ("vlan", "TABLE_VLAN"),
        ("application", "TABLE_APPLICATION"),
    ):
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise ConfigError(f"{env_name} est obligatoire pour la console web")
        cfg.TABLES[key] = value

    # Durée de vie de l'instantané en mémoire. 217 lignes se rechargent en ~1 s : inutile de
    # descendre plus bas, inutile de monter beaucoup plus haut (le collecteur écrit toutes
    # les 15 min au plus fréquent).
    cfg.CACHE_TTL = _get_int("WEB_CACHE_TTL", 60)

    # Repris tel quel du collecteur : sert à afficher le compte à rebours avant suppression
    # d'une ligne qui n'est plus vue. Doit valoir la même chose que côté collecteur, sinon
    # l'écran d'état ment.
    cfg.RETIRE_GRACE_HOURS = _get_int("RETIRE_GRACE_HOURS", 72)

    # Cadence attendue de chaque passe. La console ne planifie rien : elle s'en sert
    # uniquement pour juger si une source est à l'heure. Sans ces valeurs, « il y a 4 h »
    # ne voudrait rien dire — normal pour Trivy, inquiétant pour les conteneurs.
    cfg.INTERVAL_CONTAINERS = _get_int("INTERVAL_CONTAINERS", 900)
    cfg.INTERVAL_NODES = _get_int("INTERVAL_NODES", 3600)
    cfg.INTERVAL_TRIVY = _get_int("INTERVAL_TRIVY", 86400)

    # Découverte des icônes d'application : la console lit le HTML des URL saisies dans la
    # table Application pour y trouver la balise <link rel="icon">. Seule entorse au
    # principe « ce service ne parle qu'à Baserow » — voir favicons.py. À désactiver si
    # cette entorse n'est pas souhaitée ; les applications retombent sur leur initiale.
    cfg.FAVICON_DISCOVERY = _get_bool("WEB_FAVICON_DISCOVERY", True)

    cfg.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
    return cfg
