"""Instantané complet de la CMDB en mémoire, et le graphe qui va avec.

Baserow ne sait pas faire de jointure : chaque table se lit séparément, et les liens
reviennent sous forme `[{"id": 12, "value": "mytools"}]`. Plutôt que de multiplier les
requêtes par écran, on tire les six tables d'un coup et on reconstruit le graphe ici,
une fois, pour tout le monde.

C'est possible parce que le parc tient dans quelques centaines de lignes — et ce plafond
n'est pas une limite subie, c'est l'objectif du projet : avoir ce qu'il faut et pas plus.
Si cet instantané devenait lourd, ce serait le symptôme à traiter, pas le code.

Rien n'est écrit depuis ce module : il ne connaît que `fetch_all`.
"""

import ipaddress
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from collector.baserow import BaserowClient

from . import schema
from .favicons import FaviconResolver

logger = logging.getLogger("cmdb.web.store")


class BaserowIndisponible(Exception):
    """Aucun instantané disponible : Baserow est injoignable et rien n'a pu être chargé.

    Distincte d'un rechargement en échec : tant qu'un instantané existe, même daté, la
    console continue de servir et affiche son âge. Ce n'est qu'au démarrage à froid, sans
    rien en mémoire, qu'il n'y a réellement rien à montrer.
    """

# Préfixe d'URL par type d'objet. En français, et distinct du nom de table : `/ipam` est la
# vue d'ensemble des VLAN, une adresse vit sous `/ip/{id}` — sans quoi les deux routes se
# marcheraient dessus.
URL_PREFIX = {
    "node": "noeud",
    "container": "conteneur",
    "image": "image",
    "ipam": "ip",
    "vlan": "vlan",
    "application": "application",
}

# Domaines de l'écran Infrastructure, dans l'ordre d'affichage. Adossés à Node.Type, qui
# est le seul axe que le schéma fournit déjà — pas à un classement parallèle qu'il faudrait
# tenir à jour à la main en plus du reste.
VIRTUAL_TYPES = ("VM", "LXC")

# Un équipement n'apparaît que dans un seul groupe : le classement par rôle, essayé un
# temps, dispersait les serveurs physiques dans autant de catégories qu'ils portaient de
# fonctions. Le rôle reste affiché en étiquette sur chaque ligne, et reste filtrable par la
# recherche — ce qui couvre le besoin sans éclater la liste.
DOMAINS = [
    ("Physical", "Serveurs physiques"),
    ("Appliance", "Appliances réseau"),
    ("Device", "Postes et périphériques"),
    ("Camera", "Caméras"),
    ("IoT", "Objets connectés"),
]


# Teintes des listes déroulantes. Baserow nomme ses couleurs ("blue", "dark-red",
# "light-green"…) ; on n'en retient que la famille, et une seule valeur par famille, choisie
# assez médiane pour rester lisible sur fond clair comme sur fond sombre. La déclinaison
# claire/foncée de Baserow est ignorée : à l'échelle d'une étiquette de douze pixels, elle
# ne distingue rien et casse le contraste d'un des deux thèmes.
_TEINTES = {
    "blue": "#4a8fe0", "cyan": "#3aa5a5", "green": "#2f9e68", "yellow": "#c9a227",
    "orange": "#d08a2e", "red": "#e0665a", "brown": "#a6784f", "purple": "#a67fd0",
    "pink": "#d97ba8", "gray": "#8a9694", "grey": "#8a9694",
}


def _teinte(couleur):
    if not couleur:
        return None
    for famille, valeur in _TEINTES.items():
        if famille in couleur:
            return valeur
    return None


# --------------------------------------------------------------------------- accès bruts

def _sel(row, field):
    """Un single_select revient en {"id", "value", "color"}, pas en chaîne."""
    value = row.get(field)
    return value.get("value") if isinstance(value, dict) else value


def _multi(row, field):
    return [v.get("value") for v in (row.get(field) or []) if isinstance(v, dict)]


def _links(row, field):
    return [v for v in (row.get(field) or []) if isinstance(v, dict) and "id" in v]


def _num(row, field):
    """Baserow renvoie ses champs numériques en chaînes ("0", "63"). Les trier tels quels
    donnerait un ordre alphabétique — 9 après 63."""
    value = row.get(field)
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _dt(row, field):
    value = row.get(field)
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# --------------------------------------------------------------------------- objets

class Obj:
    """Une ligne Baserow plus ses relations résolues. Volontairement mince : les gabarits
    lisent les champs par leur nom Baserow exact, ce qui évite une couche de renommage à
    maintenir en parallèle des renommages faits dans Baserow."""

    def __init__(self, kind, row, name_field):
        self.kind = kind
        self.row = row
        self.id = row["id"]
        self.name = row.get(name_field) or f"#{row['id']}"
        self.source = _sel(row, "Source")

    # -- accès champs
    def get(self, field):
        return self.row.get(field)

    def sel(self, field):
        return _sel(self.row, field)

    def multi(self, field):
        return _multi(self.row, field)

    def sel_couleur(self, field):
        """Couleur choisie dans Baserow pour cette option, traduite en teinte affichable."""
        value = self.row.get(field)
        return _teinte(value.get("color")) if isinstance(value, dict) else None

    def multi_colore(self, field):
        """Options multiples avec leur teinte Baserow : [(libellé, teinte), ...]."""
        return [(v.get("value"), _teinte(v.get("color")))
                for v in (self.row.get(field) or []) if isinstance(v, dict)]

    def num(self, field):
        return _num(self.row, field)

    def dt(self, field):
        return _dt(self.row, field)

    def origin(self, field):
        return schema.origin(self.kind, field, self.source)

    @property
    def is_manual(self):
        return (self.source or "").lower() == "manual"

    @property
    def url(self):
        return f"/{URL_PREFIX[self.kind]}/{self.id}"

    def __repr__(self):
        return f"<{self.kind} {self.name}>"


