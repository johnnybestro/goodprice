#!/usr/bin/env bash
# Instaleaza toate unit-urile systemd (API + timere scrapere) pe container.
# Ruleaza PE container (ca root).
set -euo pipefail
SRC="/opt/goodprice/deploy/systemd"
cp "$SRC"/goodprice-*.service "$SRC"/goodprice-*.timer /etc/systemd/system/
systemctl daemon-reload

# API always-on
systemctl enable --now goodprice-api
echo ">> goodprice-api: $(systemctl is-active goodprice-api)"

# timere zilnice
for t in "$SRC"/goodprice-*.timer; do
  systemctl enable --now "$(basename "$t")"
done
systemctl list-timers 'goodprice-*' --no-pager
