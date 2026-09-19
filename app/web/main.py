"""TinyCMDB — console web au-dessus de Baserow.

Consultation, et modification des seuls champs que le collecteur ne réécrit jamais. La
liste de ces champs est appliquée côté serveur (`ecriture.py`), pas dans les gabarits :
l'interface décide ce qu'elle affiche, elle ne décide pas de ce qui est permis. Tout le
reste du service ne connaît que `fetch_all`.

Le token Baserow vit dans ce process et n'atteint jamais le navigateur — c'est la raison
d'être de ce service plutôt qu'une page statique qui interrogerait Baserow directement.
Un token de base Baserow ne se restreint ni par origine ni par ligne : exposé, il donne
accès à toute la base, pas au seul écran affiché.

Démarrage : `uvicorn web.main:app --host 0.0.0.0 --port 8080` (voir compose.yaml).
"""

import logging
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

import signaux
import version

from . import config, ecriture, schema, store

BASE_DIR = Path(__file__).resolve().parent

cfg = config.load()
logging.basicConfig(level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("cmdb.web")

if not cfg.BASEROW_VERIFY_TLS:
    # Baserow est derrière un certificat interne signé par sa propre autorité. Sans ça,
    # urllib3 écrit un avertissement par requête et le journal devient illisible — donc
    # inutile, donc jamais lu le jour où il contient quelque chose.
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="TinyCMDB", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
data = store.Store(cfg)
redacteur = ecriture.Redacteur(data.client, cfg)

# Chemin d'URL vers type d'objet : l'inverse de store.URL_PREFIX, pour retrouver
# la table visée par un formulaire à partir de l'adresse d'où il vient.
KIND_PAR_PREFIXE = {v: k for k, v in store.URL_PREFIX.items()}


def _fmt_datetime(value):
    return value.astimezone().strftime("%d/%m/%Y %H:%M") if value else "—"


def _fmt_age(value):
    """Âge en clair. Une date absolue ne dit rien d'utile ici : ce qu'on veut savoir, c'est
    si l'information est fraîche ou périmée."""
    if not value:
        return "jamais"
    from datetime import datetime, timezone
    if isinstance(value, (int, float)):
        # Le collecteur publie son état en secondes epoch (JSON n'a pas de type date) ;
        # tout le reste de la console manipule des datetime.
        value = datetime.fromtimestamp(value, timezone.utc)
    seconds = (datetime.now(timezone.utc) - value).total_seconds()
    if seconds < 90:
        return "à l'instant"
    if seconds < 5400:
        return f"il y a {int(seconds // 60)} min"
    if seconds < 172800:
        return f"il y a {int(seconds // 3600)} h"
    return f"il y a {int(seconds // 86400)} j"


def _fmt_hours(value):
    if value is None:
        return "—"
    if abs(value) < 48:
        return f"{value:.0f} h"
    return f"{value / 24:.0f} j"


def _fmt_int(value):
    """Sépare les milliers par une espace fine insécable : 10387 -> 10 387."""
    try:
        return f"{int(float(value)):,}".replace(",", " ")
    except (TypeError, ValueError):
        return value


templates.env.filters["datefr"] = _fmt_datetime
templates.env.filters["ago"] = _fmt_age
templates.env.filters["hours"] = _fmt_hours
templates.env.filters["milliers"] = _fmt_int


# --------------------------------------------------------------------------- rendu

def actif_version(nom):
    """Horodatage d'un fichier statique, ajouté en paramètre d'URL.

    Le code est monté en lecture seule depuis l'hôte : on modifie un fichier et on
    redémarre, sans reconstruire l'image. Sans ce paramètre, le navigateur garderait
    l'ancienne feuille en cache et la modification resterait invisible — exactement le
    genre de faux problème qu'on passe une heure à chercher ailleurs.
    """
    try:
        return int((BASE_DIR / "static" / nom).stat().st_mtime)
    except OSError:
        return 0


def render(request, name, snap, status_code=200, **context):
    context.update({
        "snap": snap,
        "css_version": actif_version("console.css"),
        "js_version": actif_version("console.js"),
        "cache_age": data.age_seconds,
        "version": version.libelle(),
        "version_longue": version.libelle(court=False),
        "ORIGIN_LABELS": schema.ORIGIN_LABELS,
        "ORIGIN_SHORT": schema.ORIGIN_SHORT,
    })
    # Signature Starlette actuelle : la requête d'abord. L'ancienne forme (name, context)
    # est interprétée comme (request, name) et échoue plus loin, sans message clair.
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def snapshot(force=False):
    return data.get(force=force)


# --------------------------------------------------------------------------- fiches

# Champs affichés par type d'objet, dans cet ordre. Le mode de rendu est explicite plutôt
# que déduit du contenu : un champ vide doit s'afficher comme un champ vide de son type,
# pas disparaître ou changer d'allure selon la ligne.
FICHE_FIELDS = {
    "node": [
        ("Type", "select"), ("Status", "select"), ("Roles", "multi"),
        ("Criticality", "select"), ("OS", "text"), ("vmid", "text"),
        ("vCPU", "text"), ("RAM_Gb", "unit:Go"), ("Disk_Gb", "unit:Go"),
        ("Backup", "bool"), ("Guest_agent", "bool"),
        ("Source", "select"), ("Last_seen", "datetime"), ("notes", "long"),
    ],
    "container": [
        ("Status", "select"), ("Type", "select"), ("Stack", "text"),
        ("Service", "text"), ("Restart_policy", "select"), ("Ports", "mono"),
        ("Compose_path", "mono"), ("UID", "mono"),
        ("Last_seen", "datetime"), ("Notes", "long"),
    ],
    "image": [
        ("Registry", "text"), ("Repository", "text"), ("Tag", "text"),
        ("Available_version", "text"), ("Update_available", "bool"),
        ("CVE_critical", "text"), ("CVE_high", "text"), ("Last_scan", "date"),
        ("Digest_local", "mono"), ("Last_seen", "datetime"), ("Notes", "long"),
    ],
    "ipam": [
        ("Address", "mono"), ("Hostname", "text"), ("FQDN", "text"),
        ("MAC", "mono"), ("Vendor", "text"), ("Type", "select"),
        ("Status", "select"), ("Source", "select"),
        ("Last_seen", "datetime"), ("Notes", "long"),
    ],
    "application": [
        ("Status", "select"), ("Criticality", "select"), ("Capability", "multi"),
        ("URL", "url"), ("URL 2", "url"), ("Doc", "url"), ("Notes", "long"),
    ],
    "vlan": [
        ("vlan_id", "text"), ("subnet", "mono"), ("gateway", "mono"),
        ("zone", "select"), ("dhcp", "bool"), ("dhcp_range", "mono"),
        ("dns", "mono"), ("notes", "long"),
    ],
}


def fiche_rows(obj):
    rows = []
    for field, mode in FICHE_FIELDS.get(obj.kind, []):
        rows.append({
            "field": field,
            "mode": mode,
            "value": obj.get(field),
            "origin": obj.origin(field),
        })
    return rows


def origine_uniforme(rows):
    """Renvoie l'origine commune à tous les champs, ou None s'il y en a plusieurs.

    Sur une table entièrement manuelle — VLAN, Application — répéter « manuel » sur chaque
    ligne consomme une colonne pour dire quinze fois la même chose. Le gabarit l'annonce
    alors une seule fois, et récupère la largeur pour le contenu.
    """
    origines = {r["origin"] for r in rows}
    return origines.pop() if len(origines) == 1 else None


def _detail(request, kind, obj_id, edition=False, erreur=None, enregistres=0,
            saisie=None, code=200, cree=0, rattaches=-1):
    snap = snapshot()
    obj = snap.get(kind, obj_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{kind} {obj_id} introuvable")
    rows = fiche_rows(obj)
    # Calculé à chaque affichage, y compris en consultation : c'est ce qui décide de la
    # présence du bouton « Modifier ». Une ligne dont aucun champ n'est à toi ne doit pas
    # proposer une porte qui ne mène nulle part.
    champs = ecriture.formulaire(snap, obj)
    if saisie is not None:
        for descr in champs:
            descr["valeur"] = saisie.get(descr["champ"], descr["valeur"])
    # Le rattachement aux stacks n'est pas un champ de la ligne Application : il est porté
    # par les conteneurs. Il rejoint pourtant les autres dans le même formulaire — sans
    # quoi la fiche aurait deux modes d'édition et deux boutons d'enregistrement, dont un
    # visible en lecture. Ce qui le distingue est une affaire d'écriture, pas d'écran.
    if kind == "application" and ecriture.rattachement_ouvert():
        champs = champs + [ecriture.champ_stacks(snap, obj)]
    return render(request, "fiche.html", snap, obj=obj, rows=rows,
                  origine_commune=origine_uniforme(rows),
                  champs=champs, edition=bool(edition and champs),
                  rattaches=rattaches,
                  erreur=erreur, enregistres=enregistres, cree=cree, status_code=code)


# --------------------------------------------------------------------------- écriture

# Un formulaire HTML sans champ fichier s'envoie en `application/x-www-form-urlencoded`,
# que la bibliothèque standard sait lire. `request.form()` de Starlette exige, lui,
# `python-multipart` — un analyseur de formats composites dont ce service n'a aucun usage
# (Baserow n'accepte de toute façon pas de fichier avec un database token). Dix lignes ici
# évitent une dépendance de plus et un analyseur de plus exposé au navigateur.
TAILLE_MAX_FORMULAIRE = 256 * 1024


async def _formulaire(request):
    type_contenu = (request.headers.get("content-type") or "").split(";")[0].strip()
    if type_contenu != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="formulaire attendu")
    corps = await request.body()
    if len(corps) > TAILLE_MAX_FORMULAIRE:
        raise HTTPException(status_code=413, detail="formulaire trop volumineux")
    # keep_blank_values : un champ texte qu'on vient de vider arrive comme chaîne vide, et
    # doit être distingué d'un champ absent — c'est précisément ce qui efface une valeur.
    return FormData(parse_qsl(corps.decode("utf-8"), keep_blank_values=True))


def _meme_origine(request):
    """Le formulaire vient-il bien d'une page de ce site ?

    Cette console n'a pas d'authentification : elle est protégée par le fait de n'être
    publiée que sur l'IP du VLAN management. Rien n'empêche en revanche une page tierce,
    ouverte dans le même navigateur, de poster vers elle. Un contrôle d'origine coûte
    quatre lignes et ferme ce cas.
    """
    origine = request.headers.get("origin")
    if origine:
        return urlsplit(origine).netloc == request.url.netloc
    referer = request.headers.get("referer")
    if referer:
        return urlsplit(referer).netloc == request.url.netloc
    # Ni Origin ni Referer : un client en ligne de commande, pas un navigateur piégé.
    return True


# --------------------------------------------------------------------------- routes

@app.get("/")
def dashboard(request: Request):
    snap = snapshot()
    return render(request, "dashboard.html", snap)


@app.get("/file/{key}")
def queue_detail(request: Request, key: str):
    snap = snapshot()
    queue = snap.queue(key)
    if queue is None:
        raise HTTPException(status_code=404, detail="file inconnue")
    return render(request, "file.html", snap, queue=queue)


def _vue_infra(request, nouveau=False, creation_erreur=None, creation=None, code=200):
    snap = snapshot()
    creation_champs = ecriture.formulaire_creation(snap, "node")
    if creation is not None:
        for descr in creation_champs:
            descr["valeur"] = creation.get(descr["champ"], descr["valeur"])
    return render(request, "infra.html", snap,
                  creation_champs=creation_champs,
                  creation_ouverte=bool(nouveau or creation_erreur),
                  creation_erreur=creation_erreur, status_code=code)


@app.get("/infra")
def infra(request: Request, nouveau: int = Query(0)):
    return _vue_infra(request, nouveau=nouveau)


@app.get("/securite")
def securite(request: Request, tri: str = Query("cve")):
    snap = snapshot()
    images = list(snap.images)
    if tri == "maj":
        images.sort(key=lambda i: (not i.get("Update_available"), i.name.lower()))
    elif tri == "nom":
        images.sort(key=lambda i: i.name.lower())
    elif tri == "scan":
        images.sort(key=lambda i: (i.get("Last_scan") or "", i.name.lower()))
    return render(request, "securite.html", snap, images=images, tri=tri)


def _vue_applications(request, nouvelle=False, creation_erreur=None, creation=None, code=200):
    snap = snapshot()
    apps = sorted(snap.apps, key=lambda a: (-a.cve_critical, a.name.lower()))
    creation_champs = ecriture.formulaire_creation(snap, "application")
    if creation is not None:
        for descr in creation_champs:
            descr["valeur"] = creation.get(descr["champ"], descr["valeur"])
    return render(request, "applications.html", snap, apps=apps,
                  creation_champs=creation_champs,
                  creation_ouverte=bool(nouvelle or creation_erreur),
                  creation_erreur=creation_erreur, status_code=code)


@app.get("/applications")
def applications(request: Request, nouvelle: int = Query(0)):
    return _vue_applications(request, nouvelle=nouvelle)


@app.get("/stacks")
def stacks(request: Request):
    snap = snapshot()
    return render(request, "stacks.html", snap,
                  hors_stack=[c for c in snap.containers if not c.stack])


@app.get("/stack/{key:path}")
def stack_detail(request: Request, key: str, rattaches: int = Query(-1),
                 edition: int = Query(0), erreur: str = Query("")):
    snap = snapshot()
    stack = snap.stacks_by_key.get(key)
    if stack is None:
        raise HTTPException(status_code=404, detail="stack introuvable")
    modifiable = ecriture.rattachement_ouvert()
    return render(request, "stack.html", snap, stack=stack,
                  rattachement=ecriture.champ_applications(snap, stack) if modifiable else None,
                  edition=bool(edition and modifiable),
                  rattaches=rattaches, erreur=erreur)


@app.get("/ipam")
def ipam(request: Request):
    snap = snapshot()
    return render(request, "ipam.html", snap, orphans=[ip for ip in snap.ips if not ip.vlan])


def _detail_vlan(request, obj_id, edition=False, erreur=None, enregistres=0,
                 saisie=None, code=200, nouvelle="", creation_erreur=None, creation=None):
    """Le VLAN a son propre gabarit — la grille d'occupation n'a d'équivalent nulle part
    ailleurs — mais le même formulaire que les autres fiches."""
    snap = snapshot()
    vlan = snap.get("vlan", obj_id)
    if vlan is None:
        raise HTTPException(status_code=404, detail="VLAN introuvable")
    rows = fiche_rows(vlan)
    champs = ecriture.formulaire(snap, vlan)
    if saisie is not None:
        for descr in champs:
            descr["valeur"] = saisie.get(descr["champ"], descr["valeur"])
    # Formulaire de création d'adresse, rendu une fois par page et non par case : les 256
    # cases ne diffèrent que par l'adresse, que le clic vient y déposer.
    creation_champs = ecriture.formulaire_creation(snap, "ipam")
    if creation is not None:
        for descr in creation_champs:
            descr["valeur"] = creation.get(descr["champ"], descr["valeur"])
    elif nouvelle:
        for descr in creation_champs:
            if descr["champ"] == "Address":
                descr["valeur"] = nouvelle

    return render(request, "vlan.html", snap, vlan=vlan, grid=store.vlan_grid(vlan),
                  rows=rows, origine_commune=origine_uniforme(rows),
                  champs=champs, edition=bool(edition and champs),
                  erreur=erreur, enregistres=enregistres,
                  creation_champs=creation_champs,
                  creation_ouverte=bool(nouvelle or creation_erreur),
                  creation_erreur=creation_erreur, status_code=code)


@app.get("/vlan/{vlan_id}")
def vlan_detail(request: Request, vlan_id: int, edition: int = Query(0),
                enregistres: int = Query(-1), nouvelle: str = Query("")):
    return _detail_vlan(request, vlan_id, edition=edition, enregistres=enregistres,
                        nouvelle=nouvelle)


@app.get("/recherche")
def recherche(request: Request, q: str = Query("")):
    snap = snapshot()
    return render(request, "recherche.html", snap, q=q, results=snap.search(q))


def _collecte(demande=""):
    """Ce que la console sait du collecteur : son état publié, et les demandes encore en
    attente. Le canal peut être absent (volume non monté) — la page doit alors se passer
    du bloc plutôt que d'échouer."""
    return {
        "canal": signaux.disponible(),
        "etat": signaux.lire(),
        "passes": signaux.PASSES,
        "attente": {p: signaux.demande_posee(p) for p in signaux.PASSES},
        "demande": demande if demande in signaux.PASSES else "",
    }


@app.get("/etat")
def etat(request: Request, demande: str = Query("")):
    snap = snapshot()
    composants = snap.controls(erreur_rechargement=data.derniere_erreur)
    return render(request, "etat.html", snap, sources=snap.sources_state(),
                  expiring=snap.expiring(), composants=composants,
                  collecte=_collecte(demande),
                  # Une stack sans application n'est pas une anomalie du collecteur : c'est
                  # une saisie qui manque. Elle a pourtant sa place ici, sur l'écran qu'on
                  # ouvre pour savoir si l'inventaire est fiable — et la conséquence est
                  # concrète, cette stack ne se met jamais à jour toute seule.
                  orphelines=[st for st in snap.stacks if not st.apps],
                  en_defaut=[c for c in composants if c["verdict"] in ("warn", "ko")])


@app.get("/noeud/{obj_id}")
def node_detail(request: Request, obj_id: int, edition: int = Query(0),
                enregistres: int = Query(-1)):
    return _detail(request, "node", obj_id, edition=edition, enregistres=enregistres)


@app.get("/conteneur/{obj_id}")
def container_detail(request: Request, obj_id: int, edition: int = Query(0),
                     enregistres: int = Query(-1)):
    return _detail(request, "container", obj_id, edition=edition, enregistres=enregistres)


@app.get("/image/{obj_id}")
def image_detail(request: Request, obj_id: int, edition: int = Query(0),
                 enregistres: int = Query(-1)):
    return _detail(request, "image", obj_id, edition=edition, enregistres=enregistres)


@app.get("/ip/{obj_id}")
def ip_detail(request: Request, obj_id: int, edition: int = Query(0),
              enregistres: int = Query(-1), cree: int = Query(0)):
    return _detail(request, "ipam", obj_id, edition=edition, enregistres=enregistres,
                   cree=cree)


@app.get("/application/{obj_id}")
def application_detail(request: Request, obj_id: int, edition: int = Query(0),
                       enregistres: int = Query(-1), cree: int = Query(0),
                       rattaches: int = Query(-1)):
    return _detail(request, "application", obj_id, edition=edition, enregistres=enregistres,
                   cree=cree, rattaches=rattaches)


@app.post("/modifier/{prefixe}/{obj_id}")
async def modifier(request: Request, prefixe: str, obj_id: int):
    """Enregistre une saisie sur une ligne existante.

    Une seule route pour les six types : ce qui change d'un type à l'autre est la liste
    des champs, et elle est déclarée dans `ecriture.py`. Un gestionnaire par table ne
    ferait que recopier six fois la même séquence.
    """
    kind = KIND_PAR_PREFIXE.get(prefixe)
    if kind is None:
        raise HTTPException(status_code=404, detail="type inconnu")
    if not _meme_origine(request):
        raise HTTPException(status_code=403, detail="origine du formulaire non reconnue")

    snap = snapshot()
    obj = snap.get(kind, obj_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{kind} {obj_id} introuvable")

    donnees = await _formulaire(request)

    def reafficher(message, code):
        """Réaffichage du formulaire tel qu'il vient d'être rempli. Le vider pour une
        option mal choisie obligerait à retaper le reste."""
        saisie = ecriture.saisie_brute(snap, ecriture.formulaire(snap, obj), donnees)
        if kind == "vlan":
            return _detail_vlan(request, obj_id, edition=True, erreur=message,
                                saisie=saisie, code=code)
        return _detail(request, kind, obj_id, edition=True, erreur=message,
                       saisie=saisie, code=code)

    # Le rattachement aux stacks voyage dans le même formulaire mais ne s'écrit pas sur
    # la même ligne : il est porté par les conteneurs. Il part donc d'abord, et il est
    # déclaré à `appliquer` pour que celle-ci ne le prenne pas pour un champ intrus.
    hors_ligne = ("stack",) if kind == "application" else ()
    rattaches = 0
    try:
        if hors_ligne and "stack" in set(donnees.getlist("soumis")):
            rattaches = redacteur.rattacher_application(snap, obj, donnees)
        changements = redacteur.appliquer(snap, obj, donnees, hors_ligne=hors_ligne)
    except ecriture.EcritureRefusee as exc:
        return reafficher(str(exc), 400)
    except ecriture.EcritureImpossible as exc:
        return reafficher(f"Baserow a refusé l'enregistrement : {exc}", 502)

    if changements or rattaches:
        # L'instantané en mémoire est antérieur à l'écriture : sans ce rechargement, la
        # fiche réaffichée montrerait l'ancienne valeur pendant une minute, et donnerait
        # à croire que l'enregistrement a échoué.
        snapshot(force=True)

    # Redirection après POST : sans elle, un rafraîchissement du navigateur renvoie le
    # formulaire une seconde fois.
    suffixe = f"?enregistres={len(changements)}"
    if hors_ligne:
        suffixe += f"&rattaches={rattaches}"
    return RedirectResponse(f"/{prefixe}/{obj_id}{suffixe}", status_code=303)


@app.post("/creer/ip")
async def creer_ip(request: Request):
    """Crée une adresse réservée à la main, depuis la grille d'un VLAN.

    En cas de refus, la page du VLAN est réaffichée avec le formulaire ouvert et rempli :
    l'adresse est retrouvée par le champ caché `vlan`, puisque la ligne n'existe pas encore
    et ne peut pas servir de point de retour.
    """
    if not _meme_origine(request):
        raise HTTPException(status_code=403, detail="origine du formulaire non reconnue")
    donnees = await _formulaire(request)
    try:
        vlan_id = int(donnees.get("vlan") or 0)
    except ValueError:
        raise HTTPException(status_code=400, detail="VLAN non précisé")

    snap = snapshot()
    try:
        nouvel_id = redacteur.creer(snap, "ipam", donnees)
    except (ecriture.EcritureRefusee, ecriture.EcritureImpossible) as exc:
        message = str(exc) if isinstance(exc, ecriture.EcritureRefusee) \
            else f"Baserow a refusé la création : {exc}"
        code = 400 if isinstance(exc, ecriture.EcritureRefusee) else 502
        return _detail_vlan(request, vlan_id, creation_erreur=message, code=code,
                            creation=ecriture.saisie_brute(
                                snap, ecriture.formulaire_creation(snap, "ipam"), donnees))
    snapshot(force=True)
    return RedirectResponse(f"/ip/{nouvel_id}?cree=1", status_code=303)


@app.post("/creer/application")
async def creer_application(request: Request):
    if not _meme_origine(request):
        raise HTTPException(status_code=403, detail="origine du formulaire non reconnue")
    donnees = await _formulaire(request)
    snap = snapshot()
    try:
        nouvel_id = redacteur.creer(snap, "application", donnees)
    except (ecriture.EcritureRefusee, ecriture.EcritureImpossible) as exc:
        message = str(exc) if isinstance(exc, ecriture.EcritureRefusee) \
            else f"Baserow a refusé la création : {exc}"
        code = 400 if isinstance(exc, ecriture.EcritureRefusee) else 502
        return _vue_applications(request, creation_erreur=message, code=code,
                                 creation=ecriture.saisie_brute(
                                     snap, ecriture.formulaire_creation(snap, "application"), donnees))
    snapshot(force=True)
    return RedirectResponse(f"/application/{nouvel_id}?cree=1", status_code=303)


@app.post("/creer/noeud")
async def creer_noeud(request: Request):
    """Ajoute un équipement que le collecteur ne peut pas découvrir.

    La ligne porte `Source = Manual`, posé par `ecriture._derives()` et non par ce
    formulaire : c'est une conséquence du geste, pas une option. Sans elle, le portable
    saisi ce matin disparaîtrait à la passe suivante, faute d'être visible sur un
    hyperviseur.
    """
    if not _meme_origine(request):
        raise HTTPException(status_code=403, detail="origine du formulaire non reconnue")
    donnees = await _formulaire(request)
    snap = snapshot()
    try:
        nouvel_id = redacteur.creer(snap, "node", donnees)
    except (ecriture.EcritureRefusee, ecriture.EcritureImpossible) as exc:
        message = str(exc) if isinstance(exc, ecriture.EcritureRefusee) \
            else f"Baserow a refusé la création : {exc}"
        code = 400 if isinstance(exc, ecriture.EcritureRefusee) else 502
        return _vue_infra(request, creation_erreur=message, code=code,
                          creation=ecriture.saisie_brute(
                              snap, ecriture.formulaire_creation(snap, "node"), donnees))
    snapshot(force=True)
    return RedirectResponse(f"/noeud/{nouvel_id}?cree=1", status_code=303)


@app.post("/rafraichir")
def rafraichir(request: Request):
    """Force un rechargement immédiat. Utile après avoir corrigé quelque chose dans
    Baserow sans vouloir attendre l'expiration du cache.

    Le retour se fait sur la page d'où l'on vient, mais uniquement si c'est un chemin de
    ce site : renvoyer l'utilisateur vers une URL fournie par un en-tête, c'est offrir une
    redirection ouverte à qui sait la déclencher.
    """
    snapshot(force=True)
    referer = request.headers.get("referer") or ""
    retour = "/"
    if referer and _meme_origine(request):
        parsed = urlsplit(referer)
        if parsed.path.startswith("/"):
            retour = urlunsplit(("", "", parsed.path, parsed.query, ""))
    return RedirectResponse(retour, status_code=303)


@app.post("/rattacher/stack")
async def rattacher_stack(request: Request):
    """Pose les applications servies sur tous les conteneurs d'une stack.

    La clé de la stack voyage dans le formulaire et non dans le chemin : elle contient une
    barre oblique (hôte/nom), et la faire traverser une route la rendrait ambiguë pour un
    gain nul.
    """
    if not _meme_origine(request):
        raise HTTPException(status_code=403, detail="origine du formulaire non reconnue")
    donnees = await _formulaire(request)
    snap = snapshot()
    stack = snap.stacks_by_key.get(donnees.get("cle") or "")
    if stack is None:
        raise HTTPException(status_code=404, detail="stack introuvable")
    try:
        ecrits = redacteur.rattacher_stack(snap, stack, donnees)
    except (ecriture.EcritureRefusee, ecriture.EcritureImpossible) as exc:
        return RedirectResponse(f"{stack.url}?edition=1&erreur={quote(str(exc))}",
                                status_code=303)
    if ecrits:
        snapshot(force=True)
    return RedirectResponse(f"{stack.url}?rattaches={ecrits}", status_code=303)


@app.post("/rattacher/application/{obj_id}")
async def rattacher_application(request: Request, obj_id: int):
    """Pose cette application sur les conteneurs des stacks cochées, et la retire des
    autres — elle seule, jamais les applications voisines."""
    if not _meme_origine(request):
        raise HTTPException(status_code=403, detail="origine du formulaire non reconnue")
    donnees = await _formulaire(request)
    snap = snapshot()
    app_obj = snap.get("application", obj_id)
    if app_obj is None:
        raise HTTPException(status_code=404, detail="application introuvable")
    try:
        ecrits = redacteur.rattacher_application(snap, app_obj, donnees)
    except (ecriture.EcritureRefusee, ecriture.EcritureImpossible) as exc:
        return _detail(request, "application", obj_id, erreur=str(exc), code=400)
    if ecrits:
        snapshot(force=True)
    return RedirectResponse(f"/application/{obj_id}?rattaches={ecrits}", status_code=303)


@app.post("/collecter/{passe}")
def collecter(request: Request, passe: str):
    """Demande au collecteur de repasser tout de suite, sans attendre son intervalle.

    La console n'écrit rien dans Baserow ici et n'attend pas le résultat : elle pose une
    demande, le collecteur la relève dans les deux secondes et travaille à son rythme.
    Attendre la fin de la collecte dans la requête HTTP ferait une page qui tourne dix
    minutes, et un redémarrage de la console pendant ce temps annulerait tout.
    """
    if not _meme_origine(request):
        raise HTTPException(status_code=403, detail="origine du formulaire non reconnue")
    if passe not in signaux.PASSES:
        raise HTTPException(status_code=404, detail="passe inconnue")
    if not signaux.demander(passe):
        raise HTTPException(status_code=503,
                            detail="canal indisponible : le volume partagé avec le "
                                   "collecteur n'est pas monté")
    return RedirectResponse(f"/etat?demande={passe}", status_code=303)


@app.exception_handler(store.BaserowIndisponible)
def baserow_indisponible(request: Request, exc: store.BaserowIndisponible):
    """Baserow hors service au démarrage à froid : une page qui désigne la vraie cause
    plutôt qu'une trace d'erreur, qui ferait chercher le défaut dans la console."""
    logger.warning("Instantané indisponible : %s", exc)
    return templates.TemplateResponse(
        request, "indisponible.html",
        {"url": cfg.BASEROW_URL, "detail": str(exc),
         "css_version": actif_version("console.css"),
         "js_version": actif_version("console.js"),
         "version": version.libelle()},
        status_code=503)


@app.get("/livez", response_class=PlainTextResponse)
def livez():
    """Le process répond — rien de plus, et c'est voulu.

    C'est cette sonde que surveille Docker. `/healthz` charge l'inventaire, donc échoue
    quand Baserow est indisponible : l'utiliser ferait redémarrer la console en boucle
    pour une panne qui n'est pas la sienne, et qu'un redémarrage n'arrange pas.
    """
    return "ok"


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    """Le service est-il en mesure de répondre utilement — Baserow compris."""
    snap = snapshot()
    return f"ok {len(snap.nodes)} noeuds {len(snap.containers)} conteneurs"
