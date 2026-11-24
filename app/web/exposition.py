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


def _vide(v):
    return v is None or v == "" or v == []


def champs_de(snap, obj, vides=True, choisis=None):
    """Les champs d'un objet. Sans ceux qui n'ont pas de sens pour lui (la carte mère d'un
    LXC), sans les vides si `vides` est faux, restreints à `choisis` s'il est donné."""
    sortie = {}
    for nom in champs_scalaires(snap, obj.kind):
        if not ecriture.applicable(obj, nom) or (choisis and nom not in choisis):
            continue
        v = _valeur(obj.get(nom))
        if not vides and _vide(v):
            continue
        sortie[nom] = v
    return sortie


def cpu_resume(cpu, sockets=None):
    """Ce qu'on veut savoir d'un processeur sans ouvrir le référentiel."""
    if cpu is None:
        return None
    return {**resume(cpu), **{k: v for k, v in champs_de_cpu(cpu).items() if not _vide(v)},
            **({"sockets": sockets} if sockets and sockets > 1 else {})}


def champs_de_cpu(cpu):
    return {nom: _valeur(cpu.get(nom)) for nom in
            ("Vendor", "Arch", "Generation", "Cores", "Threads", "Base_MHz", "Boost_MHz",
             "TDP_W", "L3_MB", "x86_level", "iGPU", "Virtualization", "Max_RAM_Gb")}


def objet(snap, obj):
    """Description complète d'un objet : ses champs, leur provenance, ses relations, et ce
    qu'on peut y modifier."""
    champs = champs_de(snap, obj)
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
            "processeur": cpu_resume(obj.cpu, obj.num("Sockets")),
            "composants": _resumes(obj.composants),
            "peripheriques_attribues": _resumes(obj.devices),
        }
    elif k == "cpu":
        sortie["liens"] = {"noeuds": _resumes(obj.nodes)}
    elif k == "composant":
        sortie["liens"] = {"hote": resume(obj.host), "attribue_a": resume(obj.assigned)}
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


def ligne_liste(snap, obj, vides=True, choisis=None):
    """Forme courte, pour une liste : le résumé et les champs scalaires."""
    return {**resume(obj), "champs": champs_de(snap, obj, vides=vides, choisis=choisis)}


# Comparaisons admises dans un filtre structuré : `{"RAM_Gb": ">=16"}`.
_OPERATEURS = (">=", "<=", "!=", ">", "<", "=")


