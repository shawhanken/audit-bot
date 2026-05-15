# 文档审计服务

[English →](./README.md) · [安装部署手册 →](./INSTALL.zh.md)

`refs/common/2026-05-15-doc-audit-service-design.md` 中多维度文档审计服务的
骨架实现。三部分组成：

- **`action/`** — composite GitHub Action 客户端。抽取结构化索引、计算
  资源级 diff、跑确定性规则、dispatch 到服务器、聚合、渲染报告、回写 GitHub。
- **`server/`** — FastAPI 后端。并行编排 5 个维度 agent（一致性 / 安全性 /
  技术可行性 / 架构可行性 / 风格），通过**服务器端仓库镜像**做跨仓库审计、
  结果缓存、预算账本、审计历史。
- **`bin/`** — 一键运维脚本（`up.sh` / `down.sh` / `status.sh` /
  `pull-mirrors.sh` / `token.sh`），见 [INSTALL.zh.md](./INSTALL.zh.md)。

## 审计范围

默认审 CIP 之间一致性。开启**镜像**后，还能审 CIP 与白皮书、CIP 与 `node`
/ `runner` / `cbfs`（任意 sibling 仓库）当前 main 代码之间的一致性：

| 范围 | 机制 |
|---|---|
| CIP ↔ CIP | `paths: [cips/**]` in `.github/doc-audit.yml` |
| CIP ↔ 白皮书 | `paths: [cips/**, whitepaper/**]` |
| CIP ↔ 活代码（`node`、`runner`、`cbfs` …） | `related_code_mirrors: [node, runner, cbfs]` — 服务器从 `~/workspace/<name>/` 读 |

镜像就是 operator 在本机 clone 的普通 git 仓库，靠 `bin/pull-mirrors.sh`
拉取最新——服务器自己不需要 GitHub 凭证。

## 实现状态

设计 §3.1 列出的每个组件都有可工作的实现 + 聚焦的单测（action 46 条，
server 48 条 + 1 条 gated 真实 CLI 冒烟）。相对完整设计被刻意精简的部分：

| 特性 | 状态 |
|---|---|
| 5 个维度 | agent 通过 `claude -p` 子进程驱动 |
| 规则 R001–R009 | 已实现；R001 增加了 within-HEAD 检测 |
| 安全 regex + 危险命令检测 | 已实现（S001–S005 secret，S010–S013 命令）|
| 风格规则 | 已实现（W001 大小写，W002 锚点格式）|
| 技术维度 stage A | 已实现；T001/T003 deterministic 发出 |
| **跨仓库审计（mirrors）** | **已实现；配置项 `related_code_mirrors`** |
| 跨 PR 去重 | 哈希式（G-Eval 语义级去重为后续）|
| 代码索引 | 正则；同时读 request excerpts 与 mirror 文件 |
| 缓存 | 内存 TTL dict + diff-hash 反向缓存 |
| 预算账本 + 软/硬上限 | SQLite + 可插拔 notifier（默认日志桩）|
| Token → 仓库绑定 | SQLite；旧版 `DOC_AUDIT_SERVER_TOKEN` 仍兼容 |
| 审计历史 | SQLite |
| `.doc-audit-ignore` 生效 | 已实现 |
| GitHub Check Runs + sticky 评论 | 通过 `gh api`，含 `DRY_RUN` 测试模式 |
| SARIF 生成 | 已生成；上传委托给 `codeql-action/upload-sarif` |
| `/metrics` Prometheus 端点 | 已实现（counter / gauge / histogram，无外部依赖）|
| **一键运维脚本** | **`bin/up.sh` / `down.sh` / `status.sh` / `pull-mirrors.sh` / `token.sh`** |
| Workflow 模板 | `examples/workflow-doc-audit.yml`（接入方拷贝即用）|
| Docker / K8s 部署 | **未包含**——见 INSTALL.zh.md 的 systemd 单元方案 |
| GitHub Secret Scanning 信号融合 | **未包含**——需要真实 GitHub API |
| `/audit rerun` 等 PR 命令 | **未包含**——workflow 桩已就位，handler 未接 |

## 一键运维命令

```bash
# 起 server + tunnel，并把 tunnel URL 写到指定仓库的 variable。
bin/up.sh --update-repos shawhanken/refs

# 看活性、镜像状态、最近 run、预算。
bin/status.sh shawhanken/refs

# git pull 所有配置的镜像。
bin/pull-mirrors.sh

# 颁发一条仓库绑定的 token。
bin/token.sh mint shawhanken/refs prod

# 全部停掉。
bin/down.sh
```

