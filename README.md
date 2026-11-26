# GoodPrice

Comparator de prețuri pentru cumpărături în România. Userul își încarcă lista,
primește **unde o ia cel mai ieftin** (un magazin întreg) plus top alternative și
opțiunea de **split pe 2-3 magazine** când e profitabil.

## Stack

| Strat | Tehnologie |
|---|---|
| Frontend web | Next.js (TypeScript) ca PWA instalabilă |
| Mobile (faza 2) | Expo / React Native (cod partajat, monorepo Turborepo) |
| Backend API | FastAPI (Python) |
| Scraping | httpx + curl_cffi (API ascuns) → Scrapy (scară) → Playwright-stealth (fallback) |
| DB | PostgreSQL + TimescaleDB (istoric prețuri) + pgvector (matching) |
| Cache / cozi | Redis + Celery |
| Optimizare coș | Google OR-Tools (split pe magazine) |

## Magazine țintă (MVP)

Live: **Sezamo** (~14.3k), **Freshful** (~18k), **Mega Image** (~7.7k, GraphQL),
**Carrefour** (~87k URL-uri produs, Magento SSR — include nonfood; timer 21:30, ~7h),
**Lidl** (~1.1k văzute, vitrina de promoții — prețuri de magazin fizic).
Urmează: Auchan, Penny; Monitorul Prețurilor pt. lanțurile fără online.
(Kaufland.ro nu are shop online.)

### Carrefour (`scrapers/carrefour.py`)
Magento + React. Cloudflare păzește /graphql (403, legat de TLS fingerprint), dar
paginile SSR sunt libere. Categoriile alimentare proaspete nu-s în sitemap (necesită
selecție de magazin) → sursa = sitemap-urile de produse (~87k PDP-uri). PDP: JSON-LD
Product + dataLayer (preț listă vs preț vânzare = promo, categorie, disponibilitate).