class Stack:
    """Un projet Docker Compose, reconstitué à partir de `Container.Stack`.

    La stack n'a pas de table à elle : le collecteur rapporte le libellé du projet Compose
    sur chaque conteneur, comme un fait technique, et le regroupement se fait ici. C'est
    suffisant, et ça évite une table de plus à tenir à jour pour une information qui est
    déjà portée par les conteneurs eux-mêmes.

    La clé inclut l'hôte : trois conteneurs peuvent porter le même nom sur trois machines
    (`docker-socket-proxy`), et ce sont leurs stacks qui les distinguent — une par hôte
    inventorié.
    """

    kind = "stack"

    def __init__(self, name, host, containers):
        self.name = name
        self.host = host
        self.containers = sorted(containers, key=lambda c: c.name.lower())
        self.key = f"{host.name if host else '?'}/{name}"
        self.url = "/stack/" + quote(self.key, safe="")
        self.images = sorted(
            {c.image.id: c.image for c in self.containers if c.image}.values(),
            key=lambda i: (-(i.num("CVE_critical") or 0), i.name.lower()),
        )
        self.apps = list({a.id: a for c in self.containers for a in c.apps}.values())
        self.cve_critical = sum(i.num("CVE_critical") or 0 for i in self.images)
        self.cve_high = sum(i.num("CVE_high") or 0 for i in self.images)
        self.updates = sum(1 for i in self.images if i.get("Update_available"))
        self.compose_path = next(
            (c.get("Compose_path") for c in self.containers if c.get("Compose_path")), None)
        self.running = sum(1 for c in self.containers if c.sel("Status") == "Running")

    @property
    def etat(self):
        """Une stack va bien quand tous ses conteneurs vont bien.

        C'est la définition utile : un projet Compose se déploie et se perd d'un bloc, un
        seul service à terre suffit à rendre l'ensemble inopérant. Pas de demi-mesure.
        """
        if any(getattr(c, "perdu", False) for c in self.containers):
            return "perdu"
        if not self.containers:
            return None
        return "ok" if self.running == len(self.containers) else "ko"

    @property
    def is_manual(self):
        return False

    def __repr__(self):
        return f"<stack {self.key}>"


# --------------------------------------------------------------------------- instantané

