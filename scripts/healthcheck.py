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
    check_freshness()
    if failures:
        print("HEALTHCHECK ESUAT:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("healthcheck OK — goldens + prospetime + API")