def _comparer(valeur, attendu):
    """Vrai si `valeur` (celle de l'objet) satisfait `attendu` (celle du filtre).

    Une liste (rôles, applications) satisfait le filtre si l'un de ses éléments le fait ;
    un texte est comparé sans tenir compte de la casse ; un nombre peut être comparé
    (`>=16`) ; `null` désigne un champ vide, et `"!="` + rien un champ renseigné.
    """
    if isinstance(valeur, list):
        if attendu is None:
            return not valeur
        return any(_comparer(v, attendu) for v in valeur)
    if attendu is None:
        return _vide(valeur)
    if isinstance(attendu, bool):
        return bool(valeur) == attendu
    texte = str(attendu).strip()
    operateur = next((o for o in _OPERATEURS if texte.startswith(o)), None)
    cible = texte[len(operateur):].strip() if operateur else texte
    if operateur in (">=", "<=", ">", "<"):
        try:
            a, b = float(valeur), float(cible)
        except (TypeError, ValueError):
            return False
        return {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[operateur]
    egal = (str(valeur).strip().lower() == cible.lower()) if not _vide(valeur) else (cible == "")
    return not egal if operateur == "!=" else egal


def _valeur_filtrable(snap, obj, nom):
    """Valeur d'un champ pour un filtre : les liens se comparent par le nom de leur cible."""
    v = obj.get(nom)
    if isinstance(v, list) and v and isinstance(v[0], dict) and "color" not in v[0]:
        return [x.get("value") for x in v]
    return _valeur(v)


def lister(snap, kind, filtre=None, limite=200, depart=0, filtres=None):
    """Objets d'un type. `filtre` : texte cherché dans tous les champs. `filtres` :
    {champ: valeur} où toutes les conditions doivent tenir (voir `_comparer`)."""
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
    if filtres:
        if not isinstance(filtres, dict):
            raise Introuvable("`filtres` : un objet {champ: valeur} est attendu.")
        connus = {c.nom for c in modele.table(kind).champs} | {p["nom"] for p in snap.perso.get(kind, [])}
        inconnus = set(filtres) - connus
        if inconnus:
            raise Introuvable(f"champs inconnus sur {PREFIXES[kind]} : {', '.join(sorted(inconnus))}. "
                              "`schema` les liste.")
        objets = [o for o in objets
                  if all(_comparer(_valeur_filtrable(snap, o, n), v) for n, v in filtres.items())]
    total = len(objets)
    return total, objets[depart:depart + limite]


# --------------------------------------------------------------------------- contexte

def _noeud_compact(snap, n, materiel=True):
    """Un nœud en une poignée de lignes : ce qu'il est, où il tourne, ce qu'il porte."""
    if n is None:
        return None
    champs = champs_de(snap, n, vides=False)
    champs.pop("Config", None)
    champs.pop("Last_seen", None)
    sortie = {**resume(n), "champs": champs,
              "adresses": [ip.name for ip in n.ips],
              "applications": [a.name for a in n.apps]}
    if n.containers:
        sortie["stacks"] = sorted({c.stack.key for c in n.containers if c.stack})
        sortie["conteneurs"] = len(n.containers)
    if materiel:
        cpu = n.cpu or (n.parent.cpu if n.parent and n.sel("Type") in ("VM", "LXC") else None)
        if cpu is not None:
            cle = "processeur" if n.cpu else "processeur_hote"
            sortie[cle] = cpu_resume(cpu, n.num("Sockets") if n.cpu else None)
        if n.composants:
            sortie["composants"] = [_composant_compact(snap, c) for c in n.composants]
        if n.devices:
            sortie["peripheriques_attribues"] = [_composant_compact(snap, c) for c in n.devices]
    return sortie


def _composant_compact(snap, c):
    champs = champs_de(snap, c, vides=False)
    for k in ("UID", "Source", "Last_seen", "Name"):
        champs.pop(k, None)
    return {**resume(c), "champs": champs,
            "attribue_a": c.assigned.name if c.assigned else None}


def contexte(snap, obj):
    """Tout ce qu'il faut pour raisonner sur un objet, en un seul appel.

    Un agent qui dépanne ou conçoit a besoin de la chaîne entière — ce qui tourne, sur
    quoi, avec quel matériel, au service de quoi — et non d'un objet isolé suivi de dix
    lectures pour reconstituer ses voisins. Les voisins sont rendus en forme compacte
    (champs renseignés seulement) ; la forme complète reste celle de `lire`.
    """
    sortie = {"objet": objet(snap, obj)}
    k = obj.kind
    if k == "node":
        if obj.parent:
            sortie["hote"] = _noeud_compact(snap, obj.parent)
        sortie["materiel"] = {
            "processeur": cpu_resume(obj.cpu, obj.num("Sockets")) if obj.cpu else (
                {"hote": cpu_resume(obj.parent.cpu, obj.parent.num("Sockets"))}
                if obj.parent and obj.parent.cpu else None),
            "composants": [_composant_compact(snap, c) for c in obj.composants],
            "peripheriques_attribues": [_composant_compact(snap, c) for c in obj.devices],
        }
        sortie["invites"] = [_noeud_compact(snap, g, materiel=False) for g in obj.children]
        sortie["conteneurs"] = [ligne_liste(snap, c, vides=False) for c in obj.containers]
        sortie["adresses"] = [ligne_liste(snap, ip, vides=False) for ip in obj.ips]
    elif k == "container":
        sortie["hote"] = _noeud_compact(snap, obj.host)
        if obj.host and obj.host.parent:
            sortie["hyperviseur"] = _noeud_compact(snap, obj.host.parent)
        sortie["image"] = ligne_liste(snap, obj.image, vides=False) if obj.image else None
        sortie["stack"] = stack(snap, obj.stack) if obj.stack else None
    elif k == "application":
        hotes = list(obj.hosts)
        sortie["hotes"] = [_noeud_compact(snap, h) for h in hotes]
        parents = {h.parent.id: h.parent for h in hotes if h.parent}
        sortie["hyperviseurs"] = [_noeud_compact(snap, p) for p in parents.values()]
        sortie["stacks"] = [stack(snap, st) for st in obj.stacks]
    elif k == "composant":
        sortie["hote"] = _noeud_compact(snap, obj.host)
        sortie["attribue_a"] = _noeud_compact(snap, obj.assigned, materiel=False)
    elif k == "cpu":
        sortie["noeuds"] = [_noeud_compact(snap, n, materiel=False) for n in obj.nodes]
    elif k == "ipam":
        sortie["noeud"] = _noeud_compact(snap, obj.node)
        sortie["vlan"] = ligne_liste(snap, obj.vlan, vides=False) if obj.vlan else None
    elif k == "image":
        sortie["hotes"] = [_noeud_compact(snap, h, materiel=False)
                           for h in {c.host.id: c.host for c in obj.containers if c.host}.values()]
    elif k == "vlan":
        sortie["adresses"] = [ligne_liste(snap, ip, vides=False) for ip in obj.ips]
    return sortie


def contexte_stack(snap, st):
    return {"stack": stack(snap, st),
            "hote": _noeud_compact(snap, st.host),
            "hyperviseur": _noeud_compact(snap, st.host.parent) if st.host and st.host.parent else None,
            "conteneurs": [ligne_liste(snap, c, vides=False) for c in st.containers]}


def capacite(snap):
    """Par hyperviseur : le matériel, ce qui est promis aux invités, et ce qui reste.

    La mémoire ne se partage pas, les cœurs si : le rapport vCPU / cœur physique dit si un
    hôte est chargé, le rapport mémoire dit s'il peut encore accueillir quelqu'un.
    """
    sortie = []
    for e in snap.hypervisor_capacity:
        hv = e["host"]
        sortie.append({
            **resume(hv),
            "processeur": cpu_resume(e["cpu"], e["sockets"]),
            "coeurs_physiques": e["coeurs"],
            "threads": e["vcpu_capacite"],
            "vcpu_alloues_demarres": e["vcpu"], "vcpu_alloues_total": e["vcpu_total"],
            "vcpu_par_coeur_physique": e["vcpu_par_coeur"],
            "ram_go": e["ram_capacite"], "ram_allouee_demarres_go": e["ram"],
            "ram_allouee_total_go": e["ram_total"],
            "ram_libre_go": (e["ram_capacite"] - e["ram"]) if e["ram_capacite"] else None,
            "ram_type": hv.sel("RAM_type"), "ram_ecc": hv.sel("RAM_ECC"),
            "ram_max_go": hv.num("RAM_max_Gb"),
            "disques_go": e["disques_go"],
            "invites": e["invites"], "invites_demarres": e["demarres"],
            "peripheriques_attribues": [
                {"composant": c.name, "vm": c.assigned.name}
                for c in hv.composants if c.assigned],
        })
    return sortie


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
                   "applications": len(snap.apps), "processeurs": len(snap.cpus),
                   "composants": len(snap.composants), **snap.totals},
    }


