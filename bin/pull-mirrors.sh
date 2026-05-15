#!/usr/bin/env bash
# Run `git pull` on every configured mirror so the audit server reads
# current code on the next request.
#
# Usage:
#   bin/pull-mirrors.sh                       # pull all $DOC_AUDIT_MIRRORS
#   bin/pull-mirrors.sh node runner           # pull a subset
#
# Failure modes are non-fatal: an offline mirror or a dirty working tree
# logs a warning and the script continues to the next mirror.

set -uo pipefail
source "$(dirname "$0")/_lib.sh"

if [[ $# -gt 0 ]]; then
  MIRRORS="$*"
else
  MIRRORS="$DEFAULT_MIRRORS"
fi

for m in $MIRRORS; do
  d="$WORKSPACE_ROOT/$m"
  if [[ ! -d "$d/.git" ]]; then
    log "$m: not a git repo at $d — skipping"
    continue
  fi
  before="$(git -C "$d" rev-parse --short HEAD 2>/dev/null || echo '?')"
  if ! git -C "$d" diff --quiet --ignore-submodules HEAD 2>/dev/null; then
    log "$m: working tree dirty — skipping pull (commit/stash first)"
    continue
  fi
  out="$(git -C "$d" pull --ff-only 2>&1)"
  rc=$?
  after="$(git -C "$d" rev-parse --short HEAD 2>/dev/null || echo '?')"
  if [[ $rc -eq 0 ]]; then
    if [[ "$before" == "$after" ]]; then
      log "$m: already up to date ($after)"
    else
      log "$m: $before → $after"
    fi
  else
    # First line of git's stderr usually has the actual reason.
    reason="$(echo "$out" | head -1 | sed 's/^fatal: //')"
    log "$m: pull failed — $reason"
  fi
done
