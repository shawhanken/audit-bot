# Doc Audit — 安装部署手册

[English →](./INSTALL.md)

从零搭一套像 `cowboy-001` 上的参考部署：审计服务器与被审计的源代码仓库
同机共存，通过 Cloudflare quick tunnel 暴露给 GitHub Actions runner。

全部单机部署。当超出单机规模时，schema 与设计文档的 Postgres / Redis /
K8s 拓扑对齐——每个组件可单独替换。

## 1. 拓扑

```
GitHub PR ──► Actions runner ─► HTTPS ─► Cloudflare quick tunnel
                                                  │
                                          ┌───────▼───────┐
                                          │  cowboy-001   │
                                          │               │
                                          │  audit-bot/   │  ← 本仓库
                                          │  server/ bin/ │
                                          │  state/ logs/ │
                                          │               │
                                          │  ~/workspace/ │
                                          │   ├─ audit-bot│
                                          │   ├─ node     │  ← 镜像
                                          │   ├─ runner   │  ← 镜像
                                          │   ├─ cbfs     │  ← 镜像
                                          │   └─ refs     │  ← 被审计 + 镜像
                                          └───────────────┘
```

**镜像**就是 workspace 目录下的普通 `git clone`。审计服务器把
`related_code_mirrors: [node, runner, cbfs]` 解析到该目录——不调
GitHub、不需要 GitHub 凭证，仅读 operator 通过 env 显式 allowlist
的内容。

## 2. 依赖

| 工具 | 用途 | 安装 |
|---|---|---|
| Python ≥ 3.11 | server + action 脚本 | `apt install python3 python3-pip` |
| `uvicorn`/`fastapi`/`pydantic`/`pyyaml`/`requests` | server 运行时 | `pip install -r server/requirements.txt` |
| `claude` CLI | LLM agent | https://docs.claude.com/en/docs/claude-code/setup ；之后 `claude /login` |
| `gh` CLI | PR / secret / variable 管理 | https://cli.github.com/ ；之后 `gh auth login` |
| `jq` | publish.sh 用 | `apt install jq` |
| `git` | 显然 | `apt install git` |
| `cloudflared`（可选）| 让 GitHub Actions runner 能访问到 server 的公网入口 | `bin/up.sh` 首次运行时会告诉你放哪儿（见 §6）|

## 3. clone audit-bot 与镜像仓库

选一个父目录，约定俗成是 `~/workspace`。**audit-bot 必须与每个待审计的
镜像仓库同级**：

```bash
mkdir -p ~/workspace
cd ~/workspace
git clone https://github.com/your-org/audit-bot.git
git clone https://github.com/your-org/node.git
git clone https://github.com/your-org/runner.git
git clone https://github.com/your-org/cbfs.git
git clone https://github.com/your-org/refs.git    # 被审计的文档仓库
```

如果用其他父目录，让 bot 知道：

```bash
export DOC_AUDIT_MIRROR_ROOT=/srv/audit-mirrors
```

## 4. 装 Python 依赖

```bash
cd ~/workspace/audit-bot/server
pip install -r requirements.txt
```

`pip install --user` 也行；`bin/up.sh` 不需要 root。

## 5. 上线前先本地跑测试

```bash
cd ~/workspace/audit-bot
cd action  && python3 -m pytest tests/ -q   # 46 条
cd ../server && python3 -m pytest tests/ -q   # 48 + 1 跳过
```

都过就说明基本盘没问题。

## 6. Tunnel（选一种）

审计 server 跑在 `127.0.0.1:8089`。GitHub Actions runner 通过 tunnel
到达。三种方案，按难度递增：

### 6a. Cloudflare quick tunnel — 零配置，**不适合长期演示**

```bash
mkdir -p ~/bin
curl -sL -o ~/bin/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x ~/bin/cloudflared
~/bin/cloudflared --version
```

`bin/up.sh` 默认用这个。**注意**：

* hostname 每次 `cloudflared` 重启就换。`bin/up.sh` 会 `gh variable set`
  自动把新 URL 推到 consumer repo，30s 后开的 PR 仍能正常审。
* Cloudflare 对单一源 IP 的 quick-tunnel 分配有**强 rate limit**——
  几分钟内重启多次会返回 `429 Too Many Requests`，几小时内 bot 不可达。
  Watchdog 故意配置成"tunnel 单独挂了不重启"以避开这个限制。

适合：本地 dev、冒烟、一次性审计。
不适合：客户演示。

### 6b. Cloudflare named tunnel — 稳定 hostname，免费，演示推荐

