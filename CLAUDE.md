# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Skeleton implementation of the multi-dimension document audit service
described in `../refs/common/2026-05-15-doc-audit-service-design.md`. Three
parts:

- **`action/`** — composite GitHub Action client. Python + bash. Stateless;
  every run cold-starts. Extracts a structured index from the changed
  markdown corpus, runs deterministic rule passes, posts the request to the
  server, then aggregates results and publishes back to GitHub.
- **`server/`** — FastAPI backend. Orchestrates five dimension agents
  (consistency / security / technical / architecture / style) in parallel,
  consults server-side **repo mirrors** for cross-repo audits, caches
  results, tracks per-repo budget, and persists audit history in SQLite.
- **`bin/`** — one-click operator scripts (`up.sh`, `down.sh`, `status.sh`,
  `pull-mirrors.sh`, `token.sh`). See `INSTALL.md`.

The two halves communicate via the JSON schemas in
`action/src/common/schema.py` (dataclasses) ↔ `server/app/schema.py`
(pydantic). Keep these in sync — schema changes have to land in both files.

## Common commands

```bash
# Action-side tests (46 unit tests).
cd action && python3 -m pytest tests/ -q

# Server-side tests (48 tests + 1 gated real-CLI smoke).
cd server && python3 -m pytest tests/ -q

# Single test by node id.
python3 -m pytest tests/unit/test_rules.py::test_r001_opcode_collision_within_head_when_pr_introduces_duplicate -q

# Enable the real `claude -p` smoke test (costs tokens; needs a logged-in CLI).
RUN_REAL_CLAUDE=1 python3 -m pytest server/tests/test_claude_client.py::test_real_cli_smoke -q

# Operator shortcuts (preferred over starting components by hand).
bin/up.sh --update-repos owner/repo    # pull mirrors, start server + tunnel
bin/status.sh owner/repo                # liveness, mirrors, budget, recent runs
bin/pull-mirrors.sh                     # git pull all configured mirrors
bin/down.sh                             # stop server + tunnel

# Drive the Action manually against a synthetic PR (spins up the server too).
./examples/run_locally.sh
```

The action expects to be invoked through `action/entrypoint.sh`, which reads
GitHub-Actions-style env (`GITHUB_WORKSPACE`, `RUNNER_TEMP`,
`GITHUB_BASE_SHA`, `GITHUB_HEAD_SHA`, `GITHUB_PR_NUMBER`,
`GITHUB_REPOSITORY`) and falls back to sensible defaults for local runs.

## Architecture facts that aren't obvious from one file

### Server has no GitHub credentials, ever

A load-bearing design decision. The Action holds the GitHub token and does
all GitHub I/O; the server holds the LLM token and does compute. To respect
this boundary the Action uploads `documents` (markdown content) and
`related_code_excerpts` in the request body — the server never clones
the repo. When extending, do not add `git clone` / `gh api` to the server.

### Server-side `RepoMirror` is the cross-repo audit primitive

`server/app/mirror.py` reads from sibling git clones the operator keeps
under `$DOC_AUDIT_MIRROR_ROOT` (default `~/workspace`). A consumer repo's
`.github/doc-audit.yml` declares which mirrors to consult per target:

```yaml
related_code_mirrors: [node, runner, cbfs]
```

The Action forwards the list of names (not file contents); the server's
`TechnicalAgent.prepare_inputs` resolves them against the local mirror
root and merges the files into the `CodeIndex` alongside the action's
`related_code_excerpts`. This sidesteps the GitHub-credentials boundary
because the mirrors are *local filesystem reads* — the operator (not the
server) is responsible for `git pull`-ing them via `bin/pull-mirrors.sh`.

The `RepoMirror` security model has two layers:
1. **Allowlist by name** — only directories listed in `available` are
   readable, even if other subdirs exist under the mirror root.
2. **Path containment** — `..` traversal and absolute paths are rejected;
   every read resolves under the named mirror's base.

### Why the LLM is driven through `claude -p`, not the SDK

