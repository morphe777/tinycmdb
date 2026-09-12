<img src="docs/logo.svg" width="72" align="left" alt="">

# TinyCMDB

**L'inventaire d'un homelab, et rien de plus.** Un collecteur qui découvre
Proxmox, Docker, les vulnérabilités et les mises à jour disponibles, et une
console web qui distingue à chaque champ ce qu'il a écrit de ce que vous avez
saisi.

<br clear="left">

*[English version](README.md) — version de référence.*

---

Une CMDB de homelab se dégrade toujours de la même façon : on y saisit quelque
chose d'utile, un script le réécrit au passage suivant, on ne s'en aperçoit pas,
et on cesse d'y croire. TinyCMDB ne résout qu'un problème, mais entièrement :
**savoir, champ par champ, qui est propriétaire de quoi** — et ne jamais laisser
saisir dans un champ dont la disparition est programmée.

Le reste en découle. Un collecteur qui ne touche qu'à ce qu'il possède. Une
console qui n'ouvre à l'édition que ce qui survivra. Une base qui reflète ce qui
est en place plutôt qu'un historique.

### Ce que ça fait

- **Découvre** les nœuds Proxmox (hyperviseurs, VM, LXC) et les conteneurs de
  chaque hôte Docker, via un socket-proxy en lecture seule.
