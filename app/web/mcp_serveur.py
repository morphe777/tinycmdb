"""Serveur MCP de TinyCMDB, sous /mcp (transport « streamable HTTP »).

Mêmes jetons que l'API (`Authorization: Bearer tcmdb_…`), mêmes règles : chaque outil
passe par `exposition.py`, donc par le rédacteur des formulaires de l'interface.

Le mode d'emploi destiné à l'agent existe en trois exemplaires d'un même texte
(`guide_mcp.md`) : les *instructions* envoyées à l'initialisation de la session, un outil
`guide` pour les clients qui ne transmettent pas ces instructions au modèle, et une
ressource `tinycmdb://guide`.

Sans état (`stateless_http`) : chaque requête porte son jeton et se suffit à elle-même.
Rien à garder entre deux appels, rien à perdre au redémarrage de la console.
"""

import contextvars
import json
import logging

import anyio
from cmdb.depot import BaseInaccessible
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import version

from . import ecriture, exposition

logger = logging.getLogger("cmdb.web.mcp")

_jeton = contextvars.ContextVar("jeton_mcp", default=None)

INSTRUCTIONS = """TinyCMDB est l'inventaire d'infrastructure : nœuds (machines physiques, VM, LXC, \
équipements), leur matériel (processeur, carte mère, mémoire, cartes PCIe, disques, \
périphériques USB et la VM qui les reçoit), la configuration Proxmox de chaque invité, \
conteneurs et stacks Docker, images et leurs CVE, adresses IP, VLAN, applications au sens \
métier, et les liens entre eux.

Commence par l'outil `guide` si tu n'as jamais utilisé ce serveur : il explique le modèle, \
la règle de provenance des champs et le format des écritures.

L'essentiel : `rechercher` pour trouver, `contexte` pour comprendre un objet et tout son \
voisinage en un appel (hôte, matériel, invités, applications), `lire` pour le détail d'un \
objet et la liste `modifiables`, `lister` avec `filtres` pour interroger le parc \
(« les machines arm64 », « les disques d'usure >= 50 »), `capacite` pour la place qui reste \
sur les hyperviseurs, `journal(depuis=…)` pour ce qui a changé. `modifier` / `creer` pour \
écrire : seuls les champs listés dans `modifiables` s'écrivent — les autres sont réécrits \
par le collecteur. Les caractéristiques d'un modèle de processeur (type `cpu`) se \
saisissent une fois et servent ensuite à toutes les machines qui le portent : si tu les \
connais et qu'elles manquent, propose de les compléter. Rien ne se supprime par ce serveur."""

LECTURE = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
ECRITURE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                           openWorldHint=False)


def _courant(droit="lecture"):
    jeton = _jeton.get()
    if jeton is None:
        raise ToolError("Jeton absent.")
    if droit == "ecriture" and jeton["droits"] != "ecriture":
        raise ToolError(f"Le jeton « {jeton['nom']} » est en lecture seule : cet outil "
                        "demande un jeton en écriture (Administration → Jetons).")
    return jeton


