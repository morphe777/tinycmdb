"""Vérification de vie best-effort par ping ICMP.

Signal informatif seulement, jamais une preuve absolue : un hôte peut bloquer l'ICMP en
entrée sans être inutilisé. Ne sert donc qu'à distinguer "répond" de "silence" là où
Proxmox ne peut rien confirmer directement (IPAM.status), jamais à écraser une donnée
Proxmox de première main (ex. `os`, `Guest_agent`).
"""

import logging
import subprocess

logger = logging.getLogger("cmdb.netcheck")


def is_alive(ip, timeout=1):
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        logger.debug("Ping impossible vers %s (binaire 'ping' absent ?)", ip)
        return False
