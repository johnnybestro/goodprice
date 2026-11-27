"""
Matching produse intre magazine (fara EAN) — prima varianta.

Strategie:
  1. Blocking pe brand normalizat (comparam doar produse cu acelasi brand).
  2. In fiecare bloc: similaritate Jaccard pe tokenii numelui (fara brand/gramaj)
     + potrivire de gramaj (cantitate convertita in g/ml).
  3. Perechile peste prag -> canonical_product + legatura pe store_product.

Rularea:  PYTHONPATH=. python3 -m goodprice.matching [--dry]
"""
from __future__ import annotations

import re
import sys
import unicodedata

from . import db

NAME_SIM_THRESHOLD = 0.45
STOPWORDS = {"cu", "si", "de", "la", "in", "din", "fara", "pentru", "un", "o", "gr", "grasime"}
_UNITS = (r"(kg|g|l|ml|buc|bucati|bucata|capsule|capsula|plicuri|plic|"
          r"role|rola|tablete|tableta|portii|portie|doze|doza|spalari)")
_AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*" + _UNITS + r"\b", re.I)
_MULTI_RE = re.compile(r"(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*" + _UNITS + r"\b", re.I)


def _canon(val: float, unit: str):
    """Aduce (valoare, unitate) la o baza comparabila: g, ml sau buc (bucata)."""
    unit = unit.lower()
    if unit == "kg":
        return round(val * 1000), "g"
    if unit == "l":
        return round(val * 1000), "ml"
    if unit in ("g", "ml"):
        return round(val), unit
    return round(val), "buc"  # toate unitatile de numarare -> buc


def _s(x) -> str:
    if isinstance(x, (bytes, bytearray, memoryview)):
        return bytes(x).decode("utf-8", "replace")
    return x if isinstance(x, str) else str(x)


def strip_diacritics(s: str) -> str:
    s = _s(s)
    s = s.replace("ș", "s").replace("ş", "s").replace("ț", "t").replace("ţ", "t")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def norm_brand(brand: str | None) -> str | None:
    if not brand:
        return None
    b = strip_diacritics(brand).lower()
    b = re.sub(r"[^a-z0-9]", "", b)
    return b or None


def amount_in_base(text: str | None):
    """Extrage cantitatea totala din text (nume sau raw_amount).
    Returneaza (valoare, 'g'|'ml'|'buc') sau None. Ex: 'cca 470 g' -> (470,'g'),
    '6x1.5L' -> (9000,'ml'), '10 capsule' -> (10,'buc')."""
    if not text:
        return None
    m = _MULTI_RE.search(text)
    if m:
        n, val, unit = int(m.group(1)), float(m.group(2).replace(",", ".")), m.group(3)
        base_val, base_unit = _canon(val, unit)
        return base_val * n, base_unit
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    return _canon(float(m.group(1).replace(",", ".")), m.group(2))


def name_tokens(name: str, brand: str | None) -> set[str]:
    s = strip_diacritics(name).lower()
    s = _MULTI_RE.sub(" ", s)
    s = _AMOUNT_RE.sub(" ", s)
    if brand:
        s = s.replace(strip_diacritics(brand).lower(), " ")
    toks = re.findall(r"[a-z0-9]+", s)
    return {t for t in toks if len(t) > 1 and t not in STOPWORDS}


# Atribute care deosebesc produse altfel identice (acelasi brand, aceeasi cantitate, nume aproape
# la fel). name_tokens le pierde: tokenii de un caracter sunt aruncati ("cod 1", "marime M") iar
# "3.5%" devine "3", "5". Daca AMBELE produse au atributul si valorile difera, nu e acelasi
# produs. GOLDEN 2026-11-27: Mega Image "Oua cu Omega 3 cod 2, 8 bucati" era grupat cu Sezamo
# "Agricola Oua ochioase cod 1" (8 buc).
_PCT_RANGE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%?\s*-\s*(\d+(?:[.,]\d+)?)\s*%")   # 33-36%, 3.5%-4.1%
_ATTR_RES = {
    "pct": re.compile(r"(\d+(?:[.,]\d+)?)\s*%"),
    "cod": re.compile(r"\bcod\.?\s*([0-4])\b"),
    "marime": re.compile(r"\bmarimea?\s+(xxl|xl|m-l|s|m|l)\b"),
}


