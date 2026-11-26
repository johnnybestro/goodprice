"""
Scraper Kaufland.ro (oferte saptamanale) -> Postgres.

Kaufland.ro nu are shop online — dar ofertele saptamanale sunt publicate cu date
complet structurate, embed-ate server-side in pagina (window.SSR = {...}, componenta
"OfferTemplate"): titlu (de regula brandul), subtitlu (descriere + gramaj), pret nou,
pret vechi (formattedOldPrice), procent reducere, pret/unitate gata calculat
("(=1 kg 11.99)"), perioada de valabilitate si categoria.

BONUS unic in tot sistemul: ~90% din oferte au EAN-ul in numele imaginii
(ex. .../4002420747912_RO_P) — il salvam in store_product.ean.

O singura cerere HTTP pe zi (pagina saptamanii curente). Robots.txt permite zona
de oferte; fara Cloudflare pe aceste pagini (verificat 2026-11-26).
Exista si oferte per magazin (/.kloffers.storeName=RO1000.json + /.klstorefinder.json,
209 magazine) — nefolosite: luam vitrina nationala.

Ruleaza:  PYTHONPATH=. python3 scrapers/kaufland.py
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from goodprice import db                       # noqa: E402
from goodprice.models import ScrapedProduct    # noqa: E402

URL = "https://www.kaufland.ro/oferte/oferte-saptamanale/saptamana-curenta.html"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_SSR_RE = re.compile(r"window\.SSR\['[^']+'\] = (\{.*?\});?\s*</script>", re.S)
_BASEPRICE_RE = re.compile(r"=\s*([\d.,]+)\s*(kg|g|l|ml|buc)\s+([\d.,]+)", re.I)
_EAN_RE = re.compile(r"/(\d{8,14})_[A-Z]")


def _num(s: str | None) -> float | None:
    if not s:
        return None
    s = str(s).strip().replace(" ", "")
    # formatul RO: "1.234,56" sau "59,99"; API-ul da si float-uri direct
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _price_per_unit(base_price: str | None):
    """'(=1 kg 11.99)' -> (11.99,'kg'); '(=1 g 0.03)' -> (30.0,'kg');
    '(=1 buc 0.10)' -> (0.10,'buc')."""
    if not base_price:
        return None, None
    m = _BASEPRICE_RE.search(base_price)
    if not m:
        return None, None
    qty, unit, price = _num(m.group(1)), m.group(2).lower(), _num(m.group(3))
    if not qty or price is None:
        return None, None
    per_one = price / qty
    if unit == "g":
        return round(per_one * 1000, 4), "kg"
    if unit == "ml":
        return round(per_one * 1000, 4), "l"
    return round(per_one, 4), unit


def parse_offers(html: str) -> list[dict]:
    m = _SSR_RE.search(html)
    if not m:
        raise RuntimeError("window.SSR negasit — s-a schimbat structura paginii Kaufland")
    props = json.loads(m.group(1)).get("props") or {}
    cycles = (props.get("offerData") or {}).get("cycles") or []
    out = []
    for cyc in cycles:
        for cat in cyc.get("categories") or []:
            cat_name = re.sub(r"^[0-9_]+", "", cat.get("name") or "").replace("_", " ").strip()
            for o in cat.get("offers") or []:
                o["_cat"] = cat_name or None
                out.append(o)
    return out


def normalize(o: dict, today: datetime.date) -> ScrapedProduct | None:
    price = o.get("price")
    if not isinstance(price, (int, float)):
        return None
    # doar ofertele valabile AZI — preturile expirate/viitoare ar minti comparatia
    d_from, d_to = o.get("dateFrom"), o.get("dateTo")
    iso = today.isoformat()
    if (d_from and d_from > iso) or (d_to and d_to < iso):
        return None
    title = (o.get("title") or "").strip()
    subtitle = re.sub(r"\s+", " ", o.get("subtitle") or "").strip()
    name = f"{title} {subtitle}".strip()
    if not name:
        return None
    old = _num(o.get("formattedOldPrice"))
    on_sale = old is not None and old > float(price)
    ppu, unit = _price_per_unit(o.get("basePrice"))
    raw_amount = (o.get("unit") or "").strip() or None
    if ppu is None and raw_amount and re.fullmatch(r"(1\s*)?kg", raw_amount, re.I):
        # vandut la cantar: pretul afisat ESTE lei/kg
        ppu, unit = round(float(price), 4), "kg"
    ean = None
    ean_m = _EAN_RE.search(o.get("listImage") or "")
    if ean_m and ean_m.group(1) != str(o.get("klNr")):
        # cand produsul n-are EAN, imaginea e numita dupa klNr -> nu e EAN real
        ean = ean_m.group(1)
    # titlul e brandul in conventia flyer-ului ("Dove" + "Deodorant spray 200 ml");
    # cand nu e (ex. "Masa pentru cafea"), brandul gresit doar nu gaseste perechi.
    # Exceptie: fructele/legumele raman FARA brand ca sa intre in matching-ul
    # la cantar (lei/kg) cu celelalte magazine.
    is_produce = bool(re.search(r"legume|fructe", o.get("_cat") or "", re.I))
    brand = None if is_produce else (title or None)
    return ScrapedProduct(
        external_id=str(o.get("klNr") or o.get("offerId")),
        name=name,
        brand=brand,
        raw_amount=raw_amount,
        unit=unit,
        price=old if on_sale else float(price),
        sale_price=float(price) if on_sale else None,
        price_per_unit=ppu,
        currency="RON",
        in_stock=True,          # oferta de magazin fizic (vitrina, nu e-commerce)
        ean=ean,
        slug=None,
        url=URL,
        category_path=o.get("_cat"),
    )


def run() -> None:
    today = datetime.date.today()
    with httpx.Client(headers={"User-Agent": UA, "Accept-Language": "ro"},
                      timeout=60, follow_redirects=True) as client:
        offers = parse_offers(client.get(URL).text)
    print(f"Oferte in pagina: {len(offers)}")

    seen: set[str] = set()
    products: list[ScrapedProduct] = []
    skipped = 0
    for o in offers:
        p = normalize(o, today)
        if p is None:
            skipped += 1
        elif p.external_id not in seen:   # acelasi klNr poate aparea in mai multe campanii
            seen.add(p.external_id)
            products.append(p)

    with_ean = sum(1 for p in products if p.ean)
    with_disc = sum(1 for p in products if p.sale_price is not None)
    print(f"Valide azi: {len(products)} (EAN: {with_ean}, cu reducere: {with_disc}, "
          f"sarite/expirate: {skipped})")
    if len(products) < 50:
        print("! Suspect de putine oferte — nu scriu in DB (pagina schimbata sau goala?)")
        sys.exit(1)

    con = db.connect()
    store_id = db.get_or_create_store(con, "kaufland", "Kaufland", "offline", "RO")
    con.commit()
    written = db.write_products(con, store_id, products)
    con.close()
    print(f"\nTotal scris in Postgres (kaufland): {written} produse")


if __name__ == "__main__":
    run()
