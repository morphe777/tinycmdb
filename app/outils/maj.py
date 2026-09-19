"""Mise à jour hebdomadaire des stacks non critiques.

La plupart des images du parc suivent une étiquette flottante (`latest` sur 22 des 35
images) : leur contenu bouge, leur référence non. Les laisser en place, c'est accumuler
des vulnérabilités déjà corrigées en amont — la page Sécurité de la console en fait le
compte. Les mettre à jour à la main, c'est ne jamais le faire.

D'où ce script : ce qui peut se casser sans conséquence se met à jour tout seul, le reste
attend une décision. La frontière est la criticité de l'application, saisie dans la CMDB —
pas une liste tenue à part, qui divergerait le jour même où elle serait écrite.

Ce qu'il fait, et ce qu'il ne fait pas :

- Il agit sur les *stacks*, jamais sur les conteneurs isolés. Un conteneur hors stack n'a
  ni fichier de déploiement ni configuration reproductible : le recréer, c'est risquer de
  perdre ce que personne n'a écrit nulle part.
- Il passe par l'API de Portainer, pas par `docker compose` sur l'hôte. Quinze des dix-sept
  stacks ont leur fichier de déploiement *dans* le volume de Portainer (`/data/compose/N`) :
  les toucher depuis l'hôte laisserait Portainer afficher un état qui n'est plus le vrai.
- Il ne modifie jamais la CMDB. Il la lit.
- Il ne fait rien sans `--appliquer`. Par défaut il dit ce qu'il ferait, et c'est ce que
  fait la première exécution après chaque changement de criticité.

Usage :

    docker exec tinycmdb-collector python -m outils.maj              # simulation
    docker exec tinycmdb-collector python -m outils.maj --appliquer

Et en hebdomadaire, dans la crontab de l'hôte :

    15 4 * * 0 docker exec tinycmdb-collector python -m outils.maj --appliquer

Variables d'environnement, en plus de celles du collecteur :

    PORTAINER_URL         https://portainer.internal.lcl
    PORTAINER_TOKEN       clé d'accès (Portainer : Mon compte -> Access tokens)
    PORTAINER_VERIFY_TLS  false si certificat interne (défaut : true)
    MAJ_CRITICITES        criticités éligibles, séparées par des virgules (défaut : Low)
    MAJ_EXCLUS            noms de stacks à ne jamais toucher, séparés par des virgules
"""

import argparse
import logging
import os
import socket
import sys

import requests

logger = logging.getLogger("cmdb.maj")


class Erreur(Exception):
    pass


def _env(nom, defaut=None, obligatoire=False):
    valeur = os.environ.get(nom, defaut)
    if obligatoire and not valeur:
        raise Erreur(f"{nom} est obligatoire")
    return valeur


def _bool(nom, defaut):
    return os.environ.get(nom, str(defaut)).strip().lower() in ("1", "true", "yes", "on")


def _liste(nom, defaut=""):
    return [m.strip() for m in os.environ.get(nom, defaut).split(",") if m.strip()]


# --------------------------------------------------------------------------- CMDB

def _valeur_select(champ):
    """Un single_select revient comme {id, value, color}, ou None si vide."""
    return (champ or {}).get("value") if isinstance(champ, dict) else None


def _noms_lies(champ):
    """Un link_row revient comme [{id, value}, ...] — `value` est la clé naturelle."""
    return [item.get("value") for item in (champ or []) if item.get("value")]


