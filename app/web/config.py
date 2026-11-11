"""Configuration du service web.

Volontairement séparée de `collector/config.py` plutôt que réutilisée : le service web
n'a pas besoin de Proxmox ni des sockets Docker, et exiger ces valeurs au démarrage
ferait échouer un service qui n'en a que faire. Les deux lisent le même `.env`, chacun
n'y prend que ce qui le concerne.

Périmètre du service web = la base SQLite, et rien d'autre (hormis la lecture des pages
d'accueil des applications pour leurs icônes, voir favicons.py). C'est une propriété de
sécurité, pas un détail d'implémentation : ce process est le seul des deux à être exposé
à un navigateur, donc le seul dont la compromission mène quelque part.
"""

import os
from pathlib import Path


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

    # Même fichier que le collecteur, par le même volume /data. La console n'a pas de
    # cache à durée de vie : elle relit la base dès que quelqu'un d'autre y a écrit (voir
    # store.py), et seulement dans ce cas.
    cfg.DB_PATH = os.environ.get("TINYCMDB_DB", "/data/tinycmdb.db")
    cfg.BACKUP_DIR = os.environ.get("TINYCMDB_BACKUP_DIR", "") or str(
        Path(cfg.DB_PATH).resolve().parent / "backups")

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
    # principe « ce service ne parle qu'à sa base » — voir favicons.py. À désactiver si
    # cette entorse n'est pas souhaitée ; les applications retombent sur leur initiale.
    cfg.FAVICON_DISCOVERY = _get_bool("WEB_FAVICON_DISCOVERY", True)

    cfg.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
    return cfg
