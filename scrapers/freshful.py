"""
Scraper Freshful (eMAG / platforma Sylius) -> Postgres, via API-ul /api/v2/shop.

Pipeline:
  1. /api/web/categories                      -> slug-urile categoriilor-frunza
  2. /api/v2/shop/categories/{slug}?page=N    -> items paginat (are `pages` in raspuns)
  3. normalize -> ScrapedProduct -> scriere in DB

Listarea Freshful e mai bogata decat Sezamo: contine deja brand, price, originalPrice
si unitPriceLabel ("59,98 Lei/kg") -> un singur endpoint per pagina, fara call separat.
Freshful NU expune EAN -> matching pe brand+nume+gramaj.

Ruleaza din radacina proiectului:
    PYTHONPATH=. python3 scrapers/freshful.py [nr_categorii]   # gol = toate
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from goodprice import db                       # noqa: E402
from goodprice.models import ScrapedProduct    # noqa: E402

BASE = "https://www.freshful.ro"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ITEMS_PER_PAGE = 60
RATE_LIMIT_S = 0.3
BASE_UNITS = {"kg": "kg", "l": "l", "litru": "l", "buc": "buc", "bucata": "buc", "ml": "ml", "g": "g"}
_UNIT_RE = re.compile(r"([\d.,]+)\s*lei\s*/\s*([a-zăâîșț]+)", re.I)


def leaf_slugs(client: httpx.Client) -> list[str]:
    tree = client.get(f"{BASE}/api/web/categories").json()
    out: list[str] = []

    def walk(node):
        if isinstance(node, list):
            for c in node:
                walk(c)
        elif isinstance(node, dict):
            children = node.get("children") or node.get("subcategories") or []
            if node.get("slug") and not children:
                out.append(node["slug"])
            for c in children:
                walk(c)

    walk(tree)
    return out


def category_items(client: httpx.Client, slug: str) -> list[dict]:
    """Toate item-urile dintr-o categorie, paginat (raspunsul da `pages`)."""
    items: list[dict] = []
    page = 1
    while True:
        r = client.get(f"{BASE}/api/v2/shop/categories/{slug}",
                        params={"page": page, "itemsPerPage": ITEMS_PER_PAGE})
        if r.status_code != 200:
            break
        payload = r.json().get("payload", {})
        items.extend(payload.get("items", []))
        if page >= (payload.get("pages") or 1):
            break
        page += 1
        time.sleep(RATE_LIMIT_S)
    return items


def _parse_unit_price(label: str | None):
    """'59,98 Lei/kg' -> (59.98, 'kg')."""
    if not label:
        return None, None
    m = _UNIT_RE.search(label)
    if not m:
        return None, None
    val = float(m.group(1).replace(".", "").replace(",", "."))
    unit = BASE_UNITS.get(m.group(2).lower(), m.group(2).lower())
    return val, unit


def normalize(d: dict) -> ScrapedProduct:
    cur = d.get("price")
    orig = d.get("originalPrice")
    on_sale = orig is not None and cur is not None and orig > cur
    ppu, unit = _parse_unit_price(d.get("unitPriceLabel"))
    slug = d.get("slug", "")
    return ScrapedProduct(
        external_id=str(d.get("code") or d.get("sku")),
        name=d["name"],
        brand=d.get("brand"),
        raw_amount=None,                      # gramajul e in nume la Freshful; parsat ulterior
        unit=unit,
        price=orig if on_sale else cur,       # pretul normal
        sale_price=cur if on_sale else None,  # pretul promo
        price_per_unit=ppu,
        currency=d.get("currencyCode", "RON"),
        in_stock=bool(d.get("isAvailable")),
        ean=None,                             # Freshful nu expune EAN
        slug=slug or None,
        url=f"{BASE}/p/{slug}" if slug else None,
        category_path=None,
    )


def run(max_categories: int | None = None) -> None:
    headers = {"User-Agent": UA, "Accept": "application/json", "Accept-Language": "ro"}
    con = db.connect()
    store_id = db.get_or_create_store(con, "freshful", "Freshful", "online", "RO")
    con.commit()

    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        cats = leaf_slugs(client)
        if max_categories:
            cats = cats[:max_categories]
        print(f"Categorii-frunza de procesat: {len(cats)}")

        seen: set[str] = set()
        total = 0
        for i, slug in enumerate(cats, 1):
            products = []
            for it in category_items(client, slug):
                ext = str(it.get("code") or it.get("sku"))
                if ext in seen or "name" not in it:
                    continue
                seen.add(ext)
                products.append(normalize(it))
            total += db.write_products(con, store_id, products)
            if i % 25 == 0 or i == len(cats):
                print(f"  [{i}/{len(cats)}] categorii -> {total} produse scrise")
            time.sleep(RATE_LIMIT_S)
        print(f"\nTotal scris in Postgres: {total}")

    con.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(n)
