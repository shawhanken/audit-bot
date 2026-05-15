#!/usr/bin/env bash
# Bring up the full doc-audit stack:
#   1. (optional) git pull all configured mirrors
#   2. start uvicorn (skip if already up)
#   3. start cloudflared quick tunnel (skip if already up)
#   4. wait until both health and tunnel respond
#   5. optionally set DOC_AUDIT_SERVER_URL variable on listed consumer repos
#
# Usage:
#   bin/up.sh                       # start everything, do not touch consumer repos
#   bin/up.sh --update-repos owner/repo[,owner/repo,...]
#                                   # also set DOC_AUDIT_SERVER_URL variable
#   bin/up.sh --no-pull             # skip the `git pull` of mirrors
#   bin/up.sh --no-tunnel           # only start the local server
#
# Env overrides: see bin/_lib.sh.

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

PULL_MIRRORS=1
START_TUNNEL=1
UPDATE_REPOS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-pull)    PULL_MIRRORS=0; shift ;;
    --no-tunnel)  START_TUNNEL=0; shift ;;
    --update-repos) UPDATE_REPOS="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,15p' "$0"; exit 0 ;;
    *) die "unknown flag: $1" ;;
  esac
done

# Systemd-friendly fallback: if no --update-repos passed, read from env.
# install-service.sh writes DOC_AUDIT_CONSUMERS into .state/env which
# the unit's EnvironmentFile= picks up.
if [[ -z "$UPDATE_REPOS" && -n "${DOC_AUDIT_CONSUMERS:-}" ]]; then
  UPDATE_REPOS="$DOC_AUDIT_CONSUMERS"
fi

require_cmd python3
require_cmd curl

# --- 1. Pull mirrors ---------------------------------------------------------
if [[ "$PULL_MIRRORS" == 1 ]]; then
  "$AUDIT_BOT_ROOT/bin/pull-mirrors.sh" || log "pull-mirrors had issues; continuing"
fi

# --- 2. Start uvicorn --------------------------------------------------------
SERVER_PID="$(read_pid server)"
if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
  log "server already running (pid=$SERVER_PID)"
else
  log "starting server on :$PORT (db=$DB)"
  (
    cd "$AUDIT_BOT_ROOT/server"
    DOC_AUDIT_DB="$DB" \
    DOC_AUDIT_MIRROR_ROOT="$WORKSPACE_ROOT" \
    exec python3 -m uvicorn app.main:app \
      --host 127.0.0.1 --port "$PORT" --log-level info \
      > "$LOG_DIR/server.log" 2>&1
  ) &
  write_pid server $!
  log "waiting for /v1/health"
  wait_for_http "http://127.0.0.1:$PORT/v1/health" 40 0.5 \
    || die "server did not become healthy in 20s (see $LOG_DIR/server.log)"
  log "server up"
fi

