"""Normalizare de text partajata (fara dependinte interne — importabila de oriunde).

`name_norm` e forma canonica de cautare a unui nume de produs: lowercase, fara
diacritice, doar ASCII. Se stocheaza in store_product.name_norm (intretinuta de
db.write_products) ca Postgres sa poata cauta pe cuvant intreg (\\y) cu index,
chiar si pe baza SQL_ASCII — cautarea nu mai depinde de fetch-all + filtrare in
Python (care cerea o limita fixa => trunchiere silentioasa cand catalogul creste).
"""
from __future__ import annotations

import re
import unicodedata


def strip_diacritics(s: str) -> str:
    s = s.replace("ș", "s").replace("ş", "s").replace("ț", "t").replace("ţ", "t")
    s = s.replace("Ș", "S").replace("Ş", "S").replace("Ț", "T").replace("Ţ", "T")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def name_norm(name: str) -> str:
    s = strip_diacritics(name).lower()
    s = s.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).strip()
