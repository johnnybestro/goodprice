"""One-off: populeaza store_product.name_norm pentru randurile existente.
Ruleaza PE container:  PYTHONPATH=. .venv/bin/python scripts/backfill_name_norm.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from goodprice import db                    # noqa: E402
from goodprice.matching import _s           # noqa: E402
from goodprice.textnorm import name_norm    # noqa: E402

con = db.connect()
rows = con.execute("SELECT id, name FROM store_product WHERE name_norm IS NULL").fetchall()
print(f"de completat: {len(rows)}")
n = 0
with con.cursor() as cur:
    for pid, name in rows:
        cur.execute("UPDATE store_product SET name_norm = %s WHERE id = %s",
                    (name_norm(_s(name)), pid))
        n += 1
        if n % 10000 == 0:
            con.commit()
            print(f"  {n}...")
con.commit()
print(f"completate: {n}")
con.close()
