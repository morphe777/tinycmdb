# TinyCMDB — mode d'emploi pour un agent

TinyCMDB est l'inventaire d'une infrastructure auto-hébergée : ce qui tourne, où, sur
quel matériel, et à quoi ça sert. Une partie est **découverte** automatiquement (Proxmox,
Docker, Trivy, Cup) par un collecteur qui repasse toutes les 15 minutes ; le reste est
**saisi** à la main — par l'interface web, par l'API, ou par toi.

Ce guide est le même que celui de l'API REST (`GET /api/v1/guide`). Les outils de ce
serveur et les routes de l'API font exactement la même chose.

## Le modèle en une minute

| type          | ce que c'est                                   | clé naturelle | qui l'écrit |
|---------------|------------------------------------------------|---------------|-------------|
| `noeud`       | machine : physique, VM, LXC, appliance, caméra… | `Name`        | Proxmox, ou à la main (`Source = Manual`) |
| `conteneur`   | conteneur Docker, clé `hôte/nom`               | `UID`         | Docker uniquement |
| `image`       | image Docker, avec ses CVE et sa mise à jour   | `Reference`   | Docker, Trivy, Cup |
| `ip`          | une adresse IP                                 | `Address`     | Proxmox, ou à la main |
| `vlan`        | un sous-réseau (référentiel)                   | `name`        | à la main uniquement |
| `application` | un service au sens métier                      | `Name`        | à la main uniquement |
| `cpu`         | un **modèle** de processeur (référentiel)      | `Name`        | amorcé par Proxmox, puis à la main |
| `composant`   | carte PCIe, disque, périphérique USB           | `UID`         | Proxmox, ou à la main (`Source = Manual`) |

Une **stack** n'est pas une table : c'est le regroupement des conteneurs d'un même projet
Docker Compose sur un même hôte. Sa clé est `hôte/nom` (ex. `myTools/tinycmdb`).

Liens principaux :
- un nœud a un `parent` (son hyperviseur), des invités, des conteneurs, des adresses ;
- un conteneur a un hôte (nœud), une image, une stack, et des applications ;
- une application regroupe des conteneurs — donc des stacks — et éventuellement des
  nœuds directs (une VM qui rend un service sans Docker) ;
- une adresse appartient à un VLAN et pointe vers un nœud ;
- une machine physique a un `CPU` (lien vers le référentiel) et des composants
  (`Components`) ; un composant est installé dans un hôte (`Host`) et peut être attribué
  en passthrough à une VM (`Assigned_to`).

## Les mises à jour d'images

`Update_available` vient de Cup. Une image en `latest` est comparée par empreinte
(l'image qui tourne contre le `latest` du registre) dès que sa version ne se réduit pas à
des nombres — c'est le cas de toutes les images LinuxServer (`v26.09.3-ls301`) : la
réponse est fiable, mais `Available_version` reste vide. Un `Update_available` vrai sans
`Available_version` veut donc dire « une version plus récente existe, sans nom connu ».
Un tag figé portant un suffixe de construction (`-ls301`) n'est pas vérifiable : Cup le
déclare à jour quoi qu'il arrive.

## Le matériel

Ce qu'il faut savoir pour raisonner sur une panne ou une conception, sans aller le
chercher ailleurs :

- **sur tout nœud** : `Arch` (vocabulaire Docker : `amd64`, `arm64`, `arm/v7`…, à
  rapprocher des plateformes d'une image), `CPU_level` (niveau x86-64 *effectivement exposé*
  : celui de l'hyperviseur, ou celui du type de processeur d'une VM — une VM en `kvm64` est
  en v1 et ne lance pas un binaire compilé pour v2/v3), `Machine` (chipset et
  micrologiciel d'une VM, mode de démarrage d'un hyperviseur) ;
- **sur une machine physique** : `CPU` (le modèle), `Sockets`, `Kernel`, et ce qu'aucune
  API ne voit et qui se saisit : `Motherboard`, `BIOS`, `RAM_type`, `RAM_ECC`, `RAM_MHz`,
  `RAM_modules` (barrettes et emplacements), `RAM_max_Gb` ;
- **sur un invité Proxmox** : `CPU_type` (tel que déclaré, `kvm64 (défaut)` sans
  déclaration) et `Config` — la configuration Proxmox complète, en `clé: valeur` : disques
  et stockage, cartes réseau, pont et **VLAN** (`tag=`), démarrage automatique,
  ballooning, passthrough (`hostpciN`, `usbN`), étiquettes, notes Proxmox. Lis-la : la
  plupart des réponses sur un invité y sont ;
- **le référentiel `cpu`** : une ligne par modèle, partagée par toutes les machines qui le
  portent. Le collecteur l'amorce (cœurs, threads, fréquence de base, niveau x86-64,
  virtualisation) ; fréquence turbo, TDP, cache L3, graphique intégré, mémoire maximale
  se saisissent **une fois**. Si tu les connais avec certitude et qu'ils manquent,
  propose de les compléter avec `modifier("cpu", id, …)` : ils ne changeront plus ;
