"""Ouverture de la base, réglages, migrations.

Deux processus écrivent dans le même fichier : le collecteur et la console. Le mode WAL
est ce qui le rend possible sans que l'un bloque l'autre — une lecture ne patiente jamais
derrière une écriture, et deux écritures se succèdent à quelques millisecondes d'écart
(`busy_timeout`). La condition est que les deux processus voient le même fichier sur le
même noyau : un montage local de l'hôte, jamais un partage réseau (SMB, NFS), où les
verrous de SQLite ne sont pas fiables.
"""

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("cmdb.db")

MIGRATIONS = Path(__file__).resolve().parent / "migrations"

# Délai d'attente d'un verrou d'écriture. Une écriture de la CMDB dure quelques
# millisecondes ; dix secondes, c'est une marge pour la sauvegarde ou une reprise.
ATTENTE_MS = 10_000


def connecter(chemin, partagee=False):
    conn = sqlite3.connect(chemin, timeout=ATTENTE_MS / 1000, isolation_level=None,
                           check_same_thread=not partagee)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {ATTENTE_MS}")
    # En WAL, NORMAL ne perd rien sur une coupure du process — seulement, au pire, la
    # dernière transaction sur une coupure de courant. Le prix d'un fsync par écriture
    # n'en vaut pas la peine pour un inventaire que le collecteur réécrit à chaque passe.
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _instructions(script):
    """Découpe un script SQL en instructions, déclencheurs compris.

    `executescript` ne convient pas : il valide toute transaction en cours avant de
    commencer, ce qui empêcherait d'appliquer une migration et sa version d'un seul bloc.
    """
    courante = []
    for ligne in script.splitlines(keepends=True):
        courante.append(ligne)
        texte = "".join(courante)
        if sqlite3.complete_statement(texte):
            instruction = texte.strip()
            if instruction and instruction != ";":
                yield instruction
            courante = []
    reste = "".join(courante).strip()
    if reste and not all(l.strip().startswith("--") or not l.strip() for l in reste.splitlines()):
        raise ValueError(f"instruction SQL incomplète en fin de migration : {reste[:80]}")


def migrations_disponibles():
    fichiers = sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))
    return [(int(f.name[:3]), f) for f in fichiers]


def migrer(conn):
    """Applique les migrations manquantes. Sûr si les deux services démarrent ensemble :
    `BEGIN IMMEDIATE` sérialise, et la version est relue une fois le verrou obtenu."""
    for numero, fichier in migrations_disponibles():
        if conn.execute("PRAGMA user_version").fetchone()[0] >= numero:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] >= numero:
                conn.execute("ROLLBACK")
                continue
            for instruction in _instructions(fichier.read_text(encoding="utf-8")):
                conn.execute(instruction)
            conn.execute(f"PRAGMA user_version = {numero}")
            conn.execute("COMMIT")
            logger.info("Migration %s appliquée", fichier.name)
        except Exception:
            conn.execute("ROLLBACK")
            raise


def initialiser(chemin):
    """Crée le fichier au besoin, passe en WAL, applique les migrations."""
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    # Panne classique d'un montage bind : Docker crée le répertoire absent côté hôte,
    # propriété de root, et SQLite répond « unable to open database file » sans dire
    # pourquoi. Le dire ici, avec le remède.
    if not os.access(chemin.parent, os.W_OK | os.X_OK):
        raise PermissionError(
            f"{chemin.parent} n'est pas accessible en écriture par l'utilisateur "
            f"{os.getuid()} : côté hôte, `chown -R {os.getuid()}:{os.getgid()}` sur le "
            "répertoire monté en /data.")
    conn = connecter(str(chemin))
    try:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode.lower() != "wal":
            logger.warning("Mode WAL refusé (%s) : le fichier est-il sur un partage réseau ?", mode)
        migrer(conn)
    finally:
        conn.close()


class Connexions:
    """Une connexion par thread, et des transactions réentrantes.

    La console sert ses requêtes depuis un pool de threads ; une connexion SQLite ne se
    partage pas entre threads sans verrou. Une par thread, ouverte à la première demande
    et gardée ensuite, coûte moins qu'une ouverture par requête et ne demande aucun verrou
    applicatif.
    """

    def __init__(self, chemin):
        self.chemin = str(chemin)
        self._local = threading.local()

    @property
    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connecter(self.chemin)
            self._local.conn = conn
            self._local.profondeur = 0
        return conn

    @contextmanager
    def transaction(self):
        conn = self.conn
        if self._local.profondeur:
            self._local.profondeur += 1
            try:
                yield conn
            finally:
                self._local.profondeur -= 1
            return
        conn.execute("BEGIN IMMEDIATE")
        self._local.profondeur = 1
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        finally:
            self._local.profondeur = 0

    def fermer(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