class Snapshot:
    def __init__(self, cfg, rows_by_kind, fetched_at):
        self.cfg = cfg
        self.fetched_at = fetched_at

        self.nodes = [Obj("node", r, "Name") for r in rows_by_kind["node"]]
        self.containers = [Obj("container", r, "Name") for r in rows_by_kind["container"]]
        self.images = [Obj("image", r, "Reference") for r in rows_by_kind["image"]]
        self.ips = [Obj("ipam", r, "Address") for r in rows_by_kind["ipam"]]
        self.vlans = [Obj("vlan", r, "name") for r in rows_by_kind["vlan"]]
        self.apps = [Obj("application", r, "Name") for r in rows_by_kind["application"]]

        self.by_kind = {
            "node": {o.id: o for o in self.nodes},
            "container": {o.id: o for o in self.containers},
            "image": {o.id: o for o in self.images},
            "ipam": {o.id: o for o in self.ips},
            "vlan": {o.id: o for o in self.vlans},
            "application": {o.id: o for o in self.apps},
        }

        self._link_graph()
        self._marquer_perdus()
        self._sort()
        self.queues = _build_queues(self)

    # -- résolution des liens ------------------------------------------------
    def _resolve(self, kind, row, field):
        index = self.by_kind[kind]
        return [index[link["id"]] for link in _links(row, field) if link["id"] in index]

    def _link_graph(self):
        for node in self.nodes:
            node.parents = self._resolve("node", node.row, "Parent_host")
            node.parent = node.parents[0] if node.parents else None
            node.children = []
            node.containers = []
            node.ips = []
            node.apps = self._resolve("application", node.row, "Application")
        for node in self.nodes:
            if node.parent:
                node.parent.children.append(node)

        for image in self.images:
            image.containers = []

        for container in self.containers:
            hosts = self._resolve("node", container.row, "Host")
            container.host = hosts[0] if hosts else None
            images = self._resolve("image", container.row, "Image")
            container.image = images[0] if images else None
            container.apps = self._resolve("application", container.row, "Application - Stack")
            container.app = container.apps[0] if container.apps else None
            if container.host:
                container.host.containers.append(container)
            if container.image:
                container.image.containers.append(container)

        for ip in self.ips:
            nodes = self._resolve("node", ip.row, "Node")
            ip.node = nodes[0] if nodes else None
            vlans = self._resolve("vlan", ip.row, "VLAN")
            ip.vlan = vlans[0] if vlans else None
            if ip.node:
                ip.node.ips.append(ip)

        for vlan in self.vlans:
            vlan.ips = [ip for ip in self.ips if ip.vlan is vlan]
            vlan.network = _parse_network(vlan.get("subnet"))

        # Applications d'un nœud : celles qui lui sont liées à la main, plus celles portées
        # par les conteneurs qu'il héberge. Sans le second terme, la fiche d'un hôte Docker
        # n'affiche aucune application alors qu'il en fait tourner une dizaine — le lien
        # existe, il passe simplement par les conteneurs.
        for node in self.nodes:
            indirectes = {a.id: a for c in node.containers for a in c.apps}
            directes = {a.id: a for a in node.apps}
            node.apps_directes = list(directes.values())
            node.apps = sorted({**indirectes, **directes}.values(),
                               key=lambda a: a.name.lower())

        # Stacks : regroupement des conteneurs par projet Compose, par hôte.
        par_stack = {}
        for container in self.containers:
            nom = (container.get("Stack") or "").strip()
            container.stack = None
            if nom:
                par_stack.setdefault((container.host.id if container.host else None, nom),
                                     []).append(container)
        self.stacks = sorted(
            (Stack(nom, membres[0].host, membres) for (_, nom), membres in par_stack.items()),
            key=lambda s: s.name.lower())
        self.stacks_by_key = {s.key: s for s in self.stacks}
        for stack in self.stacks:
            for container in stack.containers:
                container.stack = stack

        for app in self.apps:
            app.containers = self._resolve("container", app.row, "Containers")
            app.vms = self._resolve("node", app.row, "VM")
            # Un hôte concerné est soit lié directement, soit l'hôte d'un de ses conteneurs.
            hosts = {n.id: n for n in app.vms}
            for container in app.containers:
                if container.host:
                    hosts.setdefault(container.host.id, container.host)
            app.hosts = sorted(hosts.values(), key=lambda n: n.name.lower())
            app.images = sorted(
                {c.image.id: c.image for c in app.containers if c.image}.values(),
                key=lambda i: (-(i.num("CVE_critical") or 0), i.name.lower()),
            )
            app.cve_critical = sum(i.num("CVE_critical") or 0 for i in app.images)
            app.cve_high = sum(i.num("CVE_high") or 0 for i in app.images)
            app.updates = sum(1 for i in app.images if i.get("Update_available"))
            app.favicon_candidats = _favicon_candidats(app.get("URL") or app.get("URL 2"))
            app.favicon = app.favicon_candidats[0] if app.favicon_candidats else None
            app.initiale = (app.name or "?").strip()[:1].upper()
            # Stacks de l'application : c'est ce qui distingue trois conteneurs portant le
            # même nom sur trois hôtes différents.
            app.stacks = sorted({c.stack.key: c.stack for c in app.containers if c.stack}.values(),
                                key=lambda s: s.name.lower())
            app.hors_stack = [c for c in app.containers if not c.stack]

    def _sort(self):
        key = lambda o: o.name.lower()
        self.nodes.sort(key=key)
        self.containers.sort(key=key)
        self.apps.sort(key=key)
        # Une image qu'aucun conteneur n'utilise ne tourne nulle part. Ce n'est ni une
        # exposition — personne ne l'exécute — ni une saisie à protéger : le seul champ
        # manuel de cette table est `Notes`, et il sert peu. Sa ligne ne survit au délai de
        # grâce que pour épargner à Trivy une analyse de cinq à quinze minutes si elle
        # revient. C'est un cache, et un cache ne s'affiche pas.
        #
        # Le prix de l'avoir affichée : trente pour cent de lignes en trop dans la page
        # Sécurité, et quatre CVE critiques sur dix qui ne concernaient plus personne.
        #
        # Elles restent dans `by_kind`, donc leur fiche reste atteignable par son adresse.
        # Simplement, plus rien n'y mène et plus rien ne les compte.
        self.images_hors_service = [i for i in self.images if not i.containers]
        self.images = [i for i in self.images if i.containers]

        self.images.sort(key=lambda i: (-(i.num("CVE_critical") or 0),
                                        -(i.num("CVE_high") or 0), i.name.lower()))
        self.ips.sort(key=lambda ip: ip.num("IP_int") or _ip_to_int(ip.get("Address")) or 0)
        self.vlans.sort(key=lambda v: v.num("vlan_id") or 0)
        for node in self.nodes:
            # Par nom : on cherche « myTools » dans la liste, pas le vmid 101.
            node.children.sort(key=lambda n: n.name.lower())
            node.containers.sort(key=key)

    def _marquer_perdus(self):
        """Marque les assets que le collecteur ne revoit plus.

        Sans ça, un conteneur supprimé de son hôte continue d'afficher « Running » jusqu'à
        sa suppression effective, parfois trois jours plus tard : le collecteur ne met pas
        à jour ce qu'il ne voit plus, il se contente de ne plus toucher la ligne. La
        console affiche donc son dernier état connu — qui est faux.

        Le repère est le même que pour les assets en sursis : le retard sur les autres
        assets de la même table, et non l'âge absolu. Un collecteur à l'arrêt ne fait
        disparaître personne.
        """
        for population in (self.nodes, self.containers, self.images, self.ips):
            for obj in population:
                obj.perdu = False
            autos = [o for o in population if not o.is_manual and o.dt("Last_seen")]
            if not autos:
                continue
            reference = max(o.dt("Last_seen") for o in autos)
            for obj in autos:
                retard = (reference - obj.dt("Last_seen")).total_seconds() / 3600.0
                obj.perdu = retard > 1.5

    # -- vues dérivées -------------------------------------------------------
    @property
    def roots(self):
        """Racines de l'arbre : tout ce qui existe physiquement. Ce ne sont pas seulement
        les hyperviseurs — la moitié du parc (appliances, caméras, IoT) n'a pas de parent
        et n'en aura jamais."""
        return [n for n in self.nodes if not n.parent]

    @property
    def hypervisors(self):
        return [n for n in self.roots if n.children]

    @property
    def physical(self):
        """Tout ce qui existe matériellement, hyperviseurs compris.

        La séparation de l'écran Infrastructure est virtuel / physique, pas « a un parent
        ou non » : un hyperviseur reste une machine réelle, à ce titre il figure dans le
        parc physique au même titre qu'un NAS, même s'il apparaît aussi comme racine de
        l'arbre de virtualisation.
        """
        return [n for n in self.nodes if n.sel("Type") not in VIRTUAL_TYPES]

    @property
    def physical_groups(self):
        """Le parc physique réparti par type, chaque équipement dans un seul groupe."""
        groups = []
        physical = self.physical
        for type_name, label in DOMAINS:
            nodes = [n for n in physical if n.sel("Type") == type_name]
            if nodes:
                groups.append({"key": type_name, "label": label, "nodes": nodes})
        connus = {t for t, _ in DOMAINS}
        autres = [n for n in physical if n.sel("Type") not in connus]
        if autres:
            groups.append({"key": "autre", "label": "Autres", "nodes": autres})
        return groups

    @property
    def totals(self):
        """Cumuls du tableau de bord. Comptés par image et non par conteneur : une image
        partagée par trois conteneurs ne se corrige qu'une fois."""
        return {
            "cve_critical": sum(i.num("CVE_critical") or 0 for i in self.images),
            "cve_high": sum(i.num("CVE_high") or 0 for i in self.images),
            "scanned": sum(1 for i in self.images if i.get("Last_scan")),
            "images": len(self.images),
            "updates": sum(1 for i in self.images if i.get("Update_available")),
        }

    @property
    def park(self):
        """Répartition du parc par type, avec la part automatique et manuelle. C'est la
        vue qui dit le plus de choses en une ligne : la moitié des nœuds ne vient pas de
        Proxmox et ne peut donc être tenue à jour que par toi."""
        groups = {}
        for node in self.nodes:
            groups.setdefault(node.sel("Type") or "(sans type)", []).append(node)
        out = []
        for type_name, members in groups.items():
            auto = sum(1 for n in members if not n.is_manual)
            roles = sorted({r for n in members for r in n.multi("Roles")})
            out.append({
                "type": type_name,
                "count": len(members),
                "auto": auto,
                "manual": len(members) - auto,
                "auto_pct": round(100 * auto / len(members)),
                "roles": roles,
            })
        out.sort(key=lambda g: -g["count"])
        return out

    @property
    def top_cve(self):
        """Les images qui concentrent le plus de vulnérabilités critiques.

        Un total global ne dit pas par où commencer. Cette liste, oui : la correction de
        quelques images fait tomber l'essentiel du compte.
        """
        classees = [i for i in self.images if (i.num("CVE_critical") or 0) > 0][:8]
        maximum = max((i.num("CVE_critical") or 0) for i in classees) if classees else 1
        return [{"image": i,
                 "critical": i.num("CVE_critical") or 0,
                 "high": i.num("CVE_high") or 0,
                 "pct": round(100 * (i.num("CVE_critical") or 0) / maximum)}
                for i in classees]

    @property
    def hosts_load(self):
        """Conteneurs par hôte Docker, pour situer la concentration du parc."""
        par_hote = {}
        for container in self.containers:
            if container.host:
                par_hote.setdefault(container.host.id, {"host": container.host,
                                                        "total": 0, "running": 0})
                par_hote[container.host.id]["total"] += 1
                if container.sel("Status") == "Running":
                    par_hote[container.host.id]["running"] += 1
        lignes = sorted(par_hote.values(), key=lambda e: -e["total"])
        maximum = max((e["total"] for e in lignes), default=1)
        for entree in lignes:
            entree["pct"] = round(100 * entree["total"] / maximum)
            entree["pct_running"] = round(100 * entree["running"] / maximum)
        return lignes

    @property
    def hypervisor_capacity(self):
        """Ressources allouées aux invités, rapportées à celles de l'hyperviseur.

        Le sur-engagement processeur est normal et souhaitable — les cœurs se partagent.
        Le sur-engagement mémoire ne l'est pas : la RAM ne se partage pas, et une machine
        qui en promet plus qu'elle n'en a tombe le jour où les invités la réclament
        ensemble. D'où deux barres distinctes, et un seuil d'alerte sur la seconde
        seulement.

        Seuls les invités démarrés sont comptés : une VM arrêtée ne consomme rien, mais
        elle redeviendra une promesse au prochain démarrage — d'où le rappel du total.
        """
        out = []
        for hv in self.hypervisors:
            demarres = [g for g in hv.children if g.sel("Status") == "Running"]
            vcpu_hv = hv.num("vCPU") or 0
            ram_hv = hv.num("RAM_Gb") or 0
            vcpu = sum(g.num("vCPU") or 0 for g in demarres)
            ram = sum(g.num("RAM_Gb") or 0 for g in demarres)
            vcpu_total = sum(g.num("vCPU") or 0 for g in hv.children)
            ram_total = sum(g.num("RAM_Gb") or 0 for g in hv.children)
            out.append({
                "host": hv,
                "vcpu": vcpu, "vcpu_capacite": vcpu_hv, "vcpu_total": vcpu_total,
                "vcpu_pct": round(100 * vcpu / vcpu_hv) if vcpu_hv else 0,
                "ram": ram, "ram_capacite": ram_hv, "ram_total": ram_total,
                "ram_pct": round(100 * ram / ram_hv) if ram_hv else 0,
                "demarres": len(demarres), "invites": len(hv.children),
            })
        return out

    def get(self, kind, obj_id):
        return self.by_kind.get(kind, {}).get(obj_id)

    def queue(self, key):
        for q in self.queues:
            if q["key"] == key:
                return q
        return None

    # -- fraîcheur / sursis --------------------------------------------------
    def expiring(self):
        """Lignes automatiques que le collecteur ne revoit plus, et qui seront donc
        réellement supprimées. Le seul écran qui rende la suppression automatique
        confortable : elle se voit venir.

        Une ligne est en sursis quand elle est en retard *sur les autres lignes de sa
        table*, pas quand elle est simplement ancienne. La nuance est tout : si le
        collecteur est arrêté depuis douze heures, tout est ancien et rien n'est menacé —
        il reverra tout à son redémarrage. Comparer à l'horloge ferait clignoter les 217
        lignes à chaque interruption, et cette alerte ne voudrait plus rien dire.
        """
        now = datetime.now(timezone.utc)
        out = []
        for population in (self.nodes, self.containers, self.images, self.ips):
            autos = [o for o in population if not o.is_manual and o.dt("Last_seen")]
            if not autos:
                continue
            reference = max(o.dt("Last_seen") for o in autos)
            for obj in autos:
                seen = obj.dt("Last_seen")
                retard = (reference - seen).total_seconds() / 3600.0
                if retard < 1.5:
                    continue  # vue au même passage que les autres, à la marge près
                age = (now - seen).total_seconds() / 3600.0
                out.append({
                    "obj": obj,
                    "age_hours": age,
                    "retard_hours": retard,
                    "remaining_hours": self.cfg.RETIRE_GRACE_HOURS - age,
                    "seen": seen,
                })
        out.sort(key=lambda e: e["remaining_hours"])
        return out

    def sources_state(self):
        """Dernière écriture observée par source. Le collecteur ne publie pas son état :
        on le déduit du champ Last_seen le plus récent de chaque population, ce qui est
        exactement ce qu'on veut savoir (« ces données datent de quand ? »)."""
        now = datetime.now(timezone.utc)

        def latest(objs):
            seen = [o.dt("Last_seen") for o in objs if o.dt("Last_seen")]
            return max(seen) if seen else None

        rows = [
            {"name": "Proxmox → Nœuds", "seen": latest([n for n in self.nodes if not n.is_manual]),
             "count": sum(1 for n in self.nodes if not n.is_manual),
             "cadence": self.cfg.INTERVAL_NODES, "cle": "proxmox-noeuds",
             "ou_regarder": "PROXMOX_URL et le token API du collecteur "
                            "(il doit être créé avec --privsep 0)"},
            {"name": "Proxmox → IPAM", "seen": latest([i for i in self.ips if not i.is_manual]),
             "count": sum(1 for i in self.ips if not i.is_manual),
             "cadence": self.cfg.INTERVAL_NODES, "cle": "proxmox-ipam",
             "ou_regarder": "même source que les nœuds : si eux passent et pas l'IPAM, "
                            "regarder TABLE_VLAN et TABLE_IPAM"},
        ]
        by_host = {}
        for container in self.containers:
            host = container.host.name if container.host else "(hôte inconnu)"
            by_host.setdefault(host, []).append(container)
        for host in sorted(by_host):
            membres = by_host[host]
            perdus = sum(1 for c in membres if c.perdu)
            rows.append({
                "name": f"Docker → {host}", "seen": latest(membres), "count": len(membres),
                "cadence": self.cfg.INTERVAL_CONTAINERS, "cle": f"docker-{host}",
                # Le diagnostic le plus utile qu'on puisse produire sans sortir de la base :
                # tous les conteneurs d'un hôte muets en même temps, ce n'est pas une
                # coïncidence, c'est l'hôte qu'on ne joint plus.
                "ou_regarder": (f"les {perdus} conteneurs de cet hôte sont muets en même temps : "
                                f"le socket-proxy de {host} est probablement injoignable "
                                f"(port 2375), ou l'hôte est éteint")
                               if perdus == len(membres) and perdus
                               else f"le socket-proxy de {host}, port 2375",
            })

        rows.append({"name": "Docker → Images", "seen": latest(self.images),
                     "count": len(self.images), "cadence": self.cfg.INTERVAL_CONTAINERS,
                     "cle": "images",
                     "ou_regarder": "la passe Docker : les images sont écrites avec les conteneurs"})
        # Trivy est la seule passe à horodater son propre travail (Images.Last_scan). Cup
        # n'écrit que le résultat : sa date de dernier passage n'existe nulle part, on ne
        # peut donc pas l'afficher sans l'inventer.
        scans = [i.dt("Last_scan") for i in self.images if i.dt("Last_scan")]
        rows.append({"name": "Trivy → CVE", "seen": max(scans) if scans else None,
                     "count": len(scans), "cadence": self.cfg.INTERVAL_TRIVY, "cle": "trivy",
                     "ou_regarder": "TRIVY_ENABLED, et le volume /cache qui doit appartenir "
                                    "à l'UID 1000 — sans lui, chaque passage retélécharge "
                                    "un giga-octet de base de vulnérabilités"})

        for row in rows:
            row["age_hours"] = (now - row["seen"]).total_seconds() / 3600.0 if row["seen"] else None
            row["verdict"] = _verdict_fraicheur(row["age_hours"], row["cadence"])
        return rows

    # -- recherche -----------------------------------------------------------
    def controls(self, erreur_rechargement=None):
        """État des composants de la CMDB elle-même, et non de chaque collecte.

        La question posée par cet écran est « est-ce que mon dispositif d'inventaire
        fonctionne », pas « quand telle table a-t-elle été écrite pour la dernière fois ».
        Les composants sont donc les pièces du montage : le collecteur, la base, cette
        console, les socket-proxies, et les deux outils qu'il appelle.

        Une subtilité qui évite un écran de fausses alertes : **quand le collecteur est
        arrêté, l'état des socket-proxies est indéterminé, pas mauvais.** Personne ne les
        interroge, donc personne ne peut dire s'ils répondraient. Les afficher en défaut
        ferait clignoter cinq composants pour une seule panne, et masquerait la vraie.
        """
        composants = []
        now = datetime.now(timezone.utc)

        # -- Collecteur : déduit de l'écriture la plus récente, toutes tables confondues.
        ecritures = [o.dt("Last_seen")
                     for pop in (self.nodes, self.containers, self.images, self.ips)
                     for o in pop if not o.is_manual and o.dt("Last_seen")]
        derniere = max(ecritures) if ecritures else None
        age = (now - derniere).total_seconds() / 3600.0 if derniere else None
        verdict_collecteur = _verdict_fraicheur(age, self.cfg.INTERVAL_CONTAINERS)
        composants.append({
            "cle": "collecteur", "label": "Collecteur",
            "verdict": verdict_collecteur,
            "valeur": "arrêté" if verdict_collecteur == "ko" else (
                "en retard" if verdict_collecteur == "warn" else "actif"),
            "depuis": _depuis(age),
            "probleme": (f"aucune écriture depuis {_depuis(age)}, alors qu'une passe est "
                         f"attendue toutes les {_duree(self.cfg.INTERVAL_CONTAINERS / 3600)}")
                        if verdict_collecteur != "ok" else None,
        })

        composants.append({
            "cle": "baserow", "label": "Baserow",
            "verdict": "warn" if erreur_rechargement else "ok",
            "valeur": "injoignable" if erreur_rechargement else "disponible",
            "depuis": self.fetched_at and _depuis((now - self.fetched_at).total_seconds() / 3600.0),
            "probleme": f"dernier rechargement en échec : {erreur_rechargement}"
                        if erreur_rechargement else None,
        })

        composants.append({
            "cle": "console", "label": "Console web",
            "verdict": "ok", "valeur": "en ligne",
            "depuis": f"cache {self.cfg.CACHE_TTL} s", "probleme": None,
        })

        # -- Socket-proxies : un par hôte Docker inventorié.
        par_hote = {}
        for container in self.containers:
            if container.host:
                par_hote.setdefault(container.host.name, []).append(container)
        for hote in sorted(par_hote):
            membres = par_hote[hote]
            vus = [c.dt("Last_seen") for c in membres if c.dt("Last_seen")]
            age_hote = (now - max(vus)).total_seconds() / 3600.0 if vus else None
            if verdict_collecteur == "ko":
                verdict, valeur, probleme = "inconnu", "non interrogé", None
            else:
                verdict = _verdict_fraicheur(age_hote, self.cfg.INTERVAL_CONTAINERS)
                valeur = "répond" if verdict == "ok" else "muet"
                probleme = (f"les {len(membres)} conteneurs de {hote} ne sont plus remontés "
                            f"depuis {_depuis(age_hote)}") if verdict != "ok" else None
            composants.append({
                "cle": f"proxy-{hote}", "label": f"socket-proxy {hote}",
                "verdict": verdict, "valeur": valeur,
                "depuis": f"{len(membres)} conteneurs", "probleme": probleme,
            })

        # -- Cup : aucun horodatage propre, son état n'est pas mesurable.
        composants.append({
            "cle": "cup", "label": "Cup",
            "verdict": "inconnu" if verdict_collecteur == "ko" else "ok",
            "valeur": f"{self.totals['updates']} MAJ détectées",
            "depuis": "sans horodatage", "probleme": None,
        })

        scans = [i.dt("Last_scan") for i in self.images if i.dt("Last_scan")]
        age_trivy = (now - max(scans)).total_seconds() / 3600.0 if scans else None
        verdict_trivy = _verdict_fraicheur(age_trivy, self.cfg.INTERVAL_TRIVY)
        composants.append({
            "cle": "trivy", "label": "Trivy",
            "verdict": verdict_trivy,
            "valeur": f"{len(scans)} / {len(self.images)} images",
            "depuis": _depuis(age_trivy),
            "probleme": (f"dernière analyse il y a {_depuis(age_trivy)}, pour une cadence "
                         f"de {_duree(self.cfg.INTERVAL_TRIVY / 3600)}")
                        if verdict_trivy != "ok" else None,
        })
        return composants

    def search(self, query):
        query = (query or "").strip().lower()
        if len(query) < 2:
            return []
        results = []
        for obj, extras in self._search_index():
            haystack = " ".join(x for x in extras if x).lower()
            if query in haystack:
                results.append(obj)
        results.sort(key=lambda o: (0 if o.name.lower().startswith(query) else 1, o.name.lower()))
        return results[:60]

    def _search_index(self):
        for node in self.nodes:
            yield node, [node.name, node.get("OS"), node.sel("Type"), str(node.get("vmid") or ""),
                         " ".join(node.multi("Roles")), node.get("notes")]
        for container in self.containers:
            yield container, [container.name, container.get("Service"), container.get("Stack"),
                              container.get("Ports"), container.get("Compose_path"),
                              container.image.name if container.image else ""]
        for image in self.images:
            yield image, [image.name, image.get("Repository"), image.get("Registry"),
                          image.get("Tag"), image.get("Digest_local"), image.get("Available_version")]
        for ip in self.ips:
            yield ip, [ip.name, ip.get("MAC"), ip.get("Hostname"), ip.get("FQDN"), ip.get("Vendor")]
        for app in self.apps:
            yield app, [app.name, " ".join(app.multi("Capability")), app.get("URL"), app.get("Notes")]
        for vlan in self.vlans:
            yield vlan, [vlan.name, vlan.get("subnet"), str(vlan.get("vlan_id") or ""),
                         vlan.sel("zone"), vlan.get("gateway")]
        for stack in self.stacks:
            yield stack, [stack.name, stack.compose_path,
                          stack.host.name if stack.host else ""]


