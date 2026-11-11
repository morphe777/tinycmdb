"""Ce que l'API et le serveur MCP montrent de l'inventaire, et comment ils y écrivent.

Les deux portes partagent ce module : un objet se décrit de la même façon en JSON pour
une application et pour un agent, et une écriture suit le même chemin — celui des
formulaires de l'interface (`ecriture.py`). Les règles ne sont donc pas réécrites ici :
une saisie JSON est traduite en formulaire, puis confiée au même rédacteur.
"""

from datetime import datetime, timezone
from pathlib import Path

from cmdb import modele
from starlette.datastructures import FormData

from . import ecriture, schema, store

# Type d'objet, du nom public (celui des URL de l'interface) au nom interne.
TYPES = {prefixe: kind for kind, prefixe in store.URL_PREFIX.items()}
PREFIXES = dict(store.URL_PREFIX)

GUIDE = Path(__file__).resolve().parent / "guide_mcp.md"


class Introuvable(Exception):
    pass


def kind_de(type_public):
    kind = TYPES.get((type_public or "").strip().lower())
    if kind is None:
        raise Introuvable(f"type inconnu : « {type_public} ». Types : {', '.join(TYPES)}")
    return kind


def guide():
    return GUIDE.read_text(encoding="utf-8")


def _valeur(v):
    if isinstance(v, dict):
        return v.get("value")
    if isinstance(v, list):
        return [x.get("value") if isinstance(x, dict) else x for x in v]
    return v


def resume(obj):
    if obj is None:
        return None
    if obj.kind == "stack":
        return {"type": "stack", "cle": obj.key, "nom": obj.name,
                "hote": obj.host.name if obj.host else None}
    return {"type": PREFIXES[obj.kind], "id": obj.id, "nom": obj.name, "url": obj.url}


def _resumes(objets):
    return [resume(o) for o in objets if o is not None]


def champs_scalaires(snap, kind):
    """Champs d'une table hors relations, dans l'ordre du modèle, puis les personnalisés."""
    noms = [c.nom for c in modele.table(kind).champs if c.type not in ("lien", "liens", "inverse")]
    return noms + [p["nom"] for p in snap.perso.get(kind, [])]


def objet(snap, obj):
    """Description complète d'un objet : ses champs, leur provenance, ses relations, et ce
    qu'on peut y modifier."""
    champs = {nom: _valeur(obj.get(nom)) for nom in champs_scalaires(snap, obj.kind)}
    sortie = {
        **resume(obj),
        "champs": champs,
        "provenance": {nom: obj.origin(nom) for nom in champs},
        "modifiables": [c["champ"] for c in ecriture.formulaire(snap, obj)],
        "perdu": bool(getattr(obj, "perdu", False)),
    }
    k = obj.kind
    if k == "node":
        sortie["liens"] = {
            "parent": resume(obj.parent),
            "invites": _resumes(obj.children),
            "conteneurs": _resumes(obj.containers),
            "adresses": _resumes(obj.ips),
            "applications": _resumes(obj.apps),
            "applications_directes": _resumes(obj.apps_directes),
            "stacks": _resumes({c.stack.key: c.stack for c in obj.containers if c.stack}.values()),
        }
    elif k == "container":
        sortie["liens"] = {
            "hote": resume(obj.host), "image": resume(obj.image),
            "stack": resume(obj.stack), "applications": _resumes(obj.apps),
        }
    elif k == "image":
        sortie["liens"] = {
            "conteneurs": _resumes(obj.containers),
            "hotes": _resumes({c.host.id: c.host for c in obj.containers if c.host}.values()),
            "applications": _resumes({a.id: a for c in obj.containers for a in c.apps}.values()),
        }
    elif k == "ipam":
        sortie["liens"] = {"noeud": resume(obj.node), "vlan": resume(obj.vlan)}
    elif k == "vlan":
        sortie["liens"] = {"adresses": _resumes(obj.ips)}
    elif k == "application":
        sortie["liens"] = {
            "hotes": _resumes(obj.hosts), "noeuds_directs": _resumes(obj.vms),
            "stacks": _resumes(obj.stacks), "conteneurs": _resumes(obj.containers),
            "images": _resumes(obj.images),
        }
        sortie["cve"] = {"critiques": obj.cve_critical, "hautes": obj.cve_high,
                         "mises_a_jour": obj.updates}
    return sortie


