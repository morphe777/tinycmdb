"""Copies de la base, cohérentes et datées, à côté d'elle dans /data/backups.

Pourquoi ne pas se contenter de la sauvegarde du dossier par l'hôte : copier
`tinycmdb.db` pendant qu'il est écrit peut donner un fichier incohérent, et en WAL une
partie des dernières écritures vit encore dans `tinycmdb.db-wal`. `VACUUM INTO` écrit une
base autonome, complète, compactée, en un seul fichier — que l'hôte peut alors copier
sans précaution particulière.

Restaurer : arrêter les deux services, remplacer `tinycmdb.db` par la copie choisie,
supprimer `tinycmdb.db-wal` et `tinycmdb.db-shm`, redémarrer.
"""

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import db

logger = logging.getLogger("cmdb.sauvegarde")

MOTIF = re.compile(r"^tinycmdb-(\d{4}-\d{2}-\d{2}_\d{6})\.db$")


def dossier_par_defaut(chemin_base):
    return Path(chemin_base).resolve().parent / "backups"


def lister(dossier):
    dossier = Path(dossier)
    if not dossier.is_dir():
        return []
    copies = []
    for f in dossier.iterdir():
        m = MOTIF.match(f.name)
        if not m or not f.is_file():
            continue
        stat = f.stat()
        copies.append({
            "nom": f.name,
            "chemin": f,
            "taille": stat.st_size,
            "date": datetime.strptime(m.group(1), "%Y-%m-%d_%H%M%S").replace(tzinfo=timezone.utc),
        })
    copies.sort(key=lambda c: c["date"], reverse=True)
    return copies


def derniere(dossier):
    copies = lister(dossier)
    return copies[0] if copies else None


def sauvegarder(chemin_base, dossier, conservation):
    """Écrit une copie et applique la rotation. Renvoie le chemin de la copie."""
    dossier = Path(dossier)
    dossier.mkdir(parents=True, exist_ok=True)
    horodatage = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    destination = dossier / f"tinycmdb-{horodatage}.db"
    temporaire = destination.with_suffix(".db.partiel")
    if temporaire.exists():
        temporaire.unlink()
    conn = db.connecter(str(chemin_base))
    try:
        conn.execute("VACUUM INTO ?", (str(temporaire),))
    finally:
        conn.close()
    # Contrôle avant de la déclarer valide : une copie qu'on n'a jamais relue n'est pas
    # une sauvegarde, c'est un espoir.
    verif = sqlite3.connect(str(temporaire))
    try:
        etat = verif.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        verif.close()
    if etat != "ok":
        temporaire.unlink(missing_ok=True)
        raise RuntimeError(f"copie corrompue ({etat}), abandonnée")
    temporaire.rename(destination)
    supprimees = rotation(dossier, conservation)
    logger.info("Sauvegarde écrite : %s (%d o), %d anciennes supprimées",
                destination.name, destination.stat().st_size, len(supprimees))
    return destination


def rotation(dossier, conservation):
    conservation = max(int(conservation or 1), 1)
    supprimees = []
    for copie in lister(dossier)[conservation:]:
        copie["chemin"].unlink(missing_ok=True)
        supprimees.append(copie["nom"])
    return supprimees


def copie(dossier, nom):
    """Chemin d'une copie désignée par son nom, ou None — jamais un chemin arbitraire."""
    if not MOTIF.match(nom or ""):
        return None
    chemin = Path(dossier) / nom
    return chemin if chemin.is_file() else None
