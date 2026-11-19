# TinyCMDB — notes pour Claude

## Pourquoi ce projet existe

Inventaire du homelab (Proxmox, LXC, Docker via Portainer) qui sert de **base de faits à
une IA** : aide à la conception d'architecture, aux choix, au dépannage. Tout ce qu'un
agent doit savoir pour raisonner sur l'infra doit pouvoir s'y lire par l'API ou le MCP,
sans aller le chercher ailleurs.

## Conventions

- Code, commentaires, commits, interface : **en français**. Les noms de champ restent ceux
  du modèle (`Name`, `RAM_Gb`, `Assigned_to`…), un seul vocabulaire de la base à l'API.
- Les commentaires disent *pourquoi*, pas *quoi* — c'est le style de tout le dépôt.
- Toute nouvelle capacité est exposée **à la fois** dans l'API (`web/api.py`) et le MCP
  (`web/mcp_serveur.py`), par le module commun `web/exposition.py`, et documentée dans
  `web/guide_mcp.md` (le guide que lit l'agent). Le README (anglais) suit.
- `web/schema.py` est la seule source de vérité sur la provenance (auto / enrichi /
  manuel). Un changement de ce qu'écrit le collecteur s'y reflète, sinon la console ment.
- Toute écriture passe par `web/ecriture.py` (console, API et MCP partagent les mêmes
  règles). L'API et le MCP ne suppriment rien.
- Le schéma évolue par migrations numérotées (`app/cmdb/migrations/NNN_*.sql`), jamais à la
  main. `cmdb/reprise_baserow.py` ne connaît que les six tables d'origine (`ORDRE_BASEROW`).
- **La CMDB observe, elle n'agit pas** : aucun accès en écriture à Proxmox, Docker ou
  Portainer. **Le collecteur ne va pas chercher d'informations sur Internet** (décision du
  2026-11-19) : les caractéristiques d'un modèle de processeur se saisissent une fois dans
  le référentiel `cpu`, à la main ou par un agent.

## Développer et tester

- Pas de suite de tests dans le dépôt. Pour vérifier : un venv
  (`uv venv` puis `uv pip install -r requirements.txt httpx`), une base temporaire
  (`TINYCMDB_DB=…`), `fastapi.testclient` sur `web.main:app`, et pour le collecteur un faux
  `requests.Session` substitué dans `collector/sources/proxmox.py`.
- `dev.sh` lance la console de dev en Docker sur mydev (jamais le collecteur).
- Prod : image épinglée sur un numéro de version (`compose.yaml`, `compose.portainer.yaml`).
  Une version = un commit « Version X.Y.Z » qui touche `app/VERSION` et les deux compose.
- Commit uniquement sur demande.

## Plus tard (idées validées, pas encore faites)

- **Petit agent pour le matériel hors Proxmox.** 5 à 6 machines au moins (NAS, Raspberry
  Pi, routeur…), plus des boîtiers LoRa d'un autre projet. Pour l'instant tout est saisi à
  la main (`Source = Manual`). Un agent installé sur la machine remonterait `lscpu`,
  `dmidecode` (carte mère, BIOS, barrettes), `lspci`, `lsusb`, `lsblk`/SMART. Question de
  conception à trancher : l'API n'écrit que les champs manuels — il faudrait une voie
  « source automatique » dédiée (jeton de type collecteur ?) plutôt que d'ouvrir les
  champs auto à l'API.
- **Source Unraid** (VM sur Proxmox, HBA en passthrough) : les disques derrière le HBA sont
  invisibles de Proxmox. L'API GraphQL intégrée à Unraid 7.2+ (clé d'API) donne la
  grappe, les disques, leur santé, les partages. Ses conteneurs passent déjà par le
  collecteur Docker si un socket-proxy y tourne (rôle `docker` sur le nœud).
- **Topologie réseau** : un outil dédié existe, à évaluer (lien carte réseau ↔ port de
  switch ↔ VLAN). Pas dans la CMDB pour l'instant.
- Pools ZFS et stockages Proxmox (`/nodes/{node}/disks/zfs`, `/storage`).
- Architecture des hôtes Docker hors Proxmox (`GET /version` du socket-proxy → `Arch`).
