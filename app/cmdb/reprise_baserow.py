"""Reprise unique des données de Baserow dans la base SQLite, avec vérification.

    python -m cmdb.reprise_baserow             # base vide exigée
    python -m cmdb.reprise_baserow --remplacer # écrase l'inventaire déjà présent
    python -m cmdb.reprise_baserow --verifier  # compare seulement, n'écrit rien

Lit les variables de l'ancienne configuration : BASEROW_URL, BASEROW_TOKEN (ou
WEB_BASEROW_TOKEN), BASEROW_VERIFY_TLS et les six TABLE_*. La base visée est TINYCMDB_DB
(/data/tinycmdb.db par défaut).

Ce qui est repris : les six tables, ligne par ligne, **avec leurs identifiants** — les
adresses des fiches (/noeud/12) et les favoris restent valides ; toutes les options des
listes, employées ou non, avec leur couleur et leur ordre. Ce qui ne l'est pas : les
champs calculés de Baserow (recherches, comptes), qui sont recalculés par la console, et
`Application.Source`, que rien n'emploie.

La vérification reconstruit le graphe de la console deux fois — depuis Baserow, puis depuis
SQLite — et compare chaque objet, chaque champ et chaque lien. Le moindre écart fait
échouer la commande : la bascule ne se fait que sur une reprise prouvée identique.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import requests

from . import db, modele
from .depot import Depot

logger = logging.getLogger("cmdb.reprise")

ENV_TABLES = {"node": "TABLE_NODE", "container": "TABLE_CONTAINER", "image": "TABLE_IMAGES",
              "ipam": "TABLE_IPAM", "vlan": "TABLE_VLAN", "application": "TABLE_APPLICATION"}

# Les tables que Baserow connaissait, dans l'ordre d'écriture. Processeurs et composants
# sont nés après lui (migration 002) : une reprise les vide, elle ne les remplit pas.
ORDRE_BASEROW = tuple(k for k in modele.ORDRE if k in ENV_TABLES)


class Baserow:
    def __init__(self, url, jeton, verify):
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Token {jeton}"
        self.session.verify = verify

    def champs(self, table_id):
        r = self.session.get(f"{self.url}/api/database/fields/table/{table_id}/", timeout=30)
        r.raise_for_status()
        return r.json()

    def lignes(self, table_id):
        lignes, url = [], f"{self.url}/api/database/rows/table/{table_id}/"
        params = {"user_field_names": "true", "size": 200}
        while url:
            r = self.session.get(url, params=params, timeout=30)
            r.raise_for_status()
            donnees = r.json()
            lignes.extend(donnees["results"])
            url, params = donnees.get("next"), None
        return lignes


def lire_baserow():
    url = os.environ.get("BASEROW_URL", "")
    jeton = os.environ.get("BASEROW_TOKEN") or os.environ.get("WEB_BASEROW_TOKEN", "")
    verify = os.environ.get("BASEROW_VERIFY_TLS", "true").strip().lower() in ("1", "true", "yes", "on")
    manquantes = [v for v in ("BASEROW_URL",) if not os.environ.get(v)] + \
                 ([] if jeton else ["BASEROW_TOKEN"]) + \
                 [v for v in ENV_TABLES.values() if not os.environ.get(v)]
    if manquantes:
        raise SystemExit("Variables manquantes : " + ", ".join(manquantes))
    if not verify:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    client = Baserow(url, jeton, verify)
    champs, lignes = {}, {}
    for kind, env in ENV_TABLES.items():
        table_id = os.environ[env]
        champs[kind] = client.champs(table_id)
        lignes[kind] = client.lignes(table_id)
        logger.info("Baserow %s (table %s) : %d lignes", kind, table_id, len(lignes[kind]))
    return champs, lignes


def _valeur_reprise(c, v):
    """Valeur Baserow -> forme acceptée par Depot.ecrire."""
    if c.type == "select":
        return v.get("id") if isinstance(v, dict) else None
    if c.type == "multi":
        return [o["id"] for o in (v or []) if isinstance(o, dict)]
    if c.type in ("lien", "liens"):
        return [o["id"] for o in (v or []) if isinstance(o, dict)]
    if c.type == "bool":
        return bool(v)
    return v if v not in ("",) else None


def importer(depot, champs, lignes, remplacer=False):
    conn = depot.conn
    deja = {k: depot.compter(k) for k in modele.TABLES}
    if any(deja.values()) and not remplacer:
        raise SystemExit("La base contient déjà un inventaire ("
                         + ", ".join(f"{n} {k}" for k, n in deja.items() if n)
                         + "). Relancer avec --remplacer pour l'écraser.")
    with depot.transaction():
        # Les liens pointent parfois vers une ligne qui sera insérée plus loin (un
        # hyperviseur listé après ses invités) : contrôle des clés étrangères à la fin.
        conn.execute("PRAGMA defer_foreign_keys = ON")
        for kind in reversed(modele.ORDRE):
            conn.execute(f"DELETE FROM {modele.table(kind).sql}")
        for table in ("choix", "node_application", "container_application", "option"):
            conn.execute(f"DELETE FROM {table}")

        n_options = 0
        for kind, definition in champs.items():
            for f in definition:
                c = modele.champ(kind, f["name"])
                if c is None or c.type not in ("select", "multi"):
                    continue
                for ordre, o in enumerate(f.get("select_options") or [], start=1):
                    conn.execute("INSERT INTO option (id, tab, champ, valeur, couleur, ordre) "
                                 "VALUES (?, ?, ?, ?, ?, ?)",
                                 (o["id"], kind, c.nom, o["value"], o.get("color") or "gray", ordre))
                    n_options += 1

        # Les options des champs nés après Baserow ont disparu avec les autres : elles
        # reviennent telles que la migration qui les a créées les pose.
        for instruction in db._instructions(
                (db.MIGRATIONS / "002_materiel.sql").read_text(encoding="utf-8")):
            if instruction.startswith("INSERT OR IGNORE INTO option"):
                conn.execute(instruction)

        for kind in ORDRE_BASEROW:
            t = modele.table(kind)
            for ligne in sorted(lignes[kind], key=lambda r: r["id"]):
                valeurs = {c.nom: _valeur_reprise(c, ligne.get(c.nom))
                           for c in t.champs if c.type != "inverse" and c.nom in ligne}
                depot.ecrire(kind, None, valeurs, id_impose=ligne["id"], verifier=False,
                             creer_options=False)
        depot._journaliser(conn, "reprise", "reprise Baserow", libelle=", ".join(
            f"{len(lignes[k])} {modele.table(k).libelle.lower()}" for k in ORDRE_BASEROW)
            + f", {n_options} options")
    logger.info("Reprise écrite : %d options, %s", n_options,
                ", ".join(f"{len(lignes[k])} {k}" for k in ORDRE_BASEROW))


# --------------------------------------------------------------------------- vérification

def _norm(c_type, v):
    if c_type == "select":
        return v.get("value") if isinstance(v, dict) else None
    if c_type == "multi":
        return sorted(o.get("value") for o in (v or []))
    if c_type in ("lien", "liens", "inverse"):
        return sorted(o.get("id") for o in (v or []))
    if c_type == "bool":
        return bool(v)
    if c_type == "entier":
        if v in (None, ""):
            return None
        return int(float(v))
    if isinstance(v, str):
        return v.strip() or None
    return v


def _graphe(snap):
    """Ce que la console calcule : c'est là qu'un écart se verrait à l'écran."""
    g = {}
    ids = lambda objs: sorted(o.id for o in objs)
    for n in snap.nodes:
        g[("node", n.id)] = {"parent": n.parent.id if n.parent else None,
                             "invites": ids(n.children), "conteneurs": ids(n.containers),
                             "ips": ids(n.ips), "apps": ids(n.apps)}
    for c in snap.containers:
        g[("container", c.id)] = {"hote": c.host.id if c.host else None,
                                  "image": c.image.id if c.image else None,
                                  "apps": ids(c.apps), "stack": c.stack.key if c.stack else None}
    for ip in snap.ips:
        g[("ipam", ip.id)] = {"noeud": ip.node.id if ip.node else None,
                              "vlan": ip.vlan.id if ip.vlan else None}
    for a in snap.apps:
        g[("application", a.id)] = {"conteneurs": ids(a.containers), "vms": ids(a.vms),
                                    "hotes": ids(a.hosts), "stacks": sorted(s.key for s in a.stacks)}
    g["stacks"] = sorted(s.key for s in snap.stacks)
    g["files"] = {q["key"]: q["count"] for q in snap.queues}
    return g


