#!/usr/bin/env bash
# Ruleaza PE container (goodprice). Creeaza baza, userul si extensiile.
# Idempotent: poate fi rulat de mai multe ori.
set -euo pipefail

DB_NAME="goodprice"
DB_USER="goodprice"
DB_PASS="${GOODPRICE_DB_PASS:-goodprice_dev}"   # schimba in productie

export DEBIAN_FRONTEND=noninteractive

# pgvector pentru matching semantic (Postgres 17 pe Debian 13)
PGV="postgresql-$(pg_lsclusters -h | awk '{print $1; exit}')-pgvector"
apt-get install -y -qq "$PGV" || apt-get install -y -qq postgresql-17-pgvector || true

runuser -u postgres -- psql -tc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1 || \
  runuser -u postgres -- psql -c "CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS';"

runuser -u postgres -- psql -tc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 || \
  runuser -u postgres -- psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"

runuser -u postgres -- psql -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS vector;"
runuser -u postgres -- psql -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"

echo ">> DB '$DB_NAME' gata (user '$DB_USER', extensii: vector, pg_trgm)"
