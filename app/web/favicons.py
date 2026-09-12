"""Découverte de l'icône d'une application.

Pourquoi ce module existe : `<origine>/favicon.ico` ne trouve qu'une minorité des icônes.
La plupart des applications déclarent la leur dans le HTML — `<link rel="icon"
href="/assets/logo-a1b2.png">` — avec un chemin versionné qu'aucune convention ne permet
de deviner. C'est pour ça que le navigateur affiche une icône dans son onglet là où la
console n'en trouvait pas : lui lit la page, pas seulement la racine.

Compromis assumé sur le périmètre réseau. La console ne parlait qu'à Baserow ; elle
récupère désormais aussi le HTML des applications listées dans la table Application. La
concession reste bornée :

- seules sont interrogées les URL saisies à la main dans `Application.URL` — jamais une
  adresse venue d'ailleurs, jamais une redirection suivie hors de l'hôte d'origine ;
- seul le HTML est lu, et sur ses 200 premiers kilo-octets. Les octets de l'image, eux,
  ne transitent jamais par ce service : c'est le navigateur qui charge l'icône ensuite ;
- la découverte est désactivable (`WEB_FAVICON_DISCOVERY=false`), et son échec est sans
  conséquence : l'application retombe sur son initiale.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

import requests

logger = logging.getLogger("cmdb.web.favicons")

# <link rel="apple-touch-icon" href="..."> et ses variantes, attributs dans n'importe quel
# ordre. Une expression régulière suffit : on cherche une balise auto-fermante dans un
# en-tête, pas à comprendre le document.
_LINK = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_REL = re.compile(r"""\brel\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_HREF = re.compile(r"""\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_SIZES = re.compile(r"""\bsizes\s*=\s*["']\s*(\d+)""", re.IGNORECASE)

_MAX_HTML = 200_000


def origin_of(url):
    if not url:
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


class FaviconResolver:
    """Résout une icône par origine, et garde le résultat.

    Le cache est volontairement long : une icône ne change pas d'un jour à l'autre, et une
    application éteinte ne doit pas faire payer son délai d'attente à chaque rechargement
    de l'instantané.
    """

    def __init__(self, enabled=True, timeout=4, workers=8, ttl=86400, verify_tls=False):
        self.enabled = enabled
        self.timeout = timeout
        self.workers = workers
        self.ttl = ttl
        self.verify_tls = verify_tls
        self._cache = {}  # origine -> (url_icone | None, horodatage)

    def apply(self, apps):
        """Pose `app.favicon` sur chaque application. Sans découverte, chacune garde le
        `/favicon.ico` de son origine, déjà calculé par le store."""
        if not self.enabled:
            return
        origins = {}
        for app in apps:
            origin = origin_of(app.get("URL") or app.get("URL 2"))
            if origin:
                origins.setdefault(origin, []).append(app)

        a_chercher = [o for o in origins if not self._frais(o)]
        if a_chercher:
            started = time.time()
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for origin, icone in zip(a_chercher, pool.map(self._decouvrir, a_chercher)):
                    self._cache[origin] = (icone, time.time())
            trouvees = sum(1 for o in a_chercher if self._cache[o][0])
            logger.info("Icônes : %d origines interrogées en %.1fs, %d trouvées",
                        len(a_chercher), time.time() - started, trouvees)

        for origin, membres in origins.items():
            icone = self._cache.get(origin, (None, 0))[0]
            for app in membres:
                # L'icône découverte passe en tête ; les emplacements conventionnels
                # restent derrière, en repli pour le navigateur.
                if icone:
                    app.favicon_candidats = [icone] + [c for c in app.favicon_candidats
                                                       if c != icone]
                    app.favicon = icone

    def _frais(self, origin):
        entree = self._cache.get(origin)
        return entree is not None and (time.time() - entree[1]) < self.ttl

    def _decouvrir(self, origin):
        try:
            resp = requests.get(origin, timeout=self.timeout, verify=self.verify_tls,
                                stream=True, headers={"Accept": "text/html"})
            # Pas de raise_for_status : une page de connexion renvoyée en 401 ou 403 reste
            # une vraie page, et déclare son icône comme n'importe quelle autre. Le code de
            # retour dit si l'accès est accordé, pas si le document est exploitable.
            if "html" not in resp.headers.get("Content-Type", ""):
                return None
            html = resp.raw.read(_MAX_HTML, decode_content=True).decode("utf-8", "replace")
        except Exception as exc:
            logger.debug("Icône introuvable pour %s : %s", origin, exc)
            return None
        return self._meilleur_lien(html, origin)

    @staticmethod
    def _meilleur_lien(html, base):
        """Retient le plus grand format déclaré.

        `apple-touch-icon` passe devant à taille égale : c'est presque toujours un PNG carré
        et opaque, là où un `.ico` historique peut être une vignette de 16 pixels.
        """
        meilleur, meilleur_score = None, -1
        for balise in _LINK.findall(html[:_MAX_HTML]):
            rel = _REL.search(balise)
            href = _HREF.search(balise)
            if not rel or not href:
                continue
            rels = rel.group(1).lower().split()
            if not any(r in ("icon", "shortcut", "apple-touch-icon",
                             "apple-touch-icon-precomposed") for r in rels):
                continue
            taille = _SIZES.search(balise)
            score = int(taille.group(1)) if taille else 0
            if "apple-touch-icon" in rels:
                score += 1
            if score > meilleur_score:
                meilleur, meilleur_score = href.group(1).strip(), score
        if not meilleur or meilleur.startswith("data:"):
            return None
        return urljoin(base + "/", meilleur)
