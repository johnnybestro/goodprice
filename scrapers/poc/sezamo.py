"""
GoodPrice — PoC scraper Sezamo (grup Rohlik).

Dovada de concept pentru Faza 0: arata ca putem extrage date complet
structurate (nume, brand, gramaj, pret, pret/unitate, stoc) dintr-un magazin
online RO, doar cu HTTP GET, fara browser si fara sa luptam cu anti-bot.

Cum functioneaza:
  1. Ia arborele de categorii din API-ul intern (flat.json).
  2. Pentru fiecare categorie, descarca pagina si extrage starea React-Query
     "dehydrated" embed-ata in HTML (un JSON cu toate produsele paginii).
  3. Normalizeaza fiecare produs si calculeaza pretul pe unitate de baza.
  4. Salveaza in SQLite (poc.db) — sursa de adevar pentru PoC.

Productie (faza ulterioara): inlocuim urllib cu httpx + curl_cffi (spoof TLS),
prindem endpoint-ul XHR de paginare cu Playwright si mutam pe Postgres+Timescale.
Etic: rate-limit bland, respectam robots.txt, luam doar preturi publice.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

BASE = "https://www.sezamo.ro"
NAV_URL = f"{BASE}/services/frontend-service/renderer/navigation/flat.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DB_PATH = Path(__file__).parent / "poc.db"
RATE_LIMIT_S = 1.0  # bland cu serverul lor

# unitati de baza pentru pretul/unitate comparabil
BASE_UNITS = {"kg": "kg", "l": "l", "buc": "buc", "piece": "buc", "pcs": "buc"}


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ro"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "ignore")


def _balanced_object(s: str, start: int) -> str | None:
    """Returneaza obiectul JSON balansat care incepe la s[start] == '{'."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


@dataclass
class Product:
    store: str
    store_product_id: int
    name: str
    brand: str | None
    textual_amount: str | None       # ex: "cca 470 g", "1,5 L"
    unit: str | None                 # unitatea de pret (kg / l / buc)
    price: float                     # pret afisat (lei)
    sale_price: float | None
    price_per_unit: float | None     # lei / unitate de baza (lei/kg, lei/l)
    currency: str
    in_stock: bool
    url: str | None
    scraped_at: int


def categories() -> list[dict]:
    nav = json.loads(_get(NAV_URL)).get("navigation", {})
    # categoriile-frunza (fara copii) au listari de produse
    return [c for c in nav.values() if not c.get("children")]


def parse_products_from_page(html: str) -> list[dict]:
    """Extrage obiectele-produs din starea React-Query embed-ata."""
    text = html.replace('\\"', '"').replace("\\\\", "\\")
    out: dict[int, dict] = {}
    for m in re.finditer(r'\{"productId":\d+', text):
        obj = _balanced_object(text, m.start())
        if not obj:
            continue
        try:
            d = json.loads(obj)
        except json.JSONDecodeError:
            continue
        if "prices" in d and "name" in d:
            out[d["productId"]] = d
    return list(out.values())


def normalize(d: dict, cat_link: str) -> Product:
    prices = d.get("prices", {})
    unit = (d.get("unit") or "").lower()
    return Product(
        store="sezamo",
        store_product_id=d["productId"],
        name=d["name"],
        brand=d.get("brand"),
        textual_amount=d.get("textualAmount"),
        unit=BASE_UNITS.get(unit, unit or None),
        price=prices.get("originalPrice"),
        sale_price=prices.get("salePrice"),
        price_per_unit=prices.get("unitPrice"),
        currency=prices.get("currency", "RON"),
        in_stock=(d.get("stock", {}).get("availabilityStatus") == "AVAILABLE"),
        url=f"{BASE}/p{d['productId']}-{d.get('slug', '')}" if d.get("slug") else None,
        scraped_at=int(time.time()),
    )


def init_db(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS price (
            store TEXT, store_product_id INTEGER, name TEXT, brand TEXT,
            textual_amount TEXT, unit TEXT, price REAL, sale_price REAL,
            price_per_unit REAL, currency TEXT, in_stock INTEGER, url TEXT,
            scraped_at INTEGER,
            PRIMARY KEY (store, store_product_id, scraped_at)
        )""")
    con.commit()


def run(max_categories: int = 8) -> None:
    con = sqlite3.connect(DB_PATH)
    init_db(con)
    cats = categories()
    print(f"Categorii-frunza descoperite: {len(cats)} (procesez {max_categories})")
    total = 0
    for cat in cats[:max_categories]:
        try:
            html = _get(BASE + cat["link"])
        except Exception as e:
            print(f"  ! {cat['link']}: {e}")
            continue
        rows = parse_products_from_page(html)
        for d in rows:
            p = normalize(d, cat["link"])
            con.execute(
                "INSERT OR REPLACE INTO price VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(asdict(p).values()),
            )
        con.commit()
        total += len(rows)
        print(f"  {cat['name'][:30]:30} -> {len(rows):3} produse")
        time.sleep(RATE_LIMIT_S)
    print(f"\nTotal produse salvate in {DB_PATH.name}: {total}")
    con.close()


if __name__ == "__main__":
    run()
