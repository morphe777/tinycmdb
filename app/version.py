"""Version de TinyCMDB, et ce qui permet de la rattacher à une image.

Trois informations, et elles ne se valent pas :

- **La version** vient du fichier `VERSION`, versionné dans le dépôt. C'est la seule
  que l'on écrit à la main, et celle qui doit correspondre à l'étiquette git.
- **Le commit** et **la date de construction** sont injectés par le Dockerfile au moment
  de la construction. Eux seuls répondent à la question qui compte : « est-ce que le
  conteneur qui tourne est bien celui que je viens de publier ». Un fichier du dépôt ne
  peut pas y répondre, puisqu'il dit ce qu'il dit quelle que soit l'image.

Hors conteneur — en développement — le commit et la date sont absents, et c'est juste :
il n'y a pas eu de construction.
"""

import os
from pathlib import Path

_FICHIER = Path(__file__).resolve().parent / "VERSION"


def _lire():
    try:
        return _FICHIER.read_text(encoding="utf-8").strip() or "inconnue"
    except OSError:
        return "inconnue"


VERSION = _lire()
COMMIT = os.environ.get("TINYCMDB_COMMIT", "")
CONSTRUITE_LE = os.environ.get("TINYCMDB_BUILD_DATE", "")


def libelle(court=True):
    """« 0.1.3 · 8bc3f7c » en pied de page, avec la date en plus quand on la demande."""
    bouts = [VERSION]
    if COMMIT:
        bouts.append(COMMIT[:7])
    if not court and CONSTRUITE_LE:
        bouts.append(CONSTRUITE_LE)
    return " · ".join(bouts)