# --------------------------------------------------------------------------- files d'attente

def _q(key, label, hint, severity, objets, unit):
    # Clé "objets" et non "items" : dans un gabarit Jinja, `file.items` résout la méthode
    # du dictionnaire, pas la valeur — l'erreur est silencieuse jusqu'à l'affichage.
    return {"key": key, "label": label, "hint": hint, "severity": severity,
            "objets": objets, "count": len(objets), "unit": unit}


def _build_queues(snap):
    """Ce qui demande une action, du plus coûteux à ignorer au moins coûteux.

    Une file vide n'est pas retirée de la liste : voir « 0 » est une information, et
    faire disparaître la ligne ferait douter de sa présence au prochain problème.
    """
    images_crit = [i for i in snap.images if (i.num("CVE_critical") or 0) > 0]
    images_update = [i for i in snap.images if i.get("Update_available")]
    never_scanned = [i for i in snap.images if not i.get("Last_scan")]

    guests = [n for n in snap.nodes if n.sel("Type") in ("VM", "LXC")]
    stopped = [n for n in guests if n.sel("Status") == "Stopped"]
    no_backup = [n for n in guests if not n.get("Backup")]

    containers_down = [c for c in snap.containers if c.sel("Status") in ("Exited", "Restarting")]
    containers_no_app = [c for c in snap.containers if not c.apps]
    containers_standalone = [c for c in snap.containers if c.sel("Type") == "Container"]

    ip_to_check = [i for i in snap.ips if i.sel("Status") == "To check"]
    nodes_no_ip = [n for n in snap.nodes if not n.ips]
    apps_empty = [a for a in snap.apps if not a.containers and not a.vms]

    return [
        _q("cve", "Images portant au moins une CVE critique",
           "corrigeable par une mise à jour d'image dans la plupart des cas",
           "crit", images_crit, "images"),
        _q("containers-down", "Conteneurs arrêtés ou en redémarrage permanent",
           "un redémarrage en boucle est une panne, pas un état",
           "crit", containers_down, "conteneurs"),
        _q("updates", "Images avec une version plus récente disponible",
           "remontée par Cup depuis le registre d'origine",
           "warn", images_update, "images"),
        _q("no-backup", "Guests Proxmox hors de toute sauvegarde",
           "calculé sur les VM et LXC uniquement — les équipements manuels n'ont pas cette information",
           "warn", no_backup, "guests"),
        _q("stopped", "VM et LXC arrêtés",
           "gabarits assumés, ou candidats au ménage",
           "warn", stopped, "guests"),
        _q("ip-check", "Adresses IP marquées « à vérifier »",
           "statut posé à la main, jamais par le collecteur",
           "warn", ip_to_check, "adresses"),
        _q("containers-no-app", "Conteneurs rattachés à aucune application",
           "le lien est manuel : sans lui, impossible de savoir ce qu'une CVE menace vraiment",
           "info", containers_no_app, "conteneurs"),
        _q("standalone", "Conteneurs lancés hors de tout docker compose",
           "rien ne les redéploie à l'identique en cas de perte de l'hôte",
           "info", containers_standalone, "conteneurs"),
        _q("nodes-no-ip", "Nœuds sans aucune adresse IP connue",
           "présents à l'inventaire, injoignables depuis la CMDB",
           "info", nodes_no_ip, "nœuds"),
        _q("never-scanned", "Images jamais analysées par Trivy",
           "aucune information CVE, ni bonne ni mauvaise",
           "info", never_scanned, "images"),
        _q("apps-empty", "Applications sans aucune ressource rattachée",
           "sans ressource, une application n'a plus lieu d'exister",
           "info", apps_empty, "applications"),
    ]


