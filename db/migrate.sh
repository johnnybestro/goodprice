#!/usr/bin/env bash
# Aplica db/schema.sql pe baza goodprice. Idempotent. Ruleaza PE container.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
runuser -u postgres -- psql -d goodprice -v ON_ERROR_STOP=1 -f "$DIR/schema.sql"

# tabelele sunt create de postgres (superuser, pt. extensii); dam drepturi userului app
runuser -u postgres -- psql -d goodprice -v ON_ERROR_STOP=1 <<'SQL'
GRANT ALL ON ALL TABLES IN SCHEMA public TO goodprice;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO goodprice;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO goodprice;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO goodprice;
SQL
echo ">> schema aplicata + drepturi acordate"
runuser -u postgres -- psql -d goodprice -c "\dt"
