"""Configurare din .env / variabile de mediu (fara dependinte externe)."""
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Incarca un .env simplu in os.environ (nu suprascrie ce exista deja)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv(_ROOT / ".env")


def _db_url() -> str:
    if url := os.environ.get("GOODPRICE_DATABASE_URL"):
        return url
    u = os.environ.get("GOODPRICE_DB_USER", "goodprice")
    p = os.environ.get("GOODPRICE_DB_PASS", "goodprice_dev")
    h = os.environ.get("GOODPRICE_DB_HOST", "localhost")
    port = os.environ.get("GOODPRICE_DB_PORT", "5432")
    name = os.environ.get("GOODPRICE_DB_NAME", "goodprice")
    return f"postgresql://{u}:{p}@{h}:{port}/{name}"


DATABASE_URL = _db_url()
REDIS_URL = os.environ.get("GOODPRICE_REDIS_URL", "redis://localhost:6379/0")