def creer(data, redacteur):
    serveur = MCPServer(name="TinyCMDB", title="TinyCMDB — inventaire d'infrastructure",
                        instructions=INSTRUCTIONS, version=version.VERSION)

    def snap():
        return data.get()

    def _objet(type_, ident):
        try:
            kind = exposition.kind_de(type_)
        except exposition.Introuvable as exc:
            raise ToolError(str(exc))
        s = snap()
        obj = s.get(kind, int(ident))
        if obj is None:
            raise ToolError(f"{type_} {ident} introuvable. Utilise `rechercher` pour retrouver "
                            "son identifiant.")
        return s, obj

    @serveur.tool(annotations=LECTURE)
    def guide() -> str:
        """Mode d'emploi complet de ce serveur : modèle, provenance des champs, format des
        écritures, limites. À lire avant la première écriture."""
        _courant()
        return exposition.guide()

    @serveur.resource("tinycmdb://guide", name="guide", mime_type="text/markdown",
                      description="Mode d'emploi de TinyCMDB pour un agent")
    def ressource_guide() -> str:
        return exposition.guide()

    @serveur.tool(annotations=LECTURE)
    def schema(type: str | None = None) -> dict:
        """Tables et champs : nom exact, type, provenance possible (auto / enrichi / manuel)
        et, pour les listes, les options admises. `type` limite la réponse à un type :
        noeud, conteneur, image, ip, vlan, application, cpu ou composant."""
        _courant()
        try:
            return exposition.schema_public(snap(), type)
        except exposition.Introuvable as exc:
            raise ToolError(str(exc))

    @serveur.tool(annotations=LECTURE)
    def rechercher(texte: str) -> dict:
        """Recherche dans tout l'inventaire : nom, IP, MAC, FQDN, OS, ports, stack, chemin
        de compose, image, rôle, processeur, carte mère, composant (modèle, chipset, numéro
        de série)… Renvoie au plus 60 références {type, id, nom}."""
        _courant()
        if len((texte or "").strip()) < 2:
            raise ToolError("Deux caractères au moins.")
        return {"resultats": [exposition.resume(o) for o in snap().search(texte)]}

    @serveur.tool(annotations=LECTURE)
    def lister(type: str, filtre: str | None = None, filtres: dict | None = None,
               champs: list[str] | None = None, vides: bool = False, limite: int = 50,
               depart: int = 0) -> dict:
        """Liste les objets d'un type (noeud, conteneur, image, ip, vlan, application, cpu,
        composant), avec leurs champs.

        `filtre` : texte cherché dans tous les champs. `filtres` : conditions exactes, toutes
        requises — {"Arch": "arm64"}, {"RAM_Gb": ">=16"}, {"Category": "Disk",
        "Wearout": ">=50"}, {"Host": "pve1"} (un lien se compare par le nom de sa cible),
        {"Motherboard": null} (champ vide), {"BIOS": "!="} (champ renseigné). Opérateurs :
        >=, <=, >, <, !=. `champs` : ne rendre que ces champs. `vides` : rendre aussi les
        champs vides (faux par défaut). Pagination par `limite` (200 au plus) et `depart`."""
        _courant()
        try:
            kind = exposition.kind_de(type)
            s = snap()
            total, objets = exposition.lister(s, kind, filtre, max(1, min(limite, 200)),
                                              max(0, depart), filtres)
        except exposition.Introuvable as exc:
            raise ToolError(str(exc))
        return {"total": total, "depart": depart,
                "objets": [exposition.ligne_liste(s, o, vides=vides, choisis=champs)
                           for o in objets]}

    @serveur.tool(annotations=LECTURE)
    def contexte(type: str, id: int | None = None, cle: str | None = None) -> dict:
        """Un objet et tout son voisinage en un seul appel : pour un nœud, son hôte, son
        processeur et ses caractéristiques, ses composants et ceux qui lui sont attribués,
        ses invités, conteneurs et adresses ; pour un conteneur ou une application, la
        chaîne hôte → hyperviseur → matériel. À préférer à une suite de `lire` dès qu'il
        s'agit de raisonner (dépannage, conception). `type` + `id`, ou `type="stack"` +
        `cle` (« hôte/nom »)."""
        _courant()
        if type == "stack":
            s = snap()
            st = s.stacks_by_key.get(cle or "")
            if st is None:
                raise ToolError(f"stack {cle} introuvable. `stacks` les liste toutes.")
            return exposition.contexte_stack(s, st)
        if id is None:
            raise ToolError("`id` est requis (ou `type=\"stack\"` avec `cle`).")
        s, obj = _objet(type, id)
        return exposition.contexte(s, obj)

    @serveur.tool(annotations=LECTURE)
    def capacite() -> dict:
        """Par hyperviseur : processeur et cœurs physiques, mémoire (type, ECC, maximum),
        disques, et ce qui est promis aux invités — vCPU par cœur physique, mémoire allouée
        et libre — avec les périphériques passés à des VM. Pour décider où placer une
        nouvelle machine."""
        _courant()
        return {"hyperviseurs": exposition.capacite(snap())}

    @serveur.tool(annotations=LECTURE)
    def lire(type: str, id: int) -> dict:
        """Un objet complet : champs, provenance de chacun, liens résolus, et `modifiables`
        — la liste exacte des champs que `modifier` acceptera sur cet objet."""
        _courant()
        s, obj = _objet(type, id)
        return exposition.objet(s, obj)

    @serveur.tool(annotations=LECTURE)
    def stacks() -> dict:
        """Toutes les stacks Docker Compose : hôte, état, conteneurs, applications servies
        et leur criticité, CVE cumulées."""
        _courant()
        s = snap()
        return {"stacks": [exposition.stack(s, st)
                           for st in sorted(s.stacks, key=lambda st: st.key.lower())]}

    @serveur.tool(annotations=LECTURE)
    def stack(cle: str) -> dict:
        """Une stack, par sa clé « hôte/nom » (ex. myTools/tinycmdb)."""
        _courant()
        s = snap()
        st = s.stacks_by_key.get(cle)
        if st is None:
            raise ToolError(f"stack {cle} introuvable. `stacks` les liste toutes.")
        return exposition.stack(s, st)

    @serveur.tool(annotations=LECTURE)
    def a_traiter() -> dict:
        """Files d'action, de la plus coûteuse à ignorer à la moins coûteuse : chacune avec
        son explication et les objets concernés."""
        _courant()
        return {"files": exposition.a_traiter(snap())}

    @serveur.tool(annotations=LECTURE)
    def sursis() -> dict:
        """Lignes que le collecteur ne revoit plus, et le temps qu'il leur reste avant
        suppression automatique."""
        _courant()
        return {"lignes": exposition.sursis(snap())}

    @serveur.tool(annotations=LECTURE)
    def etat() -> dict:
        """Santé du dispositif : collecteur, base, sauvegarde, socket-proxies, Trivy, Cup,
        fraîcheur de chaque source, et totaux de l'inventaire."""
        _courant()
        return exposition.etat(snap())

    @serveur.tool(annotations=LECTURE)
    def journal(type: str | None = None, id: int | None = None, acteur: str | None = None,
                depuis: str | None = None, champ: str | None = None,
                limite: int = 50) -> dict:
        """Historique, du plus récent au plus ancien : écritures de l'interface, de l'API et
        du MCP, et ce que le collecteur a constaté — créations, suppressions, et chaque
        champ qui a changé (un conteneur passé à Exited, une image mise à jour, une VM dont
        la mémoire ou la configuration a bougé). C'est la question « qu'est-ce qui a changé
        depuis… » d'un dépannage : `depuis` (date ISO 8601) borne la période. Filtrable par
        objet, par champ et par acteur (`interface`, `collecteur`, `api:<jeton>`,
        `mcp:<jeton>` ; une partie du nom suffit)."""
        _courant()
        try:
            return {"entrees": exposition.journal(data.depot, type, id, acteur, limite,
                                                  depuis, champ)}
        except exposition.Introuvable as exc:
            raise ToolError(str(exc))

    @serveur.tool(annotations=ECRITURE)
    def modifier(type: str, id: int, champs: dict) -> dict:
        """Modifie des champs manuels d'un objet. `champs` : {nom exact: valeur}. Seuls les
        champs de `modifiables` (voir `lire`) sont acceptés. Une liste ou un lien
        remplace la valeur actuelle. Renvoie les changements et l'objet à jour."""
        jeton = _courant("ecriture")
        s, obj = _objet(type, id)
        try:
            changements = exposition.modifier(s, redacteur, obj, champs, f"mcp:{jeton['nom']}")
        except (ecriture.EcritureRefusee, ecriture.EcritureImpossible) as exc:
            raise ToolError(str(exc))
        s = data.get(force=True)
        return {"changements": changements, "objet": exposition.objet(s, s.get(obj.kind, obj.id))}

    @serveur.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                              idempotentHint=False, openWorldHint=False))
    def creer(type: str, champs: dict) -> dict:
        """Crée un nœud, une adresse IP, un VLAN, une application, un processeur (`cpu`,
        un modèle du référentiel) ou un composant (une carte, un disque, un périphérique
        d'une machine que Proxmox ne voit pas — `Host` désigne la machine). La clé naturelle
        est obligatoire (Name, Address ou name). La ligne est marquée saisie manuelle : le
        collecteur ne la touchera pas. Renvoie l'objet créé."""
        jeton = _courant("ecriture")
        try:
            kind = exposition.kind_de(type)
            ident = exposition.creer(snap(), redacteur, kind, champs, f"mcp:{jeton['nom']}")
        except exposition.Introuvable as exc:
            raise ToolError(str(exc))
        except (ecriture.EcritureRefusee, ecriture.EcritureImpossible) as exc:
            raise ToolError(str(exc))
        s = data.get(force=True)
        return exposition.objet(s, s.get(kind, ident))

    @serveur.tool(annotations=ECRITURE)
    def rattacher_stack(cle: str, applications: list[int]) -> dict:
        """Fixe les applications servies par une stack : la liste est posée sur tous ses
        conteneurs, et remplace l'ancienne. Une liste vide détache la stack."""
        jeton = _courant("ecriture")
        s = snap()
        st = s.stacks_by_key.get(cle)
        if st is None:
            raise ToolError(f"stack {cle} introuvable.")
        try:
            n = exposition.rattacher_stack(s, redacteur, st, applications, f"mcp:{jeton['nom']}")
        except (ecriture.EcritureRefusee, ecriture.EcritureImpossible) as exc:
            raise ToolError(str(exc))
        s = data.get(force=True)
        return {"conteneurs_modifies": n, "stack": exposition.stack(s, s.stacks_by_key[cle])}

    # L'application HTTP est créée ici : c'est elle qui instancie le gestionnaire de
    # sessions, que le cycle de vie de la console démarre ensuite.
    serveur.asgi = serveur.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        # Pas de liste d'hôtes autorisés : c'est le jeton qui protège ce point d'accès, et
        # un navigateur piégé ne peut pas l'ajouter à une requête d'un autre site.
        host="0.0.0.0")
    return serveur


