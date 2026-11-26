"""
Scraper Carrefour (carrefour.ro) -> Postgres.

Magento + frontend React. Cloudflare pazeste /graphql (403 challenge, legat de
amprenta TLS), DAR paginile SSR se servesc liber prin HTTP simplu. Categoriile
alimentare proaspete nu exista in sitemap (partea de "supermarket" cere selectie
de magazin), asa ca sursa e sitemap-ul de PRODUSE:
  /pub/sitemap/sitemap_002.xml  (~46k URL-uri /produse/)

Fiecare PDP contine:
  - JSON-LD Product: name, sku, brand, offers.price (pretul curent de vanzare)
  - dataLayer "detail": price (pretul de lista), category ("Bacanie & Lichide/..."),
    dimension10 ("available"/...)
Gramajul e in nume ("1 L", "500g"). Fara GTIN/EAN.

Atentie la volum: ~46k pagini/zi la ~3 req/s => ~4-5h. Timerul ruleaza noaptea
(23:45), ca sa termine inainte de matching (06:00).

Ruleaza:  PYTHONPATH=. python3 scrapers/carrefour.py [max_produse]
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from goodprice import db                       # noqa: E402
from goodprice.models import ScrapedProduct    # noqa: E402

BASE = "https://carrefour.ro"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
RATE_LIMIT_S = 0.15

_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_DETAIL_RE = re.compile(r'"detail":\s*\{.*?"products":\s*(\[.*?\])', re.S)


def sitemap_urls(client: httpx.Client) -> list[str]:
    urls: list[str] = []
    for sm in ("sitemap_001", "sitemap_002"):
        r = client.get(f"{BASE}/pub/sitemap/{sm}.xml")
        urls += re.findall(r"<loc>(https://carrefour\.ro/produse/[^<]+)</loc>", r.text)
    return sorted(set(urls))


def parse_pdp(html: str, url: str) -> ScrapedProduct | None:
    ld = None
    for m in _LD_RE.finditer(html):
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and d.get("@type") == "Product":
            ld = d
            break
    if not ld or not ld.get("sku") or not ld.get("name"):
        return None

    offers = ld.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    sell = offers.get("price")
    try:
        sell = float(sell) if sell is not None else None
    except (TypeError, ValueError):
        sell = None
    in_stock = "InStock" in str(offers.get("availability", ""))

    list_price = None
    category = None
    m = _DETAIL_RE.search(html)
    if m:
        try:
            det = json.loads(m.group(1))[0]
            list_price = float(det.get("price")) if det.get("price") is not None else None
            category = det.get("category")
        except (json.JSONDecodeError, IndexError, TypeError, ValueError):
            pass

    price = list_price if list_price is not None else sell
    if price is None:
        return None
    on_sale = sell is not None and list_price is not None and sell < list_price
    brand = ld.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")

    return ScrapedProduct(
        external_id=str(ld["sku"]),
        name=ld["name"],
        brand=brand or None,
        raw_amount=None,               # gramajul e in nume
        unit=None,
        price=price,
        sale_price=sell if on_sale else None,
        price_per_unit=None,
        currency="RON",
        in_stock=in_stock,
        slug=None,
        url=url,
        category_path=category,
    )


def run(max_products: int | None = None) -> None:
    con = db.connect()
    store_id = db.get_or_create_store(con, "carrefour", "Carrefour", "online", "RO")
    con.commit()

    written = 0
    failed = 0
    degraded = 0
    batch: list[ScrapedProduct] = []
    with httpx.Client(headers={"User-Agent": UA, "Accept-Language": "ro"},
                      timeout=30, follow_redirects=True) as client:
        urls = sitemap_urls(client)
        if max_products:
            urls = urls[:max_products]
        print(f"URL-uri produs in sitemap: {len(urls)}")
        for i, url in enumerate(urls, 1):
            p = None
            try:
                r = client.get(url)
                if r.status_code == 200:
                    p = parse_pdp(r.text, url)
            except Exception:
                pass
            if p:
                degraded = 0
                batch.append(p)
            else:
                failed += 1
                degraded += 1
                if degraded >= 40:
                    print(f"  ! {degraded} esecuri consecutive — probabil blocati; ma opresc curat")
                    break
            if len(batch) >= 100:
                written += db.write_products(con, store_id, batch)
                batch = []
            if i % 500 == 0 or i == len(urls):
                print(f"  [{i}/{len(urls)}] scrise={written + len(batch)} esuate={failed}")
            time.sleep(RATE_LIMIT_S + random.uniform(0, 0.1))
        if batch:
            written += db.write_products(con, store_id, batch)

    con.close()
    print(f"\nTotal scris in Postgres (carrefour): {written}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(n)