### Mega Image (`scrapers/megaimage.py`)
Apollo GraphQL pe `/api/v1/` cu persisted queries (GET + sha256Hash — dacă apare 400,
recapturează hash-ul cu `_discover_megaimage_api.py`). Nu cere cookie-uri, doar
headerele Apollo (`apollographql-client-name` etc.). `GetProductSearch` cu searchQuery
GOL enumeră tot catalogul (~7.7k), paginat 50/pagină. Prețul are `value` +
`discountedPriceFormatted` (promo) + `supplementaryPriceLabel2` („10.49 Lei/L" = ppu).

### Matching multi-magazin
`goodprice/matching.py` grupează incremental pe perechi de magazine (ordinea STORES =
prioritate): un grup canonic = același produs în 2+ magazine, max un produs/magazin.
Timer zilnic 06:00 (`goodprice-matching.timer`), după toate scraperele.

## Cele două probleme grele

1. **Product matching** — același produs, nume diferite între magazine.
   Strategie: EAN (cel mai sigur) → fuzzy nume+brand+gramaj (`pg_trgm`) →
   embeddings (`pgvector`) → validare umană pentru cazuri incerte.
2. **Optimizare split** — atribuirea fiecărui produs unui magazin minimizând
   `cost + penalizare × (nr_magazine − 1)`, limitat la 2-3 magazine (OR-Tools).

## Infrastructură (dev/staging)

Rulează pe un LXC Proxmox, always-on. Mac = editor, container = runtime.

| | |
|---|---|
| Host | LXC 113 `goodprice` @ `192.168.1.112` (`goodprice.lan`), nod `pve-proxmox` |
| OS / resurse | Debian 13.1 · 16GB RAM · 48 cores · 120GB disc |
| Servicii | PostgreSQL 17 (+ pgvector 0.8, pg_trgm) · Redis 8 · Python 3.13 |
| DB | `goodprice` / user `goodprice` (parolă dev: `goodprice_dev`) |
| Acces | `ssh goodprice` (cheie `~/.ssh/goodprice_lxc`) |
| Deploy | `./scripts/deploy.sh` (rsync Mac → `/opt/goodprice`) |

Setup DB: `ssh goodprice 'cd /opt/goodprice && bash scripts/provision-db.sh'`.
Notă: Redis în LXC neprivilegiat are nevoie de drop-in-ul care dezactivează
`PrivateUsers` & co. (vezi `/etc/systemd/system/redis-server.service.d/lxc.conf`).

## Status — Faza 0 (validare) ✅

PoC Sezamo (`scrapers/poc/sezamo.py`) dovedește că putem extrage date complet
structurate cu un simplu HTTP GET, fără browser și fără anti-bot:

- 956 categorii descoperite via API-ul intern (`/services/frontend-service/...`)
- produse cu **nume, gramaj, preț, preț/unitate (lei/kg), stoc** — preț/unitate 100% acoperit
- date din starea React-Query embed-ată în pagină

Rulează: `python3 scrapers/poc/sezamo.py` → scrie în `scrapers/poc/poc.db` (SQLite).

### API Sezamo (descoperit cu Playwright)
- Categorii: `/services/frontend-service/renderer/navigation/flat.json`
- Produse/categorie (paginat): `/api/v1/categories/normal/{catId}/products?page=N&size=100`
- Card produs (preț): `/api/v1/products/card?products=X&products=Y&categoryType=normal`
- Info produs (brand!): `/api/v1/products?products=X` — cardul NU întoarce brandul
- **EAN: indisponibil pe Sezamo** (Rohlik folosește doar id-uri interne) → matching pe brand+nume+gramaj.

## Roadmap

- **Faza 0** ✅ Validare scraping (un magazin).
- **Faza 1** MVP web (în curs):
  - ✅ Schema Postgres (`db/schema.sql`) + pipeline scraper → DB
  - ✅ Sezamo complet (~13.4k produse) — `scrapers/sezamo.py`
  - ✅ Freshful complet (~17.5k produse) — `scrapers/freshful.py`
  - ✅ Cron zilnic (timere systemd: Sezamo 04:00, Freshful 04:30, Lidl 05:00)
  - ✅ Lidl (`scrapers/lidl.py`): vitrina Nuxt 3, date embed-ate în `__NUXT_DATA__`
    (format devalue — array plat cu indecși, vezi clasa `Devalue`). Sursa = sitemap-ul
    oficial de produse (`/p/export/RO/ro/product_sitemap.xml.gz`, ~2.077 URL-uri) —
    acoperă și paginile de campanie săptămânale (oferte Lidl Plus etc.), nu doar
    categoria principală (~474). Produsele arhivate n-au preț → sărite.
    `basePrice.text` dă lei/kg gata calculat; carnea „per 100 g" → preț×10 = lei/kg;
    titlul pe pagina de detaliu e în `keyfacts.fullTitle`; `eans` există dar e mereu gol.
  - ✅ Matching (brand+nume+gramaj din `raw_amount`+count/pack) + produse la cântar
    (fără brand, comparate pe lei/kg) — `goodprice/matching.py` — ~3.4k perechi
  - ✅ Căutare cu ranking pe relevanță (cuvânt întreg > poziție > lungime)
  - ✅ Optimizare coș (single-store + split) — `goodprice/optimize.py`
  - ✅ API FastAPI + frontend web — `goodprice/api.py`, `frontend/index.html`
  - ⬜ Matching: parsare count/pack-size (capsule, scutece) ca să nu potrivească mărimi diferite
  - ⬜ Migrare frontend pe Next.js PWA + mai multe magazine

## Aplicația

Rulează pe container (serviciu systemd `goodprice-api`), accesibilă în LAN:

    http://goodprice.lan:8000   (sau http://192.168.1.112:8000)

- Flux principal — **CONFIGURATOR** (design-ul userului): un input, scrii un termen
  („apa") + Enter → `GET /api/configure` întoarce TOATE variantele din întreg catalogul
  (nu doar cele potrivite) cu atribute structurate → UI-ul arată FAȚETE obligatorii
  (Cantitate: 1l/2l/5l/8l…; Tip: plată/carbogazoasă, boabe/măcinată…; Grăsime %;
  Ambalaj) → **nu te lasă să adaugi** până n-ai ales toate fațetele active + produsul
  concret din shortlist. Mai mulți termeni deodată (virgule) intră într-o coadă.
- Corectitudine: produsele potrivite au 2 prețuri (comparabile); cele dintr-un singur
  magazin apar onest „doar la X" și constrâng optimizarea (se cumpără de acolo).
- `POST /api/optimize` acceptă `canonical_id` (2 prețuri) sau `sp_id` (1 preț).
  - CORECTITUDINE > acoperire: comparăm doar produse identice. Reprezentantul auto-ales
    nu-i mereu ideal (ex. „ouă"→ouă de prepeliță, „bere"→0.0%, fiindcă doar acelea au
    match identic între magazine) — dar comparația e corectă; user-ul poate schimba.
  - Model abandonat: „cel mai ieftin din categorie per magazin" (`goodprice/staples.py`,
    experimental, nefolosit) — compara produse diferite + scotea erori de date (0.03 lei).
- Căutarea merge DIRECT în Postgres pe `store_product.name_norm` (ASCII lowercase,
  întreținută de `db.write_products`; backfill: `scripts/backfill_name_norm.py`;
  index gin_trgm; word-boundary `\y`) — fără fetch-all + cap în Python.
  Postmortem „Azuga 8l" (26 iul): cap-ul de 400 trunchia silențios produsele cu
  termenul pe poziție >0 după ce catalogul a crescut (Carrefour). Regulă: nicio
  limită fixă pe rezultate fără semnal de trunchiere.
- Healthcheck zilnic 06:30 (`scripts/healthcheck.py`): produse-etalon (GOLDENS) +
  prospețime per magazin (max 3 zile) + API viu; eșec → unit systemd failed.
  Adaugă un golden nou la fiecare raport de produs lipsă.
- Adăugare manuală (search) pentru un produs anume.
- Produsele la cântar (fructe/legume) se compară pe lei/kg.
- Lista se salvează automat (localStorage, cheie `goodprice_lines_v2`).
- PWA instalabilă pe telefon/desktop: `manifest.webmanifest` + `sw.js` + `icon.svg`.
- API: `GET /api/search?q=`, `POST /api/optimize`, `GET /api/stores`.
- Instalare unit-uri systemd: `ssh goodprice 'cd /opt/goodprice && bash scripts/install-systemd.sh'`.

Note: Kaufland.ro nu are shop online (doar cataloage) → magazin #2 = Freshful.
Nici Sezamo nici Freshful nu expun EAN → matching pe brand+nume+gramaj (+embeddings ulterior).
- **Faza 2** Split pe 2-3 magazine (OR-Tools), conturi, panou admin matching.
- **Faza 3** Liste sugerate de AI, app mobil, alerte de preț.

## Note legale
GDPR pe datele userilor; respectăm robots.txt și rate-limit blând; luăm doar
prețuri publice. Sursă oficială de evaluat: Monitorul Prețurilor (guvern RO).