# --------------------------------------------------------------------------- utilitaires réseau

# Emplacements conventionnels, essayés dans l'ordre par le navigateur quand la découverte
# côté serveur n'a rien donné.
_CHEMINS_ICONE = ("/favicon.ico", "/apple-touch-icon.png", "/favicon.png", "/favicon.svg")


def _verdict_fraicheur(age_heures, cadence_secondes):
    """Une date n'est un problème que rapportée à ce qu'on attendait.

    « Il y a quatre heures » est parfaitement normal pour Trivy, qui passe une fois par
    jour, et franchement inquiétant pour les conteneurs, attendus tous les quarts d'heure.
    Le verdict est donc toujours relatif à la cadence configurée, jamais à l'horloge.

    Deux passages manqués restent du bruit : un redémarrage, une coupure réseau. Au-delà
    de dix, ce n'est plus un aléa.
    """
    if age_heures is None:
        return "ko"
    attendu = max(cadence_secondes, 60) / 3600.0
    if age_heures <= attendu * 2:
        return "ok"
    if age_heures <= attendu * 10:
        return "warn"
    return "ko"


def _favicon_candidats(url):
    """Liste d'URL d'icône à essayer, de la plus probable à la moins probable.

    C'est le NAVIGATEUR qui les essaie, une par une, en repli sur l'échec de la
    précédente (voir macros.html). Ce n'est pas qu'un détail d'implémentation : le poste
    qui affiche la page a des droits que ce service n'a pas. Un reverse proxy filtrant
    par IP source répond 403 à la console et sert la vraie page au navigateur — dans ce
    cas la découverte échoue, mais l'icône s'affiche quand même.

    Une application sans icône, ou injoignable, retombe sur son initiale.
    """
    if not url:
        return []
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return []
    origine = f"{parts.scheme}://{parts.netloc}"
    return [origine + chemin for chemin in _CHEMINS_ICONE]


