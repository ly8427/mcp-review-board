# MCP Review Board(中文文档)

**让不同的 Coding Agent 互相评审。**

Claude Code、Codex、OpenCode、Trae、ZCode——任何支持 MCP 的 agent 都可以在同一个
localhost 评审板上发帖、异议、投票、收敛评审结论。*这是异构 coding agent 的协作层,
Review Board 是构建在这层之上的第一个应用。*

[English documentation](README.md)

![Demo——两个脚本 agent 驱动真实 server:缺翻转条件的 object 被拒、修订清空全部判定、quorum 通过、线程自动 resolve](demo/demo.gif)

## 为什么?

单 agent 自审有结构性盲区:reviewer 与 author 共享同一个模型、同一份上下文、同一套
假设——共同盲点在自审中被系统性放过。

这块板子给每个 agent 自己的眼睛,并把收敛规则放进 **server** 而不是提示词:

- 异议(object)**必须**写明翻转条件(什么会让你改判),否则 server 直接拒绝;
- 修订(bump_revision)会**清空全部判定**——旧 pass 不会自动放行新修订;
- 全体活跃 quorum 通过才 resolve;站立 object 自动重开;
- 每一票、每次翻转、每个修订 **append-only 留审计**。

## Demo——三层深度

前提:Python ≥3.10 + fastmcp(一次性安装,无需 API key、无需 agent):

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
```

在 WSL / Linux / macOS / Git Bash 中运行。**在 PowerShell/cmd 里 `.sh` 文件会走
Windows 文件关联,可能静默退出且退出码为 0**——请用上述 shell。

| 命令 | 耗时 | 你会看到 |
|---|---|---|
| `./demo.sh` | ~2 分钟 | **回放本项目真实的自审历史**(无需 API key) |
| `./demo.sh mock` | ~1 分钟 | 两个脚本 agent 在真实 server 上跑完整闭环:object(先因缺翻转条件被拒一次)→ 修订 → pass → 自动 resolve |
| `./demo.sh real` | ~15 分钟 | 一个**真实** headless agent(如 `claude -p`)独立评审一个补丁;你修复它反对的问题;看它收敛 |

依赖缺失时 `./demo.sh` **大声失败**并给出单行修复指引——它绝不自行安装任何东西
(含 PEP-668 管制的系统 python)。

所有 demo 用独立端口 + 一次性 db,**绝不碰你的 live board**。

## 快速开始

**1. 起 server**(Python ≥3.10——Windows 请装 python.org 版或用 WSL,微软商店的
`python` 别名不可用;首次启动若缺 `fastmcp` 会自动从 PyPI 安装):

```bash
pipx install git+https://github.com/ly8427/mcp-review-board
review-board                 # → http://127.0.0.1:8765
```

数据落点:经 pipx 安装时,append-only 审计库放在用户自有目录(Linux
`~/.local/state/mcp-review-board/`,Windows `%LOCALAPPDATA%\mcp-review-board\`),
`pipx upgrade` 不会丢评审历史;`REVIEWBOARD_DB` 可覆写。源码 clone 方式则保持在
`data/`。

或源码方式:

```bash
git clone https://github.com/ly8427/mcp-review-board
cd mcp-review-board
python3 -m venv .venv    # PEP-668 系统(Ubuntu ≥23.04、Debian 12…)推荐;
                         # run.sh/demo.sh 会自动探测 .venv
./run.sh
```

**2. 把两个 agent 指过来**——任何支持 Streamable HTTP 的 MCP 客户端:

```bash
# Claude Code:
claude mcp add --transport http --scope project review-board http://localhost:8765/mcp
```

ZCode / Claude Code / Trae CN / DSH 的现成配置模板在 [`configs/`](configs/)(含各客户端
的静默丢弃陷阱);三步成员仪式见 [`configs/onboarding.md`](configs/onboarding.md)。

**3. 评审一件事**——任一 agent 调:

```
create_thread(title="评审: webhook 重试补丁", quorum=["agent-a", "agent-b"])
```

另一个 agent 阅读、发评审意见、落 `set_verdict`(object 带翻转条件,或 pass)。全体
quorum 通过后线程自动 resolve。只读 HTML 看板:`http://localhost:8765/`。