# --- 3. Start tunnel ---------------------------------------------------------
TUNNEL_URL=""
if [[ "$START_TUNNEL" == 1 ]]; then
  TUNNEL_PID="$(read_pid tunnel)"
  if [[ -n "$TUNNEL_PID" ]] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
    log "tunnel already running (pid=$TUNNEL_PID)"
    TUNNEL_URL="$(cat "$STATE_DIR/tunnel.url" 2>/dev/null || true)"
  else
    if [[ ! -x "$TUNNEL_BIN" ]]; then
      die "cloudflared not found at $TUNNEL_BIN — install it or pass --no-tunnel"
    fi

    # Two modes:
    #   DOC_AUDIT_TUNNEL_MODE=named — run a named tunnel; URL is stable and
    #     must have been written to $STATE_DIR/tunnel.url out-of-band
    #     (operator did `cloudflared tunnel route dns ...` once).
    #   DOC_AUDIT_TUNNEL_MODE=quick (default) — allocate a quick tunnel each
    #     start. Hostname rotates; rate-limited by Cloudflare.
    MODE="${DOC_AUDIT_TUNNEL_MODE:-quick}"
    rm -f "$LOG_DIR/cloudflared.log"
    case "$MODE" in
      named)
        [[ -z "${DOC_AUDIT_NAMED_TUNNEL:-}" ]] && die "DOC_AUDIT_TUNNEL_MODE=named requires DOC_AUDIT_NAMED_TUNNEL=<tunnel-name>"
        [[ ! -f "$STATE_DIR/tunnel.url" ]] && die "named-tunnel mode: write the stable URL to $STATE_DIR/tunnel.url first"
        log "starting named cloudflared tunnel '$DOC_AUDIT_NAMED_TUNNEL'"
        "$TUNNEL_BIN" tunnel run --url "http://localhost:$PORT" "$DOC_AUDIT_NAMED_TUNNEL" \
          > "$LOG_DIR/cloudflared.log" 2>&1 &
        write_pid tunnel $!
        TUNNEL_URL="$(cat "$STATE_DIR/tunnel.url")"
        # Wait for the daemon to actually register a connection.
        for _ in $(seq 1 30); do
          if grep -q "Registered tunnel connection" "$LOG_DIR/cloudflared.log" 2>/dev/null; then
            break
          fi
          sleep 1
        done
        log "named tunnel up at: $TUNNEL_URL"
        ;;
      quick)
        log "starting cloudflared quick tunnel"
        rm -f "$STATE_DIR/tunnel.url"
        "$TUNNEL_BIN" tunnel --url "http://localhost:$PORT" --no-autoupdate \
          > "$LOG_DIR/cloudflared.log" 2>&1 &
        write_pid tunnel $!
        for _ in $(seq 1 60); do
          url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/cloudflared.log" | head -1 || true)"
          if [[ -n "$url" ]]; then
            TUNNEL_URL="$url"
            echo "$url" > "$STATE_DIR/tunnel.url"
            break
          fi
          # Detect Cloudflare 429 early — no point waiting 60s on something
          # that already failed.
          if grep -q "429 Too Many Requests" "$LOG_DIR/cloudflared.log" 2>/dev/null; then
            die "Cloudflare quick-tunnel rate limit (429). Switch to a named tunnel (INSTALL.md §6b) or wait."
          fi
          sleep 1
        done
        [[ -z "$TUNNEL_URL" ]] && die "tunnel did not announce URL (see $LOG_DIR/cloudflared.log)"
        log "quick tunnel up: $TUNNEL_URL"
        ;;
      *)
        die "unknown DOC_AUDIT_TUNNEL_MODE: $MODE (use 'quick' or 'named')"
        ;;
    esac
  fi
fi

# --- 4. Tell consumer repos about the new tunnel URL -------------------------
if [[ -n "$UPDATE_REPOS" && -n "$TUNNEL_URL" ]]; then
  require_cmd gh
  IFS=',' read -r -a REPO_LIST <<<"$UPDATE_REPOS"
  for repo in "${REPO_LIST[@]}"; do
    log "setting DOC_AUDIT_SERVER_URL on $repo"
    gh variable set DOC_AUDIT_SERVER_URL -R "$repo" -b "$TUNNEL_URL" \
      || log "(failed for $repo — check gh auth and repo write access)"
  done
fi

# --- 5. Print summary --------------------------------------------------------
cat <<SUMMARY

doc-audit is up.

  health:      http://127.0.0.1:$PORT/v1/health
  metrics:     http://127.0.0.1:$PORT/v1/metrics
  tunnel URL:  ${TUNNEL_URL:-(disabled)}
  server log:  $LOG_DIR/server.log
  tunnel log:  $LOG_DIR/cloudflared.log
  mirrors:     ${DEFAULT_MIRRORS}
  state dir:   $STATE_DIR

Next steps:
  * bin/status.sh    inspect liveness, budget, recent runs
  * bin/down.sh      stop everything
SUMMARY
