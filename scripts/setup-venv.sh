#!/usr/bin/env bash
# Creeaza venv-ul Python pe container si instaleaza dependintele. Ruleaza PE container.
# (Debian e externally-managed/PEP 668 -> folosim venv.)
set -euo pipefail
cd /opt/goodprice
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
# Pentru pasul curent (schema + scriere in DB) e suficient:
.venv/bin/pip install -q "psycopg[binary]==3.2.*" "httpx[http2]==0.27.*"
echo ">> venv gata:"
.venv/bin/python -c "import psycopg, httpx; print('psycopg', psycopg.__version__, '| httpx', httpx.__version__)"
