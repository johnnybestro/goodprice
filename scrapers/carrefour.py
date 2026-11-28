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

Volum (masurat 2026-11-27/28): un PDP are ~330 KB si TTFB 1.2-1.9 s secvential; cu 3 cereri
in paralel Carrefour raspunde in 3-5 s, deci ~0.75 pagini/s. Tot sitemap-ul ar dura ~33h si o
parte din URL-uri sunt produse moarte (200 fara JSON-LD Product). De aceea fiecare rulare e o
COADA PE PRIORITATI cu BUGET DE TIMP (se opreste curat cand bugetul expira):
  1. produsele Carrefour care au pereche in alt magazin (canonical_product cu >= 2 magazine)
     — singurele care conteaza pentru comparatie; mereu primele;
  2. daily: restul produselor cunoscute din DAILY_CATEGORIES, cele mai vechi (last_seen) primele;
     full: toate produsele cunoscute, cele mai vechi primele;
  3. URL-uri din sitemap nevazute niciodata (daily: max NEW_PER_DAY; full: toate).
Paginile moarte se tin minte DEAD_DAYS zile in $STATE_DIRECTORY/carrefour-dead.json si nu se mai
cer (full le reverifica dupa expirare). Moduri:
  daily — buget 7h (21:30 -> ~04:30 UTC, inainte de matching 06:00)
  full  — buget 14h, duminica (sau cand avem prea putine produse cunoscute)
  new   — doar URL-uri nevazute (completare), buget CARREFOUR_MAX_HOURS
  auto  — (timerul) full duminica, altfel daily

Ruleaza:  PYTHONPATH=. python3 scrapers/carrefour.py [auto|daily|full|new] [max_produse]
Env:      CARREFOUR_WORKERS (implicit 3), CARREFOUR_MODE (implicit auto),
          CARREFOUR_MAX_HOURS (suprascrie bugetul), STATE_DIRECTORY (systemd StateDirectory)
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
BUDGET_H = {"daily": 7.0, "full": 14.0, "new": 7.0}
DEAD_DAYS = 30
STATE_DIR = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/goodprice"))

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


def _local_weekday() -> int:
    # fara tzdata: Romania e UTC+2 iarna / UTC+3 vara; la 21:30 UTC ziua e aceeasi oricum
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)).weekday()


def load_dead() -> dict[str, str]:
    try:
        d = json.loads((STATE_DIR / "carrefour-dead.json").read_text())
    except (OSError, ValueError):
        return {}
    cutoff = (datetime.date.today() - datetime.timedelta(days=DEAD_DAYS)).isoformat()
    return {u: day for u, day in d.items() if day >= cutoff}


def save_dead(dead: dict[str, str]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_DIR / "carrefour-dead.json.tmp"
        tmp.write_text(json.dumps(dead))
        tmp.replace(STATE_DIR / "carrefour-dead.json")
    except OSError as e:
        print(f"  ! nu pot salva lista de pagini moarte: {e}", flush=True)


def pick_urls(mode: str, client: httpx.Client, con, store_id: int,
              dead: dict[str, str]) -> tuple[str, list[str]]:
    rows = con.execute(
        """SELECT sp.url, sp.category_path,
                  EXISTS (SELECT 1 FROM store_product o
                          WHERE o.canonical_product_id = sp.canonical_product_id
                            AND o.store_id <> sp.store_id) AS matched
           FROM store_product sp
           WHERE sp.store_id = %s AND sp.url IS NOT NULL
           ORDER BY sp.last_seen ASC""", (store_id,)).fetchall()
    con.rollback()
    known = {u for u, _, _ in rows}
    comparable = [u for u, c, m in rows if not m and c and c.startswith(DAILY_CATEGORIES)]
    if mode == "auto":
        mode = "full" if (_local_weekday() == FULL_WEEKDAY or len(comparable) < MIN_KNOWN_FOR_DAILY) \
            else "daily"
    sm = sitemap_urls(client)
    in_sm = set(sm)
    matched = [u for u, _, m in rows if m and u in in_sm]
    new = [u for u in sm if u not in known and u not in dead]
    print(f"sitemap: {len(sm)}; cunoscute: {len(known)} (cu pereche: {len(matched)}, "
          f"comparabile fara pereche: {len(comparable)}); nevazute: {len(new)}; "
          f"moarte (<{DEAD_DAYS}z): {len(dead)}", flush=True)
    if mode == "new":
        return mode, new
    if mode == "daily":
        rest = [u for u in comparable if u in in_sm]
        return mode, matched + rest + new[:NEW_PER_DAY]
    if mode == "full":
        mset = set(matched)
        rest = [u for u, _, _ in rows if u in in_sm and u not in mset]
        return mode, matched + new + rest
    raise SystemExit(f"mod necunoscut: {mode} (auto|daily|full|new)")


_tls = threading.local()
_clients: list[httpx.Client] = []
_clients_lock = threading.Lock()


def _new_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA, "Accept-Language": "ro"},
                        timeout=httpx.Timeout(30, pool=30), follow_redirects=True)


