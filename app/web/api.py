"""API REST de TinyCMDB, sous /api/v1.

Toute requête présente un jeton créé dans Administration → Jetons :
`Authorization: Bearer tcmdb_…`. Un jeton *lecture* ouvre les GET ; un jeton *écriture*
ouvre en plus les modifications et les créations, soumises exactement aux règles des
formulaires de l'interface — seuls les champs manuels s'écrivent, et chaque écriture
entre au journal sous le nom du jeton.

L'API ne supprime rien : la suppression d'une ligne reste un geste de l'interface.

La documentation interactive est servie par FastAPI, sans jeton : /api/v1/docs, et le
schéma OpenAPI sous /api/v1/openapi.json. Ils décrivent les routes, pas l'inventaire — le
bouton « Authorize » de la page permet ensuite d'essayer chaque route avec un jeton.
"""

import json

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from cmdb.depot import BaseInaccessible
from fastapi.responses import JSONResponse, PlainTextResponse

import version

from . import ecriture, exposition, store

TYPES_DOC = "noeud, conteneur, image, ip, vlan, application, cpu, composant"

INVITE = ("Jeton requis : en-tête « Authorization: Bearer <jeton> ». Les jetons se créent "
          "dans Administration → Jetons de la console.")


def exiger_jeton(data, request, droit="lecture"):
    entete = request.headers.get("authorization") or ""
    secret = entete[7:].strip() if entete.lower().startswith("bearer ") else None
    jeton = data.depot.verifier_jeton(secret) if secret else None
    if jeton is None:
        raise HTTPException(status_code=401, detail=INVITE,
                            headers={"WWW-Authenticate": 'Bearer realm="tinycmdb"'})
    if droit == "ecriture" and jeton["droits"] != "ecriture":
        raise HTTPException(status_code=403,
                            detail=f"Le jeton « {jeton['nom']} » est en lecture seule.")
    return jeton


def stacks_v0(snap):
    """Forme historique de /api/stacks, gardée à l'identique pour ses lecteurs."""
    return [{
        "nom": st.name,
        "cle": st.key,
        "hote": st.host.name if st.host else None,
        "chemin": st.compose_path,
        "conteneurs": [c.name for c in st.containers],
        "applications": [{"nom": a.name, "criticite": a.sel("Criticality")}
                         for a in sorted(st.apps, key=lambda a: a.name.lower())],
    } for st in sorted(snap.stacks, key=lambda st: st.key.lower())]


