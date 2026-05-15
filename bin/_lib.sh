#!/usr/bin/env bash
# Shared helpers for the doc-audit operator scripts. Sourced — not executed.
#
# Conventions:
#   * AUDIT_BOT_ROOT  parent of action/ + server/ + bin/. Auto-detected.
#   * WORKSPACE_ROOT  parent dir holding mirrors (default $HOME/workspace).
#   * STATE_DIR       where pid/log/url files live (default $AUDIT_BOT_ROOT/.state).

set -u

AUDIT_BOT_ROOT="${AUDIT_BOT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(cd "$AUDIT_BOT_ROOT/.." && pwd)}"
STATE_DIR="${STATE_DIR:-$AUDIT_BOT_ROOT/.state}"

# Server defaults
PORT="${DOC_AUDIT_PORT:-8089}"
DB="${DOC_AUDIT_DB:-$AUDIT_BOT_ROOT/server/doc_audit.sqlite3}"
LOG_DIR="${LOG_DIR:-$STATE_DIR/logs}"

# Tunnel — cloudflared quick tunnel by default.
TUNNEL_BIN="${TUNNEL_BIN:-${HOME}/bin/cloudflared}"

# Mirrors to expose to the server. Operator controls this allowlist; the
# RepoMirror security model rejects anything not on this list even if a
# matching directory exists in $WORKSPACE_ROOT.
DEFAULT_MIRRORS="${DOC_AUDIT_MIRRORS:-node runner cbfs refs}"

mkdir -p "$STATE_DIR" "$LOG_DIR"

log() { printf '[%(%H:%M:%S)T] %s\n' -1 "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

write_pid() { echo "$2" > "$STATE_DIR/$1.pid"; }
read_pid()  { [[ -f "$STATE_DIR/$1.pid" ]] && cat "$STATE_DIR/$1.pid" || true; }
clear_pid() { rm -f "$STATE_DIR/$1.pid"; }

kill_pid() {
  local name="$1" pid
  pid="$(read_pid "$name")"
  if [[ -z "$pid" ]]; then
    log "$name not running"
    return 0
  fi
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null && log "$name stopped (pid=$pid)"
    # Give it a moment to clean up; force-kill if still alive.
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    kill -9 "$pid" 2>/dev/null || true
  else
    log "$name pid $pid already gone"
  fi
  clear_pid "$name"
}

wait_for_http() {
  # wait_for_http <url> <attempts> [<sleep_secs>]
  local url="$1" max="${2:-30}" pause="${3:-0.5}"
  for ((i=1; i<=max; i++)); do
    if curl -sf "$url" >/dev/null 2>&1; then return 0; fi
    sleep "$pause"
  done
  return 1
}

require_cmd() {
  command -v "$1" >/dev/null || die "missing required command: $1"
}
