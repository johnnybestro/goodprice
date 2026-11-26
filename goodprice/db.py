"""Strat de acces la baza de date (psycopg 3)."""
from __future__ import annotations

import psycopg

from .config import DATABASE_URL
from .models import ScrapedProduct
from .textnorm import name_norm


def connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL)


def get_or_create_store(con: psycopg.Connection, slug: str, name: str,
                        type_: str = "online", country: str = "RO") -> int:
    row = con.execute(
        """INSERT INTO store (slug, name, type, country) VALUES (%s, %s, %s, %s)
           ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
           RETURNING id""",
        (slug, name, type_, country),
    ).fetchone()
    return row[0]


def write_products(con: psycopg.Connection, store_id: int,
                   products: list[ScrapedProduct]) -> int:
    """Upsert SKU-uri + insert observatii pret + refresh current_price.

    Intr-o singura tranzactie. Returneaza numarul de produse scrise.
    """
    n = 0
    with con.transaction():
        for p in products:
            sp_id = con.execute(
                """INSERT INTO store_product
                       (store_id, external_id, name, name_norm, brand, raw_amount, unit,
                        ean, slug, url, category_path, last_seen)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (store_id, external_id) DO UPDATE SET
                       name = EXCLUDED.name, name_norm = EXCLUDED.name_norm,
                       brand = EXCLUDED.brand,
                       raw_amount = EXCLUDED.raw_amount, unit = EXCLUDED.unit,
                       ean = COALESCE(EXCLUDED.ean, store_product.ean),
                       slug = EXCLUDED.slug, url = EXCLUDED.url,
                       category_path = EXCLUDED.category_path, last_seen = now()
                   RETURNING id""",
                (store_id, p.external_id, p.name, name_norm(p.name), p.brand,
                 p.raw_amount, p.unit, p.ean, p.slug, p.url, p.category_path),
            ).fetchone()[0]

            con.execute(
                """INSERT INTO price_history
                       (store_product_id, price, sale_price, price_per_unit,
                        currency, in_stock)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (sp_id, p.price, p.sale_price, p.price_per_unit, p.currency, p.in_stock),
            )

            con.execute(
                """INSERT INTO current_price
                       (store_product_id, price, sale_price, price_per_unit,
                        currency, in_stock, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (store_product_id) DO UPDATE SET
                       price = EXCLUDED.price, sale_price = EXCLUDED.sale_price,
                       price_per_unit = EXCLUDED.price_per_unit,
                       currency = EXCLUDED.currency, in_stock = EXCLUDED.in_stock,
                       updated_at = now()""",
                (sp_id, p.price, p.sale_price, p.price_per_unit, p.currency, p.in_stock),
            )
            n += 1
    return n