def _thread_client() -> httpx.Client:
    # un client (si o conexiune) per worker: pool-ul partajat intre thread-uri s-a blocat
    # in productie (2026-11-28, toti workerii agatati in client.get, zero trafic)
    c = getattr(_tls, "client", None)
    if c is None:
        c = _tls.client = _new_client()
        with _clients_lock:
            _clients.append(c)
    return c


def fetch(url: str, stop: threading.Event) -> tuple[str, ScrapedProduct | None]:
    """('ok', p) | ('dead', None) = pagina exista dar nu e produs / 404 | ('err', None)."""
    if stop.is_set():
        return "skip", None
    res: tuple[str, ScrapedProduct | None] = ("err", None)
    try:
        r = _thread_client().get(url)
        if r.status_code == 200:
            p = parse_pdp(r.text, url)
            res = ("ok", p) if p else ("dead", None)
        elif r.status_code in (404, 410):
            res = ("dead", None)
    except Exception:
        pass
    # pauza per worker: WORKERS cereri in zbor, fiecare urmata de 0.15-0.25 s
    time.sleep(RATE_LIMIT_S + random.uniform(0, 0.1))
    return res


def run(mode: str = "auto", max_products: int | None = None) -> None:
    con = db.connect()
    store_id = db.get_or_create_store(con, "carrefour", "Carrefour", "online", "RO")
    con.commit()

    written = failed = dead_n = degraded = done = 0
    batch: list[ScrapedProduct] = []
    stop = threading.Event()
    t0 = time.monotonic()
    dead = load_dead()
    today = datetime.date.today().isoformat()
    with _new_client() as client:
        mode, urls = pick_urls(mode, client, con, store_id, dead)
        if max_products:
            urls = urls[:max_products]
        budget_h = float(os.environ.get("CARREFOUR_MAX_HOURS") or BUDGET_H.get(mode, 7.0))
        deadline = t0 + budget_h * 3600
        print(f"mod={mode} workers={WORKERS} buget={budget_h}h: {len(urls)} pagini in coada",
              flush=True)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = [ex.submit(fetch, u, stop) for u in urls]
            for fut, url in zip(futures, urls):     # in ordinea cozii (prioritate)
                status, p = fut.result()
                if status == "skip":
                    continue
                done += 1
                if status == "ok":
                    degraded = 0
                    batch.append(p)
                    dead.pop(url, None)
                elif status == "dead":
                    degraded = 0
                    dead_n += 1
                    dead[url] = today
                else:                               # 403/429/5xx/timeout => posibil blocati
                    failed += 1
                    degraded += 1
                    if degraded >= 40:
                        print(f"  ! {degraded} erori HTTP consecutive — probabil blocati; "
                              "ma opresc curat", flush=True)
                        stop.set()
                if not stop.is_set() and time.monotonic() > deadline:
                    print(f"  buget de {budget_h}h epuizat — ma opresc curat", flush=True)
                    stop.set()
                if stop.is_set():
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
                if len(batch) >= 100:
                    written += db.write_products(con, store_id, batch)
                    batch = []
                    save_dead(dead)
                if done % 500 == 0:
                    rate = done / max(time.monotonic() - t0, 1)
                    print(f"  [{done}/{len(urls)}] scrise={written + len(batch)} moarte={dead_n} "
                          f"erori={failed} ({rate:.2f} pagini/s)", flush=True)
        if batch:
            written += db.write_products(con, store_id, batch)
        save_dead(dead)
    for c in _clients:
        c.close()

    con.close()
    print(f"\nTotal scris in Postgres (carrefour, mod={mode}): {written}; moarte={dead_n} "
          f"erori={failed}; {done}/{len(urls)} pagini in {(time.monotonic() - t0) / 3600:.1f}h",
          flush=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = args.pop(0) if args and not args[0].isdigit() else os.environ.get("CARREFOUR_MODE", "auto")
    n = int(args[0]) if args else None
    run(mode, n)
