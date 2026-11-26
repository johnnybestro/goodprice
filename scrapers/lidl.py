"""
Scraper Lidl.ro -> Postgres.

Lidl.ro nu vinde alimente online — site-ul e o vitrina (Nuxt 3) cu preturile din
magazin. Sursa COMPLETA e sitemap-ul oficial de produse
(/p/export/RO/ro/product_sitemap.xml.gz, ~2000 URL-uri): asa prindem si paginile
de campanie care se schimba saptamanal (oferte Lidl Plus, tematice etc.), nu doar
categoria principala (~470). Produsele arhivate din sitemap nu au pret -> sarite.

Datele sunt embed-ate server-side in <script id="__NUXT_DATA__"> in formatul
"devalue" (array plat in care obiectele refera valorile prin indecsi).
Campuri: keyfacts.fullTitle, price.price (curent), price.oldPrice (inainte de
reducere), price.packaging.text ("0,75 L"), price.basePrice.text
("1 L = 19,99 lei" — pretul pe unitate gata calculat). `eans` exista dar e
mereu gol (verificat). Carnea la cantar are packaging "per 100 g" -> pret x10 = lei/kg.

Ruleaza:  PYTHONPATH=. python3 scrapers/lidl.py [max_produse]
"""
from __future__ import annotations

import gzip
import random
import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from goodprice import db                       # noqa: E402
from goodprice.models import ScrapedProduct    # noqa: E402

BASE = "https://www.lidl.ro"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
RATE_LIMIT_S = 1.0    # Lidl throttluieste la ritm sustinut (verificat) — mergem incet

_NUXT_RE = re.compile(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)
_BASEPRICE_RE = re.compile(r"([\d.,]+)\s*(kg|g|l|ml|buc)\s*=\s*([\d.,]+)\s*lei", re.I)
_DEVALUE_MARKERS = {"Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef"}


def _num(s: str) -> float:
    return float(s.replace(".", "").replace(",", ".")) if "," in s else float(s)


class Devalue:
    """Rezolvator pentru formatul devalue al Nuxt 3 (array plat cu indecsi)."""

    def __init__(self, flat: list):
        self.flat = flat

    def resolve(self, idx, depth=0):
        if depth > 10 or not isinstance(idx, int) or not (0 <= idx < len(self.flat)):
            return None
        v = self.flat[idx]
        if isinstance(v, dict):
            return {k: self.resolve(i, depth + 1) for k, i in v.items()}
        if isinstance(v, list):
            if len(v) == 2 and isinstance(v[0], str) and v[0] in _DEVALUE_MARKERS:
                return self.resolve(v[1], depth + 1)
            return [self.resolve(i, depth + 1) for i in v]
        return v

    def products(self) -> list[dict]:
        out = []
        for i, v in enumerate(self.flat):
            # listare: are fullTitle; pagina de detaliu: are keyfacts (titlul e acolo)
            if isinstance(v, dict) and "erpNumber" in v and "price" in v \
                    and ("fullTitle" in v or "keyfacts" in v):
                p = self.resolve(i)
                if p and isinstance(p.get("price"), dict):
                    out.append(p)
        return out

    def num_found(self) -> int | None:
        for v in self.flat:
            if isinstance(v, dict) and "numFound" in v:
                n = self.flat[v["numFound"]] if isinstance(v["numFound"], int) else None
                if isinstance(n, int):
                    return n
        return None


def parse_page(html: str) -> tuple[list[dict], int | None]:
    m = _NUXT_RE.search(html)
    if not m:
        return [], None
    dv = Devalue(json.loads(m.group(1)))
    return dv.products(), dv.num_found()


def _price_per_unit(base_price_text: str | None):
    """'1 L = 19,99 lei' -> (19.99, 'l'); '100 g = 2,39 lei' -> (23.90, 'kg')."""
    if not base_price_text:
        return None, None
    m = _BASEPRICE_RE.search(base_price_text)
    if not m:
        return None, None
    qty, unit, price = _num(m.group(1)), m.group(2).lower(), _num(m.group(3))
    per_one = price / qty if qty else None
    if per_one is None:
        return None, None
    if unit == "g":
        return round(per_one * 1000, 4), "kg"
    if unit == "ml":
        return round(per_one * 1000, 4), "l"
    return round(per_one, 4), {"kg": "kg", "l": "l", "buc": "buc"}[unit]


def normalize(d: dict) -> ScrapedProduct | None:
    pr = d.get("price") or {}
    current = pr.get("price")
    if current is None:
        return None
    # pe paginile de detaliu, titlul e in keyfacts (fullTitle e null la top-level)
    if not d.get("fullTitle"):
        kf = d.get("keyfacts") or {}
        d = {**d, "fullTitle": kf.get("fullTitle") or kf.get("title")}
    if not d.get("fullTitle"):
        return None
    old = pr.get("oldPrice")
    on_sale = isinstance(old, (int, float)) and old > current
    ppu, unit = _price_per_unit((pr.get("basePrice") or {}).get("text"))
    packaging = (pr.get("packaging") or {}).get("text") or ""
    if ppu is None and re.search(r"per\s*100\s*g", packaging, re.I):
        # carne/mezeluri la cantar: pretul afisat e per 100 g -> lei/kg
        eff = float(current if not on_sale else current)
        ppu, unit = round(eff * 10, 4), "kg"
    brand = (d.get("brand") or {}).get("name") or None
    path = d.get("canonicalPath")
    return ScrapedProduct(
        external_id=str(d["erpNumber"]),
        name=d["fullTitle"],
        brand=brand,
        raw_amount=(pr.get("packaging") or {}).get("text"),
        unit=unit,
        price=float(old) if on_sale else float(current),
        sale_price=float(current) if on_sale else None,
        price_per_unit=ppu,
        currency=pr.get("currencyCode") or "RON",
        in_stock=True,          # preturi de magazin fizic (vitrina, nu e-commerce)
        slug=None,
        url=f"{BASE}{path}" if path else None,
        category_path="alimente-bauturi",
    )


