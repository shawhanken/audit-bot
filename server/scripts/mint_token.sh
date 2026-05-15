#!/usr/bin/env bash
# Mint a doc-audit server token bound to a specific repo.
#
# Usage:
#   scripts/mint_token.sh <owner>/<repo> [label]
#
# Prints the plaintext token on stdout. SQLite stores only the sha256;
# capture the output immediately — there's no way to retrieve it later.
#
# Env overrides:
#   DOC_AUDIT_DB   path to the SQLite file (default: doc_audit.sqlite3 in CWD)

set -euo pipefail

if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
  cat >&2 <<EOF
Usage: $0 <owner>/<repo> [label]

Examples:
  $0 acme/docs
  $0 acme/docs ci-prod
EOF
  exit 64
fi

REPO="$1"
LABEL="${2:-}"

if [[ ! "$REPO" =~ ^[^/]+/[^/]+$ ]]; then
  echo "error: repo must be in <owner>/<repo> form, got '$REPO'" >&2
  exit 64
fi

SERVER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB="${DOC_AUDIT_DB:-$SERVER_DIR/doc_audit.sqlite3}"

cd "$SERVER_DIR"
exec python3 - "$REPO" "$LABEL" "$DB" <<'PY'
import sys
from app.api import build_app

repo, label, db_path = sys.argv[1], sys.argv[2] or None, sys.argv[3]
app = build_app(db_path=db_path)
token = app.state.audit_state.tokens.issue(repo=repo, label=label)
print(token)
PY
