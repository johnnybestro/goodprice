"""
API GoodPrice (FastAPI): cauta produse si optimizeaza cosul pe date reale.

Ruleaza:  PYTHONPATH=. .venv/bin/uvicorn goodprice.api:app --host 0.0.0.0 --port 8000
Frontend servit la  /  ; API sub  /api/*.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import re
import time

from . import db, optimize, staples
from .matching import amount_in_base, strip_diacritics

app = FastAPI(title="GoodPrice")
_FE = Path(__file__).resolve().parent.parent / "frontend"


def _s(x):
    if isinstance(x, (bytes, bytearray, memoryview)):
        return bytes(x).decode("utf-8", "replace")
    return x


def store_names():
    con = db.connect()
    try:
        rows = con.execute("SELECT slug, name FROM store ORDER BY name").fetchall()
        return {_s(r[0]): _s(r[1]) for r in rows}
    finally:
        con.close()


@app.get("/api/stores")
def stores():
    return store_names()


def _unit_price(price, ppu, unit, raw_amount, name):
    """Pretul pe unitate afisat (lei/kg, lei/l, lei/buc).

    La unit='buc' magazinele nu trimit acelasi lucru in price_per_unit: Freshful si Sezamo dau
    pretul pe bucata, Mega Image pretul PE PACHET ("Oua cu Omega 3 cod 2, 8 bucati" 17.39 ->
    17.39 lei/buc in loc de 2.17), Kaufland uneori 1.00. Cand numele/cantitatea spune cate
    bucati are pachetul, calculam din pret; altfel ramane valoarea magazinului."""
    if unit and _s(unit).lower() == "buc" and price is not None:
        amt = amount_in_base(_s(raw_amount) if raw_amount else None) or amount_in_base(_s(name))
        if amt and amt[1] == "buc" and amt[0] > 0:
            return round(float(price) / amt[0], 2)
    return float(ppu) if ppu is not None else None


def _search_products(con, q: str, limit: int) -> list[dict]:
    """Produse canonice (in ambele magazine) pt. un termen, ordonate pe relevanta."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    rows = con.execute(
        """SELECT c.id, c.name, c.brand, s.slug, cp.price, cp.price_per_unit, sp.unit,
                  c.weighted, sp.name AS sp_name, sp.raw_amount,
                  CASE
                    WHEN c.name ILIKE %(w)s OR c.name ILIKE %(p)s THEN 0
                    WHEN c.name ILIKE %(mid)s OR c.name ILIKE %(end)s THEN 1
                    ELSE 2
                  END AS rank,
                  position(lower(%(q)s) in lower(c.name)) AS pos,
                  length(c.name) AS len
           FROM canonical_product c
           JOIN store_product sp ON sp.canonical_product_id = c.id
           JOIN store s ON s.id = sp.store_id
           JOIN current_price cp ON cp.store_product_id = sp.id
           WHERE c.name ILIKE %(sub)s AND cp.price IS NOT NULL
           ORDER BY rank, pos, len
           LIMIT 1000""",
        {"q": q, "w": q, "p": f"{q} %", "mid": f"% {q} %",
         "end": f"% {q}", "sub": f"%{q}%"},
    ).fetchall()

    by_id: dict = {}
    for cid, name, brand, slug, price, ppu, unit, weighted, sp_name, raw, *_ in rows:
        it = by_id.setdefault(cid, {"canonical_id": cid, "name": _s(name),
                                    "brand": _s(brand), "unit": _s(unit),
                                    "weighted": bool(weighted),
                                    "prices": {}, "unit_prices": {}})
        it["prices"][_s(slug)] = float(price)
        up = _unit_price(price, ppu, unit, raw, sp_name)
        if up is not None:
            it["unit_prices"][_s(slug)] = up
    results = []
    for it in by_id.values():
        key = "unit_prices" if it["weighted"] else "prices"
        if len(it[key]) >= 2:                 # comparabil doar daca e in ambele magazine
            results.append(it)
    return results[:limit]