def _depuis(heures):
    if heures is None:
        return "jamais"
    if heures < 1.5:
        return f"{int(heures * 60)} min"
    if heures < 48:
        return f"{heures:.0f} h"
    return f"{heures / 24:.0f} j"


def _duree(heures):
    if heures < 1:
        return f"{int(heures * 60)} min"
    if heures < 48:
        return f"{heures:.0f} h"
    return f"{heures / 24:.0f} j"


def _lister_sursis(sursis):
    """Nomme les assets concernés plutôt que de renvoyer à un tableau : savoir quoi
    chercher vaut mieux que savoir où chercher."""
    noms = [e["obj"].name for e in sursis[:4]]
    reste = len(sursis) - len(noms)
    texte = ", ".join(noms)
    return texte + (f" et {reste} autres" if reste > 0 else "")


def _parse_network(subnet):
    if not subnet:
        return None
    try:
        return ipaddress.ip_network(subnet.strip(), strict=False)
    except ValueError:
        logger.warning("Sous-réseau VLAN illisible : %r", subnet)
        return None


def _ip_to_int(address):
    if not address:
        return None
    try:
        return int(ipaddress.ip_address(address.strip()))
    except ValueError:
        return None


def vlan_grid(vlan):
    """Grille des 256 adresses d'un /24. Les autres préfixes renvoient None : dessiner
    2^16 cases n'aiderait personne, et un /30 en grille n'a pas de sens non plus."""
    network = vlan.network
    if network is None or network.version != 4 or network.prefixlen != 24:
        return None
    by_host = {}
    for ip in vlan.ips:
        value = _ip_to_int(ip.get("Address"))
        if value is not None and ipaddress.ip_address(value) in network:
            by_host[value - int(network.network_address)] = ip
    gateway = _ip_to_int(vlan.get("gateway"))
    gateway_host = (gateway - int(network.network_address)) if gateway is not None else None

    cells = []
    for host in range(256):
        ip = by_host.get(host)
        if ip is not None:
            state = "manual" if ip.is_manual else "auto"
            if ip.sel("Status") == "To check":
                state = "check"
            elif ip.sel("Status") == "Reserved":
                state = "reserved"
        elif host in (0, 255):
            state = "network"
        elif host == gateway_host:
            state = "gateway"
        else:
            state = "free"
        cells.append({"host": host, "address": f"{network.network_address + host}",
                      "ip": ip, "state": state})
    return cells