- **les composants** : relevés sur chaque hyperviseur (cartes réseau, GPU, HBA,
  accélérateurs, disques avec santé SMART et usure, périphériques USB), avec le groupe
  IOMMU et la VM qui les reçoit. Nom, catégorie, marque et modèle sont proposés une fois
  par le collecteur puis restent à la main (une correction tient) ; `Reference`, `Ports`,
  `Speed_Mbps` (en Mbit/s : 2500 pour 2,5 Gb/s), `Standard`, `Firmware` se saisissent.
  Un disque qu'une VM reçoit derrière un contrôleur passé en entier n'est pas visible :
  le contrôleur, si, attribué à la VM. `Assigned_to` (« Utilisé par ») vide signifie que
  l'hôte s'en sert lui-même. `PCI_id` est l'identité de la carte (`10de:2484 1462:3907` :
  puce, puis carte) : une carte déplacée d'un emplacement ou d'un hôte à l'autre garde sa
  ligne et ce qui y a été saisi — le collecteur reprend celle de la carte de même identité
  qui vient de disparaître, s'il n'y en a qu'une ; le journal note le changement d'`UID`
  et de `Host`. Un disque, lui, est identifié par son numéro de série où qu'il soit ;
- **les GPU et accélérateurs** sont des composants (`Category` = `GPU` ou `Accelerator`),
  **une ligne par unité** : quatre cartes font quatre lignes, un Mac Mini en a une pour
  son GPU et une pour son Neural Engine, avec `Bus` = `SoC` quand l'unité est intégrée à
  la puce. Le champ `iGPU` du référentiel `cpu` ne fait que nommer le graphique d'un
  modèle ; c'est le composant qui dit ce qu'en a une machine donnée. Sur ces deux
  catégories seulement, et toujours saisis : `VRAM_Gb` (la mémoire dont il dispose, ce qui
  borne la taille d'un modèle chargeable), `Shared_memory` (vrai si c'est celle de la
  machine — iGPU, puce Apple : ce qu'il prend manque alors aux applications), `GPU_cores`,
  `Compute_API` (CUDA, ROCm, oneAPI, Metal, Vulkan, OpenCL, VA-API, QSV, NVENC, Edge TPU,
  Hailo, Core ML). Pour savoir où faire tourner une charge d'IA ou un transcodage :
  `lister("composant", filtres={"Category": "GPU"})`.

Les champs sans objet pour une ligne (la carte mère d'un LXC, la configuration d'un
hyperviseur, le `vmid`, l'agent invité ou la sauvegarde d'une machine physique) ne sont ni
rendus ni modifiables. Une même écriture peut changer le type (ou la catégorie) et
renseigner les champs qu'il ouvre : passer un composant en `GPU` et poser sa `VRAM_Gb` se
fait en un `modifier`.

Les noms de champ sont exacts et sensibles à la casse (`Name`, `notes` sur un nœud mais
`Notes` sur une adresse, `URL 2`, `Application - Stack`). L'outil `schema` les liste
tous, avec leur type, leur provenance et, pour les listes, les options admises.

## La règle qui compte : la provenance

Chaque champ a une provenance, rendue par `lire` dans `provenance` :

- **auto** — réécrit par le collecteur à chaque passage. Le modifier ne servirait à rien :
  c'est donc refusé.
- **enrichi** — recalculé par le collecteur même sur une ligne saisie à la main (VLAN,
  constructeur, nom DNS d'une adresse manuelle). Refusé aussi.
- **manuel** — à toi, le collecteur n'y touche jamais. Seuls ces champs s'écrivent.

`lire` renvoie aussi `modifiables` : la liste exacte des champs que tu peux écrire sur
cet objet. **Consulte-la avant d'écrire** — elle dépend de la ligne : une VM découverte
par Proxmox n'expose que `Roles`, `Criticality`, `notes` et `Application`, alors qu'un
équipement saisi à la main expose tout.

## Comment s'y prendre

1. **Trouver** : `rechercher` (nom, IP, MAC, port, chemin de compose, OS, processeur,
   composant…) renvoie des références `{type, id, nom}`. Pour interroger un type,
   `lister` avec `filtres` : `lister("noeud", filtres={"Arch": "arm64"})`,
   `lister("composant", filtres={"Category": "Disk", "Wearout": ">=50"})`,
   `lister("noeud", filtres={"Type": "Physical", "BIOS": null})` (BIOS non renseigné).
   `champs=[…]` restreint la réponse ; les champs vides sont omis par défaut.
2. **Comprendre** : `contexte(type, id)` rend l'objet **et tout son voisinage** en un
   appel — pour un nœud : hôte, processeur et ses caractéristiques, composants,
   périphériques attribués, invités, conteneurs, adresses ; pour un conteneur ou une
   application : la chaîne jusqu'à l'hyperviseur et son matériel. C'est le point de
   départ d'un dépannage ou d'une conception. `lire(type, id)` donne le détail d'un seul
   objet, avec la provenance de chaque champ et `modifiables`. Pour une stack,
   `stack(cle)` ou `contexte("stack", cle=…)`. Pour placer une nouvelle charge,
   `capacite` : par hyperviseur, cœurs physiques, mémoire, et ce qui est déjà promis.
3. **Agir** (jeton en écriture seulement) :
   - `modifier(type, id, champs)` — seuls les champs de `modifiables` ;
   - `creer(type, champs)` — pour `noeud`, `ip`, `vlan`, `application`, `cpu` et
     `composant` ; la ligne créée est marquée saisie manuelle, donc protégée du
     collecteur. Pour une machine que Proxmox ne voit pas (NAS, Raspberry Pi, routeur) :
     crée son modèle de processeur s'il manque, puis renseigne `CPU` sur le nœud et crée
     ses composants avec `Host` ;
   - `rattacher_stack(cle, applications)` — fixe les applications servies par tous les
     conteneurs d'une stack (remplace la liste).
4. **Vérifier** : chaque écriture renvoie l'objet à jour ; `journal` montre l'historique
   d'un objet, écritures de l'interface comprises ; `journal(acteur="mcp:<ton jeton>")`
   retrouve tes propres écritures.

**Ce qui a changé** : le journal tient aussi ce que le collecteur constate — chaque champ
qui change d'une passe à l'autre (un conteneur passé à `Exited`, une image dont
l'empreinte a changé, une VM qui a gagné de la mémoire, une configuration Proxmox
modifiée, un disque dont la santé SMART se dégrade). Devant une panne, commence par
`journal(depuis="2026-11-17", acteur="collecteur")`, éventuellement restreint à un objet
(`type`, `id`) ou à un champ (`champ="Status"`). Les `Last_seen` et les compteurs de CVE
n'y figurent pas.

Pour un point de situation : `a_traiter` (files d'action : CVE critiques sans
application, stacks sans rattachement, mises à jour disponibles…), `sursis` (lignes que
le collecteur ne voit plus et qui seront supprimées), `etat` (santé du collecteur, de la
base, des sauvegardes, des sources).

## Format des valeurs à l'écriture

- texte : chaîne ; une chaîne vide ou `null` **efface** le champ ;
- nombre : entier ;
- case à cocher : `true` / `false` ;
- date : `"AAAA-MM-JJ"` ;
- liste à un choix : le libellé exact d'une option existante (`"High"`) ;
- liste à plusieurs choix : liste de libellés (`["Docker", "Backup"]`) — elle
  **remplace** la liste actuelle, n'ajoute pas ;
- lien : identifiant, ou liste d'identifiants (`"Application": [3]`) — remplace aussi.

Une option qui n'existe pas est refusée : les listes se définissent dans
l'administration de la console, pas par l'API. Ne devine pas un libellé, lis `schema`.

## Ce que tu ne peux pas faire, et pourquoi

- **Supprimer** une ligne : réservé à l'interface, avec confirmation humaine.
- **Écrire un champ auto ou enrichi** : il serait écrasé au passage suivant.
- **Créer un conteneur ou une image** : ce sont des faits Docker, le collecteur seul
  les connaît.
- **Chercher sur Internet à la place de la CMDB** : ce n'est pas interdit, mais ce que tu
  trouves (les caractéristiques d'un modèle, par exemple) a sa place dans le référentiel
  — propose de l'y écrire, pour que la question ne se repose pas.
- **Créer une option ou un champ** : c'est l'administration de la console.

Toute écriture est journalisée sous le nom de ton jeton (`mcp:<nom>`).

## Erreurs

Un refus explique toujours sa raison en clair — champ non modifiable sur cette ligne,
option inconnue, doublon de clé naturelle, référence inexistante. Corrige d'après le
message ; ne réessaie pas à l'identique. Un objet « perdu » (`perdu: true`) est un objet
que le collecteur ne revoit plus : son dernier état connu est probablement faux.
