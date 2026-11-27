"""
Healthcheck zilnic GoodPrice — ruleaza dupa scrapere + matching (timer 06:30).

Verifica lucrurile pe care un client le-ar observa primul:
  1. API-ul raspunde.
  2. Produse-ETALON: termeni cunoscuti trebuie sa intoarca produse cunoscute
     (prinde regresii de cautare — ex. bugul "Azuga 8l" trunchiat de limita).
  3. Prospetimea datelor: fiecare magazin are preturi actualizate in ultimele 3 zile.

Exit != 0 la orice esec => vizibil in journal + systemd status failed.
"""
import sys
import urllib.request
import json
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from goodprice import db                  # noqa: E402
from goodprice.matching import _s         # noqa: E402

API = os.environ.get("GOODPRICE_API_URL", "http://localhost:8000")

# (termen, fragment care TREBUIE sa apara in numele unui rezultat, magazin asteptat sau None)
GOLDENS = [
    ("apa",   "azuga apa natural alcalina", "sezamo"),
    ("apa",   "borsec",                     None),
    ("lapte", "zuzu",                       None),
    ("oua",   "oua",                        None),
    ("paine", "paine",                      None),
    ("cafea", "lavazza",                    None),
    ("bere",  "ursus",                      None),
    ("iaurt", "activia",                    None),
]

# Staple-uri: pretul standard per magazin trebuie sa fie plauzibil (lei pe cantitatea standard).
# GOLDEN 2026-11-27: Mega Image "Oua ..., 6 bucati" 9.99 lei aparea 99.90 lei / 10 buc, pentru ca
# price_per_unit al Mega Image la unit='buc' e pretul pachetului, nu al bucatii.
STAPLE_RANGES = {
    "oua": (5.0, 60.0),     # 10 buc
}

failures: list[str] = []


def check_api():
    try:
        with urllib.request.urlopen(f"{API}/api/stores", timeout=10) as r:
            stores = json.load(r)
        if len(stores) < 2:
            failures.append(f"API: doar {len(stores)} magazine")
        return stores
    except Exception as e:
        failures.append(f"API nu raspunde: {e}")
        return {}


def check_goldens():
    from goodprice.textnorm import name_norm
    for term, frag, store in GOLDENS:
        try:
            with urllib.request.urlopen(f"{API}/api/configure?q={term}", timeout=30) as r:
                prods = json.load(r)["products"]
        except Exception as e:
            failures.append(f"configure({term}): {e}")
            continue
        hit = None
        for p in prods:
            if frag in name_norm(p["name"]) and (store is None or store in p["prices"]):
                hit = p
                break
        if not hit:
            failures.append(f"GOLDEN LIPSA: '{term}' nu contine '{frag}'"
                            + (f" ({store})" if store else "") + f" [{len(prods)} rezultate]")


def check_staples():
    terms = list(STAPLE_RANGES)
    req = urllib.request.Request(f"{API}/api/resolve", json.dumps({"terms": terms}).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            items = json.load(r)["items"]
    except Exception as e:
        failures.append(f"resolve({terms}): {e}")
        return
    for it in items:
        lo, hi = STAPLE_RANGES[it["term"]]
        chosen = it.get("chosen") or {}
        if chosen.get("type") != "staple":
            failures.append(f"STAPLE LIPSA: '{it['term']}' nu mai e rezolvat ca staple")
            continue
        for store, price in chosen["prices"].items():
            if not lo <= price <= hi:
                failures.append(f"STAPLE PRET ABSURD: '{it['term']}' {store} = {price} lei / "
                                f"{chosen['basis']} (asteptat {lo}-{hi}): {chosen['picked'].get(store)}")


def check_unit_prices():
    """GOLDEN 2026-11-27: Mega Image "Oua cu Omega 3 cod 2, 8 bucati" 17.39 lei aparea 17.39 lei/buc
    (price_per_unit = pretul pachetului). La pachete de N buc: unit_price * N ~= pret."""
    try:
        with urllib.request.urlopen(f"{API}/api/configure?q=oua", timeout=30) as r:
            prods = json.load(r)["products"]
    except Exception as e:
        failures.append(f"configure(oua): {e}")
        return
    for p in prods:
        amount = (p.get("attrs") or {}).get("amount") or ""
        parts = amount.split()
        if len(parts) != 2 or parts[1] != "buc" or not parts[0].isdigit() or int(parts[0]) < 2:
            continue
        n = int(parts[0])
        for store, price in p["prices"].items():
            up = p["unit_prices"].get(store)
            if up is not None and abs(up * n - price) > max(0.05 * price, 0.1):
                failures.append(f"PRET UNITAR GRESIT: {store} '{p['name']}' {price} lei / {n} buc "
                                f"dar unit_price {up} lei/buc")


def check_matching_attrs():
    """GOLDEN 2026-11-27: un grup (canonical_product) nu poate amesteca produse cu procente,
    coduri de oua sau marimi diferite ("Oua cu Omega 3 cod 2" + "Oua ochioase cod 1",
    "Lapte 1.5%" + "Lapte 3.5%"). Aceeasi regula ca in matching.attrs_conflict."""
    from goodprice.matching import attrs_conflict, name_attrs
    con = db.connect()
    try:
        rows = con.execute(
            """SELECT sp.canonical_product_id, s.slug, sp.name FROM store_product sp
               JOIN store s ON s.id = sp.store_id WHERE sp.canonical_product_id IS NOT NULL"""
        ).fetchall()
    finally:
        con.close()
    groups: dict = {}
    for cid, slug, name in rows:
        groups.setdefault(cid, []).append((_s(slug), _s(name), name_attrs(_s(name))))
    bad = 0
    for cid, members in groups.items():
        for i, (sa, na, aa) in enumerate(members):
            for sb, nb, ab in members[i + 1:]:
                if attrs_conflict(aa, ab):
                    bad += 1
                    if bad <= 5:
                        failures.append(f"MATCHING GRESIT #{cid}: {sa} '{na}' vs {sb} '{nb}'")
    if bad > 5:
        failures.append(f"MATCHING GRESIT: inca {bad - 5} perechi")


def check_freshness():
    con = db.connect()
    try:
        rows = con.execute(
            """SELECT s.slug, max(cp.updated_at), count(*)
               FROM store s JOIN store_product sp ON sp.store_id=s.id
               JOIN current_price cp ON cp.store_product_id=sp.id
               GROUP BY s.slug"""
        ).fetchall()
    finally:
        con.close()
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    for slug, last, cnt in rows:
        age = (now - last).days if last else 999
        if age > 3:
            failures.append(f"STALE: {_s(slug)} — ultima actualizare acum {age} zile ({cnt} produse)")


if __name__ == "__main__":
    check_api()
    check_goldens()
    check_staples()
    check_unit_prices()
    check_matching_attrs()
    check_freshness()
    if failures:
        print("HEALTHCHECK ESUAT:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("healthcheck OK — goldens + staple-uri + pret unitar + matching + prospetime + API")
