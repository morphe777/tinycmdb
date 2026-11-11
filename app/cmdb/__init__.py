"""Stockage de TinyCMDB : une base SQLite, partagée par le collecteur et la console.

Ce paquet est le seul à connaître SQL. Le collecteur et la console lui parlent en
« lignes » — des dictionnaires indexés par le nom des champs (`Name`, `Parent_host`,
`Application - Stack`…), dans la forme que Baserow rendait. Ce choix n'est pas une
nostalgie : c'est ce qui a permis de changer de stockage sans réécrire les seize gabarits,
le graphe de `web/store.py` ni les règles de réconciliation du collecteur.
"""
