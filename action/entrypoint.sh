#!/usr/bin/env bash
# Doc-audit composite action entrypoint.
#
# Orchestrates the per-target pipeline:
#   extract_index(base) → extract_index(head) → diff → rules → dispatch → aggregate → report
#
# Designed to be runnable both inside a GitHub Action and from a developer
# workstation. When `GITHUB_WORKSPACE` / `RUNNER_TEMP` are unset, defaults
# point at the current directory and a local tmp dir.

set -euo pipefail

ACTION_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${GITHUB_WORKSPACE:-$(pwd)}"
WORK_DIR="${RUNNER_TEMP:-/tmp}/doc-audit"
mkdir -p "$WORK_DIR"

CONFIG_PATH="${INPUT_CONFIG:-.github/doc-audit.yml}"
SERVER_URL="${INPUT_SERVER_URL:-}"
SERVER_TOKEN="${INPUT_SERVER_TOKEN:-}"
ANTHROPIC_KEY_FALLBACK="${INPUT_ANTHROPIC_KEY_FALLBACK:-}"
ENABLE_SARIF="${INPUT_ENABLE_SARIF:-false}"

BASE_SHA="${GITHUB_BASE_SHA:-}"
HEAD_SHA="${GITHUB_HEAD_SHA:-HEAD}"
PR_NUMBER="${GITHUB_PR_NUMBER:-0}"

export PYTHONPATH="${ACTION_DIR}/src:${PYTHONPATH:-}"

CHANGED_FILES="$WORK_DIR/changed_files.txt"
if [[ -n "$BASE_SHA" ]]; then
  git -C "$REPO_ROOT" diff --name-only "$BASE_SHA"..."$HEAD_SHA" > "$CHANGED_FILES" || true
else
  : > "$CHANGED_FILES"
fi

# List target names by parsing the config (simple YAML scan; we don't need
# the full config parser available in shell).
mapfile -t TARGETS < <(python3 - "$REPO_ROOT/$CONFIG_PATH" <<'PY'
import sys, yaml
with open(sys.argv[1]) as fp:
    cfg = yaml.safe_load(fp) or {}
for t in cfg.get("targets", []) or []:
    print(t["name"])
PY
)

PIPELINE_FAILED=0
for target in "${TARGETS[@]}"; do
  echo ">>> target=$target"

  BASE_IDX="$WORK_DIR/index_${target}_base.json"
  HEAD_IDX="$WORK_DIR/index_${target}_head.json"
  DIFF="$WORK_DIR/diff_${target}.json"
  RULES_OUT="$WORK_DIR/findings_rules_${target}.json"
  AGG_OUT="$WORK_DIR/findings_aggregated_${target}.json"
  REPORT_MD="$WORK_DIR/report_${target}.md"
  CHECK_RUNS="$WORK_DIR/check_runs_${target}.json"
  SARIF="$WORK_DIR/doc-audit_${target}.sarif"
  DOC_MANIFEST="$WORK_DIR/manifest_${target}.json"

  # Collect target_paths via YAML inspection.
  python3 - "$REPO_ROOT/$CONFIG_PATH" "$target" "$DOC_MANIFEST" "$REPO_ROOT" <<'PY'
import json, sys, glob, os, yaml
cfg_path, target, manifest_path, repo_root = sys.argv[1:5]
with open(cfg_path) as fp:
    cfg = yaml.safe_load(fp) or {}
tgt = next(t for t in cfg["targets"] if t["name"] == target)
paths = tgt["paths"]
manifest = {}
for p in paths:
    for full in glob.glob(os.path.join(repo_root, p), recursive=True):
        if not os.path.isfile(full) or not (full.endswith(".md") or full.endswith(".mdx")):
            continue
        rel = os.path.relpath(full, repo_root)
        manifest[rel] = ""  # blob sha placeholder
with open(manifest_path, "w") as fp:
    json.dump(manifest, fp)
