"""
Scraper Sezamo (grup Rohlik) -> Postgres, via API-ul JSON intern /api/v1/.

Pipeline:
  1. nav flat.json            -> id-urile categoriilor-frunza
  2. categories/.../products  -> toate productId-urile (paginat, size=100)
  3. products/card            -> cardurile complete (nume, brand, gramaj, pret) in batch
  4. normalize -> ScrapedProduct -> scriere in DB (store_product + price_history + current_price)

Nota: Sezamo NU expune EAN/barcode (doar id-uri interne). Matching-ul intre
magazine pentru Sezamo se va baza pe brand+nume+gramaj (+embeddings), nu pe EAN.

Ruleaza din radacina proiectului:
    PYTHONPATH=. python3 scrapers/sezamo.py [nr_categorii]   # gol = toate
"""
from __future__ import annotations

import sys
import time
from itertools import islice
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from goodprice import db                       # noqa: E402
from goodprice.models import ScrapedProduct    # noqa: E402

BASE = "https://www.sezamo.ro"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PAGE_SIZE = 100
CARD_BATCH = 50
RATE_LIMIT_S = 0.3
BASE_UNITS = {"kg": "kg", "l": "l", "buc": "buc", "piece": "buc", "pcs": "buc"}


def _batches(seq, n):
    it = iter(seq)
    while chunk := list(islice(it, n)):
        yield chunk


def leaf_category_ids(client: httpx.Client) -> list[int]:
    nav = client.get(f"{BASE}/services/frontend-service/renderer/navigation/flat.json").json()
    return [c["id"] for c in nav.get("navigation", {}).values() if not c.get("children")]


def category_product_ids(client: httpx.Client, cat_id: int) -> list[int]:
    """Toate productId-urile dintr-o categorie, paginat."""
    ids: list[int] = []
    page = 0
    while True:
        r = client.get(
            f"{BASE}/api/v1/categories/normal/{cat_id}/products",
            params={"page": page, "size": PAGE_SIZE, "sort": "recommended", "filter": ""},
        )
        if r.status_code != 200:
            break
        pids = r.json().get("productIds", [])
        ids.extend(pids)
        if len(pids) < PAGE_SIZE:
            break
        page += 1
        time.sleep(RATE_LIMIT_S)
    return ids


def fetch_cards(client: httpx.Client, product_ids: list[int]) -> list[dict]:
    """Produsele complete pentru un set de id-uri, in batch.

    Combina doua endpointuri (ambele iau acelasi parametru `products`):
      - /products/card  -> pret, nume, gramaj, stoc, slug
      - /products       -> brand (cardul NU intoarce brandul!)
    """
    out: dict[int, dict] = {}
    for chunk in _batches(product_ids, CARD_BATCH):
        params = [("products", str(p)) for p in chunk]
        rc = client.get(f"{BASE}/api/v1/products/card",
                        params=params + [("categoryType", "normal")])
        if rc.status_code == 200:
            for c in rc.json():
                out[c["productId"]] = c
        # imbogateste cu brandul din endpointul de info
        rb = client.get(f"{BASE}/api/v1/products", params=params)
        if rb.status_code == 200:
            for b in rb.json():
                if b["id"] in out:
                    out[b["id"]]["brand"] = b.get("brand")
        time.sleep(RATE_LIMIT_S)
    return list(out.values())


def normalize(d: dict) -> ScrapedProduct:
    prices = d.get("prices", {})
    unit = (d.get("unit") or "").lower()
    slug = d.get("slug", "")
    pid = d["productId"]
    return ScrapedProduct(
        external_id=str(pid),
        name=d["name"],
        brand=d.get("brand"),
        raw_amount=d.get("textualAmount"),
        unit=BASE_UNITS.get(unit, unit or None),
        price=prices.get("originalPrice"),
        sale_price=prices.get("salePrice"),
        price_per_unit=prices.get("unitPrice"),
        currency=prices.get("currency", "RON"),
        in_stock=(d.get("stock", {}).get("availabilityStatus") == "AVAILABLE"),
        ean=None,  # Sezamo nu expune EAN
        slug=slug or None,
        url=f"{BASE}/p{pid}-{slug}" if slug else None,
        category_path=str(d.get("mainCategoryId")) if d.get("mainCategoryId") else None,
    )


def run(max_categories: int | None = None) -> None:
    headers = {"User-Agent": UA, "Accept-Language": "ro", "Accept": "application/json"}
    con = db.connect()
    store_id = db.get_or_create_store(con, "sezamo", "Sezamo", "online", "RO")
    con.commit()

    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        cats = leaf_category_ids(client)
        if max_categories:
            cats = cats[:max_categories]
        print(f"Categorii de procesat: {len(cats)}")

        # 1) colecteaza toate productId-urile unice (un produs poate fi in mai multe categorii)
        all_ids: set[int] = set()
        for i, cat_id in enumerate(cats, 1):
            before = len(all_ids)
            all_ids.update(category_product_ids(client, cat_id))
            if i % 25 == 0 or i == len(cats):
                print(f"  [{i}/{len(cats)}] categorii -> {len(all_ids)} produse unice")
        print(f"Total produse unice: {len(all_ids)}")

        # 2) ia cardurile complete si scrie in DB, pe masura ce vin
        total = 0
        for chunk in _batches(sorted(all_ids), CARD_BATCH):
            cards = fetch_cards(client, chunk)
            products = [normalize(d) for d in cards if "prices" in d and "name" in d]
            total += db.write_products(con, store_id, products)
        print(f"\nTotal scris in Postgres: {total}")

    con.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(n)
