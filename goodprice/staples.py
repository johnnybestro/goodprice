"""
[EXPERIMENTAL — NEFOLOSIT] Nu e conectat in api.py.

Model "categorie-cea-mai-ieftina": pentru un termen generic, ia cel mai ieftin produs
CALIFICAT din fiecare magazin, normalizat la o cantitate standard. Abandonat deocamdata
fiindca (a) selectia minimului scoate erorile de date (pret/unitate mis-parsat -> 0.03),
si (b) filtrele include/exclude scapa produse gresite (biscuiti la "unt") -> compara
produse diferite = inselator. Corectitudinea cere ACELASI produs (vezi api.py /resolve,
care compara produse potrivite intre magazine). De reluat cand avem: taxonomie curata +
plaje de sanitate pe pret + validare per categorie.

Staple-uri: termeni generici de lista de cumparaturi ("oua", "lapte", "paine").

Pentru acesti termeni user-ul NU vrea un produs anume, ci "cel mai ieftin
echivalent din fiecare magazin". Magazinele vand branduri diferite (ex. ouale:
Sezamo=ALBERT/Regalina, Freshful=Agricola/Cortina), deci matching-ul pe produs
identic nu ajuta — comparam la nivel de CATEGORIE:

  per magazin: filtreaza produsele calificate (include/exclude regex pe nume)
               -> pretul pe unitatea de baza -> cel mai ieftin
  compara:     pret normalizat la o cantitate standard (10 oua, 1 l lapte...)

Nota de implementare: baza e SQL_ASCII/ctype C (mostenire de la provisionare), deci
regexul Postgres NU intelege diacriticele UTF-8 (\\y esueaza pe "Ouă"/"Pâine").
Filtram in Python: numele se normalizeaza cu strip_diacritics()+lower(), iar
pattern-urile de mai jos sunt scrise ASCII, fara diacritice.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .matching import _s, amount_in_base, strip_diacritics


@dataclass
class Staple:
    label: str                      # afisat in UI
    include: str                    # regex pe nume (case-insensitive)
    exclude: str = ""               # regex de respingere
    basis_qty: float = 1            # cantitatea standard de comparat...
    basis_unit: str = "buc"         # ...in aceasta unitate: buc | g | ml
    aliases: list[str] = field(default_factory=list)


# pattern-urile ruleaza pe numele normalizat: strip_diacritics().lower()
STAPLES: dict[str, Staple] = {
    "oua": Staple("Ouă", r"\boua\b", r"prepelit|ciocolat|paste|fidea|biscuit|maionez|lichid|vopsit",
                  10, "buc", ["ou", "oua de gaina"]),
    "lapte": Staple("Lapte", r"^lapte\b|\blapte (proaspat|de consum|integral|semidegresat|uht)\b",
                    r"praf|condensat|cocos|migdal|soia|ovaz|orez|batut|cafea|ciocolat|corp|demachiant|matern",
                    1000, "ml"),
    "paine": Staple("Pâine", r"\bpaine\b", r"crocant|uscata|pesmet|prajitor|inghetata",
                    500, "g", ["pine", "franzela"]),
    "banane": Staple("Banane", r"^banane?\b", r"ciocolat|uscat|chips|pudra|aroma", 1000, "g", ["banana"]),
    "portocale": Staple("Portocale", r"^portocale\b", r"suc|deshidrat|aroma", 1000, "g", ["portocala"]),
    "lamai": Staple("Lămâi", r"^lamai", r"suc|sirop|ice tea|aroma|acid|perje", 500, "g", ["lamaie", "lamai netratate"]),
    "mere": Staple("Mere", r"^mere\b", r"suc|uscat|otet|piure|chips", 1000, "g", ["mar"]),
    "rosii": Staple("Roșii", r"^rosii\b", r"conserv|decojite|pasta|suc|bulion|uscat", 1000, "g", ["rosie", "tomate"]),
    "castraveti": Staple("Castraveți", r"^castraveti", r"murat|otet|borcan", 500, "g", ["castravete"]),
    "cartofi": Staple("Cartofi", r"^cartofi\b", r"chips|prajit|pai|piure|dulci", 1000, "g", ["cartof"]),
    "ceapa": Staple("Ceapă", r"^ceapa\b", r"praf|prajit|deshidrat", 1000, "g"),
    "morcovi": Staple("Morcovi", r"^morcovi?\b", r"suc|baby food", 1000, "g", ["morcov"]),
    "usturoi": Staple("Usturoi", r"^usturoi\b", r"praf|granul|pasta", 100, "g"),
    "bere": Staple("Bere", r"\bbere\b", r"0[.,]0|fara alcool|ginger", 500, "ml"),
    "apa": Staple("Apă minerală", r"^apa (mineral|plata|carbogazoas)",
                  r"micelar|demachiant|gura|toalet|flori|baterie", 2000, "ml", ["apa minerala", "apa plata"]),
    "unt": Staple("Unt", r"^unt\b|\bunt (de masa|[0-9])", r"arahide|migdal|cocos|shea|corp|cacao", 200, "g"),
    "iaurt": Staple("Iaurt", r"^iaurt\b", r"baut|de baut", 400, "g"),
    "smantana": Staple("Smântână", r"^smantana\b", r"vegetal", 300, "g"),
    "telemea": Staple("Telemea", r"\btelemea\b", "", 300, "g", ["branza telemea"]),
    "cascaval": Staple("Cașcaval", r"^cascaval\b", r"afumat|pane", 300, "g"),
    "faina": Staple("Făină", r"^faina\b", r"porumb|migdal|cocos|orez|secara|integral", 1000, "g"),
    "zahar": Staple("Zahăr", r"^zahar\b", r"brun|pudra|vanilat|cocos", 1000, "g"),
    "sare": Staple("Sare", r"^sare\b", r"baie|masina|dedurizare|grunj", 1000, "g"),
    "ulei": Staple("Ulei de floarea-soarelui", r"^ulei\b.*floarea", "", 1000, "ml", ["ulei de floarea soarelui"]),
    "orez": Staple("Orez", r"^orez\b", r"lapte|expandat|prajit", 1000, "g"),
    "paste": Staple("Paste (spaghete)", r"^spaghet|paste fainoase|^penne|^fusilli", r"sos|condiment|dinti",
                    500, "g", ["spaghete", "macaroane"]),
    "piept de pui": Staple("Piept de pui", r"piept de pui", r"afumat|crenvur|mezel|snitel|marinat", 1000, "g", ["pui"]),
    "carne tocata": Staple("Carne tocată", r"carne tocata|tocata (de )?(vita|porc|amestec)", "",
                           500, "g", ["tocatura"]),
    "cafea": Staple("Cafea măcinată", r"cafea.*macinat|macinat.*cafea", r"decofeinizat|instant|capsule|lapte",
                    250, "g", ["cafea macinata"]),
    "hartie igienica": Staple("Hârtie igienică", r"hartie igienica", "", 8, "buc", ["hartie"]),
    "detergent": Staple("Detergent rufe (capsule)", r"detergent.*capsule|capsule.*detergent", "", 20, "buc"),
}

_ALIAS_INDEX: dict[str, str] = {}
for key, st in STAPLES.items():
    _ALIAS_INDEX[key] = key
    for a in st.aliases:
        _ALIAS_INDEX[a] = key


def normalize_term(term: str) -> str:
    t = strip_diacritics(term).lower().strip()
    return re.sub(r"\s+", " ", t)


def lookup(term: str) -> tuple[str, Staple] | None:
    key = _ALIAS_INDEX.get(normalize_term(term))
    return (key, STAPLES[key]) if key else None


def _unit_price(price, ppu, unit, raw_amount, name, basis_unit):
    """Pretul pe unitatea de baza (lei/buc, lei/g, lei/ml) sau None."""
    # 1) coloana price_per_unit (lei/kg, lei/l, lei/buc) cand unitatea se potriveste
    if ppu is not None and unit:
        u = _s(unit).lower()
        if basis_unit == "g" and u == "kg":
            return float(ppu) / 1000
        if basis_unit == "ml" and u == "l":
            return float(ppu) / 1000
    # 2) parseaza cantitatea din raw_amount/nume. La bucati are prioritate fata de price_per_unit:
    #    magazinele nu dau acelasi lucru pe unit='buc' — Freshful da pretul pe bucata, Mega Image
    #    pretul PE PACHET ("Oua ..., 6 bucati" 9.99 lei, price_per_unit 9.99), care inmultit cu 10
    #    dadea 99.90 lei / 10 oua.
    amt = (amount_in_base(raw_amount) or amount_in_base(name)) if price is not None else None
    if amt and amt[1] == basis_unit and amt[0] > 0:
        return float(price) / amt[0]
    # 3) bucati fara numar in nume: pretul magazinului pe bucata, doar cand e clar pe bucata
    #    (mai mic decat pretul pachetului); altfel nu stim cate bucati sunt => nu comparam.
    if basis_unit == "buc" and ppu is not None and unit and _s(unit).lower() == "buc":
        if price is None or float(ppu) < float(price):
            return float(ppu)
    return None


# Cache in proces: preturile se schimba o data pe zi (scrapere + matching dimineata), iar
# incarcarea catalogului (~40k randuri) + filtrarea dura ~1s la FIECARE /api/resolve si
# /api/optimize cu staple-uri. Dupa CACHE_TTL secunde datele se reincarca.
CACHE_TTL = 300
_ROWS_CACHE: dict = {"rows": None, "t": 0.0}
_RESOLVED_CACHE: dict = {}   # cheie staple -> (timp, rezultat)


def _fetch_all(con) -> list[tuple]:
    """Toate produsele cu pret curent (cache CACHE_TTL). Filtrarea se face in Python
    (Unicode corect), fiindca regexul Postgres nu duce diacriticele pe baza SQL_ASCII."""
    now = time.time()
    if _ROWS_CACHE["rows"] is None or now - _ROWS_CACHE["t"] > CACHE_TTL:
        _ROWS_CACHE["rows"] = _fetch_all_uncached(con)
        _ROWS_CACHE["t"] = now
    return _ROWS_CACHE["rows"]


def _fetch_all_uncached(con) -> list[tuple]:
    return con.execute(
        """SELECT s.slug, sp.name, sp.brand, sp.raw_amount, cp.price, cp.price_per_unit, sp.unit
           FROM store_product sp
           JOIN store s ON s.id = sp.store_id
           JOIN current_price cp ON cp.store_product_id = sp.id
           WHERE cp.in_stock IS NOT false"""
    ).fetchall()


def resolve_staple(con, staple: Staple, rows: list[tuple] | None = None) -> dict | None:
    """Cel mai ieftin produs calificat per magazin, normalizat la cantitatea standard.
    Returneaza None daca nu e disponibil in cel putin 2 magazine.
    `rows` poate fi refolosit intre apeluri (vezi _fetch_all). Rezultatul e tinut CACHE_TTL
    secunde; se intoarce o copie, pentru ca apelantii adauga chei (ex. "staple")."""
    key = (staple.label, staple.include, staple.exclude, staple.basis_qty, staple.basis_unit)
    hit = _RESOLVED_CACHE.get(key)
    if hit and time.time() - hit[0] <= CACHE_TTL:
        return dict(hit[1]) if hit[1] is not None else None
    res = _resolve_staple_uncached(con, staple, rows)
    _RESOLVED_CACHE[key] = (time.time(), res)
    return dict(res) if res is not None else None


def _resolve_staple_uncached(con, staple: Staple, rows: list[tuple] | None = None) -> dict | None:
    if rows is None:
        rows = _fetch_all(con)
    inc = re.compile(staple.include)
    exc = re.compile(staple.exclude) if staple.exclude else None

    best: dict[str, dict] = {}
    for slug, name, brand, raw, price, ppu, unit in rows:
        name = _s(name)
        norm = strip_diacritics(name).lower()
        if not inc.search(norm) or (exc and exc.search(norm)):
            continue
        per = _unit_price(price, ppu, unit, _s(raw) if raw else None, name, staple.basis_unit)
        if per is None or per <= 0:
            continue
        slug = _s(slug)
        std = per * staple.basis_qty
        if slug not in best or std < best[slug]["std_price"]:
            best[slug] = {"std_price": std, "product": name, "brand": _s(brand) if brand else None}
    if len(best) < 2:
        return None

    unit_lbl = {"buc": "buc", "g": "g", "ml": "ml"}[staple.basis_unit]
    qty = int(staple.basis_qty) if staple.basis_qty == int(staple.basis_qty) else staple.basis_qty
    return {
        "type": "staple",
        "label": staple.label,
        "basis": f"{qty} {unit_lbl}",
        "prices": {s: round(v["std_price"], 2) for s, v in best.items()},
        "picked": {s: (v["brand"] + " · " if v["brand"] else "") + v["product"] for s, v in best.items()},
    }
