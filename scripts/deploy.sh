#!/usr/bin/env bash
# Sincronizeaza codul de pe Mac pe containerul LXC goodprice (192.168.1.112).
# Mac = editor, container = runtime. Ruleaza: ./scripts/deploy.sh
set -euo pipefail

HOST="goodprice"            # alias din ~/.ssh/config
DEST="/opt/goodprice"
SRC="$(cd "$(dirname "$0")/.." && pwd)/"

echo ">> deploy $SRC -> $HOST:$DEST"
ssh "$HOST" "mkdir -p $DEST"
rsync -az --delete \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.venv' \
  --exclude 'scrapers/poc/poc.db' \
  "$SRC" "$HOST:$DEST/"
echo ">> gata. cod sincronizat in $HOST:$DEST"
