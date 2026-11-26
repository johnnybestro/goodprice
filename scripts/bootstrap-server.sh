#!/usr/bin/env bash
# Instaleaza GoodPrice COMPLET pe un server Debian 12/13 proaspat. Ruleaza ca root.
#
#   git clone <repo> /opt/goodprice
#   cd /opt/goodprice
#   bash scripts/bootstrap-server.sh                      # instalare curata (DB goala)
#   bash scripts/bootstrap-server.sh --restore /cale/goodprice.dump   # + datele
#
# Idempotent: poate fi rulat din nou fara sa strice nimic.
# Parola DB se genereaza la prima rulare si se scrie in /opt/goodprice/.env.
set -euo pipefail

DEST=/opt/goodprice
RESTORE=""
[ "${1:-}" = "--restore" ] && RESTORE="${2:?lipseste calea catre dump}"

[ "$(id -u)" = 0 ] || { echo "ruleaza ca root"; exit 1; }
[ "$(pwd)" = "$DEST" ] || { echo "cloneaza repo-ul in $DEST si ruleaza de acolo (unit-urile systemd au calea hardcodata)"; exit 1; }

export DEBIAN_FRONTEND=noninteractive

echo ">> [1/7] pachete"
apt-get update -qq
apt-get install -y -qq git curl ca-certificates openssl \
  postgresql postgresql-contrib redis-server \
  python3 python3-venv rsync

PGVER="$(pg_lsclusters -h | awk '{print $1; exit}')"
apt-get install -y -qq "postgresql-${PGVER}-pgvector"

echo ">> [2/7] Postgres pe UTF-8 (lectia SQL_ASCII de pe LXC)"
systemctl enable --now postgresql
ENC="$(runuser -u postgres -- psql -tAc 'SHOW server_encoding')"
if [ "$ENC" != "UTF8" ]; then
  HAS_DB="$(runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='goodprice'" || true)"
  if [ "$HAS_DB" = "1" ]; then
    echo "!! clusterul e $ENC si baza goodprice EXISTA — nu distrug date; migreaza manual"; exit 1
  fi
  echo "   cluster $ENC gol -> il recreez pe C.UTF-8"
  pg_dropcluster --stop "$PGVER" main
  pg_createcluster --start --locale C.UTF-8 "$PGVER" main
fi
runuser -u postgres -- psql -tAc 'SHOW server_encoding'

echo ">> [3/7] .env + parola DB"
if [ ! -f .env ]; then
  DB_PASS="$(openssl rand -hex 16)"
  sed -e "s/goodprice_dev/$DB_PASS/g" \
      -e "s/^GOODPRICE_HOST=.*/GOODPRICE_HOST=$(hostname -I | awk '{print $1}')/" \
      .env.example > .env
  chmod 600 .env
  echo "   .env generat (parola noua)"
else
  DB_PASS="$(grep -m1 '^GOODPRICE_DB_PASS=' .env | cut -d= -f2)"
  echo "   .env existent — pastrez parola"
fi

echo ">> [4/7] baza de date"
GOODPRICE_DB_PASS="$DB_PASS" bash scripts/provision-db.sh
runuser -u postgres -- psql -c "ALTER ROLE goodprice PASSWORD '$DB_PASS';" > /dev/null

if [ -n "$RESTORE" ]; then
  echo ">> [5/7] restaurare dump: $RESTORE"
  runuser -u postgres -- pg_restore -d goodprice --no-owner --clean --if-exists "$RESTORE" || true
  bash db/migrate.sh      # re-aplica schema lipsa + drepturile (idempotent pe dump)
else
  echo ">> [5/7] schema (DB goala)"
  bash db/migrate.sh
fi

echo ">> [6/7] venv Python"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements-server.txt

echo ">> [7/7] systemd (API + timere zilnice)"
systemctl enable --now redis-server
bash scripts/install-systemd.sh

echo
echo ">> GATA. Verificare:"
sleep 2
curl -fsS http://localhost:8000/api/stores && echo
if [ -n "$RESTORE" ]; then
  PYTHONPATH=. .venv/bin/python scripts/healthcheck.py || true
else
  echo "   (DB goala — ruleaza scraperele sau restaureaza un dump, apoi scripts/healthcheck.py)"
fi
echo
echo "De facut dupa (nu le face scriptul): firewall (22/80/443), fail2ban, domeniu + Caddy HTTPS."
