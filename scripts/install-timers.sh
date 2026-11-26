#!/usr/bin/env bash
# Instaleaza timer-ele systemd pe container. Ruleaza PE container (ca root).
set -euo pipefail
SRC="/opt/goodprice/deploy/systemd"
cp "$SRC"/goodprice-*.service "$SRC"/goodprice-*.timer /etc/systemd/system/
systemctl daemon-reload
for t in "$SRC"/goodprice-*.timer; do
  name="$(basename "$t")"
  systemctl enable --now "$name"
  echo ">> activat $name"
done
systemctl list-timers 'goodprice-*' --no-pager
