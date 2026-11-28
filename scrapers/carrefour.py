"""
Scraper Carrefour (carrefour.ro) -> Postgres.

Magento + frontend React. Cloudflare pazeste /graphql (403 challenge, legat de
amprenta TLS), DAR paginile SSR se servesc liber prin HTTP simplu. Categoriile
alimentare proaspete nu exista in sitemap (partea de "supermarket" cere selectie
de magazin), asa ca sursa e sitemap-ul de PRODUSE:
  /pub/sitemap/sitemap_001.xml + sitemap_002.xml  (~90k URL-uri /produse/ la 2026-11-28)

Fiecare PDP contine:
  - JSON-LD Product: name, sku, brand, offers.price (pretul curent de vanzare)
  - dataLayer "detail": price (pretul de lista), category ("Bacanie & Lichide/..."),
    dimension10 ("available"/...)
Gramajul e in nume ("1 L", "500g"). Fara GTIN/EAN.

Volum (masurat 2026-11-27): un PDP are ~330 KB si TTFB 1.2-1.9 s, deci secvential
~0.5 pagini/s => ~2 zile pentru tot sitemap-ul. De aceea:
  - WORKERS cereri in paralel (implicit 3, fiecare cu pauza+jitter) => ~2 pagini/s,
    sub cele ~3 req/s pentru care a fost gandit scraperul;
  - moduri:
      daily — URL-urile deja cunoscute din categoriile comparabile cu celelalte magazine
              (DAILY_CATEGORIES) + URL-urile din sitemap pe care nu le-am vazut niciodata
              (max NEW_PER_DAY). ~40% din sitemap, ~4-5h.
      full  — tot sitemap-ul (~90k), ~12h (duminica noaptea -> luni dimineata). Reclasifica produsele si prinde schimbarile
              de categorie.
      new   — doar URL-urile din sitemap necunoscute (bootstrap / completare).
      auto  — (implicit, timerul) full duminica sau daca nu avem inca destule produse
              cunoscute, altfel daily.

Ruleaza:  PYTHONPATH=. python3 scrapers/carrefour.py [auto|daily|full|new] [max_produse]
Env:      CARREFOUR_WORKERS (implicit 3), CARREFOUR_MODE (implicit auto)
"""
from __future__ import annotations

import json
import random
import re
import datetime
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from goodprice import db                       # noqa: E402
from goodprice.models import ScrapedProduct    # noqa: E402

BASE = "https://carrefour.ro"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
RATE_LIMIT_S = 0.15
WORKERS = max(1, min(int(os.environ.get("CARREFOUR_WORKERS", "3")), 4))
NEW_PER_DAY = 3000
FULL_WEEKDAY = 6                 # duminica (ora Romaniei, vezi _local_weekday)
MIN_KNOWN_FOR_DAILY = 2000       # sub atat, "auto" face full (bootstrap)

# Categorii comparabile cu Sezamo/Freshful/Mega Image/Lidl/Kaufland (prefix de category_path).
# Restul (TEX, electronice, mobila, carti, jucarii, auto...) se reverifica doar saptamanal.
DAILY_CATEGORIES = (
    "Bacanie & Lichide",
    "Cosmetice & Ingrijire personala",
    "Articole bebelusi",
    "Casa, Gradina & Petshop/Produse de curatenie pentru casa",
    "Casa, Gradina & Petshop/Petshop",
)

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


def known_urls(con, store_id: int) -> tuple[set[str], list[str]]:
    """(toate URL-urile cunoscute, URL-urile cunoscute din DAILY_CATEGORIES)."""
    rows = con.execute(
        "SELECT url, category_path FROM store_product WHERE store_id = %s AND url IS NOT NULL",
        (store_id,)).fetchall()
    known = {u for u, _ in rows}
    daily = sorted(u for u, c in rows if c and c.startswith(DAILY_CATEGORIES))
    return known, daily


