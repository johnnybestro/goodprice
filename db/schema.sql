-- GoodPrice — schema de baza (Faza 1)
-- Idempotent: CREATE ... IF NOT EXISTS. Aplica cu: bash db/migrate.sh
--
-- Model: store_product (SKU per magazin) -> canonical_product (produsul "ideal"
-- partajat intre magazine, populat de matching). price_history = serie temporala
-- (o linie per scrape); current_price = ultimul pret, pentru cautari rapide.

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

-- magazinele
CREATE TABLE IF NOT EXISTS store (
    id          SERIAL PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'online',   -- online | offline
    country     TEXT NOT NULL DEFAULT 'RO',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- produsul canonic: cheia comparatiei intre magazine (populat de matching)
CREATE TABLE IF NOT EXISTS canonical_product (
    id          BIGSERIAL PRIMARY KEY,
    ean         TEXT UNIQUE,                       -- cea mai sigura cheie de match
    name        TEXT NOT NULL,
    brand       TEXT,
    category    TEXT,
    base_unit   TEXT,                              -- kg | l | buc
    weighted    BOOLEAN NOT NULL DEFAULT false,    -- produs la cantar (compara lei/kg, nu pret absolut)
    embedding   vector(384),                       -- pentru matching semantic (faza 2)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- SKU per magazin: o linie per produs per magazin
CREATE TABLE IF NOT EXISTS store_product (
    id                   BIGSERIAL PRIMARY KEY,
    store_id             INT NOT NULL REFERENCES store(id),
    external_id          TEXT NOT NULL,            -- id-ul intern al magazinului
    name                 TEXT NOT NULL,
    name_norm            TEXT,                     -- lowercase, fara diacritice (cautare \y cu index)
    brand                TEXT,
    raw_amount           TEXT,                     -- "cca 470 g", "1,5 L"
    unit                 TEXT,                     -- kg | l | buc
    ean                  TEXT,
    slug                 TEXT,
    url                  TEXT,
    category_path        TEXT,
    canonical_product_id BIGINT REFERENCES canonical_product(id),
    first_seen           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (store_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_sp_ean       ON store_product(ean) WHERE ean IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sp_canonical ON store_product(canonical_product_id);
CREATE INDEX IF NOT EXISTS idx_sp_name_trgm ON store_product USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_sp_name_norm_trgm ON store_product USING gin (name_norm gin_trgm_ops);

-- observatii de pret (serie temporala; o linie per scrape)
-- TODO faza 2: converteste in hypertable TimescaleDB cand creste volumul.
CREATE TABLE IF NOT EXISTS price_history (
    id               BIGSERIAL PRIMARY KEY,
    store_product_id BIGINT NOT NULL REFERENCES store_product(id),
    price            NUMERIC(10,2),
    sale_price       NUMERIC(10,2),
    price_per_unit   NUMERIC(12,4),                -- lei / base_unit
    currency         TEXT NOT NULL DEFAULT 'RON',
    in_stock         BOOLEAN,
    scraped_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ph_sp_time ON price_history(store_product_id, scraped_at DESC);

-- ultimul pret per SKU (reimprospatat la fiecare scrape) — pentru optimizarea cosului
CREATE TABLE IF NOT EXISTS current_price (
    store_product_id BIGINT PRIMARY KEY REFERENCES store_product(id),
    price            NUMERIC(10,2),
    sale_price       NUMERIC(10,2),
    price_per_unit   NUMERIC(12,4),
    currency         TEXT NOT NULL DEFAULT 'RON',
    in_stock         BOOLEAN,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
