"""Source Cup -> table Images (Available_version, Update_available).

Cup (https://github.com/sergi0g/cup) tourne en sous-processus dans ce conteneur — pas de
serveur séparé, pas de fichier de config partagé. Le binaire (5 Mo) est installé dans
l'image (voir Dockerfile) et appelé en ligne de commande avec la liste d'images à vérifier.

Deux modes d'appel (voir main.py:run_cup_pass, qui décide lequel utiliser par image) :
- `socket=None` -> `-s none`, aucun accès Docker, juste des appels registre. Marche pour
  toute image à tag versionné, et pour un `latest` dont on a substitué la vraie version
  (label OCI) au tag.
- `socket="tcp://host:port"` -> pointe vers le docker-socket-proxy en lecture seule d'UN
  hôte donné (même principe que pour le collecteur, jamais un accès direct). Seul moyen de
  vérifier un `latest` sans label : comparaison par empreinte, pas de version lisible.
"""

import json
import logging
import subprocess

logger = logging.getLogger("cmdb.cup")


def collect(image_references, socket=None, timeout=120):
    """Renvoie (results, rejected).

    `results` : {reference: {"available_version": ..., "update_available": ...}}.
    `available_version` n'est présent que si Cup a trouvé une mise à jour et connaît le
    tag correspondant — jamais le cas en comparaison par empreinte (`socket` fourni),
    Cup ne fait pas la démarche inverse "cette empreinte correspond à quel tag ?".

    `rejected` : liste des références que Cup n'a pas pu traiter du tout (tag qu'il ne sait
    pas interpréter en mode `-s none` — ex. "ihm-latest", un hash de build...). À l'appelant
    de décider quoi en faire (ex. retenter en comparaison par empreinte, voir main.py).

    Cup échoue sur le lot entier dès qu'UNE référence pose problème plutôt que de l'ignorer
    et continuer — on la retire et on retente, sans perdre les autres images du passage.
    """
    refs = sorted(image_references)
    rejected = []
    resp = None
    while refs:
        try:
            resp = subprocess.run(
                ["cup", "-s", socket or "none", "check", "-r", *refs],
                capture_output=True, text=True, timeout=timeout, check=True,
            )
            break
        except subprocess.CalledProcessError as e:
            bad_ref = _find_rejected_reference(e.stderr, refs)
            if not bad_ref:
                logger.warning("Cup en échec sans référence identifiable, abandon : %s", e.stderr.strip())
                return {}, refs
            logger.debug("Cup : référence rejetée ce passage (%s) : %s", bad_ref, e.stderr.strip())
            rejected.append(bad_ref)
            refs.remove(bad_ref)

    if resp is None:
        return {}, rejected
    data = json.loads(resp.stdout)

    results = {}
    for image in data.get("images", []):
        reference = image.get("reference")
        if not reference:
            continue
        result = image.get("result") or {}
        if result.get("error"):
            logger.debug("Cup : %s -> %s", reference, result["error"])
            rejected.append(reference)
            continue
        has_update = result.get("has_update")
        info = result.get("info") or {}

        fields = {}
        if has_update is not None:
            fields["update_available"] = bool(has_update)
        if has_update and info.get("new_tag"):
            fields["available_version"] = info["new_tag"]
        results[reference] = fields
    return results, rejected


def _find_rejected_reference(stderr, candidates):
    """Cup nomme la référence en cause dans son message d'erreur (texte libre, pas de code
    d'erreur structuré) : on se contente de vérifier laquelle des références encore dans le
    lot apparaît dans le message, plutôt que de dépendre du libellé exact."""
    for ref in candidates:
        if ref in stderr:
            return ref
    return None