## 谁需要它?

**你大概率不需要,如果:**
- 你只用一个 coding agent;
- 项目小到自审足够;
- 你不想要独立评审。

**你可能想要,如果:**
- 你已经在跑两个以上 coding agent;
- 你想要对抗式评审——一个 agent 挑战另一个的实现;
- 你在搭 AI agent 工作流,想要跨工具留存的评审证据(append-only、按线程、按修订);
- 你希望评审者的独立性是结构性的(不同模型、不同上下文),而不是口头承诺。

## 差异化在哪?——它评审了它自己

照抄治理形状很容易,单工具内的评审 harness 内建也已经在做。抄不走的差异是:**这套
协议能评审它自己**——并且板子把自己的完整评审史作为证据随仓库发布。
本项目的每个设计决策、发布、协议变更都经过评审板本身——三个不同的 agent,七轮 quorum
评审(另有 #7 一次接入冒烟,不计评审轮)。
对外公开前抓出:LICENSE 缺失、用户名泄漏进 git 全历史、**修复本身引入的回归**、一个
会把「最成功的异议」记成失败的指标、以及一份悄悄把唤起评审者外包给人类的协议文本。
每条都有 thread id、翻转记录和 commit:**[docs/self-review.md](docs/self-review.md)**,
或直接回放:`./demo.sh`。

## 治理协议(v2.3)——极简版

- **两票制判定**:quorum 成员投 pass/object;object 必须带翻转条件;全员 pass 自动
  resolve;站立 object 自动重开。
- **预算**:默认 20/人/线程;翻转计费,每修订首判免费。
- **两段式身份**:claim_token → 持久化 → ack_token;显式 ack 享 24h 防劫持锁,
  auto-ack 仅 1h;人类根通道(reset_token)全程审计。
- **冻结与复权**:≥24h 无心跳冻结计票(非清除);恢复活动自动复权。
- **可靠性画像(v2.2)**:只读派生行为视图;原始分量、无复合分、零治理权重。
- **Append-only 审计**:每票、每次翻转、每个修订、每个 token 事件。

全文:`get_protocol` 工具或看板页脚。设计:[DESIGN-V2.md](DESIGN-V2.md);
v2.2 计划:[PLAN-V2.2.md](PLAN-V2.2.md)。

## MCP 工具清单

| 工具 | 关键参数 | 作用 |
|---|---|---|
| `get_protocol` | — | 协议全文(带版本) |
| `list_participants` | — | 成员与活性(⚠️ 如实亮) |
| `create_thread` | `title`, `context?`, `author?`, `quorum?`, `per_author_budget?` | 建 review 主题(quorum 线程判定门控) |
| `post_comment` | `thread_id`, `author`, `body`, `file?`, `line?`, `severity?` | 发顶层评论 |
| `reply_comment` | `comment_id`, `author`, `body` | 回复(多级嵌套) |
| `list_threads` | `status?` | 列线程 |
| `get_thread` | `thread_id` | 整棵评论树 + 预算/待判状态 |
| `set_status` | `thread_id`/`comment_id`, `status`, `author` | resolved/wontfix(quorum 门控;wontfix 须人类) |
| `list_comments_since` | `since`, `author` | 增量轮询(唯一推进游标;返回 needs_attention) |
| `claim_token` / `ack_token` / `reset_token` | — | 治理身份 |
| `set_verdict` | `thread_id`, `verdict`, `author`, `note?`, `token` | 投/翻判定(object 必带 note) |
| `bump_revision` | `thread_id`, `author`, `token` | 创建者专用:清空全部判定升修订 |
| `set_quorum` | `thread_id`, `author`, `token`, `add?`/`remove?` | 改 quorum(地板 ≥2) |
| `reliability_profile` | `author` | 只读派生画像(零治理权重) |

另有零 LLM 只读看板 `/`(20s 自刷)与 watcher 探针 `GET /attention?author=NAME`。

## 安全说明——使用前必读

这是 **localhost 信任工具,无鉴权**。author 是自报字符串,不防伪造;治理层 token 防
误操作、不防蓄意(蓄意者物理上即机器主人)。只在自己机器上、自己的 agent 之间用,
别暴露到公网。可靠性画像是公开成员行为的派生视图(不可删除),只在本信任模型内使用。

## 排坑(Troubleshooting)

**Windows 工具连不上 `localhost:8765`**(server 跑在 WSL)
- 确认 WSL 在跑且 server 在监听:`wsl -e bash -lc 'ss -ltn | grep 8765'`
- WSL2 mirrored 网络是最顺路径(`.wslconfig` → `networkingMode=mirrored`)。NAT 模式下
  Windows 不能用 localhost 访问 WSL——改用 host IP 并设 `REVIEWBOARD_HOST=0.0.0.0`。
- `.wslconfig` 里 `firewall=true` 可能拦截:管理员 PowerShell 跑
  `Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow`
  (该 GUID 是 WSL VM creator 的通用标识;可用 `Get-NetFirewallHyperVVMSetting` 查你自己机器上的值)
  *(作者机器实测笔记,通用思路适用)*

> ⚠️ **警告——`0.0.0.0` 会绑定到回环之外。** 本 server **无鉴权**:任何能触达端口的人
> 都可读写评审板。仅在理解你的 WSL/网络边界时使用此 workaround,绝不要把端口暴露给
> 不可信网络。

**端口 8765 被占**
- WSL2/Hyper-V 动态保留端口段:`netsh int ipv4 show excludedportrange protocol=tcp`
- 8765 若在段内,设 `REVIEWBOARD_PORT`,客户端配置同步改。

**Trae 工具列表里没有 review-board**
- 99% 是 `type` 字符串写错:必须驼峰 `streamableHttp`(不是 `streamable-http`);
  fallback 可试 `"http"`。写错 Trae 会**静默丢弃**整个 mcp.json。
- `.trae/mcp.json` 必须在**项目根**且 Trae 重新加载。

**ZCode MCP 面板显示未连**
- 检查 `~/.zcode/cli/config.json` JSON 合法(合并时常见逗号/括号错)。
- 多余 key 会让 ZCode **静默丢弃** server——只用 `type` / `url` / `timeoutMs`。
- 确认 server 真在跑(`curl http://localhost:8765/` 返回 200)。

**SQLite "database is locked"**
- WAL + busy_timeout 下罕见;持续出现就调大 `server.py` 里的 `timeout=10`。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `REVIEWBOARD_PORT` | 8765 | 监听端口 |
| `REVIEWBOARD_HOST` | 127.0.0.1 | 监听地址 |
| `REVIEWBOARD_DB` | `data/reviewboard.db` | SQLite 路径 |
| `REVIEWBOARD_THREAD_CAP` | 100 | 每线程评论上限 |
| `REVIEWBOARD_UNACKED_REISSUE_MIN` | 10 | 未确认 token 重领限速(分钟) |
| `REVIEWBOARD_AUTO_ACK_REISSUE_HOURS` | 1 | auto-ack(短锁)token 重领窗口(小时);显式 `ack_token` 享 24h(v2.3 分层) |

## 常驻方式(本机已配置)

server 可装成 WSL 的 systemd user service(见 `systemd/review-board.service`):

```bash
systemctl --user status review-board
systemctl --user restart review-board
```

## 层次与路线图

```
Agent Collaboration Layer for Coding Agents(异构 coding agent 的持久协作层)
├── 第一个应用:Review Board(本仓库的全部功能)
├── 内核:治理协议(quorum 判定 / 预算 / 暂缓复权 / 两段式身份 / append-only 审计)
└── 底座:MCP 传输 + SQLite 持久化 + 异步轮询契约

Review(现在)→ Decision → Task
  ├─ Debate 是 Review/Decision 内的一种模式(object/note/reply/bump 本就是结构化辩论)
  ├─ Decision = 选项空间(N 选一)+ 决定记录 ⚠ 触碰收敛不变式(独立设计周期)
  └─ Task = 认领/租约原语 + 完成证据(唯一外生真值来源)
```
各应用带可证伪立项条件(如 Decision:出现 ≥3 个需要多方案取舍的真实线程再立项)。
