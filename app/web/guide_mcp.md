# TinyCMDB — mode d'emploi pour un agent

TinyCMDB est l'inventaire d'une infrastructure auto-hébergée : ce qui tourne, où, et à
quoi ça sert. Une partie est **découverte** automatiquement (Proxmox, Docker, Trivy,
Cup) par un collecteur qui repasse toutes les 15 minutes ; le reste est **saisi** à la
main — par l'interface web, par l'API, ou par toi.

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

Une **stack** n'est pas une table : c'est le regroupement des conteneurs d'un même projet
Docker Compose sur un même hôte. Sa clé est `hôte/nom` (ex. `myTools/tinycmdb`).

Liens principaux :
- un nœud a un `parent` (son hyperviseur), des invités, des conteneurs, des adresses ;
- un conteneur a un hôte (nœud), une image, une stack, et des applications ;
- une application regroupe des conteneurs — donc des stacks — et éventuellement des
  nœuds directs (une VM qui rend un service sans Docker) ;
- une adresse appartient à un VLAN et pointe vers un nœud.

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

1. **Trouver** : `rechercher` (nom, IP, MAC, port, chemin de compose, OS…) renvoie des
   références `{type, id, nom}`. Pour parcourir un type, `lister`.
2. **Comprendre** : `lire(type, id)` donne les champs, leur provenance, les liens
   résolus. Pour une stack, `stack(cle)`.
3. **Agir** (jeton en écriture seulement) :
   - `modifier(type, id, champs)` — seuls les champs de `modifiables` ;
   - `creer(type, champs)` — pour `noeud`, `ip`, `vlan` et `application` seulement ;
     la ligne créée est marquée saisie manuelle, donc protégée du collecteur ;
   - `rattacher_stack(cle, applications)` — fixe les applications servies par tous les
     conteneurs d'une stack (remplace la liste).
4. **Vérifier** : chaque écriture renvoie l'objet à jour ; `journal` montre l'historique
   d'un objet, écritures de l'interface comprises ; `journal(acteur="mcp:<ton jeton>")`
   retrouve tes propres écritures.

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
- **Créer une option ou un champ** : c'est l'administration de la console.

Toute écriture est journalisée sous le nom de ton jeton (`mcp:<nom>`).

## Erreurs

Un refus explique toujours sa raison en clair — champ non modifiable sur cette ligne,
option inconnue, doublon de clé naturelle, référence inexistante. Corrige d'après le
message ; ne réessaie pas à l'identique. Un objet « perdu » (`perdu: true`) est un objet
que le collecteur ne revoit plus : son dernier état connu est probablement faux.