def journal(depot, type_public=None, ident=None, acteur=None, limite=50, depuis=None,
            champ=None):
    kind = kind_de(type_public) if type_public else None
    if depuis:
        try:
            moment = datetime.fromisoformat(str(depuis).replace("Z", "+00:00"))
        except ValueError:
            raise Introuvable(f"`depuis` : date ISO 8601 attendue (« 2026-11-17 » ou "
                              f"« 2026-11-17T08:00:00Z »), « {depuis} » reçu")
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        depuis = moment.astimezone(timezone.utc).isoformat()
    entrees = depot.journal(limite=max(1, min(limite, 500)), kind=kind, ident=ident,
                            acteur=acteur, depuis=depuis, champ=champ)
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
    # Évalué pour le type (ou la catégorie) que pose cette même écriture : passer une carte
    # en GPU et renseigner sa mémoire se fait en un appel.
    descripteurs = ecriture.formulaire(snap, obj, ecriture.pilote_soumis(obj.kind, valeurs))
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
    # Ici on le dit, plutôt que de l'écarter en silence comme le fait la console : un agent
    # qui croit avoir enregistré la mémoire d'un GPU doit apprendre qu'il manque la catégorie.
    hors_sujet = ecriture.sans_objet(kind, valeurs)
    if hors_sujet:
        pilote = ecriture.PILOTES[kind][0]
        raise ecriture.EcritureRefusee(
            "Champs sans objet pour cette ligne : " + ", ".join(sorted(hors_sujet))
            + f" (selon {pilote} = {valeurs.get(pilote)!r}).")
    return redacteur.creer(snap, kind, _formulaire(descripteurs, valeurs), acteur=acteur)


def rattacher_stack(snap, redacteur, st, applications, acteur):
    if not isinstance(applications, (list, tuple)):
        raise ecriture.EcritureRefusee("`applications` : liste d'identifiants attendue.")
    paires = [("soumis", ecriture.CHAMP_APPLICATION)] + [
        (f"champ.{ecriture.CHAMP_APPLICATION}", str(a)) for a in applications]
    return redacteur.rattacher_stack(snap, st, FormData(paires), acteur=acteur)


def horodatage():
    return datetime.now(timezone.utc).isoformat()