_PAGE_RE = re.compile(r"/c/[a-z0-9-]+/[as]\d+")
_ERP_FROM_URL = re.compile(r"/p(\d+)$")


def sitemap_urls(client: httpx.Client) -> list[str]:
    r = client.get(f"{BASE}/p/export/RO/ro/product_sitemap.xml.gz")
    raw = r.content
    xml = gzip.decompress(raw).decode("utf-8", "ignore") if raw[:2] == b"\x1f\x8b" \
        else raw.decode("utf-8", "ignore")
    return re.findall(r"<loc>(https://www\.lidl\.ro/p/[^<]+)</loc>", xml)


def discover_pages(client: httpx.Client) -> list[str]:
    """Paginile de listare, descoperite dinamic: categoriile statice (s-id) +
    campaniile saptamanale (a-id — ID-urile se schimba saptamanal!) de pe homepage
    si de pe fiecare categorie statica."""
    found: set[str] = set()
    html = client.get(BASE + "/").text
    found.update(_PAGE_RE.findall(html))
    for path in [p for p in sorted(found) if re.search(r"/s\d+$", p)]:
        try:
            html = client.get(BASE + path).text
            found.update(_PAGE_RE.findall(html))
        except httpx.HTTPError:
            continue
        time.sleep(RATE_LIMIT_S + random.uniform(0, 0.6))
    return sorted(found)


def crawl_listing(client: httpx.Client, path: str, seen: set[str]) -> list[ScrapedProduct]:
    """Toate produsele noi dintr-o pagina de listare, cu paginare ?offset= (pas 12)."""
    out: list[ScrapedProduct] = []
    offset = 0
    num_found = None
    empty_pages = 0
    while True:
        url = f"{BASE}{path}" + (f"?offset={offset}" if offset else "")
        try:
            prods, nf = parse_page(client.get(url).text)
        except httpx.HTTPError:
            break
        num_found = num_found or nf
        fresh = 0
        for d in prods:
            p = normalize(d)
            if p and p.external_id not in seen:
                seen.add(p.external_id)
                out.append(p)
                fresh += 1
        empty_pages = 0 if fresh else empty_pages + 1
        offset += 12
        if not prods or empty_pages >= 3 or (num_found and offset >= num_found) or offset > 1200:
            break
        time.sleep(RATE_LIMIT_S + random.uniform(0, 0.6))
    return out


def run(max_products: int | None = None) -> None:
    headers = {"User-Agent": UA, "Accept-Language": "ro"}
    con = db.connect()
    store_id = db.get_or_create_store(con, "lidl", "Lidl", "offline", "RO")
    con.commit()

    written = 0
    seen: set[str] = set()
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        # 1) listari: categorii + campanii saptamanale (rapid, produsele curente)
        pages = discover_pages(client)
        print(f"Pagini de listare descoperite: {len(pages)}")
        for path in pages:
            fresh = crawl_listing(client, path, seen)
            if fresh:
                written += db.write_products(con, store_id, fresh)
                print(f"  {path[:56]:56} +{len(fresh):4}  (total {written})")
            time.sleep(RATE_LIMIT_S + random.uniform(0, 0.6))

        # 2) completare din sitemap-ul de produse: doar URL-urile nevazute in listari
        try:
            smap = sitemap_urls(client)
        except httpx.HTTPError:
            smap = []
        todo = [u for u in smap
                if (m := _ERP_FROM_URL.search(u)) and m.group(1) not in seen]
        if max_products:
            todo = todo[:max_products]
        print(f"Sitemap: {len(smap)} URL-uri, de completat (nevazute): {len(todo)}")
        batch: list[ScrapedProduct] = []
        no_price = 0
        degraded = 0   # pagini fara NICIUN obiect-produs = throttling (arhivatele AU obiect, doar fara pret)
        for i, url in enumerate(todo, 1):
            try:
                prods, _ = parse_page(client.get(url).text)
                p = normalize(prods[0]) if prods else None
            except Exception:
                prods, p = [], None
            degraded = degraded + 1 if not prods else 0
            if degraded >= 25:
                print(f"  ! {degraded} pagini consecutive fara date — probabil throttling; ma opresc")
                break
            if p and p.external_id not in seen:
                seen.add(p.external_id)
                if not p.url:
                    p.url = url
                batch.append(p)
            else:
                no_price += 1
            if len(batch) >= 50:
                written += db.write_products(con, store_id, batch)
                batch = []
            if i % 300 == 0 or i == len(todo):
                print(f"  sitemap [{i}/{len(todo)}] scrise={written + len(batch)} "
                      f"arhivate/fara_pret={no_price}")
            time.sleep(RATE_LIMIT_S + random.uniform(0, 0.6))
        if batch:
            written += db.write_products(con, store_id, batch)

    con.close()
    print(f"\nTotal scris in Postgres (lidl): {written} produse unice")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(n)
