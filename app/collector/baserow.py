"""Client Baserow : cache, upsert par clé naturelle, suppression de ce qui n'est plus vu.

Toute la logique de réconciliation vit ici et nulle part ailleurs. Les sources (proxmox.py,
docker.py) ne connaissent pas Baserow : elles renvoient des dicts normalisés, ce module
décide comment les écrire.

Choix explicite (revient sur la règle "ne jamais supprimer" du prompt d'origine) : la CMDB
doit refléter ce qui est actuellement en place, pas un historique. Ce qui n'est plus vu après
`RETIRE_GRACE_HOURS` est supprimé pour de bon, sauf les lignes `source=manual` — protégées
indéfiniment, puisqu'une saisie manuelle a forcément une raison d'être.
"""

import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger("cmdb.baserow")


class BaserowClient:
    def __init__(self, base_url, token, verify_tls=True, timeout=15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
        })
        self.session.verify = verify_tls

    def _url(self, table_id, row_id=None):
        path = f"/api/database/rows/table/{table_id}/"
        if row_id is not None:
            path += f"{row_id}/"
        return self.base_url + path

    def fetch_all(self, table_id):
        """Récupère toutes les lignes d'une table, page après page."""
        rows = []
        url = self._url(table_id)
        params = {"user_field_names": "true", "size": 200}
        while url:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            rows.extend(data["results"])
            url = data.get("next")
            # l'URL "next" renvoyée par Baserow porte déjà tous les paramètres
            params = None
        return rows

    def build_cache(self, table_id, key_field):
        """Indexe une table par sa clé naturelle. À appeler avant toute écriture qui la référence
        (règle 2 : Node avant Images avant Application avant Container)."""
        cache = {}
        for row in self.fetch_all(table_id):
            key = row.get(key_field)
            if key:
                cache[key] = row
        return cache

    def create_row(self, table_id, fields):
        resp = self.session.post(
            self._url(table_id),
            params={"user_field_names": "true"},
            json=fields,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def update_row(self, table_id, row_id, fields):
        resp = self.session.patch(
            self._url(table_id, row_id),
            params={"user_field_names": "true"},
            json=fields,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_row(self, table_id, row_id):
        resp = self.session.delete(self._url(table_id, row_id), timeout=self.timeout)
        resp.raise_for_status()

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
          champ (comparaison insensible à la casse : le libellé de l'option est éditable dans
          Baserow), seul `last_seen_field` est mis à jour (règle 4) : toute correction manuelle
          est protégée, y compris contre le propriétaire habituel du champ.
        - `last_seen_field` est toujours posé (règle 5) sauf si explicitement `None`. Son nom
          exact est à préciser par l'appelant (`Last_seen` partout actuellement).
        - `manual_gate_field` est initialisé à `auto_label` uniquement à la création, jamais
          réécrit ensuite (règle 4). `auto_label` doit matcher exactement le libellé Baserow
          (contrairement à la lecture, l'écriture d'un select est sensible à la casse).
        - `create_only_fields` n'est posé qu'à la création, jamais réécrit ensuite — pour un
          champ qui doit démarrer avec une valeur par défaut puis devenir librement modifiable
          à la main sans jamais être écrasé (ex. `Application.Name`, une fois `Stack` sorti
          comme clé technique séparée).

        Retourne (ligne_baserow, a_ete_cree).
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
            updated = self.update_row(table_id, existing["id"], payload)
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
            except requests.RequestException:
                logger.exception("Suppression %s/%s en échec", table_id, key)