PY

  # Build extract_index args (one --target-path per glob)
  EXTRACT_ARGS=()
  while IFS= read -r p; do
    EXTRACT_ARGS+=("--target-path" "$p")
  done < <(python3 - "$REPO_ROOT/$CONFIG_PATH" "$target" <<'PY'
import sys, yaml
cfg_path, target = sys.argv[1:3]
with open(cfg_path) as fp:
    cfg = yaml.safe_load(fp) or {}
tgt = next(t for t in cfg["targets"] if t["name"] == target)
for p in tgt["paths"]:
    print(p)
PY
)

  # base index (best-effort via git checkout into a worktree)
  if [[ -n "$BASE_SHA" ]]; then
    BASE_WORKTREE="$WORK_DIR/base_${target}"
    rm -rf "$BASE_WORKTREE"
    git -C "$REPO_ROOT" worktree add --detach "$BASE_WORKTREE" "$BASE_SHA" >/dev/null 2>&1 || true
    python3 -m extract_index --repo-root "$BASE_WORKTREE" --target-name "$target" \
      "${EXTRACT_ARGS[@]}" --ref "$BASE_SHA" --out "$BASE_IDX" || echo "{\"opcodes\":[]}" > "$BASE_IDX"
    git -C "$REPO_ROOT" worktree remove --force "$BASE_WORKTREE" >/dev/null 2>&1 || true
  else
    echo '{"opcodes":[],"addresses":[],"errors":[],"cips":[],"xrefs":[],"terms":[],"code_blocks":[],"code_symbols_referenced":[],"constants":[],"files_parsed":[]}' > "$BASE_IDX"
  fi

  python3 -m extract_index --repo-root "$REPO_ROOT" --target-name "$target" \
    "${EXTRACT_ARGS[@]}" --ref "$HEAD_SHA" --out "$HEAD_IDX"

  python3 -m diff --base "$BASE_IDX" --head "$HEAD_IDX" --changed-files "$CHANGED_FILES" --out "$DIFF"

  python3 -m rules_runner --repo-root "$REPO_ROOT" --config "$REPO_ROOT/$CONFIG_PATH" \
    --target "$target" --base-index "$BASE_IDX" --head-index "$HEAD_IDX" --diff "$DIFF" \
    --changed-files "$CHANGED_FILES" --out "$RULES_OUT"

  # Related-code arg list
  RELATED_ARGS=()
  while IFS= read -r d; do
    RELATED_ARGS+=("--related-code-dir" "$d")
  done < <(python3 - "$REPO_ROOT/$CONFIG_PATH" "$target" <<'PY'
import sys, yaml
cfg_path, target = sys.argv[1:3]
with open(cfg_path) as fp:
    cfg = yaml.safe_load(fp) or {}
tgt = next(t for t in cfg["targets"] if t["name"] == target)
for d in tgt.get("related_code", []) or []:
    print(d)
PY
)

  python3 -m dispatch --config "$REPO_ROOT/$CONFIG_PATH" --target "$target" \
    --repo-owner "${GITHUB_REPOSITORY_OWNER:-local}" \
    --repo-name "$(basename "$REPO_ROOT")" \
    --pr-number "$PR_NUMBER" --base-sha "$BASE_SHA" --head-sha "$HEAD_SHA" \
    --head-index "$HEAD_IDX" --diff "$DIFF" --changed-files "$CHANGED_FILES" \
    --rules-findings "$RULES_OUT" --documents-manifest "$DOC_MANIFEST" \
    --repo-root "$REPO_ROOT" \
    "${RELATED_ARGS[@]}" \
    --server-url "$SERVER_URL" --server-token "$SERVER_TOKEN" \
    --anthropic-key-fallback "$ANTHROPIC_KEY_FALLBACK" --out-dir "$WORK_DIR"

  IGNORE_FILE="$(python3 - "$REPO_ROOT/$CONFIG_PATH" <<'PY'
import sys, yaml
with open(sys.argv[1]) as fp:
    cfg = yaml.safe_load(fp) or {}
print((cfg.get("global") or {}).get("ignore_file", ".doc-audit-ignore"))
PY
)"
  python3 -m aggregate --repo-root "$REPO_ROOT" --rules-findings "$RULES_OUT" \
    --semantic-glob-dir "$WORK_DIR" --target "$target" --ignore-file "$IGNORE_FILE" \
    --out "$AGG_OUT"

  SARIF_FLAGS=()
  if [[ "$ENABLE_SARIF" == "true" ]]; then
    SARIF_FLAGS=("--out-sarif" "$SARIF")
  fi

  if ! python3 -m report --findings "$AGG_OUT" --meta "${AGG_OUT%.json}_meta.json" \
        --config "$REPO_ROOT/$CONFIG_PATH" --target "$target" --out-md "$REPORT_MD" \
        --out-check-runs "$CHECK_RUNS" "${SARIF_FLAGS[@]}"; then
    PIPELINE_FAILED=1
  fi
  echo ">>> target=$target done; report → $REPORT_MD"

  # --- Publish to GitHub (best-effort, skipped when gh is missing) -----------
  COMMENT_MARKER="$(python3 - "$REPO_ROOT/$CONFIG_PATH" <<'PY'
import sys, yaml
with open(sys.argv[1]) as fp:
    cfg = yaml.safe_load(fp) or {}
print((cfg.get("global") or {}).get("comment_marker", "doc-audit-bot"))
PY
)"
  export COMMENT_MARKER
  PUBLISH_ARGS=("$target" "$REPORT_MD" "$CHECK_RUNS")
  [[ "$ENABLE_SARIF" == "true" ]] && PUBLISH_ARGS+=("$SARIF")
  bash "$ACTION_DIR/publish.sh" "${PUBLISH_ARGS[@]}" || true
done

exit $PIPELINE_FAILED
