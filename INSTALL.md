# Doc Audit — Install & Deployment Manual

[中文版 →](./INSTALL.zh.md)

This walks an operator from zero to a working setup like the reference
deployment on `cowboy-001`: an audit server colocated with the source
repos it audits against, exposed to GitHub Actions runners through a
Cloudflare quick tunnel.

Everything is single-machine. If you outgrow this, the schema lines up
with the design's Postgres/Redis/K8s topology — swap one component at a
time.

## 1. Topology

```
GitHub PR ──► Actions runner ─► HTTPS ─► Cloudflare quick tunnel
                                                  │
                                          ┌───────▼───────┐
                                          │   cowboy-001   │
                                          │                │
                                          │  audit-bot/    │  ← this repo
                                          │  server/  + bin/
                                          │  state/  logs/ db
                                          │                │
                                          │  ~/workspace/  │
                                          │   ├─ audit-bot │
                                          │   ├─ node      │  ← mirror
                                          │   ├─ runner    │  ← mirror
                                          │   ├─ cbfs      │  ← mirror
                                          │   └─ refs      │  ← consumer + mirror
                                          └────────────────┘
```

The **mirrors** are plain `git clone`s under one workspace directory. The
audit server resolves `related_code_mirrors: [node, runner, cbfs]` against
that directory — it does not call GitHub, does not need GitHub credentials,
and only reads what the operator allows via the env-scoped allowlist.

## 2. Prerequisites

| Tool | Why | How to install |
|---|---|---|
| Python ≥ 3.11 | server + action scripts | `apt install python3 python3-pip` |
| `uvicorn`, `fastapi`, `pydantic`, `pyyaml`, `requests` | server runtime | `pip install -r server/requirements.txt` |
| `claude` CLI | LLM agents | https://docs.claude.com/en/docs/claude-code/setup ; then `claude /login` |
| `gh` CLI | PR / secret / variable management | https://cli.github.com/ ; then `gh auth login` |
| `jq` | publish.sh uses it | `apt install jq` |
| `git` | the obvious | `apt install git` |
| `cloudflared` *(optional)* | public tunnel for GitHub Actions to reach the server | the `bin/up.sh` first run will tell you where to drop it (see §6) |

## 3. Clone audit-bot and the mirror repos

Pick one parent directory — convention is `~/workspace`. **audit-bot
must be a sibling of every mirror** the bot will audit against:

```bash
mkdir -p ~/workspace
cd ~/workspace
git clone https://github.com/your-org/audit-bot.git
git clone https://github.com/your-org/node.git
git clone https://github.com/your-org/runner.git
git clone https://github.com/your-org/cbfs.git
git clone https://github.com/your-org/refs.git    # the docs being audited
```

If you have a different parent dir, point the bot at it:

```bash
export DOC_AUDIT_MIRROR_ROOT=/srv/audit-mirrors
```

## 4. Install Python deps

```bash
cd ~/workspace/audit-bot/server
pip install -r requirements.txt
```

`pip install --user` is fine; `bin/up.sh` does not need root.

## 5. Verify locally before going public

```bash
cd ~/workspace/audit-bot
action/tests-friendly: cd action  && python3 -m pytest tests/ -q   # 46 tests
                       cd ../server && python3 -m pytest tests/ -q   # 48 tests + 1 skipped
```

If those pass, the bits are sane.

## 6. Tunnel (pick ONE)

The audit server runs on `127.0.0.1:8089`. GitHub Actions runners reach
it through a tunnel. Three options, from easiest to most production-grade:

### 6a. Cloudflare quick tunnel — zero config, NOT for long demos

```bash
mkdir -p ~/bin
curl -sL -o ~/bin/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x ~/bin/cloudflared
~/bin/cloudflared --version
```

`bin/up.sh` defaults to this mode. **Caveats**:
* The hostname rotates on every restart of `cloudflared`. `bin/up.sh` calls
  `gh variable set` to push the new URL to the consumer repo, so PRs
  opened ≥30 seconds after a restart still work.
* Cloudflare aggressively **rate-limits** allocations from a single source
  IP — restarting `cloudflared` many times within minutes will return
  `429 Too Many Requests` and the bot becomes unreachable until the limit
  expires (often hours). Watchdog is configured to never restart on
  tunnel-only failures specifically to avoid tripping this.

Suitable for: local dev, smoke tests, one-off audits.
Not suitable for: a customer demo that needs to be up consistently.

### 6b. Cloudflare named tunnel — stable hostname, free, recommended for demos