def _local_weekday() -> int:
    # fara tzdata: Romania e UTC+2 iarna / UTC+3 vara; la 21:30 UTC ziua e aceeasi oricum
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)).weekday()


def pick_urls(mode: str, client: httpx.Client, con, store_id: int) -> tuple[str, list[str]]:
    known, daily = known_urls(con, store_id)
    if mode == "auto":
        mode = "full" if (_local_weekday() == FULL_WEEKDAY or len(daily) < MIN_KNOWN_FOR_DAILY) \
            else "daily"
    sm = sitemap_urls(client)
    print(f"URL-uri produs in sitemap: {len(sm)}; cunoscute: {len(known)}; "
          f"cunoscute in categoriile zilnice: {len(daily)}", flush=True)
    if mode == "full":
        return mode, sm
    new = [u for u in sm if u not in known]
    if mode == "new":
        return mode, new
    if mode == "daily":
        # produsele disparute din sitemap nu se mai cer; cele noi se clasifica azi (plafonat)
        in_sm = set(sm)
        return mode, [u for u in daily if u in in_sm] + new[:NEW_PER_DAY]
    raise SystemExit(f"mod necunoscut: {mode} (auto|daily|full|new)")


def fetch(client: httpx.Client, url: str, stop: threading.Event) -> ScrapedProduct | None:
    if stop.is_set():
        return None
    p = None
    try:
        r = client.get(url)
        if r.status_code == 200:
            p = parse_pdp(r.text, url)
    except Exception:
        pass
    # pauza per worker: WORKERS cereri in zbor, fiecare urmata de 0.15-0.25 s
    time.sleep(RATE_LIMIT_S + random.uniform(0, 0.1))
    return p


def run(mode: str = "auto", max_products: int | None = None) -> None:
    con = db.connect()
    store_id = db.get_or_create_store(con, "carrefour", "Carrefour", "online", "RO")
    con.commit()

    written = 0
    failed = 0
    degraded = 0
    batch: list[ScrapedProduct] = []
    stop = threading.Event()
    t0 = time.monotonic()
    limits = httpx.Limits(max_connections=WORKERS, max_keepalive_connections=WORKERS)
    with httpx.Client(headers={"User-Agent": UA, "Accept-Language": "ro"},
                      timeout=30, follow_redirects=True, limits=limits) as client:
        mode, urls = pick_urls(mode, client, con, store_id)
        if max_products:
            urls = urls[:max_products]
        print(f"mod={mode} workers={WORKERS}: {len(urls)} pagini de cerut", flush=True)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = [ex.submit(fetch, client, u, stop) for u in urls]
            for i, fut in enumerate(as_completed(futures), 1):
                p = fut.result()
                if stop.is_set():
                    continue
                if p:
                    degraded = 0
                    batch.append(p)
                else:
                    failed += 1
                    degraded += 1
                    if degraded >= 40:
                        print(f"  ! {degraded} esecuri consecutive — probabil blocati; "
                              "ma opresc curat", flush=True)
                        stop.set()
                        ex.shutdown(wait=False, cancel_futures=True)
                        continue
                if len(batch) >= 100:
                    written += db.write_products(con, store_id, batch)
                    batch = []
                if i % 500 == 0 or i == len(urls):
                    rate = i / max(time.monotonic() - t0, 1)
                    print(f"  [{i}/{len(urls)}] scrise={written + len(batch)} esuate={failed} "
                          f"({rate:.2f} pagini/s)", flush=True)
        if batch:
            written += db.write_products(con, store_id, batch)

    con.close()
    print(f"\nTotal scris in Postgres (carrefour, mod={mode}): {written} "
          f"in {(time.monotonic() - t0) / 3600:.1f}h", flush=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = args.pop(0) if args and not args[0].isdigit() else os.environ.get("CARREFOUR_MODE", "auto")
    n = int(args[0]) if args else None
    run(mode, n)
