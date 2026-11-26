"""
Scraper Mega Image (mega-image.ro) -> Postgres.

Platforma Ahold Delhaize: Next.js + Apollo GraphQL pe /api/v1/ cu persisted
queries (GET cu operationName + variables + sha256Hash). Nu cere cookie-uri —
doar headerele Apollo (fara ele: 400). Operatia GetProductSearch cu searchQuery
GOL enumera INTREG catalogul (~7.7k produse), paginat (pageSize 50).

Campuri: name (gramajul e in nume), code, manufacturerName (brand real),
price.value / discountedPriceFormatted (promo) / supplementaryPriceLabel2
("10.49 Lei/L" — pretul pe unitate gata calculat), url (contine calea de
categorii), available.

Nota: hash-ul persisted query se poate schimba la release-urile lor — daca apar
400-uri, recaptureaza-l cu scrapers/_discover_megaimage_api.py.

Ruleaza:  PYTHONPATH=. python3 scrapers/megaimage.py [max_pagini]
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from goodprice import db                       # noqa: E402
from goodprice.models import ScrapedProduct    # noqa: E402

BASE = "https://www.mega-image.ro"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
QUERY_HASH = "1aed18b70ca933de3c1352038f798f0b8375ac515e9d0f93a23556402015418a"
HEADERS = {
    "User-Agent": UA, "Accept": "*/*", "Accept-Language": "ro-RO",
    "apollographql-client-name": "ro-mi-web-stores",
    "x-apollo-operation-name": "GetProductSearch",
    "x-default-gql-refresh-token-disabled": "true",
}
PAGE_SIZE = 50
RATE_LIMIT_S = 0.4

_MONEY_RE = re.compile(r"([\d.,]+)\s*Lei", re.I)
_PPU_RE = re.compile(r"([\d.,]+)\s*Lei\s*/\s*(l|kg|buc|bucata)", re.I)


def _money(s: str | None) -> float | None:
    if not s:
        return None
    m = _MONEY_RE.search(s)
    return float(m.group(1).replace(".", "").replace(",", ".")) if m else None


def _ppu(label: str | None):
    """'10.49 Lei/L' -> (10.49, 'l')."""
    if not label:
        return None, None
    m = _PPU_RE.search(label)
    if not m:
        return None, None
    val = float(m.group(1).replace(",", "."))
    unit = {"l": "l", "kg": "kg", "buc": "buc", "bucata": "buc"}[m.group(2).lower()]
    return val, unit


def search_page(client: httpx.Client, page: int, size: int = PAGE_SIZE) -> dict:
    v = {"lang": "ro", "searchQuery": "", "pageNumber": page, "pageSize": size,
         "filterFlag": True, "fields": "PRODUCT_TILE",
         "plainChildCategories": True, "useSpellingSuggestion": True}
    ext = {"persistedQuery": {"version": 1, "sha256Hash": QUERY_HASH}}
    url = (f"{BASE}/api/v1/?operationName=GetProductSearch"
           f"&variables={urllib.parse.quote(json.dumps(v, separators=(',', ':')))}"
           f"&extensions={urllib.parse.quote(json.dumps(ext, separators=(',', ':')))}")
    r = client.get(url)
    r.raise_for_status()
    return r.json()["data"]["productSearch"]


def normalize(d: dict) -> ScrapedProduct | None:
    pr = d.get("price") or {}
    value = pr.get("value")
    if value is None or not d.get("name"):
        return None
    sale = _money(pr.get("discountedPriceFormatted"))
    on_sale = sale is not None and sale < float(value)
    # ppu: pe promo foloseste eticheta redusa, altfel pe cea normala
    ppu, unit = _ppu(pr.get("discountedUnitPriceFormatted") if on_sale
                     else pr.get("supplementaryPriceLabel2"))
    url_path = d.get("url") or ""
    category = url_path.strip("/").split("/")[0] if url_path.startswith("/") else None
    return ScrapedProduct(
        external_id=str(d["code"]),
        name=d["name"],
        brand=d.get("manufacturerName") or None,
        raw_amount=None,                       # gramajul e in nume (ca la Freshful)
        unit=unit,
        price=float(value),
        sale_price=sale if on_sale else None,
        price_per_unit=ppu,
        currency=pr.get("currencyIso", "RON"),
        in_stock=bool(d.get("available")),
        slug=None,
        url=f"{BASE}{url_path}" if url_path else None,
        category_path=category,
    )


def run(max_pages: int | None = None) -> None:
    con = db.connect()
    store_id = db.get_or_create_store(con, "mega-image", "Mega Image", "online", "RO")
    con.commit()

    written = 0
    seen: set[str] = set()
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        first = search_page(client, 0)
        total_pages = first["pagination"]["totalPages"]
        total_results = first["pagination"]["totalResults"]
        print(f"Catalog Mega Image: {total_results} produse, {total_pages} pagini")
        pages = range(total_pages if not max_pages else min(max_pages, total_pages))
        for pg in pages:
            data = first if pg == 0 else search_page(client, pg)
            fresh = []
            for d in data.get("products", []):
                p = normalize(d)
                if p and p.external_id not in seen:
                    seen.add(p.external_id)
                    fresh.append(p)
            written += db.write_products(con, store_id, fresh)
            if (pg + 1) % 20 == 0 or pg + 1 == len(pages):
                print(f"  [{pg + 1}/{len(pages)}] scrise={written}")
            time.sleep(RATE_LIMIT_S)

    con.close()
    print(f"\nTotal scris in Postgres (mega-image): {written}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(n)