# Limite de intrare (API public, apelat din aplicatia mobila): o cerere nu poate cere tot
# catalogul. Peste limite FastAPI raspunde 422 cu detaliul campului.
MAX_Q = 100          # caractere intr-un termen
MAX_SEARCH = 50      # rezultate /api/search
MAX_TERMS = 50       # termeni /api/resolve
MAX_ITEMS = 100      # articole /api/optimize


@app.get("/api/search")
def search(q: str = Query(..., max_length=MAX_Q), limit: int = Query(20, ge=1, le=MAX_SEARCH)):
    con = db.connect()
    try:
        return {"results": _search_products(con, q, limit)}
    finally:
        con.close()


# ---------- Configurator: termen generic -> fatete (cantitate, %, tip, ambalaj) ----------
# Valori normalizate din nume; seturile definesc fatetele mutual-exclusive.
VARIETY = {"necarbogazoasa": "plată", "plata": "plată", "carbogazificata": "carbogazoasă",
           "carbogazoasa": "carbogazoasă", "boabe": "boabe", "macinata": "măcinată",
           "instant": "instant", "integrala": "integrală", "alba": "albă", "neagra": "neagră",
           "secara": "secară", "toast": "toast", "blonda": "blondă",
           "semidegresat": "semidegresat", "degresat": "degresat", "integral": "integral",
           "fara lactoza": "fără lactoză", "afumat": "afumat", "dulce": "dulce", "sarat": "sărat"}
PACKAGING = {"sticla": "sticlă", "doza": "doză", "pet": "PET", "cutie": "cutie"}
_PC_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")

_CFG_CACHE: dict = {"rows": None, "t": 0.0}


def _cfg_rows(con):
    """Tot catalogul cu pret curent (cache 120s). Filtrarea/rankarea se face in Python
    — baza e SQL_ASCII si nu duce diacriticele in ILIKE/regex."""
    if _CFG_CACHE["rows"] is None or time.time() - _CFG_CACHE["t"] > 120:
        _CFG_CACHE["rows"] = con.execute(
            """SELECT sp.id, sp.name, sp.brand, sp.raw_amount, sp.canonical_product_id,
                      s.slug, cp.price, cp.price_per_unit, COALESCE(c.weighted, false)
               FROM store_product sp
               JOIN store s ON s.id = sp.store_id
               JOIN current_price cp ON cp.store_product_id = sp.id
               LEFT JOIN canonical_product c ON c.id = sp.canonical_product_id
               WHERE cp.price IS NOT NULL AND cp.in_stock IS NOT false"""
        ).fetchall()
        _CFG_CACHE["t"] = time.time()
    return _CFG_CACHE["rows"]


def _amount_label(amt) -> str | None:
    if not amt:
        return None
    v, u = amt
    if u == "ml" and v >= 1000 and v % 50 == 0:
        return f"{v / 1000:g} l"
    if u == "g" and v >= 1000 and v % 50 == 0:
        return f"{v / 1000:g} kg"
    return f"{v:g} {u}"


def _prod_attrs(name: str, raw: str | None) -> dict:
    norm = strip_diacritics(name).lower()
    fel = next((v for k, v in VARIETY.items() if re.search(rf"\b{k}", norm)), None)
    amb = next((v for k, v in PACKAGING.items() if re.search(rf"\b{k}", norm)), None)
    pc = _PC_RE.search(norm)
    return {"amount": _amount_label(amount_in_base(raw) or amount_in_base(name)),
            "percent": pc.group(1).replace(",", ".") + "%" if pc else None,
            "fel": fel, "ambalaj": amb}


_CFG_SQL = """
    SELECT sp.id, sp.name, sp.brand, sp.raw_amount, sp.canonical_product_id,
           s.slug, cp.price, cp.price_per_unit, COALESCE(c.weighted, false), sp.unit,
           position(%(qn)s in sp.name_norm) AS pos, length(sp.name_norm) AS len
    FROM store_product sp
    JOIN store s ON s.id = sp.store_id
    JOIN current_price cp ON cp.store_product_id = sp.id
    LEFT JOIN canonical_product c ON c.id = sp.canonical_product_id
    WHERE {cond} AND cp.price IS NOT NULL AND cp.in_stock IS NOT false
    ORDER BY pos, len
    LIMIT 8000
"""