# --------------------------------------------------------------------------- cache

class Store:
    """Un instantané partagé, rafraîchi au plus toutes les `CACHE_TTL` secondes.

    Un verrou suffit : les six requêtes prennent une seconde, et deux navigateurs qui
    rafraîchissent en même temps doivent attendre le même tirage plutôt qu'en lancer deux.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.client = BaserowClient(cfg.BASEROW_URL, cfg.BASEROW_TOKEN,
                                    verify_tls=cfg.BASEROW_VERIFY_TLS, timeout=30)
        self.favicons = FaviconResolver(enabled=cfg.FAVICON_DISCOVERY,
                                        verify_tls=cfg.BASEROW_VERIFY_TLS)
        self._lock = threading.Lock()
        self._snapshot = None
        self._loaded_at = 0.0
        self.derniere_erreur = None

    def get(self, force=False):
        with self._lock:
            fresh = self._snapshot is not None and (time.time() - self._loaded_at) < self.cfg.CACHE_TTL
            if fresh and not force:
                return self._snapshot
            try:
                self._snapshot = self._load()
                self._loaded_at = time.time()
                self.derniere_erreur = None
            except Exception as exc:
                if self._snapshot is None:
                    # Aucun instantané, même périmé : il n'y a rien à montrer. Cette
                    # exception dédiée permet d'afficher une page d'indisponibilité qui
                    # désigne la vraie cause — Baserow — plutôt qu'une trace d'erreur qui
                    # ferait chercher le défaut dans la console.
                    raise BaserowIndisponible(str(exc)) from exc
                # Baserow indisponible : mieux vaut un instantané daté, clairement
                # horodaté à l'écran, qu'une page d'erreur.
                self.derniere_erreur = str(exc) or exc.__class__.__name__
                logger.exception("Rechargement impossible, instantané précédent conservé")
            return self._snapshot

    def _load(self):
        started = time.time()
        rows = {kind: self.client.fetch_all(table_id) for kind, table_id in self.cfg.TABLES.items()}
        snapshot = Snapshot(self.cfg, rows, datetime.now(timezone.utc))
        # Après coup, et sans bloquer la construction du graphe : une application éteinte
        # ne doit pas retarder l'affichage de tout le reste.
        self.favicons.apply(snapshot.apps)
        logger.info("Instantané rechargé en %.2fs : %s",
                    time.time() - started,
                    ", ".join(f"{len(v)} {k}" for k, v in rows.items())
                    + f", {len(snapshot.stacks)} stacks")
        return snapshot

    @property
    def age_seconds(self):
        return time.time() - self._loaded_at if self._snapshot else None
