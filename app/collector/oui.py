"""Fabricant à partir de l'OUI (3 premiers octets) d'une adresse MAC.

Base IEEE MA-L (https://standards-oui.ieee.org/oui/oui.csv), figée dans le dépôt sous
data/oui_vendors.csv : pas d'appel réseau à chaque lookup, le collecteur reste utilisable
hors ligne. Pour rafraîchir la base, retélécharger le CSV officiel et ne garder que les
colonnes Assignment/Organization Name.
"""

import csv
import os

_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "oui_vendors.csv")

_vendors = None


def _load():
    global _vendors
    if _vendors is not None:
        return _vendors
    vendors = {}
    try:
        with open(_DATA_PATH, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) != 2:
                    continue
                oui, vendor = row
                vendors[oui.strip().upper()] = vendor.strip()
    except OSError:
        vendors = {}
    _vendors = vendors
    return _vendors


def lookup(mac):
    """Renvoie le nom du fabricant, ou None si l'adresse est localement administrée
    (générée, aucun fabricant réel derrière) ou absente de la base."""
    if not mac:
        return None
    try:
        first_octet = int(mac.split(":")[0], 16)
    except (ValueError, IndexError):
        return None
    if first_octet & 0x02:
        return None
    oui = mac.replace(":", "").replace("-", "").upper()[:6]
    return _load().get(oui)
