"""Canal entre la console et le collecteur.

Ce sont deux processus, dans deux conteneurs, sans lien réseau entre eux : la console
lit Baserow, le collecteur l'écrit, et ils ne se parlent pas. Demander une collecte
immédiate depuis la console demande donc un canal, et un seul besoin le justifie —
« vas-y maintenant » — dans un sens unique, sans donnée, sans réponse attendue.

Un répertoire partagé et des fichiers vides suffisent, et c'est le choix retenu : pas de
port ouvert sur le collecteur, pas de file de messages, pas de second démon. Un signal
posé deux fois ne déclenche qu'une collecte (c'est la présence du fichier qui compte, pas
son nombre), et un signal posé alors que le collecteur est arrêté sera honoré à son
redémarrage plutôt que perdu.

Le collecteur publie en retour son état dans `etat.json` — la console n'avait jusqu'ici
aucun moyen de savoir ce qu'il faisait, seulement d'en déduire l'activité depuis les dates
d'écriture en base.
"""

# Nommé « signaux » et non « signal » : `app/` est dans sys.path, un module `signal.py`
# y masquerait celui de la bibliothèque standard pour tout le processus — uvicorn, qui
# s'en sert pour son arrêt propre, compris.

import json
import logging
import os
import tempfile
import time

logger = logging.getLogger("cmdb.signaux")

DOSSIER = os.environ.get("SIGNAL_DIR", "/signal")

# Deux passes, et deux seulement, parce que leurs durées n'ont rien de comparable :
# l'inventaire interroge Proxmox et les socket-proxies, la sécurité lance Trivy sur chaque
# image du parc, une par une. Les réunir sous un bouton unique ferait attendre dix minutes
# qui que ce soit venu corriger une adresse IP.
#
# Cup — la comparaison des versions disponibles — figure dans les DEUX. Il ne coûte rien :
# un seul appel pour tout le parc, une seconde et demie sur trente-deux images. Le ranger
# derrière le bouton lent obligeait à payer Trivy pour voir qu'une mise à jour vient d'être
# appliquée, ce qui est précisément la question qu'on se pose en relançant une collecte.
PASSES = {
    "inventaire": "nœuds, adresses IP, conteneurs et versions disponibles",
    "securite": "vulnérabilités des images — Trivy les analyse une par une",
}

ETAT = "etat.json"


def _chemin(nom):
    return os.path.join(DOSSIER, nom)


def _fichier(passe):
    return _chemin(f"demande-{passe}")


def disponible():
    """Le canal existe-t-il ? Sans volume partagé monté, tout le reste doit se taire
    plutôt que d'échouer : la CMDB fonctionne sans cette fonction, elle est un confort."""
    return os.path.isdir(DOSSIER)


def demander(passe):
    """Côté console. Vrai si la demande est posée (ou l'était déjà)."""
    if passe not in PASSES or not disponible():
        return False
    try:
        # O_CREAT sans O_EXCL : une demande déjà posée reste une demande, on ne
        # la réécrit pas pour ne pas repousser sa date — c'est elle qui permet
        # d'afficher « demandé il y a 3 minutes, toujours pas pris en compte ».
        fd = os.open(_fichier(passe), os.O_CREAT | os.O_WRONLY, 0o644)
        os.close(fd)
        return True
    except OSError:
        logger.exception("Signal '%s' : écriture impossible dans %s", passe, DOSSIER)
        return False


def demande_posee(passe):
    """Côté console : date de la demande en attente, ou None."""
    try:
        return os.stat(_fichier(passe)).st_mtime
    except OSError:
        return None


def en_attente():
    """Côté collecteur : une demande, quelle qu'elle soit, attend-elle ? Ne consomme
    rien — sert à écourter le sommeil, la consommation a lieu en début de cycle."""
    return any(demande_posee(p) is not None for p in PASSES)


def consommer(passe):
    """Côté collecteur : relève la demande et l'efface, en une opération.

    L'effacement a lieu AVANT la passe, jamais après : une demande arrivée pendant la
    collecte doit en déclencher une autre, puisqu'elle porte sur un état que celle en
    cours n'a pas encore lu.
    """
    try:
        os.unlink(_fichier(passe))
        return True
    except OSError:
        return False


def publier(**champs):
    """Côté collecteur : publie son état, par remplacement atomique — la console peut
    lire à n'importe quel instant, elle ne doit jamais tomber sur un fichier à moitié
    écrit."""
    if not disponible():
        return
    champs["maj"] = time.time()
    try:
        fd, provisoire = tempfile.mkstemp(dir=DOSSIER, prefix=".etat-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(champs, f)
        os.chmod(provisoire, 0o644)
        os.replace(provisoire, _chemin(ETAT))
    except OSError:
        logger.exception("État du collecteur : écriture impossible dans %s", DOSSIER)


def lire():
    """Côté console : l'état publié par le collecteur, ou {} s'il n'a jamais tourné."""
    try:
        with open(_chemin(ETAT), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
