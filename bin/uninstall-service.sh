#!/usr/bin/env bash
# Remove the doc-audit systemd units (server keeps running until you also
# run bin/down.sh).

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# Stop + disable in safe order: timers first, then service.
for unit in doc-audit-pull-mirrors.timer doc-audit-watchdog.timer doc-audit.service; do
  systemctl --user disable "$unit"  2>/dev/null || true
  systemctl --user stop    "$unit"  2>/dev/null || true
done

for unit in doc-audit.service doc-audit-watchdog.service doc-audit-watchdog.timer \
            doc-audit-pull-mirrors.service doc-audit-pull-mirrors.timer; do
  rm -f "$UNIT_DIR/$unit"
done

systemctl --user daemon-reload
log "doc-audit systemd units removed"
log "(server may still be running — bin/down.sh to fully stop)"