def ligne_liste(snap, obj):
    """Forme courte, pour une liste : le résumé et les champs scalaires."""
    return {**resume(obj),
            "champs": {nom: _valeur(obj.get(nom)) for nom in champs_scalaires(snap, obj.kind)}}


def lister(snap, kind, filtre=None, limite=200, depart=0):
    objets = list(snap.by_kind.get(kind, {}).values())
    if kind == "image":
        # Comme l'écran Sécurité : seules les images en service, triées par exposition.
        objets = list(snap.images)
    objets.sort(key=lambda o: o.name.lower())
    if filtre:
        f = filtre.strip().lower()
        objets = [o for o in objets
                  if f in " ".join(str(_valeur(o.get(n)) or "")
                                   for n in champs_scalaires(snap, kind)).lower()
                  or f in o.name.lower()]
    total = len(objets)
    return total, objets[depart:depart + limite]


def stack(snap, st):
    return {
        **resume(st),
        "chemin": st.compose_path,
        "etat": st.etat,
        "conteneurs": _resumes(st.containers),
        "applications": [{"id": a.id, "nom": a.name, "criticite": a.sel("Criticality")}
                         for a in sorted(st.apps, key=lambda a: a.name.lower())],
        "images": _resumes(st.images),
        "cve": {"critiques": st.cve_critical, "hautes": st.cve_high, "mises_a_jour": st.updates},
    }


def a_traiter(snap):
    return [{"cle": q["key"], "libelle": q["label"], "explication": q["hint"],
             "gravite": q["severity"], "nombre": q["count"],
             "objets": _resumes(q["objets"][:50])} for q in snap.queues]


def sursis(snap):
    return [{**resume(e["obj"]), "vu_le": e["seen"].isoformat(),
             "heures_restantes": round(e["remaining_hours"], 1)} for e in snap.expiring()]


def etat(snap):
    return {
        "instantane": snap.fetched_at.isoformat(),
        "composants": [{k: c[k] for k in ("cle", "label", "verdict", "valeur", "depuis",
                                           "probleme")} for c in snap.controls()],
        "sources": [{"nom": r["name"], "vu_le": r["seen"].isoformat() if r["seen"] else None,
                     "lignes": r["count"], "verdict": r["verdict"]} for r in snap.sources_state()],
        "totaux": {"noeuds": len(snap.nodes), "conteneurs": len(snap.containers),
                   "stacks": len(snap.stacks), "images": len(snap.images),
                   "adresses": len(snap.ips), "vlan": len(snap.vlans),
                   "applications": len(snap.apps), **snap.totals},
    }


def journal(depot, type_public=None, ident=None, acteur=None, limite=50):
    kind = kind_de(type_public) if type_public else None
    entrees = depot.journal(limite=max(1, min(limite, 500)), kind=kind, ident=ident,
                            acteur=acteur)
    for e in entrees:
        tab = e.pop("tab", None)
        e["type"] = PREFIXES.get(tab) if tab else None
    return entrees


def schema_public(snap, type_public=None):
    """Le modèle tel qu'un client doit le connaître pour lire et écrire."""
    kinds = [kind_de(type_public)] if type_public else list(modele.TABLES)
    sortie = {}
    for kind in kinds:
        t = modele.table(kind)
        champs = []
        for c in t.champs:
            if c.type == "inverse":
                continue
            champs.append(_descr_champ(snap, kind, c.nom, c.type, c.cible))
        for p in snap.perso.get(kind, []):
            d = _descr_champ(snap, kind, p["nom"], p["type"], None)
            d["personnalise"] = True
            d["aide"] = p.get("aide")
            champs.append(d)
        sortie[PREFIXES[kind]] = {
            "libelle": t.libelle, "cle_naturelle": t.cle,
            "creation": kind in ecriture.CREABLES,
            "champs": champs,
        }
    return sortie