User chose this during scoping. `server/app/claude_client.py` shells out to
the `claude` CLI with `--bare --tools "" --output-format json`. The real
CLI returns an envelope `{"type":"result","is_error":bool,"result":"<answer
text>",...}`; `claude_client.invoke()` unwraps the envelope, then parses the
`result` field (which is itself a string containing the model's JSON answer).
A future swap to the Anthropic SDK only touches `claude_client.py` and the
agent base class.

Tests monkey-patch `claude_client._runner` to inject fake stdout, which can
be either a real-shape envelope or a bare JSON payload — `_try_envelope` and
`_extract_json` accept both.

### Rule registry is decorator-based

Adding a rule = adding one decorated function in `action/src/rules/`.
`@rule("R0XX_short_name", "consistency")` registers it; `rules_runner.py`
finds every registered rule, filters by enabled dimensions, and runs them.
Each rule receives a `RuleContext` with `base_index`, `head_index`, `diff`,
`changed_files`, `repo_root`, `target_name`. Returning an iterable of
`Finding` is the rule's only contract.

### R001 has two firing modes

Most rules operate on `diff.by_kind[...].added` (resource-level diff). But
the diff is **keyed by resource id**, which collapses multi-file
occurrences of the same id into a single map entry. R001 therefore has a
second pass operating directly on `head_index["opcodes"]`: same opcode id
in two distinct HEAD files, with at least one file in `changed_files`. This
within-HEAD case is the one the demo fixture hits (CIP-5 0x42 vs CIP-29
0x42). When writing similar "collision" rules, decide up front whether you
need the within-HEAD pass too.

### Aggregation rules: validate → drop → downgrade → dedup

`action/src/aggregate.py` is the only place that decides what reaches the
report:

1. Every semantic finding's `locations[].file` must exist on disk and
   `line_start` must be a real line. Invalid → dropped.
2. If >30% of a dimension's findings get dropped, the entire dimension is
   marked `low_confidence` and surviving findings have `confidence ≤ 0.5`.
3. Cross-dimension dedup by `(file, line_start)` — keep highest severity,
   others become `related_findings` on the primary.

If you change validation semantics, make sure `test_aggregate.py` covers
the new behaviour — it's the contract the report layer relies on.

### Severity-gate logic

A dimension's `severity_gate` (`block` | `warn` | `off`) only affects
whether the Check Run conclusion is `failure`. The dimension's actual
findings still appear in the sticky PR comment regardless. Action exit code
is non-zero iff at least one finding's dimension has `severity_gate=block`
AND its severity is `block`.

### Three layers in security dimension

§5.3 of the design says three layers: external scanners (not implemented
in this skeleton), regex rules (`action/src/rules/security.py`), LLM
(`server/app/agents/security.py`). The LLM agent receives the rule
findings via `rules_findings_already_reported` in the prompt body so it
can dedup. When adding to one layer, check the other two for overlap.

### Per-target loop in `entrypoint.sh`

`.github/doc-audit.yml` can declare multiple targets. `entrypoint.sh`
iterates targets and runs the full pipeline (extract → diff → rules →
dispatch → aggregate → report → publish) per target. Outputs are suffixed
`_<target>` so they don't collide. Targets with empty path-filter
intersection short-circuit before calling the server.

### Publish is best-effort with a dry-run mode

`action/publish.sh` posts the sticky comment + Check Runs via `gh api`.
`DOC_AUDIT_DRY_RUN=1` redirects every intended `gh` call to a JSON log
(one line per call) instead of executing. The test suite uses this mode;
the example walkthrough sets it on so you can see what would be posted.
SARIF upload is intentionally NOT implemented here — the workflow file is
expected to use `github/codeql-action/upload-sarif`.

## What's deliberately NOT implemented (skeleton scope)

- Real Anthropic SDK path (subprocess only).
- Tree-sitter code index (regex-based in `server/app/code_index.py`).
- Redis (in-process TTL dict in `server/app/cache.py`).
- Postgres (SQLite in `server/app/{storage,budget}.py` — schemas are
  Postgres-compatible).
- GitHub Secret Scanning fusion (would live in the security agent).
- G-Eval LLM-based finding dedup (hash-only in `server/app/dedup.py`).
- `/audit rerun` / `/audit ignore` PR commands.
- Docker / K8s deployment manifests.

Each of these has a well-defined extension point — adding them shouldn't
require restructuring.

## Style notes that show up across the codebase

- All file paths in findings are repo-relative POSIX (`refs/cips/cip-1.md`,
  never absolute).
- Line numbers are 1-based throughout (matches Markdown/GitHub).
- Agents must NOT report findings outside `changed_files`. Aggregation
  trusts this; if you change it, update `aggregate.py`.
- Dimension names are the lowercase strings `consistency | security |
  technical | architecture | style`. Add new ones in `VALID_DIMENSIONS`
  (action) and the `DimensionName` Literal (server) — both must agree.