class _Garde:
    """Vérifie le jeton avant de laisser la requête atteindre le serveur MCP, et le rend
    visible aux outils par une variable de contexte."""

    def __init__(self, application_mcp, data):
        self.app = application_mcp
        self.data = data

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        entete = ""
        for nom, valeur in scope.get("headers", []):
            if nom == b"authorization":
                entete = valeur.decode("latin-1")
                break
        secret = entete[7:].strip() if entete.lower().startswith("bearer ") else None
        try:
            jeton = await anyio.to_thread.run_sync(self.data.depot.verifier_jeton, secret) \
                if secret else None
        except BaseInaccessible as exc:
            corps = json.dumps({"error": "base indisponible", "detail": str(exc)},
                               ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(corps)).encode())]})
            await send({"type": "http.response.body", "body": corps})
            return
        if jeton is None:
            corps = json.dumps({"error": "jeton requis",
                                "detail": "En-tête « Authorization: Bearer <jeton> ». Les jetons "
                                          "se créent dans Administration → Jetons."},
                               ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"www-authenticate", b'Bearer realm="tinycmdb"'),
                                    (b"content-length", str(len(corps)).encode())]})
            await send({"type": "http.response.body", "body": corps})
            return
        marque = _jeton.set(jeton)
        try:
            await self.app(scope, receive, send)
        finally:
            _jeton.reset(marque)


def application(serveur, data):
    return _Garde(serveur.asgi, data)