def verifier(depot, champs, lignes):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from web.store import Snapshot

    cfg = SimpleNamespace(RETIRE_GRACE_HOURS=72, INTERVAL_CONTAINERS=900, INTERVAL_NODES=3600,
                          INTERVAL_TRIVY=86400, BACKUP_DIR="/nonexistent", DB_PATH=depot.chemin)
    maintenant = datetime.now(timezone.utc)
    sqlite_lignes, options, _ = depot.lire_tout()
    ecarts = []

    for kind in ORDRE_BASEROW:
        t = modele.table(kind)
        avant = {r["id"]: r for r in lignes[kind]}
        apres = {r["id"]: r for r in sqlite_lignes[kind]}
        if set(avant) != set(apres):
            ecarts.append(f"{kind} : identifiants différents — absents de SQLite "
                          f"{sorted(set(avant) - set(apres))}, en trop {sorted(set(apres) - set(avant))}")
        for ident in sorted(set(avant) & set(apres)):
            for c in t.champs:
                if c.nom not in avant[ident]:
                    continue
                a, b = _norm(c.type, avant[ident].get(c.nom)), _norm(c.type, apres[ident].get(c.nom))
                if a != b:
                    ecarts.append(f"{kind}#{ident} {c.nom} : Baserow {a!r} ≠ SQLite {b!r}")
        for f in champs[kind]:
            c = modele.champ(kind, f["name"])
            if c is None or c.type not in ("select", "multi"):
                continue
            attendues = [o["value"] for o in f.get("select_options") or []]
            obtenues = [o["valeur"] for o in options.get((kind, c.nom), [])]
            if attendues != obtenues:
                ecarts.append(f"options {kind}.{c.nom} : {attendues} ≠ {obtenues}")

    g1 = _graphe(Snapshot(cfg, lignes, maintenant))
    g2 = _graphe(Snapshot(cfg, sqlite_lignes, maintenant))
    for cle in sorted(set(g1) | set(g2), key=str):
        if g1.get(cle) != g2.get(cle):
            ecarts.append(f"graphe {cle} : Baserow {g1.get(cle)!r} ≠ SQLite {g2.get(cle)!r}")
    return ecarts


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", default=os.environ.get("TINYCMDB_DB", "/data/tinycmdb.db"))
    p.add_argument("--remplacer", action="store_true",
                   help="écraser l'inventaire déjà présent dans la base")
    p.add_argument("--verifier", action="store_true",
                   help="comparer seulement, sans rien écrire")
    args = p.parse_args(argv)

    champs, lignes = lire_baserow()
    depot = Depot(args.db)
    if not args.verifier:
        importer(depot, champs, lignes, remplacer=args.remplacer)
    ecarts = verifier(depot, champs, lignes)
    if ecarts:
        print(f"\n{len(ecarts)} écart(s) entre Baserow et SQLite :", file=sys.stderr)
        for e in ecarts[:200]:
            print("  - " + e, file=sys.stderr)
        return 1
    total = sum(len(v) for v in lignes.values())
    print(f"\nVérification réussie : {total} lignes, leurs champs, leurs options et le graphe "
          f"de la console sont identiques dans Baserow et dans {args.db}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
