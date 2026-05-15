# Doc Audit Service

[中文版 →](./README.zh.md) · [Install / Deployment Manual →](./INSTALL.md)

Skeleton implementation of the multi-dimension document audit service
described in `refs/common/2026-05-15-doc-audit-service-design.md`. Two
halves, plus an operator script bundle:

- **`action/`** — composite GitHub Action client. Extracts a structured
  index from changed markdown, computes a resource-level diff, runs
  deterministic rule passes, dispatches to the server, aggregates, renders
  the report, and publishes back to GitHub.
- **`server/`** — FastAPI backend. Orchestrates five dimension agents
  (consistency / security / technical / architecture / style) in parallel,
  consults **server-side repo mirrors** for cross-repo audits, caches
  results, tracks per-repo budget, and persists audit history.
- **`bin/`** — one-click operator scripts (`up.sh`, `down.sh`,
  `status.sh`, `pull-mirrors.sh`, `token.sh`). See [INSTALL.md](./INSTALL.md).

## Audit scope

Out of the box the bot audits CIPs against each other. With **mirrors** it
also audits CIPs against the whitepaper and against the *current* source
of `node`, `runner`, `cbfs` (or any other sibling git repo):

| Scope | Mechanism |
|---|---|
| CIP ↔ CIP | `paths: [cips/**]` in `.github/doc-audit.yml` |
| CIP ↔ Whitepaper | `paths: [cips/**, whitepaper/**]` |
| CIP ↔ live code (`node`, `runner`, `cbfs`, …) | `related_code_mirrors: [node, runner, cbfs]` — server reads from `~/workspace/<name>/` |

The mirrors are plain git clones the operator keeps fresh via
`bin/pull-mirrors.sh` (no GitHub credentials needed by the server itself).

## Status

Every component listed in design §3.1 has a working implementation along
with focused unit tests (action: 46, server: 48 + 1 gated real-CLI smoke).
Pieces deliberately kept minimal vs. the full design:

| Feature                              | Status                                                          |
|--------------------------------------|-----------------------------------------------------------------|
| All 5 dimensions                     | Implemented as agents calling `claude -p` subprocess            |
| Rules R001–R009                      | Implemented; R001 has within-HEAD detection                     |
| Security regex + dangerous cmds      | Implemented (S001–S005 secrets, S010–S013 commands)             |
| Style rules                          | Implemented (W001 casing, W002 anchor format)                   |
| Technical stage A (code-index lookup) | Implemented; surfaces T001/T003 deterministically               |
| **Cross-repo audit via mirrors**     | **Implemented; `related_code_mirrors` in config**                |
| Cross-PR dedup                       | Hash-based (G-Eval LLM dedup is a follow-on)                    |
| Code index                           | Regex-based; reads request excerpts + mirror files              |
| Cache                                | In-memory TTL dict + diff-hash negative cache                   |
| Budget ledger + soft/hard breach     | SQLite + pluggable notifier (logging stub by default)           |
| Token → repo binding                 | SQLite-backed; legacy `DOC_AUDIT_SERVER_TOKEN` still works      |
| Audit history                        | SQLite                                                          |
| `.doc-audit-ignore` enforcement      | Implemented                                                     |
| GitHub Check Runs + sticky comment   | Implemented via `gh api`, `DRY_RUN` mode for tests              |
| SARIF generation                     | Implemented; upload delegated to `codeql-action/upload-sarif`   |
| `/metrics` Prometheus endpoint       | Implemented (counters, gauges, histograms; zero external dep)   |
| **One-click operator scripts**       | **`bin/up.sh`, `down.sh`, `status.sh`, `pull-mirrors.sh`, `token.sh`** |
| Workflow template                    | `examples/workflow-doc-audit.yml` (drop-in for consumer repos)  |
| Docker / K8s deployment              | **Not included** — see INSTALL.md for systemd unit              |
| GitHub Secret Scanning fusion        | **Not included** — needs live GitHub API                        |
| `/audit rerun` etc. PR commands      | **Not included** — workflow stub present, handler not wired     |

## One-click operations

```bash
# Bring up server + tunnel, point your consumer repo at the new tunnel URL.
bin/up.sh --update-repos shawhanken/refs

# Inspect liveness, mirror status, recent runs, and budget.
bin/status.sh shawhanken/refs

# Pull latest code into all configured mirrors.
bin/pull-mirrors.sh

# Mint a new repo-bound token.
bin/token.sh mint shawhanken/refs prod

# Shut everything down.
bin/down.sh
```

See [INSTALL.md](./INSTALL.md) for the operator manual (deps, mirrors,
systemd, troubleshooting).

## Layout