- **Analyse** les images avec [Trivy](https://github.com/aquasecurity/trivy)
  (CVE) et [Cup](https://github.com/sergi0g/cup) (mises à jour disponibles).
- **Tient un IPAM** par VLAN, avec une grille d'occupation du /24 où une case
  libre est un lien vers sa propre réservation.
- **Relie le tout** : hyperviseur → invité → conteneur → image → CVE, et
  l'application métier en travers — ce qui traduit « cette image porte 41
  vulnérabilités critiques » en « ce service-là est concerné ».
- **Supprime** ce qui a disparu, après un délai de grâce, et le montre venir.

### Ce que ce n'est pas

Ce n'est ni NetBox, ni GLPI, ni i-doit. Pas de gestion de parc, pas de tickets,
pas de workflow d'approbation, pas de multi-tenant, pas d'historique. Le projet
vise quelques centaines de lignes — un parc qu'une personne peut tenir dans sa
tête — et ce plafond n'est pas une limite subie, c'est l'objectif : avoir ce
qu'il faut et pas plus. Si l'inventaire devient lourd, c'est l'infrastructure
qu'il faut regarder, pas le code.

### Prérequis

- **[Baserow](https://baserow.io/)**, qui sert de stockage. TinyCMDB n'embarque
  pas de base : il écrit dans six tables Baserow par API. Voir
  [Modèle de données](#modèle-de-données) pour leur structure.
- **Docker** et **Docker Compose**.
- Au moins une source à inventorier : un cluster **Proxmox VE** et/ou des hôtes
  **Docker** joignables par socket-proxy.

## Démarrage

```bash
cp .env.example .env
chmod 600 .env
# remplir .env : BASEROW_URL, BASEROW_TOKEN, TABLE_*, et PROXMOX_URL et/ou DOCKER_HOSTS
docker compose up -d --build
docker compose logs -f collector
```

La console est alors sur le port 8080 de l'IP déclarée dans `compose.yaml`
(`192.168.10.11` est une valeur d'exemple, à remplacer par celle de votre hôte
sur le VLAN d'administration — jamais `0.0.0.0` : cette console agrège tout
l'inventaire).

Le code est dans l'image. Pour développer sans reconstruire à chaque ligne, le
remonter par-dessus :

```bash
cp compose.override.yaml.example compose.override.yaml
docker compose up -d          # ./app est désormais monté en lecture seule
docker compose restart web    # après une modification
```

Cette surcharge est ignorée par git et n'a rien à faire sur une machine de
production : l'intérêt d'une image est que ce qui tourne soit exactement ce qui a
été construit.

Pour tester en une seule passe sans attendre la boucle : `RUN_ONCE=true` dans
`.env`, puis `docker compose run --rm collector` (ou lancer `python -m collector.main`
directement dans un venv local, cf. section Tests locaux).

## Tests locaux (sans Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)  # ou source un fichier séparé
cd app && python -m collector.main
```

La console web se lance depuis le même dossier `app/` :

```bash
cd app && uvicorn web.main:app --host 0.0.0.0 --port 8099 --reload
```

Les binaires `cup` et `trivy` ne sont installés que dans l'image. Pour les tester
hors Docker, les extraire de leurs images officielles vers le venv :

```bash
id=$(docker create ghcr.io/sergi0g/cup) && docker cp "$id:/cup" .venv/bin/cup && docker rm "$id"
id=$(docker create aquasec/trivy) && docker cp "$id:/usr/local/bin/trivy" .venv/bin/trivy && docker rm "$id"
```

## Prérequis Proxmox

Le token API **doit** être créé avec `--privsep 0`. C'est le piège le plus
fréquent sur ce sujet : avec la séparation de privilèges activée, le token
n'hérite d'aucun droit, et l'API renvoie une liste vide **sans erreur explicite**
— rien ne plante, l'inventaire est juste vide. Le collecteur logge un
avertissement explicite dans ce cas précis (voir `sources/proxmox.py`), mais
mieux vaut le savoir avant :

```bash
pveum user add cmdb@pve --comment "Collecteur CMDB"
pveum acl modify / --users cmdb@pve --roles PVEAuditor
pveum user token add cmdb@pve collector --privsep 0
```

## Prérequis Docker : socket-proxy

Le collecteur ne parle jamais au socket Docker en TCP brut (équivalent root).
Déployer `socket-proxy.compose.yaml` sur **chaque** hôte Docker à inventorier
(généralement une VM Proxmox) :

```bash
docker compose -f socket-proxy.compose.yaml up -d
```

Points d'attention dans ce fichier :
- Port bindé sur l'IP du VLAN management, jamais `0.0.0.0` — Docker manipule
  iptables directement et contourne les règles ufw de l'hôte.
- `POST=0`, `read_only: true`, socket monté `:ro` : lecture seule stricte.
- `CONTAINERS=1` et `IMAGES=1` seulement : rien d'autre n'est exposé.

Dans `DOCKER_HOSTS`, le nom donné à chaque hôte **doit correspondre exactement**
à son `Node.name` tel que découvert par Proxmox. Une différence d'orthographe
(casse, tiret, etc.) laisse `Container.host` vide sans erreur — c'est le premier
problème que rencontrera l'utilisateur. Le collecteur détecte ce cas et logge un
avertissement listant les noms non résolus à chaque passe.

### Découverte automatique des hôtes Docker

Plutôt que de maintenir `DOCKER_HOSTS` à la main pour chaque VM Docker, taguer le
`Node` correspondant avec le rôle `docker` directement dans Baserow. À la passe
suivante, le collecteur le détecte et construit l'URL de son socket-proxy par
convention : `http://{Node.name}{DNS_SUFFIX}:{DOCKER_SOCKET_PROXY_PORT}`.

C'est cohérent avec la propriété du champ `roles` : Proxmox n'écrit jamais que
l'option `hypervisor` (et seulement sur les nœuds physiques) — `docker` est un tag
manuel, exactement comme `database` ou `network`. Le collecteur fusionne toujours
`roles` avec l'existant plutôt que de l'écraser, pour ne jamais effacer ce genre de
tag manuel posé sur un Node par ailleurs auto-découvert par Proxmox.

`DOCKER_HOSTS` reste utile dans deux cas : un hôte Docker qui n'a pas de `Node`
Proxmox (bare-metal, hors périmètre Proxmox), ou pour surcharger l'URL reconstruite
par convention si elle ne convient pas pour un hôte donné.

## Modèle de données

Six tables à créer dans Baserow. TinyCMDB ne crée jamais de champ et n'en
renomme aucun : il les lit par leur nom (`user_field_names`), et **un champ
renommé revient vide sans erreur** — c'est le piège le plus coûteux de ce
montage. Les identifiants des tables se déclarent dans `.env` (`TABLE_*`).

La **clé naturelle** est ce sur quoi le collecteur réconcilie : jamais l'id
interne de Baserow, qui ne veut rien dire hors de Baserow.

| Table | Clé naturelle | Écrite par |
|---|---|---|
| `Node` | `Name` | Proxmox — sauf les lignes `Source = Manual` |
| `Container` | `UID` (`{hôte}/{nom}`) | Docker, intégralement |
| `Images` | `Reference` | Docker, Trivy, Cup |
| `IPAM` | `Address` | Proxmox, Docker — enrichissement des lignes manuelles |
| `VLAN` | `name` | personne : référentiel, saisi à la main |
| `Application` | `Name` | personne : regroupement métier, saisi à la main |

Les champs attendus, table par table :

- **Node** — `Name`, `Type` (liste : Physical, VM, LXC, Appliance, Device,
  Camera, IoT), `Parent_host` (lien → Node), `vmid`, `Status` (liste), `OS`,
  `vCPU`, `RAM_Gb`, `Disk_Gb`, `Backup` (booléen), `Guest_agent` (booléen),
  `Source` (liste : Auto, Manual), `Last_seen` (date), `Container` (lien →
  Container), `IPAM` (lien → IPAM), `Roles` (liste multiple), `Criticality`
  (liste), `Application` (lien → Application), `notes`.
- **Container** — `UID`, `Name`, `Host` (lien → Node), `Image` (lien → Images),
  `Service`, `Status` (liste), `Ports`, `Restart_policy` (liste), `Stack`,
  `Compose_path`, `Type` (liste), `Last_seen` (date), `Application - Stack`
  (lien → Application), `Notes`.
- **Images** — `Reference`, `Registry`, `Repository`, `Tag`, `Digest_local`,
  `Available_version`, `Update_available` (booléen), `CVE_critical`, `CVE_high`,
  `Last_scan`, `Last_seen` (date), `Container` (lien → Container), `Notes`.
- **IPAM** — `Address`, `IP_int` (nombre), `VLAN` (lien → VLAN), `Type` (liste),
  `Node` (lien → Node), `FQDN`, `Hostname`, `MAC`, `Vendor`, `Status` (liste),
  `Source` (liste : Auto, Manual), `Last_seen` (date), `Notes`.
- **VLAN** — `name`, `vlan_id`, `subnet` (CIDR), `gateway`, `zone` (liste),
  `dhcp` (booléen), `dhcp_range`, `dns`, `notes`.
- **Application** — `Name`, `Status` (liste), `Criticality` (liste),
  `Capability` (liste multiple), `URL`, `URL 2`, `Doc`, `Notes`.

Les valeurs des listes déroulantes sont libres : la console propose celles
employées dans la base, et n'en invente aucune. Les seules dont le collecteur
dépend sont `Source = Auto | Manual` et `Node.Type` pour distinguer un invité
(`VM`, `LXC`) d'une machine réelle.

**`app/web/schema.py` est la source de vérité** sur la propriété de chaque
champ. Toute évolution du collecteur qui change ce qu'il écrit doit s'y
refléter, sinon la console ment — ce qui serait pire que de ne rien afficher.

## Règles de réconciliation

Ce sont les règles qui font qu'il s'agit d'une CMDB et pas d'un simple
inventaire. Elles sont implémentées une seule fois, dans `baserow.py` — aucune
source n'a le droit d'y déroger.

1. **Upsert par clé naturelle**, jamais par id Baserow interne :
   `Node.Name`, `Container.UID` (`{host}/{nom}`), `Images.Reference`,
   `IPAM.Address`. `Application` n'a pas de clé gérée par le collecteur : voir
   plus bas, il n'y touche pas.
2. **Ordre d'écriture imposé** : Node → Images → Container (un `link_row` a
   besoin que sa cible soit déjà en cache). Dans la passe Proxmox, les
   hyperviseurs sont écrits avant les invités.
3. **Chaque source n'écrit que les champs dont elle est propriétaire.** Un champ
   absent du payload reste intact côté Baserow. Le collecteur ne met jamais `""`
   ou `null` pour un champ *inconnu* à ce passage — en revanche il écrit
   explicitement une valeur vide (ex. `Stack: ""`) quand la source *sait*
   positivement que le champ est vide (un conteneur `docker run` sans compose,
   donc hors reproductibilité — voir la section `Application` plus bas).
4. **Les lignes `Source = Manual` sont protégées** (`Node` et `IPAM`, les deux
   tables qui portent ce champ) : seul `Last_seen` y est mis à jour, quels
   que soient les champs que la source pense posséder. `Source` passe à `Auto` à
   la création et n'est plus jamais réécrit. `Container`/`Images` n'ont pas ce
   champ : rien n'y est jamais manuel par construction (faits Docker purs).
   `Application` n'a pas non plus ce champ, pour une autre raison : le
   collecteur n'y écrit jamais rien du tout.
5. **`Last_seen` est posé à chaque passage**, même sans changement : c'est ce
   qui permet de repérer ce qui a disparu.
6. **Suppression réelle après le délai de grâce.** Choix explicite qui revient sur
   la version d'origine de ce prompt ("ne jamais supprimer") : la CMDB doit
   refléter ce qui est *actuellement* en place, pas un historique. Ce qui n'est
   plus vu depuis plus de `RETIRE_GRACE_HOURS` (défaut 72h) est supprimé pour de
   bon — sauf les lignes `Source = Manual`, protégées indéfiniment. Le délai de
   grâce est le seul filet contre un aléa ponctuel (pass qui rate, hôte HS
   quelques minutes) ; passé ce délai, l'absence est traitée comme une vraie
   disparition, y compris si la cause est une panne prolongée d'un hôte Proxmox
   ou Docker — assumé, voir `baserow.py:delete_missing`.
7. **Une source en échec n'arrête pas les autres** : chaque erreur réseau est
   capturée, loggée avec contexte, et le collecteur continue. Ça ne bloque plus
   non plus la suppression : les lignes que cette source aurait dû confirmer ce
   passage vieillissent normalement vers le délai de grâce.

## `Application` : 100% manuelle

Le collecteur n'écrit jamais dans `Application` et n'y crée jamais de ligne. Ce
n'est pas un oubli : un projet Docker Compose n'est **pas** la même chose qu'une
Application au sens métier (un regroupement logique d'assets, qui peut mélanger
plusieurs stacks, une VM sans Docker, etc.) — les confondre a causé plus de
confusion que d'aide.

Le fait technique "ce conteneur appartient à tel projet Compose" vit sur
`Container.Stack` (texte, écrit par Docker à chaque passage, exactement comme
`Container.Host`) — jamais interprété, jamais transformé en regroupement. Une
vue Baserow filtrée/groupée sur `Stack` donne déjà l'équivalent de l'ancien
comportement auto pour le cas simple (1 stack = 1 conteneur logique).

Pour créer une vraie Application (le cas où plusieurs stacks/VMs représentent un
seul produit pour toi) : créer la ligne à la main, puis lier manuellement les
`Container` (et/ou le `Node` via `Application.Host`, pour une app hors Docker)
qui la composent. Aucune limite au nombre de stacks derrière une Application.

## Console web

Service de consultation au-dessus des mêmes tables (`app/web/`), servi par le
service `web` de `compose.yaml`. Même image que le collecteur, commande
différente : une seule construction, un seul jeu de dépendances, et aucun moyen
pour les deux de diverger.

```bash
# .env : ajouter TABLE_APPLICATION (la console la lit, le collecteur non)
#        et de préférence WEB_BASEROW_TOKEN : read partout, update là où il y a des
#        champs manuels, create sur Ipam + Application seulement, delete nulle part
docker compose up -d web
```

Adapter l'IP publiée dans `compose.yaml` (`192.168.10.11:8080:8080`) à celle de
l'hôte sur le VLAN management. Jamais `0.0.0.0` : cette console agrège tout
l'inventaire, elle n'a rien à faire sur les autres segments.

**Pourquoi un service et pas une page statique.** Un *database token* Baserow ne
se restreint ni par origine ni par ligne : exposé dans un navigateur, il donne
accès à toute la base, pas au seul écran affiché. Le token vit donc côté serveur
et n'en sort jamais.

**Modification (`app/web/ecriture.py`).** La console permet de corriger les
champs que le collecteur ne réécrit jamais, et eux seuls. La liste est appliquée
côté serveur, pas dans les gabarits, et son filtre est `schema.editable()` — la
même fonction qui décide de la mention « manuel » affichée en consultation : les
deux écrans ne peuvent pas se contredire. Un nœud découvert sur Proxmox expose
ainsi quatre champs, une caméra saisie à la main en expose onze, sans qu'aucune
règle ne soit écrite deux fois.

**Création.** Deux tables l'acceptent : Ipam — une case non documentée de la
grille d'un VLAN est un lien vers la réservation de cette adresse — et
Application. Le formulaire proposé est celui des champs qui seront *manuels sur
la ligne créée* : sur l'IPAM, `schema.editable()` interrogé avec `Source =
Manual` rend Address, MAC, Node, Type, Status et Notes, et écarte le VLAN, le
constructeur et le nom DNS que le collecteur recalculera. La console impose
elle-même `Source = Manual` — sans quoi le collecteur supprimerait l'adresse dès
qu'il constaterait ne pas la voir — et pré-calcule VLAN et IP_int, que le
collecteur recalculera à l'identique, pour que la ligne soit utilisable tout de
suite plutôt qu'au prochain passage.

Ce module est le seul du service web à écrire ; tout le reste ne connaît que
`fetch_all`. Il ne supprime nulle part et ne crée nulle part ailleurs, d'où les
droits de token ci-dessus. Si Baserow devait un jour être remplacé, c'est ce
fichier qu'il faudrait réécrire, et lui seul.

Ce qui reste du ressort de Baserow, par construction : supprimer une ligne,
ajouter un champ, créer une option de liste, créer une ligne dans les quatre
autres tables. Les options proposées dans les formulaires sont celles déjà
employées dans la base — l'API ne donne pas la définition d'un `single_select` à
un *database token*.

**Ce qu'elle montre que Baserow ne peut pas montrer :**

- **La provenance de chaque champ** (`app/web/schema.py`). Baserow affiche les 19
  champs d'un nœud comme s'ils étaient tous à toi ; en réalité le collecteur en
  réécrit la majorité à chaque passage. Chaque champ de chaque fiche est marqué
  `auto` (écrasé au prochain passage), `enrichi` (recalculé même sur une ligne
  manuelle — c'est le cas du VLAN, du constructeur et du hostname d'une adresse
  IP saisie à la main) ou `manuel`. **Ce module est la seule source de vérité sur
  le sujet : toute évolution du collecteur qui change ce qu'il écrit doit s'y
  refléter, sinon la console ment.**
- **Le graphe**, que Baserow ne sait afficher qu'une table à la fois : hyperviseur
  → invité → conteneur → image → CVE, et l'IP en travers. Les six tables sont
  tirées d'un coup et recomposées en mémoire (`app/web/store.py`), ce qui est
  possible parce que le parc tient en quelques centaines de lignes.
- **Les files d'attente** : ce qui porte des CVE critiques, ce qui est en retard
  de version, ce qui tourne hors de tout `docker compose`, ce qui n'est rattaché
  à aucune application, ce qui n'a pas d'IP connue.
- **Les stacks** (`/stacks`), reconstituées à partir de `Container.Stack` — le
  collecteur rapporte le nom du projet Compose sur chaque conteneur, le
  regroupement se fait côté console. Une stack porte l'hôte, ses conteneurs, ses
  images, son cumul de CVE et son chemin de déploiement. C'est aussi ce qui
  distingue trois conteneurs homonymes (`docker-socket-proxy`) déployés sur trois
  hôtes, et l'unité de reproductibilité de l'infrastructure : ce qui n'est pas
  dans une stack ne se redéploie pas à l'identique.
- **Les lignes en sursis** (`/etat`) : ce que le collecteur ne revoit plus et qui
  sera réellement supprimé, avec le délai restant. Une ligne y apparaît quand elle
  est en retard *sur les autres lignes de sa table*, pas quand elle est simplement
  ancienne : si le collecteur est à l'arrêt, tout est ancien et rien n'est menacé.

**Interface.** Thème clair, sombre ou suivi du système (mémorisé par
navigateur), mise en page responsive jusqu'au téléphone (menu repliable, fiches
sur deux lignes par champ, tableaux larges qui défilent dans leur cadre plutôt
que d'emporter la page). Les explications ne sont pas affichées en permanence :
chaque cadre porte un bouton d'aide qui déplie le détail au clic.

**L'écran Infrastructure sépare virtuel et physique**, et le parc physique est
groupé **par type** — serveurs, appliances réseau, postes, caméras, objets
connectés — chaque équipement ne figurant que dans un seul groupe. Le groupement
par rôle a été essayé puis abandonné : il dispersait les serveurs physiques dans
autant de catégories qu'ils portaient de fonctions. Les rôles restent affichés en
étiquette sur chaque ligne, et la recherche retrouve un équipement par son rôle.

**Icônes d'application.** `<origine>/favicon.ico` ne trouve qu'une minorité des
icônes ; la console lit donc le HTML des URL de la table Application pour y
trouver la balise `<link rel="icon">` (voir `app/web/favicons.py`). C'est la seule
entorse au principe « ce service ne parle qu'à Baserow » : seul le HTML est lu,
l'image reste chargée par le navigateur. Désactivable par
`WEB_FAVICON_DISCOVERY=false`.

Le navigateur reprend ensuite la main : si le candidat retenu échoue, il essaie
les emplacements conventionnels l'un après l'autre avant de retomber sur
l'initiale. Ce n'est pas qu'un repli — **le poste qui affiche la page a des
droits que ce service n'a pas**. Un reverse proxy filtrant par IP source répond
403 à la console et sert la vraie page au navigateur : la découverte échoue,
l'icône s'affiche quand même.


## Ajouter une source

Un fichier dans `app/collector/sources/`, qui expose une fonction `collect(...)`
renvoyant des dicts normalisés — **aucun appel à Baserow dedans**. Puis une passe
dans `main.py` qui appelle `client.upsert(...)` avec les champs que renvoie la
source. Cup (`sources/cup.py`) et Trivy (`sources/trivy.py`) suivent ce modèle et
alimentent `Images` : `Available_version` / `Update_available` pour le premier,
`CVE_critical` / `CVE_high` / `Last_scan` pour le second. Tous deux tournent en
sous-processus (binaire embarqué dans l'image), sans serveur ni socket Docker
supplémentaire.

## Configuration (`.env`)

Voir `.env.example` pour la liste complète. Deux points notables :

- **Baserow** : utiliser un *database token* (`Authorization: Token <t>`), jamais
  le JWT admin — celui-ci permet de modifier la structure des tables et n'a
  rien à faire dans un process qui tourne toutes les 15 minutes. Droits
  create/read/update **et delete** (nécessaire depuis que la suppression réelle
  est activée — voir règle 6 ci-dessous) ; le JWT admin, lui, reste hors sujet.
- **TLS interne** : `BASEROW_VERIFY_TLS=false` fonctionne pour un certificat
  auto-signé sur domaine `.lcl`. Alternative propre si une CA interne existe :
  positionner `REQUESTS_CA_BUNDLE` sur le chemin de cette CA plutôt que de
  désactiver la vérification.

## Ce que ce collecteur ne fait pas

- Ne modifie jamais le schéma Baserow (pas de création/suppression de champ ou
  de table) — un champ référencé par le code (`Guest_agent`, `Last_seen` sur
  `Images`...) doit être ajouté à la main avant que le code correspondant
  fonctionne ; sinon Baserow l'ignore silencieusement, sans erreur.
- Ne supprime **jamais** une ligne `Source = Manual` — en dehors de ça, il
  supprime pour de bon ce qu'il ne voit plus (règle 6). Ce n'est *pas* un
  historique : voir la section Règles de réconciliation.
- Ne parle jamais au socket Docker en direct.
- N'a pas de state sur disque : à chaque démarrage, les caches sont reconstruits
  depuis Baserow.

## Licence

MIT — voir [LICENSE](LICENSE). Faites-en ce que vous voulez ; si vous
l'adaptez à un autre hyperviseur ou à un autre stockage, le retour d'expérience
m'intéresse.
