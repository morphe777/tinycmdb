"""Écriture du collecteur : upsert par clé naturelle, suppression de ce qui n'est plus vu.

Toute la logique de réconciliation vit ici et nulle part ailleurs. Les sources (proxmox.py,
docker.py) ne connaissent pas le stockage : elles renvoient des dicts normalisés, ce module
décide comment les écrire.

Choix explicite (revient sur la règle "ne jamais supprimer" du prompt d'origine) : la CMDB
doit refléter ce qui est actuellement en place, pas un historique. Ce qui n'est plus vu après
`RETIRE_GRACE_HOURS` est supprimé pour de bon, sauf les lignes `source=manual` — protégées
indéfiniment, puisqu'une saisie manuelle a forcément une raison d'être.

L'interface est restée celle du client Baserow d'origine (`fetch_all`, `build_cache`,
`create_row`, `update_row`, `delete_row`), une table étant désignée par son nom (`node`,
`container`…) plutôt que par un identifiant numérique. C'est ce qui a permis de passer à
SQLite sans toucher aux règles ni aux passes de `main.py`.

Chaque écriture est sa propre transaction, et non chaque passe : une passe interroge le
réseau entre deux écritures (ping, DNS, Docker, Trivy), et tenir le verrou d'écriture
pendant ce temps bloquerait la console pendant des minutes.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("cmdb.stockage")

ACTEUR = "collecteur"

# Champs dont le changement n'entre pas au journal. Le reste y entre : un conteneur passé à
# « Exited », une image dont l'empreinte a changé, une VM qui a gagné de la mémoire ou dont
# la configuration a bougé — c'est la question « qu'est-ce qui a changé depuis mardi ? »,
# la première d'un dépannage. Ceux-ci changent à chaque passe, ou au gré de la base de
# vulnérabilités de Trivy, et noieraient le reste.
MUETS = frozenset({"Last_seen", "Last_scan", "IP_int", "CVE_critical", "CVE_high"})


class Stockage:
    def __init__(self, depot):
        self.depot = depot

    def fetch_all(self, kind):
        """Toutes les lignes d'une table."""
        return self.depot.lignes(kind)

    def build_cache(self, kind, key_field):
        """Indexe une table par sa clé naturelle. À appeler avant toute écriture qui la référence
        (règle 2 : Node avant Images avant Container)."""
        cache = {}
        for row in self.fetch_all(kind):
            key = row.get(key_field)
            if key:
                cache[key] = row
        return cache

    def create_row(self, kind, fields):
        # Une option inconnue (un nouvel état Docker, par exemple) est créée plutôt que
        # refusée : perdre la ligne entière pour un libellé serait pire que de l'ajouter
        # à la liste, où elle apparaîtra en gris dans l'administration.
        return self.depot.creer(kind, fields, acteur=ACTEUR, creer_options=True)

    def update_row(self, kind, row_id, fields, avant=None):
        """Met à jour une ligne. Avec `avant` — la ligne telle que l'appelant la connaît —
        ce qui a réellement changé entre au journal sous le nom du collecteur. Sans lui,
        rien n'y entre : c'est le cas d'un `Last_seen` seul, qui n'a rien à dire."""
        if avant is None:
            return self.depot.modifier(kind, row_id, fields, creer_options=True)
        return self.depot.modifier(kind, row_id, fields, acteur=ACTEUR, creer_options=True,
                                   avant=avant, muets=MUETS)

    def delete_row(self, kind, row_id):
        self.depot.supprimer(kind, row_id, acteur=ACTEUR)

    @staticmethod
    def _select_value(row, field):
        """Un single_select revient comme {"id", "value", "color"}, pas une chaîne brute."""
        if not field:
            return None
        value = row.get(field)
        return value.get("value") if isinstance(value, dict) else value

    def upsert(
        self, cache, table_id, key_field, key_value, fields,
        manual_gate_field=None, auto_label="auto", last_seen_field="last_seen",
        create_only_fields=None,
    ):
        """Upsert générique par clé naturelle.

        - N'écrit que les champs présents dans `fields` (règle 3) : à l'appelant de ne
          jamais y mettre une valeur inconnue.
        - Si `manual_gate_field` est fourni et que la ligne existante porte "manual" sur ce
          champ (comparaison insensible à la casse : le libellé de l'option est éditable depuis
          l'administration), seul `last_seen_field` est mis à jour (règle 4) : toute correction manuelle
          est protégée, y compris contre le propriétaire habituel du champ.
        - `last_seen_field` est toujours posé (règle 5) sauf si explicitement `None`. Son nom
          exact est à préciser par l'appelant (`Last_seen` partout actuellement).
        - `manual_gate_field` est initialisé à `auto_label` uniquement à la création, jamais
          réécrit ensuite (règle 4). `auto_label` est le libellé de l'option posée
          (l'écriture retombe sur une comparaison insensible à la casse, voir cmdb/depot.py).
        - `create_only_fields` n'est posé qu'à la création, jamais réécrit ensuite — pour un
          champ qui doit démarrer avec une valeur par défaut puis devenir librement modifiable
          à la main sans jamais être écrasé (ex. `Application.Name`, une fois `Stack` sorti
          comme clé technique séparée).

        Retourne (ligne, a_ete_cree).
        """
        now = datetime.now(timezone.utc).isoformat()
        existing = cache.get(key_value)

        if existing is not None:
            existing_gate_value = self._select_value(existing, manual_gate_field)
            if manual_gate_field and (existing_gate_value or "").lower() == "manual":
                update_payload = {last_seen_field: now} if last_seen_field else {}
                updated = self.update_row(table_id, existing["id"], update_payload)
                cache[key_value] = updated
                logger.debug("= %s/%s : ligne manuelle, %s seul", table_id, key_value, last_seen_field)
                return updated, False

            payload = dict(fields)
            if last_seen_field:
                payload[last_seen_field] = now
            updated = self.update_row(table_id, existing["id"], payload, avant=existing)
            cache[key_value] = updated
            return updated, False

        payload = dict(fields)
        payload[key_field] = key_value
        if last_seen_field:
            payload[last_seen_field] = now
        if manual_gate_field:
            payload[manual_gate_field] = auto_label
        if create_only_fields:
            payload.update(create_only_fields)
        created = self.create_row(table_id, payload)
        cache[key_value] = created
        logger.info("+ %s créé : %s", table_id, key_value)
        return created, True

    def delete_missing(self, cache, table_id, seen_keys, grace_hours, last_seen_field="Last_seen", manual_gate_field=None):
        """Supprime pour de bon les lignes non revues depuis plus de `grace_hours`.

        Le délai de grâce est le seul filet de sécurité contre un aléa ponctuel (un pass qui
        rate, un hôte HS quelques minutes) : passé ce délai, l'absence est traitée comme une
        vraie disparition, pas mise en attente. Les lignes `manual_gate_field=manual` restent
        protégées indéfiniment.
        """
        now = datetime.now(timezone.utc)
        for key, row in list(cache.items()):
            if key in seen_keys:
                continue
            gate_value = self._select_value(row, manual_gate_field)
            if manual_gate_field and (gate_value or "").lower() == "manual":
                continue
            last_seen = row.get(last_seen_field)
            if not last_seen:
                continue
            seen_at = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
            age_hours = (now - seen_at).total_seconds() / 3600
            if age_hours < grace_hours:
                continue
            try:
                self.delete_row(table_id, row["id"])
                del cache[key]
                logger.info("- %s supprimé : %s (%.1fh sans passage)", table_id, key, age_hours)
            except Exception:
                logger.exception("Suppression %s/%s en échec", table_id, key)
