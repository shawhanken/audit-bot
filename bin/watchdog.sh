#!/usr/bin/env bash
# Doc Audit health watchdog.
#
# Only the LOCAL server (127.0.0.1:$PORT/v1/health) is treated as
# restart-worthy. Tunnel reachability is logged but never triggers a
# restart — Cloudflare's free quick-tunnel will rate-limit (HTTP 429) a
# host that allocates new tunnels too frequently, and a cascading
# restart loop will trip that limit within minutes.
#
# Designed to be invoked by doc-audit-watchdog.timer. Never exits non-zero
# (so systemd doesn't oscillate the timer / unit state); it just logs.

set -uo pipefail
source "$(dirname "$0")/_lib.sh"

probe_local() {
  curl -sf -m 5 "http://127.0.0.1:$PORT/v1/health" >/dev/null
}

restart_unit() {
  log "watchdog: restarting doc-audit.service"
  systemctl --user restart doc-audit.service 2>&1 | head -2 >&2 || true
}

# Two strikes (~12s apart) before restarting — avoids reacting to a single
# transient TCP blip / GC pause.
if ! probe_local; then
  log "watchdog: local probe 1 failed; sleeping 10s"
  sleep 10
  if ! probe_local; then
    log "watchdog: local probe 2 failed; restarting"
    restart_unit
    exit 0
  fi
  log "watchdog: local recovered on retry"
fi

# Tunnel probe is OBSERVATIONAL only.
url="$(cat "$STATE_DIR/tunnel.url" 2>/dev/null || true)"
if [[ -n "$url" ]]; then
  host="${url#https://}"
  host="${host%%/*}"
  ip="$(curl -sf -m 4 "https://dns.google/resolve?name=${host}&type=A" 2>/dev/null \
        | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('Answer',[{}])[0].get('data',''))" 2>/dev/null || true)"
  if [[ -n "$ip" ]]; then
    if ! curl -sf -m 6 --resolve "${host}:443:${ip}" "${url}/v1/health" >/dev/null; then
      log "watchdog: tunnel unreachable (URL=${url}) — NOT restarting (avoid CF 429)"
    fi
  fi
fi
exit 0
