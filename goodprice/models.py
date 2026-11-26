"""Modele partajate intre scrapere si stratul DB."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScrapedProduct:
    """Un produs asa cum vine de la un scraper, inainte de scriere in DB."""
    external_id: str                 # id-ul intern al magazinului (unic per magazin)
    name: str
    brand: str | None
    raw_amount: str | None           # "cca 470 g"
    unit: str | None                 # kg | l | buc
    price: float | None              # lei
    sale_price: float | None
    price_per_unit: float | None     # lei / unitate de baza
    currency: str
    in_stock: bool
    ean: str | None = None
    slug: str | None = None
    url: str | None = None
    category_path: str | None = None