def creer(data, redacteur):
    api = FastAPI(
        title="TinyCMDB",
        version=version.VERSION,
        description=("Inventaire d'infrastructure : nœuds, conteneurs, stacks, images, "
                     "adresses IP, VLAN, applications, processeurs, composants matériels."
                     "\n\n" + INVITE),
        docs_url="/docs", redoc_url=None, openapi_url="/openapi.json",
    )

    @api.exception_handler(BaseInaccessible)
    @api.exception_handler(store.BaseIndisponible)
    def base_inaccessible(request: Request, exc: Exception):
        return JSONResponse({"detail": f"base indisponible : {exc}"}, status_code=503)

    def lecture(request: Request):
        return exiger_jeton(data, request, "lecture")

    def ecriture_(request: Request):
        return exiger_jeton(data, request, "ecriture")

    def snap():
        return data.get()

    def _objet(type_, ident):
        try:
            kind = exposition.kind_de(type_)
        except exposition.Introuvable as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        s = snap()
        obj = s.get(kind, ident)
        if obj is None:
            raise HTTPException(status_code=404, detail=f"{type_} {ident} introuvable")
        return s, obj

    def _refus(exc):
        if isinstance(exc, ecriture.EcritureRefusee):
            raise HTTPException(status_code=400, detail=str(exc))
        raise HTTPException(status_code=503, detail=f"La base a refusé l'écriture : {exc}")

    @api.get("/", summary="Point d'entrée : version, types, liens utiles")
    def index(jeton=Depends(lecture)):
        return {
            "application": "TinyCMDB", "version": version.VERSION,
            "jeton": {"nom": jeton["nom"], "droits": jeton["droits"]},
            "types": list(exposition.TYPES),
            "liens": {"guide": "/api/v1/guide", "schema": "/api/v1/schema",
                      "journal": "/api/v1/journal", "capacite": "/api/v1/capacite",
                      "contexte": "/api/v1/contexte/{type}/{id}",
                      "documentation": "/api/v1/docs", "mcp": "/mcp"},
        }

    @api.get("/guide", response_class=PlainTextResponse,
             summary="Mode d'emploi (le même que celui du serveur MCP), en Markdown")
    def guide(jeton=Depends(lecture)):
        return exposition.guide()

    @api.get("/schema", summary="Tables, champs, types, provenance et options")
    def schema(type: str | None = Query(None, description=TYPES_DOC),
               jeton=Depends(lecture)):
        try:
            return exposition.schema_public(snap(), type)
        except exposition.Introuvable as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @api.get("/recherche", summary="Recherche plein texte dans tout l'inventaire")
    def recherche(q: str = Query(..., min_length=2), jeton=Depends(lecture)):
        return [exposition.resume(o) for o in snap().search(q)]

    @api.get("/stacks", summary="Les stacks Docker Compose, leurs applications et leur criticité")
    def stacks(jeton=Depends(lecture)):
        s = snap()
        return [exposition.stack(s, st) for st in sorted(s.stacks, key=lambda st: st.key.lower())]

    @api.get("/stacks/{cle:path}", summary="Une stack, par sa clé « hôte/nom »")
    def une_stack(cle: str, jeton=Depends(lecture)):
        s = snap()
        st = s.stacks_by_key.get(cle)
        if st is None:
            raise HTTPException(status_code=404, detail=f"stack {cle} introuvable")
        return exposition.stack(s, st)

    @api.put("/stacks/{cle:path}/applications",
             summary="Fixe les applications servies par tous les conteneurs d'une stack")
    def rattacher(cle: str, corps: dict = Body(..., examples=[{"applications": [3, 7]}]),
                  jeton=Depends(ecriture_)):
        s = snap()
        st = s.stacks_by_key.get(cle)
        if st is None:
            raise HTTPException(status_code=404, detail=f"stack {cle} introuvable")
        try:
            n = exposition.rattacher_stack(s, redacteur, st, corps.get("applications"),
                                           f"api:{jeton['nom']}")
        except (ecriture.EcritureRefusee, ecriture.EcritureImpossible) as exc:
            _refus(exc)
        s = data.get(force=True)
        return {"conteneurs_modifies": n, "stack": exposition.stack(s, s.stacks_by_key[cle])}

    @api.get("/a-traiter", summary="Ce qui demande une action, du plus urgent au moins urgent")
    def a_traiter(jeton=Depends(lecture)):
        return exposition.a_traiter(snap())

    @api.get("/sursis", summary="Lignes que le collecteur ne revoit plus, avant leur suppression")
    def sursis(jeton=Depends(lecture)):
        return exposition.sursis(snap())

    @api.get("/etat", summary="État du dispositif : collecteur, base, sauvegarde, sources")
    def etat(jeton=Depends(lecture)):
        return exposition.etat(snap())

    @api.get("/journal", summary="Historique des écritures, du plus récent au plus ancien")
    def journal(type: str | None = Query(None, description=TYPES_DOC),
                id: int | None = Query(None, description="identifiant de l'objet (avec type)"),
                acteur: str | None = Query(None, description="interface, collecteur, api:<jeton>, mcp:<jeton>…"),
                depuis: str | None = Query(None, description="date ISO 8601 : entrées postérieures seulement"),
                champ: str | None = Query(None, description="nom exact d'un champ"),
                limite: int = Query(50, ge=1, le=500), jeton=Depends(lecture)):
        try:
            return exposition.journal(data.depot, type, id, acteur, limite, depuis, champ)
        except exposition.Introuvable as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @api.get("/capacite", summary="Par hyperviseur : matériel, ressources promises aux invités, reste")
    def capacite(jeton=Depends(lecture)):
        return exposition.capacite(snap())

    @api.get("/contexte/stack/{cle:path}", summary="Une stack et toute sa chaîne : hôte, hyperviseur, matériel")
    def contexte_stack(cle: str, jeton=Depends(lecture)):
        s = snap()
        st = s.stacks_by_key.get(cle)
        if st is None:
            raise HTTPException(status_code=404, detail=f"stack {cle} introuvable")
        return exposition.contexte_stack(s, st)

    @api.get("/contexte/{type}/{ident}",
             summary="Un objet et tout son voisinage en un appel : hôte, matériel, invités, applications")
    def contexte(type: str, ident: int, jeton=Depends(lecture)):
        s, obj = _objet(type, ident)
        return exposition.contexte(s, obj)

    @api.get("/{type}", summary="Liste des objets d'un type")
    def liste(type: str, q: str | None = Query(None, description="filtre texte"),
              filtres: str | None = Query(None, description='JSON {champ: valeur}, ex. {"Arch": "arm64", "RAM_Gb": ">=16"}'),
              champs: str | None = Query(None, description="champs à rendre, séparés par des virgules"),
              vides: bool = Query(True, description="rendre aussi les champs vides"),
              limite: int = Query(200, ge=1, le=1000), depart: int = Query(0, ge=0),
              jeton=Depends(lecture)):
        try:
            kind = exposition.kind_de(type)
        except exposition.Introuvable as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        try:
            conditions = json.loads(filtres) if filtres else None
        except ValueError:
            raise HTTPException(status_code=400, detail="`filtres` : JSON invalide")
        choisis = [c.strip() for c in champs.split(",") if c.strip()] if champs else None
        s = snap()
        try:
            total, objets = exposition.lister(s, kind, q, limite, depart, conditions)
        except exposition.Introuvable as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"total": total, "depart": depart,
                "objets": [exposition.ligne_liste(s, o, vides=vides, choisis=choisis)
                           for o in objets]}

    @api.get("/{type}/{ident}", summary="Un objet, avec ses champs, leur provenance et ses liens")
    def detail(type: str, ident: int, jeton=Depends(lecture)):
        s, obj = _objet(type, ident)
        return exposition.objet(s, obj)

    @api.patch("/{type}/{ident}", summary="Modifie des champs manuels d'un objet")
    def modifier(type: str, ident: int,
                 corps: dict = Body(..., examples=[{"notes": "Remplacé en 2026", "Criticality": "High"}]),
                 jeton=Depends(ecriture_)):
        s, obj = _objet(type, ident)
        try:
            changements = exposition.modifier(s, redacteur, obj, corps, f"api:{jeton['nom']}")
        except (ecriture.EcritureRefusee, ecriture.EcritureImpossible) as exc:
            _refus(exc)
        s = data.get(force=True)
        return {"changements": changements, "objet": exposition.objet(s, s.get(obj.kind, ident))}

    @api.post("/{type}", status_code=201,
              summary="Crée un objet (nœud, adresse, VLAN, application, processeur, composant)")
    def creer_objet(type: str,
                    corps: dict = Body(..., examples=[{"Address": "192.168.10.42", "MAC": "AA:BB:CC:DD:EE:FF"}]),
                    jeton=Depends(ecriture_)):
        try:
            kind = exposition.kind_de(type)
        except exposition.Introuvable as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        try:
            ident = exposition.creer(snap(), redacteur, kind, corps, f"api:{jeton['nom']}")
        except (ecriture.EcritureRefusee, ecriture.EcritureImpossible) as exc:
            _refus(exc)
        s = data.get(force=True)
        return exposition.objet(s, s.get(kind, ident))

    return api