Requires a free [Cloudflare account](https://dash.cloudflare.com/sign-up)
and one of:
* a domain you own that's on Cloudflare's nameservers (so the tunnel can
  be DNS-routed to e.g. `audit.example.com`), OR
* willingness to use a `<tunnel-id>.cfargotunnel.com` URL via Cloudflare
  Zero Trust Access (no domain needed, but additional setup).

Once-per-machine setup:

```bash
# 1. (any machine with a browser) authenticate to Cloudflare.
cloudflared tunnel login
# Copy ~/.cloudflared/cert.pem onto cowboy-001 (the audit server host).

# 2. on cowboy-001:
cloudflared tunnel create doc-audit            # writes credentials to ~/.cloudflared/<id>.json
# Note the tunnel UUID it prints.

# 3a. if you have a domain on Cloudflare DNS:
cloudflared tunnel route dns doc-audit audit.your-domain.example
# Then your stable URL is https://audit.your-domain.example

# 3b. without a domain — make `<id>.cfargotunnel.com` work via Zero Trust:
# Configure an Access policy in Cloudflare dash; out of scope here.
```

Switch `bin/up.sh` to named-tunnel mode by setting `DOC_AUDIT_TUNNEL_MODE=named`
in `.state/env` (created by `bin/install-service.sh`) and recording the
stable URL once:

```bash
echo "https://audit.your-domain.example" > ~/workspace/audit-bot/.state/tunnel.url
echo "DOC_AUDIT_TUNNEL_MODE=named"  >> ~/workspace/audit-bot/.state/env
echo "DOC_AUDIT_NAMED_TUNNEL=doc-audit" >> ~/workspace/audit-bot/.state/env
```

`bin/up.sh` will then invoke `cloudflared tunnel run doc-audit` instead
of allocating a quick tunnel, and the consumer-repo variable is updated
to the stable URL.

### 6c. Direct exposure (AWS EC2 etc.) — most production-shaped

If the audit host has a public IP and you have a real domain + TLS cert
(e.g. via Let's Encrypt), expose port 443 → 8089 via nginx and skip the
tunnel entirely. Set `bin/up.sh --no-tunnel` and write the stable URL
to `.state/tunnel.url` manually.

## 7. Bring everything up

```bash
cd ~/workspace/audit-bot

# Mints a token bound to the consumer repo. SAVE THE OUTPUT — DB stores only sha256.
bin/token.sh mint shawhanken/refs prod
# → prints e.g. wo7Wrs...

# One-shot: pull mirrors, start server, start tunnel, set DOC_AUDIT_SERVER_URL.
bin/up.sh --update-repos shawhanken/refs
```

The summary at the end gives you the tunnel URL, log file locations, and
the state directory. State persists across invocations so `bin/up.sh`
re-running is idempotent — already-running components are not restarted.

Then on GitHub, set the token secret:

```bash
gh secret set DOC_AUDIT_TOKEN -R shawhanken/refs -b "<the token from mint>"
```

(`DOC_AUDIT_SERVER_URL` was already set by `--update-repos`.)

## 8. Wire the consumer repo

Inside the consumer repo (e.g. `~/workspace/refs`):

```bash
# Drop the workflow + composite-action copy into the repo.
mkdir -p .github/actions
cp -r ~/workspace/audit-bot/action  .github/actions/doc-audit
cp ~/workspace/audit-bot/examples/workflow-doc-audit.yml \
   .github/workflows/doc-audit.yml
cp ~/workspace/audit-bot/examples/doc-audit.yml \
   .github/doc-audit.yml
```

Edit `.github/workflows/doc-audit.yml`:

```yaml
- uses: ./.github/actions/doc-audit          # local copy, no tag
  with:
    config: .github/doc-audit.yml
    server_url: ${{ vars.DOC_AUDIT_SERVER_URL }}
    server_token: ${{ secrets.DOC_AUDIT_TOKEN }}
    enable_sarif: "false"
```

Edit `.github/doc-audit.yml` to match your repo layout. Example:

```yaml
targets:
  - name: cips
    paths:
      - cips/**
      - whitepaper/**
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

Commit, push, open a PR. The first PR run takes 2-5 min for the 4 LLM
dimensions to complete; subsequent runs reuse prompt cache and are
typically faster.

## 9. Day-to-day operations

```bash
bin/status.sh shawhanken/refs   # full status incl. budget
bin/pull-mirrors.sh             # before re-auditing, get the latest code
bin/down.sh                     # stop everything
```

Daily mirror refresh is a one-liner:

```bash
crontab -l | { cat; echo "0 6 * * * cd ~/workspace/audit-bot && bin/pull-mirrors.sh >> .state/logs/cron.log 2>&1"; } | crontab -
```

## 10. Long-running service (auto-start at boot, self-heal on crash)

One-time install:

```bash
bin/install-service.sh shawhanken/refs[,owner2/repo2]
```

This drops these systemd user units into `~/.config/systemd/user/`:

| Unit | Purpose |
|---|---|
| `doc-audit.service`               | Main service. ExecStart=`bin/up.sh --no-pull`. Idempotent. |
| `doc-audit-watchdog.service`      | Probes `/v1/health` twice (~12s apart); restarts main service on a real failure. **Does not** restart on tunnel-only failures (those can trip the CF 429 limit). |
| `doc-audit-watchdog.timer`        | Fires the watchdog every minute, starting 120s after boot. |
| `doc-audit-pull-mirrors.service`  | Runs `bin/pull-mirrors.sh`. |
| `doc-audit-pull-mirrors.timer`    | Fires daily at 06:00 local. |

For the service to survive a full reboot (without you logging in):

```bash
sudo loginctl enable-linger $USER
```

Verify:

```bash
systemctl --user status doc-audit.service
systemctl --user list-timers doc-audit-*
journalctl --user -u doc-audit.service -n 50 -f
```

To uninstall the systemd units (keeps `bin/up.sh` etc. working manually):

```bash
bin/uninstall-service.sh
```

### Reboot-resilience checklist (demo-grade)

1. `loginctl show-user $USER | grep Linger` shows `Linger=yes`.
2. `systemctl --user is-enabled doc-audit.service` → `enabled`.
3. Use a **named tunnel** (§6b) — quick tunnel URLs rotate and the bot
   becomes unreachable in the 60s window after each reboot.
4. After install, do a full reboot of the machine and confirm with
   `systemctl --user status doc-audit.service` that it came up.

## 11. Token management

```bash
bin/token.sh list                              # repo bindings (no plaintext)
bin/token.sh mint owner/repo [label]           # mint a new token
bin/token.sh revoke <token>                    # immediate revocation
```

After revoking, the next `/v1/audit` call with that token gets 401, and
the consumer-side Action falls into the rules-only degraded path.

## 12. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `bin/up.sh` says `server did not become healthy in 20s` | port collision or pip deps missing | check `.state/logs/server.log` |
| Tunnel URL appears but `curl` from outside times out | DNS propagation; check from a Google DNS resolver | wait 30-60s; if it stays unreachable, restart `cloudflared` |
| Action log shows `warn: server unreachable` | tunnel down or `DOC_AUDIT_SERVER_URL` stale | `bin/up.sh --update-repos owner/repo` |
| Server returns `403 token bound to X, not Y` | token / repo mismatch | re-mint via `bin/token.sh mint owner/repo` |
| Server returns `401 unknown token` | secret value wrong or revoked | check `bin/token.sh list`; set secret with current token |
| LLM dim status=degraded with `Not logged in` | `claude` CLI not authed on server host | `claude /login` |
| Action exits non-zero but no findings in comment | `block` severity gate fired; check Check Runs in the PR | rules R001-R009 use block severity; relax via `severity_gate: warn` |
| 422 on `gh api ... comments` | comment body exceeded 65536 chars | already mitigated; see `_MAX_COMMENT_CHARS` in `report.py` |
| `Argument list too long` errors | a payload (prompt, gh body) put on argv hit E2BIG | already mitigated; if you see it, check `claude_client.py` and `publish.sh` |
| Free-tier private repo Actions stuck queued | minute quota exhausted | make repo public or switch to self-hosted runner |

## 13. Resource cleanup

```bash
bin/down.sh
# To wipe history / budgets / tokens (irreversible):
rm -rf ~/workspace/audit-bot/server/doc_audit.sqlite3
# To shed state-dir artifacts (logs, tunnel URL, pids):
rm -rf ~/workspace/audit-bot/.state
```

## 14. Security boundaries to keep in mind

- Server has no GitHub credentials. The Action holds the GitHub token. If
  you add a server-side webhook handler later, that boundary must be
  re-justified.
- Mirrors are read-only and allowlisted by name. The `RepoMirror` rejects
  any `..` or absolute-path access, but the allowlist is the primary
  defense — don't put secrets-bearing repos in `DOC_AUDIT_MIRRORS`.
- `--tools ""` on the `claude -p` invocation prevents prompt-injected PR
  docs from making the model read arbitrary files.
- Tokens are stored as sha256. Plaintext is only output by `bin/token.sh
  mint` — capture it once.