@app.get("/api/configure")
def configure(q: str = Query(..., max_length=MAX_Q)):
    """Toate variantele unui termen, cautate DIRECT in Postgres pe name_norm
    (lowercase/fara diacritice, intretinuta de scrapere) cu word-boundary \\y.
    Fara fetch-all + filtrare in Python => fara limita arbitrara care trunchia
    silentios rezultatele cand catalogul crestea (bugul "Azuga 8l")."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"term": q, "products": []}
    qn = strip_diacritics(q).lower()

    con = db.connect()
    try:
        # 1) potriviri pe cuvant intreg (\y — word boundary in regexul Postgres)
        rows = con.execute(
            _CFG_SQL.format(cond="sp.name_norm ~ %(word)s"),
            {"qn": qn, "word": rf"\y{re.escape(qn)}\y"},
        ).fetchall()
        # 2) fallback substring doar cand cuvantul intreg da putine (termen partial)
        if len(rows) < 50:
            rows += con.execute(
                _CFG_SQL.format(cond="sp.name_norm LIKE %(sub)s AND NOT sp.name_norm ~ %(word)s"),
                {"qn": qn, "sub": f"%{qn}%", "word": rf"\y{re.escape(qn)}\y"},
            ).fetchall()
    finally:
        con.close()

    ents: dict = {}
    order: list = []
    for sid, name, brand, raw, cid, slug, price, ppu, weighted, unit, _pos, _len in rows:
        key = ("c", cid) if cid else ("s", sid)
        e = ents.get(key)
        if not e:
            name_s = _s(name)
            e = {"kind": "canonical" if cid else "sp",
                 "canonical_id": cid, "sp_id": None if cid else sid,
                 "name": name_s, "brand": _s(brand) if brand else None,
                 "weighted": bool(weighted), "prices": {}, "unit_prices": {},
                 "attrs": _prod_attrs(name_s, _s(raw) if raw else None)}
            ents[key] = e
            order.append(key)
        e["prices"][_s(slug)] = float(price)
        up = _unit_price(price, ppu, unit, raw, name)
        if up is not None:
            e["unit_prices"][_s(slug)] = up
    return {"term": q, "products": [ents[k] for k in order]}


class ResolveRequest(BaseModel):
    terms: list[str] = Field(..., max_length=MAX_TERMS)


@app.post("/api/resolve")
def resolve(req: ResolveRequest):
    """Lista generica -> pentru fiecare termen, produsul reprezentativ + alternative.
    Alege cel mai ieftin produs relevant (mediana ca sa evite outlieri ciudati)."""
    con = db.connect()
    try:
        # CORECTITUDINE: comparam mereu ACELASI produs (potrivit intre magazine),
        # aceeasi cantitate. Alegem cel mai relevant (nu cel mai ieftin — altfel prinde
        # decoy-uri: banane->mousse). Termenii fara produs identic apar onest ca negasiti.
        out = []
        all_rows = None  # incarcat lazy, refolosit intre termeni
        for term in req.terms:
            term = term.strip()[:MAX_Q]
            if not term:
                continue
            # 1) staple ("oua", "lapte"...): cel mai ieftin echivalent per magazin,
            #    normalizat la cantitate standard — nu cere acelasi brand peste tot.
            hit = staples.lookup(term)
            if hit:
                key, st = hit
                if all_rows is None:
                    all_rows = staples._fetch_all(con)
                generic = staples.resolve_staple(con, st, all_rows)
                if generic:
                    generic["staple"] = key
                    out.append({"term": term, "chosen": generic,
                                "alternatives": _search_products(con, term, 7)[:6]})
                    continue
            # 2) fallback: produsul potrivit intre magazine, cel mai relevant
            res = _search_products(con, term, 16)
            chosen = res[0] if res else None
            out.append({"term": term, "chosen": chosen, "alternatives": res[1:16]})
        return {"items": out}
    finally:
        con.close()


class OptItem(BaseModel):
    canonical_id: int | None = None   # produs potrivit intre magazine (2 preturi)
    sp_id: int | None = None          # SAU SKU dintr-un singur magazin (1 pret)
    staple: str | None = None         # SAU echivalent generic ("oua", "lapte"...)
    qty: int = Field(1, ge=1, le=99)


class OptRequest(BaseModel):
    items: list[OptItem] = Field(..., max_length=MAX_ITEMS)
    max_stores: int = Field(2, ge=1, le=10)


@app.post("/api/optimize")
def optimize_basket(req: OptRequest):
    if not req.items:
        return {"error": "lista goala"}
    ids = [i.canonical_id for i in req.items if i.canonical_id is not None]
    qty = {i.canonical_id: i.qty for i in req.items if i.canonical_id is not None}
    con = db.connect()
    try:
        rows = con.execute(
            """SELECT c.id, c.name, c.weighted, s.slug, cp.price, cp.price_per_unit
               FROM canonical_product c
               JOIN store_product sp ON sp.canonical_product_id = c.id
               JOIN store s ON s.id = sp.store_id
               JOIN current_price cp ON cp.store_product_id = sp.id
               WHERE c.id = ANY(%s)""",
            (ids,),
        ).fetchall()
    finally:
        con.close()

    # produsele la cantar se compara pe lei/unitate (pret absolut = pt. produse la bucata)
    items: dict = {}
    for cid, name, weighted, slug, price, ppu in rows:
        val = ppu if weighted else price
        if val is None:
            continue
        it = items.setdefault(cid, {"name": _s(name), "qty": qty.get(cid, 1),
                                    "weighted": bool(weighted), "prices": {}})
        it["prices"][_s(slug)] = float(val)
    item_list = list(items.values())

    # SKU-uri dintr-un singur magazin (din configurator): 1 pret, constrang planul
    sp_ids = [i.sp_id for i in req.items if i.sp_id is not None]
    if sp_ids:
        sp_qty = {i.sp_id: i.qty for i in req.items if i.sp_id is not None}
        con3 = db.connect()
        try:
            rows3 = con3.execute(
                """SELECT sp.id, sp.name, s.slug, cp.price
                   FROM store_product sp
                   JOIN store s ON s.id = sp.store_id
                   JOIN current_price cp ON cp.store_product_id = sp.id
                   WHERE sp.id = ANY(%s) AND cp.price IS NOT NULL""",
                (sp_ids,),
            ).fetchall()
        finally:
            con3.close()
        for sid, name, slug, price in rows3:
            item_list.append({"name": _s(name), "qty": sp_qty.get(sid, 1),
                              "prices": {_s(slug): float(price)}})

    # echivalente generice (staple): pretul standard per magazin, calculat pe loc
    staple_items = [i for i in req.items if i.staple]
    if staple_items:
        con2 = db.connect()
        try:
            all_rows = staples._fetch_all(con2)
            for i in staple_items:
                st = staples.STAPLES.get(i.staple)
                generic = staples.resolve_staple(con2, st, all_rows) if st else None
                if generic:
                    item_list.append({"name": f"{generic['label']} ({generic['basis']})",
                                      "qty": i.qty, "prices": generic["prices"]})
        finally:
            con2.close()

    stores = sorted({s for it in item_list for s in it["prices"]})
    max_stores = max(1, min(req.max_stores, len(stores)))
    result = optimize.optimize(item_list, stores, max_stores)
    result["store_names"] = store_names()
    return result


@app.get("/")
def index():
    return FileResponse(_FE / "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(_FE / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(_FE / "sw.js", media_type="application/javascript")


@app.get("/icon.svg")
def icon():
    return FileResponse(_FE / "icon.svg", media_type="image/svg+xml")