def _descr_champ(snap, kind, nom, type_, cible):
    d = {"nom": nom, "type": type_}
    if cible:
        d["cible"] = PREFIXES[cible]
    if type_ in ("select", "multi"):
        d["options"] = ecriture.options(snap, kind, nom)
    origines = {schema.origin(kind, nom, s) for s in (None, "Auto", "Manual")}
    d["provenance"] = sorted(origines)
    return d


# --------------------------------------------------------------------------- écriture

def _formulaire(descripteurs, valeurs, toutes=False):
    """Traduit {champ: valeur} en formulaire tel que l'interface l'enverrait.

    Les champs inconnus du formulaire y figurent aussi (`champ.<nom>`) : le rédacteur les
    signale alors comme non modifiables, au lieu de les ignorer en silence.
    """
    widgets = {d["champ"]: d["widget"] for d in descripteurs}
    paires = []
    for nom, valeur in valeurs.items():
        widget = widgets.get(nom)
        if widget is not None:
            paires.append(("soumis", nom))
        cle = f"champ.{nom}"
        if widget == "bool":
            if valeur not in (None, False, 0, "", "0", "false", "non", "no"):
                paires.append((cle, "1"))
            continue
        if isinstance(valeur, (list, tuple, set)):
            for v in valeur:
                if isinstance(v, dict):
                    v = v.get("id", v.get("nom"))
                paires.append((cle, "" if v is None else str(v)))
            if not valeur and widget is None:
                paires.append((cle, ""))
            continue
        if isinstance(valeur, dict):
            valeur = valeur.get("id", valeur.get("nom"))
        paires.append((cle, "" if valeur is None else str(valeur)))
    return FormData(paires)


def modifier(snap, redacteur, obj, valeurs, acteur):
    if not isinstance(valeurs, dict) or not valeurs:
        raise ecriture.EcritureRefusee("Corps attendu : un objet {champ: valeur} non vide.")
    descripteurs = ecriture.formulaire(snap, obj)
    modifiables = {d["champ"] for d in descripteurs}
    refuses = set(valeurs) - modifiables
    if refuses:
        raise ecriture.EcritureRefusee(
            "Champs non modifiables sur cette ligne : " + ", ".join(sorted(refuses))
            + (". Modifiables : " + ", ".join(sorted(modifiables)) + "." if modifiables
               else ". Aucun champ n'est modifiable sur cette ligne.")
            + " Les autres sont réécrits par le collecteur.")
    changements = redacteur.appliquer(snap, obj, _formulaire(descripteurs, valeurs),
                                      acteur=acteur)
    return [{"champ": c.champ, "avant": c.avant, "apres": c.apres} for c in changements]


def creer(snap, redacteur, kind, valeurs, acteur):
    if not isinstance(valeurs, dict) or not valeurs:
        raise ecriture.EcritureRefusee("Corps attendu : un objet {champ: valeur} non vide.")
    if kind not in ecriture.CREABLES:
        raise ecriture.EcritureRefusee(
            f"La création n'est pas ouverte sur {modele.table(kind).libelle} : ces lignes "
            "sont écrites par le collecteur.")
    descripteurs = ecriture.formulaire_creation(snap, kind)
    connus = {d["champ"] for d in descripteurs}
    inconnus = set(valeurs) - connus
    if inconnus:
        raise ecriture.EcritureRefusee(
            "Champs non saisissables à la création : " + ", ".join(sorted(inconnus))
            + ". Saisissables : " + ", ".join(sorted(connus)))
    return redacteur.creer(snap, kind, _formulaire(descripteurs, valeurs), acteur=acteur)


def rattacher_stack(snap, redacteur, st, applications, acteur):
    if not isinstance(applications, (list, tuple)):
        raise ecriture.EcritureRefusee("`applications` : liste d'identifiants attendue.")
    paires = [("soumis", ecriture.CHAMP_APPLICATION)] + [
        (f"champ.{ecriture.CHAMP_APPLICATION}", str(a)) for a in applications]
    return redacteur.rattacher_stack(snap, st, FormData(paires), acteur=acteur)


def horodatage():
    return datetime.now(timezone.utc).isoformat()
