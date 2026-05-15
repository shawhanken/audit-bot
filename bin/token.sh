#!/usr/bin/env bash
# Token management: mint / revoke / list.
#
# Usage:
#   bin/token.sh mint <owner>/<repo> [label]
#   bin/token.sh revoke <token>
#   bin/token.sh list                          # show repo bindings (no plaintext)

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

sub="${1:-}"
shift || true

case "$sub" in
  mint)
    repo="${1:-}"
    label="${2:-}"
    [[ -z "$repo" ]] && die "usage: bin/token.sh mint <owner>/<repo> [label]"
    DOC_AUDIT_DB="$DB" exec "$AUDIT_BOT_ROOT/server/scripts/mint_token.sh" "$repo" "$label"
    ;;
  revoke)
    token="${1:-}"
    [[ -z "$token" ]] && die "usage: bin/token.sh revoke <token>"
    cd "$AUDIT_BOT_ROOT/server"
    DOC_AUDIT_DB="$DB" python3 - "$token" <<'PY'
import os, sys
from app.api import build_app
app = build_app(db_path=os.environ['DOC_AUDIT_DB'])
ok = app.state.audit_state.tokens.revoke(sys.argv[1])
print("revoked" if ok else "not found / already revoked")
PY
    ;;
  list)
    cd "$AUDIT_BOT_ROOT/server"
    DOC_AUDIT_DB="$DB" python3 <<'PY'
import sqlite3, os
db = os.environ['DOC_AUDIT_DB']
con = sqlite3.connect(db)
rows = con.execute(
    "SELECT substr(token_sha256, 1, 12), repo, label, created_at, revoked_at "
    "FROM auth_token ORDER BY created_at DESC"
).fetchall()
if not rows:
    print("(no tokens)")
else:
    print(f"{'sha[:12]':<14} {'repo':<25} {'label':<15} {'created':<22} revoked")
    for sha, repo, label, created, revoked in rows:
        print(f"{sha:<14} {repo:<25} {(label or ''):<15} {created:<22} {revoked or ''}")
PY
    ;;
  ""|-h|--help)
    sed -n '2,11p' "$0"; exit 0 ;;
  *)
    die "unknown subcommand: $sub" ;;
esac