def name_attrs(name: str) -> dict[str, frozenset]:
    s = strip_diacritics(name).lower()
    s = re.sub(r"(\d)\.\s+(\d+\s*%)", r"\1.\2", s)     # "Chefir 3. 3%" (Sezamo) -> 3.3%; nu "de 10, 10%"
    out = {}
    for kind, rx in _ATTR_RES.items():
        vals = set(rx.findall(s))
        if kind == "pct":
            for lo, hi in _PCT_RANGE_RE.findall(s):         # un interval = ambele capete
                vals.update((lo, hi))
            vals = {f"{float(v.replace(',', '.')):g}" for v in vals}
        if kind == "marime":
            vals = {x for v in vals for x in v.split("-")}  # "m-l" = m sau l
        if vals:
            out[kind] = frozenset(vals)
    return out


def attrs_conflict(a: dict, b: dict) -> bool:
    """True daca un atribut apare la ambele produse fara nicio valoare comuna
    ("1.5%" vs "3.5%", "cod 1" vs "cod 2"). "min. 33%" vs "33-36%" nu e conflict."""
    return any(kind in b and not (a[kind] & b[kind]) for kind in a)


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def amount_match(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    (va, ua), (vb, ub) = a, b
    if ua != ub:
        return False
    if ua == "buc":
        return va == vb                       # bucatile trebuie sa fie exacte
    return abs(va - vb) <= max(1, 0.02 * max(va, vb))  # g/ml: toleranta 2%


# cuvinte de umplutura la produse proaspete (fructe/legume vandute la kg)
PRODUCE_STOP = {"minim", "maxim", "aprox", "cca", "circa", "eco", "bio", "punga",
                "plasa", "calibru", "categoria", "clasa", "ambalat", "ambalate",
                "vrac", "romania", "import", "proaspat", "proaspata", "proaspete",
                "proaspeti", "feliat", "feliata", "curatat", "curatata", "spalat",
                "spalate", "categorie", "soi", "buc", "bucata", "bucati", "gr",
                # texte de flyer Kaufland ("a nu se consuma coaja", "Nerec.consumul cojii")
                "consuma", "consumul", "coaja", "cojii", "nerec"}


def produce_core(name: str) -> list[str]:
    """Tokenii 'de baza' ai unui produs proaspat, fara numere/umplutura.
    'Banane minim 1kg maxim 1.3kg' -> ['banane']."""
    s = strip_diacritics(name).lower()
    s = re.sub(r"\d+[.,]?\d*", " ", s)   # scoate numerele
    toks = re.findall(r"[a-z]+", s)
    return [t for t in toks if len(t) > 2 and t not in PRODUCE_STOP and t not in STOPWORDS]


def _load(con, store_slug, require_brand=True):
    brand_cond = "sp.brand IS NOT NULL" if require_brand else "sp.brand IS NULL"
    rows = con.execute(
        f"""SELECT sp.id, sp.name, sp.brand, cp.price, cp.price_per_unit, sp.unit, sp.raw_amount
            FROM store_product sp JOIN current_price cp ON cp.store_product_id = sp.id
            WHERE sp.store_id = (SELECT id FROM store WHERE slug=%s) AND {brand_cond}""",
        (store_slug,),
    ).fetchall()
    out = []
    for r in rows:
        name, brand, raw = _s(r[1]), _s(r[2]) if r[2] else None, _s(r[6]) if r[6] else None
        # gramajul e in `raw_amount` (Sezamo) sau in nume (Freshful)
        amt = amount_in_base(raw) or amount_in_base(name)
        out.append({"id": r[0], "name": name, "brand": brand, "price": r[3],
                    "ppu": r[4], "unit": _s(r[5]) if r[5] else None, "nb": norm_brand(brand),
                    "toks": name_tokens(name, brand), "amt": amt, "core": produce_core(name),
                    "attrs": name_attrs(name)})
    return out


def find_matches(a_list, b_list):
    """Perechi (a, b, scor) intre doua magazine, blocking pe brand."""
    from collections import defaultdict
    b_by_brand = defaultdict(list)
    for b in b_list:
        b_by_brand[b["nb"]].append(b)

    matches = []
    for a in a_list:
        best, best_score = None, 0.0
        for b in b_by_brand.get(a["nb"], ()):
            if not amount_match(a["amt"], b["amt"]):
                continue
            if attrs_conflict(a["attrs"], b["attrs"]):
                continue
            score = jaccard(a["toks"], b["toks"])
            if score > best_score:
                best, best_score = b, score
        if best and best_score >= NAME_SIM_THRESHOLD:
            matches.append((a, best, best_score))
    return matches


def match_unbranded(a_list, b_list):
    """Produse proaspete fara brand: blocking pe cuvantul-cheie, potrivire prin
    containment de tokeni. Cere aceeasi unitate + pret/unitate in ambele (compara lei/kg)."""
    from collections import defaultdict
    b_by_head = defaultdict(list)
    for b in b_list:
        if b["core"]:
            b_by_head[b["core"][0]].append(b)

    matches = []
    for a in a_list:
        if not a["core"] or a["ppu"] is None:
            continue
        A = set(a["core"])
        for b in b_by_head.get(a["core"][0], ()):
            if b["ppu"] is None or a["unit"] != b["unit"]:
                continue
            # set de tokeni IDENTIC: 'banane'='banane', dar 'banane rosii' != 'banane'.
            # (containment potrivea gresit soiuri diferite cu preturi/kg total diferite)
            if A == set(b["core"]):
                matches.append((a, b, 1.0))
                break
    return matches


def assign_ids(groups: list[dict], old_cid: dict[int, int]) -> list[int | None]:
    """id-uri stabile: fiecare grup nou primeste id-ul canonical vechi cu care are cele mai multe
    produse in comun; un id vechi merge la un singur grup (cel cu suprapunerea cea mai mare).
    None = grup nou. old_cid: store_product.id -> canonical_product_id de la rularea trecuta."""
    from collections import Counter
    cands = []                                   # (suprapunere, -id, index grup, id)
    for gi, g in enumerate(groups):
        counts = Counter(old_cid[p["id"]] for p in g["members"].values() if p["id"] in old_cid)
        for cid, k in counts.items():
            cands.append((k, -cid, gi, cid))
    cands.sort(reverse=True)                     # suprapunere mare intai; la egalitate id-ul mai mic
    out: list[int | None] = [None] * len(groups)
    used: set[int] = set()
    for _k, _neg, gi, cid in cands:
        if out[gi] is None and cid not in used:
            out[gi] = cid
            used.add(cid)
    return out


def persist_groups(con, groups: list[dict]) -> tuple[int, int, int]:
    """Scrie grupurile pastrand canonical_product.id intre rulari (aplicatia poate tine id-uri,
    ex. favorite). Inainte (2026-11) fiecare rulare stergea tot si dadea id-uri noi."""
    old_cid = dict(con.execute(
        "SELECT id, canonical_product_id FROM store_product WHERE canonical_product_id IS NOT NULL"
    ).fetchall())
    ids = assign_ids(groups, old_cid)
    con.rollback()
    with con.transaction():
        con.execute("UPDATE store_product SET canonical_product_id = NULL "
                    "WHERE canonical_product_id IS NOT NULL")
        kept = new = 0
        for g, cid in zip(groups, ids):
            # reprezentativ: produsul din magazinul cu prioritatea cea mai mare
            rep = next(g["members"][s] for s in STORES if s in g["members"])
            vals = (rep["name"], rep["brand"], rep["unit"], g["weighted"])
            if cid is None:
                cid = con.execute(
                    """INSERT INTO canonical_product (name, brand, base_unit, weighted)
                       VALUES (%s,%s,%s,%s) RETURNING id""", vals).fetchone()[0]
                new += 1
            else:
                # embedding-ul se reface doar daca s-a schimbat numele
                con.execute(
                    """UPDATE canonical_product SET name=%s, brand=%s, base_unit=%s, weighted=%s,
                              embedding = CASE WHEN name = %s THEN embedding END
                       WHERE id=%s""", (*vals, rep["name"], cid))
                kept += 1
            con.execute("UPDATE store_product SET canonical_product_id=%s WHERE id = ANY(%s)",
                        (cid, [p["id"] for p in g["members"].values()]))
        gone = con.execute(
            """DELETE FROM canonical_product c WHERE NOT EXISTS
               (SELECT 1 FROM store_product sp WHERE sp.canonical_product_id = c.id)""").rowcount
    return kept, new, gone


STORES = ["sezamo", "freshful", "mega-image", "carrefour", "lidl", "kaufland"]   # ordinea = prioritatea la grupare


def run(persist=True):
    con = db.connect()
    branded = {s: _load(con, s) for s in STORES}
    produce = {s: _load(con, s, require_brand=False) for s in STORES}
    print(" | ".join(f"{s}: {len(branded[s])}+{len(produce[s])}nb" for s in STORES))

    # Grupare incrementala pe perechi de magazine: un grup = acelasi produs in 2+
    # magazine, cel mult un produs per magazin per grup.
    groups: list[dict] = []            # {"members": {store: prod}, "weighted": bool}
    where: dict[tuple, dict] = {}      # (store, id) -> grup

    def fits(g, p):
        # un grup se formeaza prin perechi: B~A si C~A nu garanteaza C~B, deci noul membru nu
        # trebuie sa contrazica pe niciunul dintre cei existenti
        return not any(attrs_conflict(p["attrs"], m["attrs"]) for m in g["members"].values())

    def try_join(a_store, b_store, pairs, weighted):
        added = 0
        for a, b, score in sorted(pairs, key=lambda x: -x[2]):
            ka, kb = (a_store, a["id"]), (b_store, b["id"])
            ga, gb = where.get(ka), where.get(kb)
            if ga is not None and gb is not None:
                continue
            if ga is not None:
                if b_store not in ga["members"] and fits(ga, b):
                    ga["members"][b_store] = b; where[kb] = ga; added += 1
            elif gb is not None:
                if a_store not in gb["members"] and fits(gb, a):
                    gb["members"][a_store] = a; where[ka] = gb; added += 1
            else:
                g = {"members": {a_store: a, b_store: b}, "weighted": weighted}
                groups.append(g); where[ka] = g; where[kb] = g; added += 1
        return added

    for i, sb in enumerate(STORES):
        for sa in STORES[i + 1:]:
            n = try_join(sa, sb, find_matches(branded[sa], branded[sb]), False)
            n += try_join(sa, sb, match_unbranded(produce[sa], produce[sb]), True)
            if n:
                print(f"  {sa} <-> {sb}: +{n} legaturi")

    multi = sum(1 for g in groups if len(g["members"]) >= 3)
    n_produce = sum(1 for g in groups if g["weighted"])
    print(f"\nGrupuri: {len(groups)} (in 3+ magazine: {multi}; la cantar: {n_produce})")

    print("Exemple (grupuri in 3+ magazine):")
    shown = 0
    for g in groups:
        if len(g["members"]) >= 3 and shown < 8:
            m = g["members"]
            any_p = next(iter(m.values()))
            prices = ", ".join(f"{s}={p['price']:.2f}" for s, p in sorted(m.items()) if p["price"])
            print(f"  {(any_p['brand'] or '?'):12.12} | {any_p['name'][:34]:34} {prices}")
            shown += 1

    if persist:
        con.rollback()  # inchide tranzactia implicita deschisa de SELECT-uri
        n_kept, n_new, n_gone = persist_groups(con, groups)
        n = n_kept + n_new
        print(f"\nid-uri: {n_kept} pastrate, {n_new} noi, {n_gone} grupuri disparute (sterse)")
        print(f"\nPersistat: {n} canonical_product + legaturi.")
    con.close()
    return groups


if __name__ == "__main__":
    run(persist="--dry" not in sys.argv)