```
audit-bot/
├── action/                  # composite-action client
│   ├── action.yml
│   ├── entrypoint.sh
│   ├── publish.sh
│   ├── src/
│   │   ├── common/{config,schema,markdown}.py
│   │   ├── rules/{registry,consistency,security,style}.py
│   │   ├── extract_index.py / diff.py / rules_runner.py
│   │   ├── dispatch.py / aggregate.py / report.py
│   └── tests/unit/...
├── server/                  # FastAPI backend
│   ├── app/
│   │   ├── main.py / api.py
│   │   ├── orchestrator.py
│   │   ├── claude_client.py        # claude -p envelope unwrap
│   │   ├── mirror.py               # NEW: server-side repo mirror reader
│   │   ├── agents/{base,consistency,security,technical,architecture,style}.py
│   │   ├── code_index.py           # consumes excerpts + mirror files
│   │   ├── dedup.py / cache.py / budget.py / storage.py / auth.py / metrics.py / notifier.py
│   └── tests/...
├── bin/                     # one-click operator scripts
│   ├── _lib.sh              # shared helpers
│   ├── up.sh                # server + tunnel + variable propagation
│   ├── down.sh              # stop both
│   ├── status.sh            # liveness + mirrors + budget + history
│   ├── pull-mirrors.sh      # git pull each mirror
│   └── token.sh             # mint / revoke / list tokens
├── examples/
│   ├── doc-audit.yml
│   ├── workflow-doc-audit.yml
│   └── run_locally.sh
├── INSTALL.md (+ INSTALL.zh.md)
└── README.md (+ README.zh.md)
```

## Quick start (without operator scripts)

```bash
# Action side (46 tests)
cd action && python3 -m pytest tests/ -q

# Server side (48 tests + 1 gated real-CLI smoke)
cd ../server && python3 -m pytest tests/ -q

# Optional: hit the real `claude` CLI once (costs tokens; needs a logged-in CLI)
RUN_REAL_CLAUDE=1 python3 -m pytest tests/test_claude_client.py::test_real_cli_smoke -q
```

Server endpoints:

- `GET  /v1/health`
- `GET  /v1/metrics` — Prometheus text format
- `POST /v1/audit` — body shape: design §7.3
- `GET  /v1/audit/{request_id}` — recall a past audit
- `GET  /v1/budget/{owner}/{name}` — current monthly spend / cap

**Auth modes** (checked in order):
1. `DOC_AUDIT_SERVER_TOKEN` env set → single static token (legacy / dev).
2. Token issued via `bin/token.sh mint` → accepted, and `/v1/audit` enforces
   that the token's bound repo matches the request's repo.
3. Neither set → open mode (local dev convenience).

## Configuration

A consumer repo's `.github/doc-audit.yml`:

```yaml
targets:
  - name: cips
    paths:
      - cips/**
      - whitepaper/**
    # Names of server-side mirrors to consult during this audit.
    # The server reads from $DOC_AUDIT_MIRROR_ROOT/<name>/ (default ~/workspace).
    # No GitHub credentials needed by the server.
    related_code_mirrors:
      - node
      - runner
      - cbfs
    dimensions:
      consistency:  { enabled: true, severity_gate: warn }
      security:     { enabled: true, severity_gate: warn }
      technical:    { enabled: true, severity_gate: warn }
      architecture: { enabled: true, severity_gate: warn }
      style:        { enabled: false }

global:
  max_usd_per_run: 2.00
  comment_marker: doc-audit-bot
  ignore_file: .doc-audit-ignore
```

## Design correspondence

| Design § | Component                                |
|----------|------------------------------------------|
| §3.1     | `action/entrypoint.sh` + `server/app/api.py` |
| §4       | `examples/doc-audit.yml`, `action/action.yml` |
| §5.2     | `action/src/rules/consistency.py` (R001–R009) |
| §5.3     | `action/src/rules/security.py`, `server/app/agents/security.py` |
| §5.4     | `server/app/agents/technical.py` + `server/app/code_index.py` + `server/app/mirror.py` |
| §5.5     | `server/app/agents/architecture.py` |
| §5.6     | `action/src/rules/style.py`, `server/app/agents/style.py` |
| §6.1     | `action/src/{extract_index,diff,rules_runner,dispatch,aggregate,report}.py`, `action/publish.sh` |
| §6.2     | `server/app/{orchestrator,agents/*,code_index,mirror,dedup,cache,budget,storage,auth,metrics,notifier}.py` |
| §7       | `action/src/common/schema.py` + `server/app/schema.py` |
| §8       | `action/src/report.py` + `action/publish.sh` |
| §9       | `action/src/dispatch.py` (`local_fallback`); per-dim degradation in `server/app/orchestrator.py` |

## Notes on the `claude -p` subprocess path

Per the user's choice during scoping, both Action-side fallback and
server-side agents drive the LLM via the `claude -p` headless CLI rather
than the Anthropic SDK directly.

`claude -p --output-format json` returns:

```json
{"type":"result","subtype":"success","is_error":false,
 "result":"<model answer as a string>",
 "total_cost_usd":0.012,"usage":{"input_tokens":...,"output_tokens":...},...}
```

`server/app/claude_client.py` unwraps the envelope, surfaces `is_error: true`
as a degraded result, captures cost/token counts from `usage`, then parses
the `result` string as JSON (tolerant of ```json fences and embedded
arrays). The prompt is piped via stdin (not argv) to avoid E2BIG on
GitHub Actions runners. `--tools ""` blocks tool use so prompt-injection
in PR docs cannot make the model read arbitrary files.

## Reading order for new contributors

1. `refs/common/2026-05-15-doc-audit-service-design.md` — the spec this implements.
2. `CLAUDE.md` — load-bearing design facts, deferred items, invariants.
3. `INSTALL.md` — operator-level setup.
4. `examples/run_locally.sh` — shortest path to seeing every component fire.
5. `action/src/rules/consistency.py` + `tests/unit/test_rules.py` — adding a rule.
6. `server/app/agents/base.py` + `server/app/agents/<dim>.py` — adding/tuning agents.