详细 operator 手册（依赖、镜像、systemd、故障速查）见
[INSTALL.zh.md](./INSTALL.zh.md)。

## 目录布局

```
audit-bot/
├── action/                  # composite-action 客户端
│   ├── action.yml / entrypoint.sh / publish.sh
│   ├── src/
│   │   ├── common/{config,schema,markdown}.py
│   │   ├── rules/{registry,consistency,security,style}.py
│   │   ├── extract_index.py / diff.py / rules_runner.py
│   │   ├── dispatch.py / aggregate.py / report.py
│   └── tests/unit/...
├── server/                  # FastAPI 后端
│   ├── app/
│   │   ├── main.py / api.py
│   │   ├── orchestrator.py
│   │   ├── claude_client.py        # 拆 claude-p envelope
│   │   ├── mirror.py               # 新增：服务器端仓库镜像读取
│   │   ├── agents/{base,consistency,security,technical,architecture,style}.py
│   │   ├── code_index.py           # 同时读 excerpts + mirror 文件
│   │   ├── dedup.py / cache.py / budget.py / storage.py / auth.py / metrics.py / notifier.py
│   └── tests/...
├── bin/                     # 一键运维脚本
│   ├── _lib.sh
│   ├── up.sh                # server + tunnel + 变量同步
│   ├── down.sh              # 全停
│   ├── status.sh            # 活性 + 镜像 + 预算 + 历史
│   ├── pull-mirrors.sh      # 每个镜像 git pull
│   └── token.sh             # mint / revoke / list
├── examples/
│   ├── doc-audit.yml / workflow-doc-audit.yml / run_locally.sh
├── INSTALL.md (+ INSTALL.zh.md)
└── README.md (+ README.zh.md)
```

## 快速上手（不用脚本）

```bash
# Action 侧（46 条）
cd action && python3 -m pytest tests/ -q

# Server 侧（48 条 + 1 条 gated 真实 CLI 冒烟）
cd ../server && python3 -m pytest tests/ -q

# 可选：跑一次真实 `claude` CLI
RUN_REAL_CLAUDE=1 python3 -m pytest tests/test_claude_client.py::test_real_cli_smoke -q
```

API：

- `GET  /v1/health`
- `GET  /v1/metrics` —— Prometheus 文本格式
- `POST /v1/audit` —— 请求体格式见设计 §7.3
- `GET  /v1/audit/{request_id}` —— 查询历史审计
- `GET  /v1/budget/{owner}/{name}` —— 当月预算

**鉴权模式**（按顺序）：
1. `DOC_AUDIT_SERVER_TOKEN` env 设置 → 单一静态 token（旧版 / 本地开发）
2. 通过 `bin/token.sh mint` 颁发的 token → 接受，且 `/v1/audit` 强制校验
   token 绑定的仓库等于请求的仓库
3. 两者都没设置 → 开放模式

## 配置示例

接入方仓库的 `.github/doc-audit.yml`：

```yaml
targets:
  - name: cips
    paths:
      - cips/**
      - whitepaper/**
    # 服务器端镜像名。服务器从 $DOC_AUDIT_MIRROR_ROOT/<name>/ 读
    #（默认 ~/workspace）。服务器自己不需要 GitHub 凭证。
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

## 关于 `claude -p` 子进程路径

按规划阶段确认的选择，Action 端本地降级和 server 端 agent 都通过
`claude -p` 驱动 LLM。真实 CLI 输出：

```json
{"type":"result","is_error":false,
 "result":"<模型回答的字符串>",
 "total_cost_usd":0.012,"usage":{"input_tokens":...,...}}
```

`server/app/claude_client.py` 拆 envelope，把 `is_error: true`（"Not
logged in" 等）当作降级；从 `usage` 抽 cost / token 计数；把 `result`
字符串当 JSON 解析（容忍 ```json 围栏与嵌入数组）。Prompt 走 stdin
而非 argv，避开 GitHub Actions runner 上的 E2BIG。`--tools ""` 禁用
工具调用，防止 PR 文档 prompt-injection 让模型乱读文件。

## 新贡献者的阅读顺序

1. `refs/common/2026-05-15-doc-audit-service-design.md` —— 实现规范。
2. `CLAUDE.md` —— 承重设计要点 / 未完成项 / 跨文件不变式。
3. `INSTALL.zh.md` —— operator 视角的部署。
4. `examples/run_locally.sh` —— 让每个组件跑一遍的最短路径。
5. `action/src/rules/consistency.py` + `tests/unit/test_rules.py` —— 加规则。
6. `server/app/agents/base.py` + `server/app/agents/<dim>.py` —— 加 / 调 agent。
