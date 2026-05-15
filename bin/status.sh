#!/usr/bin/env bash
# Show liveness of the doc-audit stack + recent runs + per-repo budget.
#
# Usage:
#   bin/status.sh                 # default
#   bin/status.sh owner/repo[,...] # also pull budget for these repos

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

REPOS="${1:-}"

# --- server ------------------------------------------------------------------
SPID="$(read_pid server)"
if [[ -n "$SPID" ]] && kill -0 "$SPID" 2>/dev/null; then
  STATE="up (pid=$SPID)"
  HEALTH="$(curl -sf "http://127.0.0.1:$PORT/v1/health" 2>/dev/null || echo '?')"
else
  STATE="down"
  HEALTH=""
fi
echo "server: $STATE"
[[ -n "$HEALTH" ]] && echo "  health: $HEALTH"

# --- tunnel ------------------------------------------------------------------
TPID="$(read_pid tunnel)"
if [[ -n "$TPID" ]] && kill -0 "$TPID" 2>/dev/null; then
  TUNNEL_STATE="up (pid=$TPID)"
  TUNNEL_URL="$(cat "$STATE_DIR/tunnel.url" 2>/dev/null || echo '?')"
else
  TUNNEL_STATE="down"
  TUNNEL_URL=""
fi
echo "tunnel: $TUNNEL_STATE"
[[ -n "$TUNNEL_URL" ]] && echo "  url:    $TUNNEL_URL"

# --- mirrors -----------------------------------------------------------------
echo "mirrors (under $WORKSPACE_ROOT):"
for m in $DEFAULT_MIRRORS; do
  d="$WORKSPACE_ROOT/$m"
  if [[ -d "$d/.git" ]]; then
    branch="$(git -C "$d" branch --show-current 2>/dev/null || echo '?')"
    short="$(git -C "$d" rev-parse --short HEAD 2>/dev/null || echo '?')"
    age="$(git -C "$d" log -1 --format=%cr 2>/dev/null || echo '?')"
    echo "  $m: branch=$branch head=$short age=$age"
  elif [[ -d "$d" ]]; then
    echo "  $m: (not a git repo)"
  else
    echo "  $m: (missing)"
  fi
done

# --- recent runs from audit_run DB ------------------------------------------
if [[ -f "$DB" ]]; then
  echo
  echo "recent audit runs (last 5):"
  python3 - "$DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
rows = con.execute(
    "SELECT request_id, repo, target, status, created_at "
    "FROM audit_run ORDER BY created_at DESC LIMIT 5"
).fetchall()
for r in rows:
    print(f"  {r[4]}  {r[1]:<25} {r[2]:<10} {r[3]:<10} {r[0][:8]}")
PY
fi

# --- budget ------------------------------------------------------------------
if [[ -n "$REPOS" && "$STATE" == "up"* ]]; then
  echo
  echo "budget:"
  IFS=',' read -r -a REPO_LIST <<<"$REPOS"
  for repo in "${REPO_LIST[@]}"; do
    body="$(curl -sf "http://127.0.0.1:$PORT/v1/budget/$repo" 2>/dev/null || echo '')"
    REPO="$repo" BODY="$body" python3 <<'PY'
import json, os
repo = os.environ["REPO"]
raw = os.environ.get("BODY", "")
try:
    d = json.loads(raw) if raw else {}
except json.JSONDecodeError:
    d = {}
if d:
    print(f"  {repo}: spent=${d.get('spent_usd', 0):.4f} "
          f"cap=${d.get('cap_usd', 0):.2f} "
          f"remaining=${d.get('remaining_usd', 0):.4f} "
          f"hard_breach={d.get('hard_breach')}")
else:
    print(f"  {repo}: (no budget endpoint response)")
PY
  done
fi
