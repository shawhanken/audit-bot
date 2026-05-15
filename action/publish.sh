#!/usr/bin/env bash
# Publish a single target's audit artifacts to GitHub.
#
# Inputs (env):
#   GITHUB_REPOSITORY    owner/repo
#   GITHUB_HEAD_SHA      sha to attach check runs to
#   GITHUB_PR_NUMBER     PR number for sticky comment
#   COMMENT_MARKER       e.g. doc-audit-bot
#   DOC_AUDIT_DRY_RUN    when "1", write JSON of intended gh calls to
#                        $DOC_AUDIT_DRY_RUN_LOG instead of invoking gh
#
# Args:
#   $1  target name
#   $2  path to report markdown (sticky comment body)
#   $3  path to check_runs JSON
#   $4  optional path to SARIF
#
# Best-effort: silently skips when `gh` is absent or GITHUB_REPOSITORY is
# unset; failures of individual gh calls are logged but do not abort the
# overall action.

set -euo pipefail

target="$1"
report_md="$2"
check_runs_json="$3"
sarif_path="${4:-}"

repo="${GITHUB_REPOSITORY:-}"
head_sha="${GITHUB_HEAD_SHA:-}"
pr_number="${GITHUB_PR_NUMBER:-0}"
marker="${COMMENT_MARKER:-doc-audit-bot}"

DRY_RUN="${DOC_AUDIT_DRY_RUN:-0}"
DRY_LOG="${DOC_AUDIT_DRY_RUN_LOG:-/dev/stderr}"

log() { echo "[publish:$target] $*" >&2; }

emit_dry() {
  # $1 = json describing the intended gh call
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '%s\n' "$1" >> "$DRY_LOG"
    return 0
  fi
  return 1
}

if [[ -z "$repo" ]]; then
  log "GITHUB_REPOSITORY not set; skipping"
  exit 0
fi
if [[ "$DRY_RUN" != "1" ]] && ! command -v gh >/dev/null; then
  log "gh not on PATH; skipping (set DOC_AUDIT_DRY_RUN=1 to test offline)"
  exit 0
fi

# --- Sticky PR comment -------------------------------------------------------
publish_sticky_comment() {
  [[ "$pr_number" == "0" || -z "$pr_number" ]] && { log "no PR number; skipping comment"; return 0; }
  [[ ! -f "$report_md" ]] && { log "no report md at $report_md"; return 0; }

  # Per-target marker so multiple targets in one PR don't overwrite one another.
  local target_marker="${marker}:${target}"

  # Body MUST never touch argv or env on the GitHub Actions runner: combined
  # with the runner's large existing envp, even a 1KB body can blow E2BIG.
  # Stage everything via tmpfiles + `gh api --input <file>` / `--rawfile`.
  local body_file
  body_file="$(mktemp)"
  cp "$report_md" "$body_file"

  if [[ "$DRY_RUN" == "1" ]]; then
    jq -nc --rawfile body "$body_file" --arg op sticky_comment --arg target "$target" \
       --arg repo "$repo" --argjson pr "$pr_number" --arg marker "$target_marker" \
       '{op:$op, target:$target, repo:$repo, pr:$pr, marker:$marker, body_len:($body|length)}' \
       >> "$DRY_LOG"
    rm -f "$body_file"
    return 0
  fi

  # Find existing comment by marker.
  local existing_id
  existing_id="$(gh api "repos/$repo/issues/$pr_number/comments" --paginate \
    --jq ".[] | select(.body | test(\"<!-- $target_marker -->\")) | .id" | head -1 || true)"

  # Build {"body": "<file content>"} as a tmpfile; pass --input <path>.
  local payload_file
  payload_file="$(mktemp)"
  jq -n --rawfile body "$body_file" '{body: $body}' > "$payload_file"

  if [[ -n "$existing_id" ]]; then
    log "updating sticky comment $existing_id"
    if ! _err="$(gh api -X PATCH "repos/$repo/issues/comments/$existing_id" \
                  --input "$payload_file" 2>&1 >/dev/null)"; then
      log "PATCH comment failed: $_err"
    fi
  else
    log "creating sticky comment"
    if ! _err="$(gh api -X POST "repos/$repo/issues/$pr_number/comments" \
                  --input "$payload_file" 2>&1 >/dev/null)"; then
      log "POST comment failed: $_err"
    fi
  fi

  rm -f "$body_file" "$payload_file"
}

# --- Check Runs --------------------------------------------------------------
publish_check_runs() {
  [[ ! -f "$check_runs_json" ]] && { log "no check_runs at $check_runs_json"; return 0; }
  [[ -z "$head_sha" ]] && { log "no head SHA; skipping check runs"; return 0; }

  python3 - "$check_runs_json" "$repo" "$head_sha" "$DRY_RUN" "$DRY_LOG" <<'PY'
import json, os, subprocess, sys
path, repo, head_sha, dry, dry_log = sys.argv[1:6]
with open(path) as fp:
    runs = json.load(fp)
for run in runs:
    payload = {
        "name": run["name"],
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": run["conclusion"],
        "output": {
            "title": run["name"],
            "summary": run.get("summary", ""),
            "annotations": (run.get("annotations") or [])[:50],
        },
    }
    if dry == "1":
        with open(dry_log, "a") as fp:
            fp.write(json.dumps({"op": "check_run", "repo": repo, "payload": payload}) + "\n")
        continue
    proc = subprocess.run(
        ["gh", "api", "-X", "POST", f"repos/{repo}/check-runs", "--input", "-"],
        input=json.dumps(payload), text=True, capture_output=True,
    )
    if proc.returncode != 0:
        print(f"[publish] check-run failed: {proc.stderr[:200]}", file=sys.stderr)
PY
}

# --- SARIF upload ------------------------------------------------------------
# We deliberately don't implement SARIF upload via raw `gh api`: the GitHub
# upload endpoint wants gzip+base64 and a ref string, and the canonical path
# is `github/codeql-action/upload-sarif@v3`. We leave the SARIF on disk as
# an Actions artifact instead (the upload step lives in the caller's
# workflow file, not here).
publish_sarif_note() {
  if [[ -n "$sarif_path" && -f "$sarif_path" ]]; then
    log "SARIF generated at $sarif_path; upload via github/codeql-action/upload-sarif"
  fi
}

publish_sticky_comment
publish_check_runs
publish_sarif_note