def lire_cmdb(session, base_url, table_container, table_application):
    """Renvoie (stacks, criticites) :

    - stacks     : {nom de stack: {"apps": {...}, "hotes": {...}, "conteneurs": [...]}}
    - criticites : {nom d'application: criticité}
    """
    def toutes_les_lignes(table):
        lignes, url, params = [], f"{base_url}/api/database/rows/table/{table}/", \
                              {"user_field_names": "true", "size": 200}
        while url:
            resp = session.get(url, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            lignes.extend(data["results"])
            url, params = data.get("next"), None
        return lignes

    criticites = {}
    for ligne in toutes_les_lignes(table_application):
        nom = (ligne.get("Name") or "").strip()
        if nom:
            criticites[nom] = _valeur_select(ligne.get("Criticality"))

    stacks = {}
    for ligne in toutes_les_lignes(table_container):
        nom_stack = (ligne.get("Stack") or "").strip()
        if not nom_stack:
            continue   # conteneur hors stack : hors périmètre, par décision explicite
        entree = stacks.setdefault(nom_stack, {"apps": set(), "hotes": set(), "conteneurs": []})
        entree["apps"].update(_noms_lies(ligne.get("Application - Stack")))
        entree["hotes"].update(_noms_lies(ligne.get("Host")))
        entree["conteneurs"].append({"nom": ligne.get("Name") or "",
                                     "uid": ligne.get("UID") or ""})
    return stacks, criticites


def ma_stack(stacks):
    """La stack qui héberge ce script — à ne jamais redéployer depuis elle-même : le
    conteneur serait détruit au milieu de la boucle, les stacks suivantes jamais traitées,
    et le journal perdu avec lui.

    Le nom d'hôte d'un conteneur est, sauf réglage contraire, le début de son identifiant :
    c'est ce que porte le champ UID de la CMDB.
    """
    moi = socket.gethostname()
    for nom, info in stacks.items():
        for conteneur in info["conteneurs"]:
            if conteneur["uid"].startswith(moi) or conteneur["nom"] == moi:
                return nom
    return None


# --------------------------------------------------------------------------- Portainer

class Portainer:
    def __init__(self, url, token, verify_tls=True, timeout=120):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": token,
                                     "Content-Type": "application/json"})
        self.session.verify = verify_tls

    def _appel(self, methode, chemin, **kwargs):
        resp = self.session.request(methode, self.url + chemin, timeout=self.timeout, **kwargs)
        if resp.status_code >= 400:
            raise Erreur(f"{methode} {chemin} -> {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.content else {}

    def stacks(self):
        """{nom: stack}. Les noms sont uniques par environnement chez Portainer ; en
        pratique ils le sont partout ici, et la CMDB ne connaît de toute façon que le nom
        du projet Compose."""
        return {s["Name"]: s for s in self._appel("GET", "/api/stacks")}

    def redeployer(self, stack):
        """Retire les conteneurs, retélécharge les images, remonte la stack.

        Deux chemins, selon l'origine du fichier de déploiement. Celui d'une stack issue
        d'un dépôt git est retiré du dépôt à chaque fois ; celui d'une stack saisie dans
        Portainer est renvoyé tel qu'il est stocké — avec ses variables d'environnement,
        qu'il faut relire et réémettre : les omettre reviendrait à les effacer.
        """
        sid, eid = stack["Id"], stack["EndpointId"]
        if stack.get("GitConfig"):
            self._appel("PUT", f"/api/stacks/{sid}/git/redeploy?endpointId={eid}",
                        json={"PullImage": True, "Prune": False})
            return "git"
        fichier = self._appel("GET", f"/api/stacks/{sid}/file")
        self._appel("PUT", f"/api/stacks/{sid}?endpointId={eid}",
                    json={"StackFileContent": fichier["StackFileContent"],
                          "Env": stack.get("Env") or [],
                          "PullImage": True, "Prune": False})
        return "fichier"


# --------------------------------------------------------------------------- décision

def selectionner(stacks, criticites, eligibles, exclus):
    """Une stack est retenue si elle porte au moins une application et si toutes ses
    applications sont éligibles.

    « Toutes » et non « au moins une » : une stack partagée entre une application sans
    importance et une autre qui compte est, dans les faits, aussi critique que la seconde.
    Et sans application du tout, on ne sait pas — donc on ne touche pas. Le défaut prudent
    est ici le seul défaut acceptable : se tromper coûte une interruption de service, se
    retenir ne coûte qu'une mise à jour manuelle.
    """
    retenues, ecartees = [], []
    for nom in sorted(stacks):
        apps = sorted(stacks[nom]["apps"])
        niveaux = {app: criticites.get(app) for app in apps}
        if nom in exclus:
            ecartees.append((nom, apps, "exclue explicitement"))
        elif not apps:
            ecartees.append((nom, apps, "aucune application liée dans la CMDB"))
        elif any(n is None for n in niveaux.values()):
            manquantes = [a for a, n in niveaux.items() if n is None]
            ecartees.append((nom, apps, f"criticité non renseignée : {', '.join(manquantes)}"))
        elif all(n in eligibles for n in niveaux.values()):
            retenues.append((nom, apps, "/".join(sorted(set(niveaux.values())))))
        else:
            bloquantes = sorted({n for n in niveaux.values() if n not in eligibles})
            ecartees.append((nom, apps, f"criticité {'/'.join(bloquantes)}"))
    return retenues, ecartees


# --------------------------------------------------------------------------- programme

def main(argv=None):
    args = argparse.ArgumentParser(
        description="Redéploie les stacks non critiques en retéléchargeant leurs images.")
    args.add_argument("--appliquer", action="store_true",
                      help="agit réellement ; sans ce drapeau, le script se contente de dire "
                           "ce qu'il ferait")
    args.add_argument("--stack", action="append", default=[],
                      help="limite à cette stack (répétable) ; la criticité reste vérifiée")
    opts = args.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

    base_url = _env("BASEROW_URL", obligatoire=True).rstrip("/")
    session = requests.Session()
    session.headers.update({"Authorization": f"Token {_env('BASEROW_TOKEN', obligatoire=True)}"})
    session.verify = _bool("BASEROW_VERIFY_TLS", True)
    if not session.verify:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    eligibles = set(_liste("MAJ_CRITICITES", "Low"))
    exclus = set(_liste("MAJ_EXCLUS"))

    stacks, criticites = lire_cmdb(session, base_url,
                                   _env("TABLE_CONTAINER", obligatoire=True),
                                   _env("TABLE_APPLICATION", obligatoire=True))
    mienne = ma_stack(stacks)
    if mienne:
        exclus.add(mienne)
        logger.info("Stack de ce conteneur exclue d'office : %s", mienne)

    retenues, ecartees = selectionner(stacks, criticites, eligibles, exclus)
    if opts.stack:
        demandees = set(opts.stack)
        inconnues = demandees - {n for n, _, _ in retenues} - {n for n, _, _ in ecartees}
        for nom in sorted(inconnues):
            logger.warning("Stack inconnue de la CMDB : %s", nom)
        retenues = [r for r in retenues if r[0] in demandees]
        ecartees = [e for e in ecartees if e[0] in demandees]

    logger.info("Criticités éligibles : %s", ", ".join(sorted(eligibles)) or "(aucune)")
    logger.info("%d stack(s) retenue(s), %d écartée(s)", len(retenues), len(ecartees))
    for nom, apps, motif in ecartees:
        logger.info("  écartée  %-22s %s", nom, motif)
    for nom, apps, niveau in retenues:
        logger.info("  retenue  %-22s %s (%s)", nom, ", ".join(apps), niveau)

    if not retenues:
        return 0
    if not opts.appliquer:
        logger.info("Simulation : rien n'a été fait. Ajouter --appliquer pour redéployer.")
        return 0

    portainer = Portainer(_env("PORTAINER_URL", obligatoire=True).rstrip("/"),
                          _env("PORTAINER_TOKEN", obligatoire=True),
                          verify_tls=_bool("PORTAINER_VERIFY_TLS", True))
    connues = portainer.stacks()

    echecs = 0
    for nom, apps, niveau in retenues:
        stack = connues.get(nom)
        if not stack:
            logger.warning("%s : inconnue de Portainer, ignorée", nom)
            echecs += 1
            continue
        try:
            origine = portainer.redeployer(stack)
            logger.info("%s : redéployée (%s)", nom, origine)
        except Exception as exc:
            # Une stack en échec n'arrête pas les autres : c'est la même règle que dans le
            # collecteur, et pour la même raison — une panne locale ne doit pas se propager
            # en interruption générale de la tâche.
            logger.error("%s : échec du redéploiement — %s", nom, exc)
            echecs += 1

    logger.info("Terminé : %d redéployée(s), %d en échec", len(retenues) - echecs, echecs)
    return 1 if echecs else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Erreur as exc:
        logger.error("%s", exc)
        sys.exit(2)