需要一个免费 [Cloudflare 账户](https://dash.cloudflare.com/sign-up)
和以下二者之一：

* 你拥有的、托管在 Cloudflare DNS 上的域名（这样可以路由到例如
  `audit.example.com`），或
* 接受 `<tunnel-id>.cfargotunnel.com` 形式的 URL，通过 Cloudflare Zero
  Trust Access 暴露（无须域名，但要在 Cloudflare 后台多做一步 Access
  policy 配置）。

一次性安装：

```bash
# 1. （任何有浏览器的机器）登录 Cloudflare
cloudflared tunnel login
# 把生成的 ~/.cloudflared/cert.pem 拷到 cowboy-001

# 2. 在 cowboy-001 上：
cloudflared tunnel create doc-audit            # 在 ~/.cloudflared/<id>.json 留凭证
# 记下打印的 tunnel UUID

# 3a. 有域名（已在 Cloudflare DNS）：
cloudflared tunnel route dns doc-audit audit.your-domain.example
# 你的稳定 URL 就是 https://audit.your-domain.example

# 3b. 没域名 — 让 `<id>.cfargotunnel.com` 通过 Zero Trust Access 工作
# Cloudflare dash 上配 Access policy，本文不展开。
```

切到 named tunnel 模式：

```bash
echo "https://audit.your-domain.example" > ~/workspace/audit-bot/.state/tunnel.url
echo "DOC_AUDIT_TUNNEL_MODE=named"  >> ~/workspace/audit-bot/.state/env
echo "DOC_AUDIT_NAMED_TUNNEL=doc-audit" >> ~/workspace/audit-bot/.state/env
```

`bin/up.sh` 会执行 `cloudflared tunnel run doc-audit` 而非分配 quick
tunnel，consumer repo 的 variable 也会指向稳定 URL。

### 6c. 直接暴露（AWS EC2 等）— 最贴生产形态

如果 audit 主机有公网 IP 和真域名+TLS 证书（如 Let's Encrypt），
用 nginx 把 443 → 8089 即可，完全跳过 tunnel。
`bin/up.sh --no-tunnel`，然后把稳定 URL 手动写进 `.state/tunnel.url`。

## 7. 一键启动

```bash
cd ~/workspace/audit-bot

# 颁发一条仓库绑定的 token。立刻保存输出——DB 只存 sha256。
bin/token.sh mint shawhanken/refs prod
# → 输出一行 token 字符串

# 一条命令：拉镜像 + 起 server + 起 tunnel + 写 DOC_AUDIT_SERVER_URL。
bin/up.sh --update-repos shawhanken/refs
```

末尾摘要给出 tunnel URL、日志位置、state 目录。state 持久化，所以
`bin/up.sh` 重跑是幂等的——已在运行的组件不会被重启。

接着在 GitHub 上设 secret：

```bash
gh secret set DOC_AUDIT_TOKEN -R shawhanken/refs -b "<上一步那个 token>"
```

（`DOC_AUDIT_SERVER_URL` 已由 `--update-repos` 设好。）

## 8. 接入仓库

进入待审计仓库（如 `~/workspace/refs`）：

```bash
# 把 workflow + composite-action 拷进去。
mkdir -p .github/actions
cp -r ~/workspace/audit-bot/action  .github/actions/doc-audit
cp ~/workspace/audit-bot/examples/workflow-doc-audit.yml \
   .github/workflows/doc-audit.yml
cp ~/workspace/audit-bot/examples/doc-audit.yml \
   .github/doc-audit.yml
```

编辑 `.github/workflows/doc-audit.yml`：

```yaml
- uses: ./.github/actions/doc-audit          # 本地拷贝，不带 tag
  with:
    config: .github/doc-audit.yml
    server_url: ${{ vars.DOC_AUDIT_SERVER_URL }}
    server_token: ${{ secrets.DOC_AUDIT_TOKEN }}
    enable_sarif: "false"
```

编辑 `.github/doc-audit.yml` 匹配仓库结构，例：

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

commit、push、开 PR。首次 PR 运行约 2–5 分钟（4 个 LLM 维度并发）；
后续运行 prompt cache 命中通常更快。

## 9. 日常运维

```bash
bin/status.sh shawhanken/refs   # 完整状态含预算
bin/pull-mirrors.sh             # 再次审计前拉最新代码
bin/down.sh                     # 全部停掉
```

每日镜像刷新加个 cron 一行搞定：

```bash
crontab -l | { cat; echo "0 6 * * * cd ~/workspace/audit-bot && bin/pull-mirrors.sh >> .state/logs/cron.log 2>&1"; } | crontab -
```

## 10. 长期服务化（开机自启 + 自愈）

一条命令装好：

```bash
bin/install-service.sh shawhanken/refs[,owner2/repo2]
```

写入 `~/.config/systemd/user/` 下的几个 systemd user unit：

| Unit | 作用 |
|---|---|
| `doc-audit.service`               | 主服务。ExecStart=`bin/up.sh --no-pull`。幂等。 |
| `doc-audit-watchdog.service`      | 探 `/v1/health` 两次（~12s 间隔）；本地真挂了才重启主服务。**不**因 tunnel 单独失败而重启（避免触发 CF 429 限制）。 |
| `doc-audit-watchdog.timer`        | 每分钟触发 watchdog，开机后 120s 起。 |
| `doc-audit-pull-mirrors.service`  | 跑 `bin/pull-mirrors.sh`。 |
| `doc-audit-pull-mirrors.timer`    | 每日 06:00 触发。 |

让 service 在你登出 / 整机重启后仍能自动起：

```bash
sudo loginctl enable-linger $USER
```

验证：

```bash
systemctl --user status doc-audit.service
systemctl --user list-timers doc-audit-*
journalctl --user -u doc-audit.service -n 50 -f
```

要拆掉 systemd 单元（保留 `bin/up.sh` 等手动入口）：

```bash
bin/uninstall-service.sh
```

### 演示级 reboot 韧性 checklist

1. `loginctl show-user $USER | grep Linger` 显示 `Linger=yes`。
2. `systemctl --user is-enabled doc-audit.service` → `enabled`。
3. 用 **named tunnel**（§6b）——quick tunnel URL 会换，重启后 60s
   内 bot 不可达。
4. 装完后真做一次整机 reboot，用 `systemctl --user status doc-audit.service`
   确认起来了。

## 11. Token 管理

```bash
bin/token.sh list                              # 看绑定列表（不含明文）
bin/token.sh mint owner/repo [label]           # 颁发
bin/token.sh revoke <token>                    # 立即撤销
```

撤销后该 token 的下一次 `/v1/audit` 返回 401，consumer 侧 Action
进入 rules-only 降级路径。

## 12. 故障速查

| 现象 | 原因 | 修法 |
|---|---|---|
| `bin/up.sh` 报 `server did not become healthy in 20s` | 端口占用或 pip 依赖缺 | 看 `.state/logs/server.log` |
| Tunnel URL 出来了但外部 curl 超时 | DNS 传播延迟 | 等 30–60 秒；不行就重启 `cloudflared` |
| Action 日志 `warn: server unreachable` | tunnel 挂了或 `DOC_AUDIT_SERVER_URL` 过期 | `bin/up.sh --update-repos owner/repo` |
| 服务器返回 `403 token bound to X, not Y` | token 绑定 repo 错了 | `bin/token.sh mint owner/repo` 重发 |
| 服务器返回 `401 unknown token` | secret 值错了或已撤销 | `bin/token.sh list`；用当前 token 重设 secret |
| LLM 维度 status=degraded、reason 含 `Not logged in` | server 主机上 `claude` CLI 没登录 | `claude /login` |
| Action 退出非零但评论里没 finding | `block` gate 命中了；看 PR 的 Checks | R001-R009 默认 block；改 `severity_gate: warn` 放宽 |
| `gh api ... comments` 返回 422 | 评论 body 超过 65536 字符 | 已缓解；看 `report.py` 里的 `_MAX_COMMENT_CHARS` |
| `Argument list too long` | argv 或 env 大到撞 E2BIG | 已缓解；如复现见 `claude_client.py` 和 `publish.sh` |
| 免费 private repo 的 Actions 卡 queued | 当月 Actions 分钟用光 | 改 public 或换 self-hosted runner |

## 13. 资源回收

```bash
bin/down.sh
# 清掉历史 / 预算 / token（不可逆）：
rm -rf ~/workspace/audit-bot/server/doc_audit.sqlite3
# 清掉 state 目录（日志、tunnel URL、pid）：
rm -rf ~/workspace/audit-bot/.state
```

## 14. 安全边界

- 服务器没有 GitHub 凭证。GitHub token 在 Action 那边。如果你以后加
  webhook handler，要重新审视这条边界。
- 镜像是只读 + 按名 allowlist 的。`RepoMirror` 拒绝任何 `..` 或绝对
  路径访问，但 allowlist 是主防线——别把含 secret 的仓库放进
  `DOC_AUDIT_MIRRORS`。
- `claude -p` 调用带 `--tools ""`，防止 PR 文档里的 prompt-injection
  让模型乱读文件。
- Token 用 sha256 存。明文只在 `bin/token.sh mint` 那一刻输出，
  必须立即抓住。
