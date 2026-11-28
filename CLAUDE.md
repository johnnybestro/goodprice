# GoodPrice — ghid pentru Claude

Comparator de prețuri pentru cumpărături în România: scrapere zilnice → Postgres →
matching produse identice între magazine → API FastAPI + PWA. Userul își face lista
în configurator și primește cel mai ieftin magazin + split profitabil pe 2-3 magazine.

## Principiul #1: CORECTITUDINE

Comparăm DOAR produse identice (același produs, aceeași cantitate). Mai bine
„doar la X" decât o comparație înșelătoare. Produsele la cântar se compară pe lei/kg.
Nicio limită fixă pe rezultate fără semnal de trunchiere (postmortem „Azuga 8l" în
README). Fiecare bug de căutare raportat devine un GOLDEN în `scripts/healthcheck.py`.

## Instalare pe un server nou (Debian 12/13)

    git clone <repo> /opt/goodprice
    cd /opt/goodprice
    bash scripts/bootstrap-server.sh                        # DB goală
    bash scripts/bootstrap-server.sh --restore cale.dump    # sau cu datele

Scriptul face tot: pachete, Postgres pe **UTF-8** (obligatoriu — vezi capcana de mai
jos), parolă DB nouă în `.env`, schemă/restaurare, venv, systemd (API + timere).
Calea `/opt/goodprice` e hardcodată în unit-uri. Deploy ulterior: `git pull` în
`/opt/goodprice` + `systemctl restart goodprice-api`.

## Hartă

| Ce | Unde |
|---|---|
| Scrapere (unul per magazin) | `scrapers/{sezamo,freshful,megaimage,carrefour,lidl,kaufland}.py` |
| Scriere DB (upsert + istoric) | `goodprice/db.py` (`write_products`) |
| Matching cross-magazin | `goodprice/matching.py` (`STORES` = prioritate) |
| API + optimizare coș | `goodprice/api.py`, `goodprice/optimize.py` |
| Frontend PWA (vanilla JS) | `frontend/index.html` |
| Schemă + migrare | `db/schema.sql`, `db/migrate.sh` |
| Unit-uri systemd | `deploy/systemd/` (instalare: `scripts/install-systemd.sh`) |
| Config (gitignored) | `.env` (șablon: `.env.example`) |
| Verificare zilnică | `scripts/healthcheck.py` (goldens + prospețime + API viu) |

Timere zilnice (UTC): sezamo 04:00 → freshful 04:30 → lidl 05:00 → megaimage 05:30
→ kaufland 05:45 → matching 06:00 → healthcheck 06:30; carrefour 21:30: zilnic categoriile comparabile (~4-5h, 3 workeri), duminica tot sitemap-ul (~12h).

## Capcane învățate (nu le redescoperi)

- **DB-ul trebuie să fie UTF-8.** Vechiul LXC avea SQL_ASCII/ctype C — de-asta există
  coloana `store_product.name_norm` (ASCII, fără diacritice, întreținută de
  `db.write_products`) pe care se face TOATĂ căutarea, cu regex word-boundary `\y`
  (în Postgres `\b` = backspace!). Nu căuta pe `name` direct.
- **psycopg3**: SELECT-urile deschid tranzacție implicită → `con.rollback()` înainte
  de scrieri batch + `con.commit()` explicit. Coloanele text pot veni `bytes` →
  trece-le prin `_s()` din `goodprice/matching.py`.
- **Scraperele folosesc API-urile/paginile ascunse, NU browser** (Playwright doar în
  `scrapers/_discover_*.py`, pentru descoperire). Rate-limit blând + jitter; gărzi de
  degradare care opresc scraperul la primul semn de throttling (Lidl throttluiește;
  Cloudflare păzește /graphql la Carrefour — mergem pe paginile SSR). Detalii per
  magazin în README.
- **Niciun magazin nu expune EAN** în afară de Kaufland (din numele imaginilor) →
  matching pe brand+nume+gramaj; gramajul Sezamo e în `raw_amount`, nu în nume.
- Modelul „cel mai ieftin din categorie per magazin" a fost RESPINS de user
  (compara produse diferite = înșelător). Nu-l reintroduce.

## Operare

- Status: `systemctl list-timers 'goodprice-*'`; loguri: `journalctl -u goodprice-<x>`.
- Orice schimbare în căutare/configurator → rulează `scripts/healthcheck.py` înainte
  și după. Eșecul healthcheck-ului e vizibil în systemd (unit failed).
- Backup: `pg_dump -Fc goodprice` (dump-urile istorice stau pe Mac-ul userului în
  `goodprice-backups/`).
- Userul e român, vorbește informal, vrea planuri prezentate direct în text cu
  recomandare (nu meniuri de opțiuni); corectitudinea comparației e nenegociabilă.
