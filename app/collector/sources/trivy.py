"""Source Trivy -> table Images (CVE_critical, CVE_high, Last_scan).

Trivy tourne en sous-processus dans ce conteneur, comme Cup — aucun accès Docker : il
scanne directement le contenu d'une image via le registre (ou via le digest exact qu'on
lui donne), pas besoin de socket-proxy ni de serveur séparé.

Contrairement à Cup, pas d'ambiguïté sur "quelle image regarder" : `main.py` lui donne
toujours `repo@sha256:<Digest_local>`, l'image exacte qui tourne réellement — jamais un
tag `latest` qui pourrait pointer ailleurs demain.

Base de vulnérabilités (~1 Go, dont la base Java, obligatoire même sans image Java au
premier lancement) : mise en cache dans /cache, un volume persistant (voir compose.yaml).
Sans ce volume, retéléchargement complet à chaque redémarrage du conteneur.
"""

import json
import logging
import subprocess

logger = logging.getLogger("cmdb.trivy")

_SEVERITIES = ("CRITICAL", "HIGH")


def collect(reference, cache_dir, timeout=300):
    """Renvoie {"cve_critical": int, "cve_high": int} pour une référence d'image donnée
    (idéalement `repo@sha256:digest`, voir docstring du module). None si le scan échoue."""
    # Pas de --skip-*-db-update : Trivy a déjà sa propre logique de fraîcheur (la base Java
    # se garde plusieurs jours) — l'imposer nous-mêmes casserait le tout premier scan, avant
    # que le cache n'existe (Trivy refuse ce flag tant qu'aucune base n'a jamais été là).
    try:
        resp = subprocess.run(
            [
                "trivy", "image",
                "--cache-dir", cache_dir,
                "--scanners", "vuln",
                "-f", "json",
                "-q",
                reference,
            ],
            capture_output=True, text=True, timeout=timeout, check=True,
        )
    except subprocess.CalledProcessError as e:
        logger.warning("Trivy : scan de '%s' en échec : %s", reference, e.stderr.strip())
        return None
    except subprocess.TimeoutExpired:
        logger.warning("Trivy : scan de '%s' expiré après %ss", reference, timeout)
        return None

    try:
        data = json.loads(resp.stdout)
    except json.JSONDecodeError:
        logger.warning("Trivy : sortie illisible pour '%s'", reference)
        return None

    counts = {sev: 0 for sev in _SEVERITIES}
    for result in data.get("Results") or []:
        for vuln in result.get("Vulnerabilities") or []:
            severity = vuln.get("Severity")
            if severity in counts:
                counts[severity] += 1

    return {"cve_critical": counts["CRITICAL"], "cve_high": counts["HIGH"]}
