# MCP Review Board

一个轻量的共享代码 review 看板,让 **ZCode (Windows)** / **Claude Code (WSL)** / **Trae CN (Windows)** 三个异构 AI 编码工具通过 MCP 工具调用发评论、回复、改状态,**彻底去掉人肉复制粘贴**。

三个工具是三个独立 runtime,原生无法互通。这个 server 是它们之间唯一干净的桥梁:都连同一个 localhost 上的 MCP server,共享一个 SQLite review 看板。

## 已验证
- ✅ FastMCP 3.4.6 + Streamable HTTP,server 跑在 WSL(Python 3.12 venv)
- ✅ Windows 工具用 `localhost:8765` 访问到 WSL server(WSL2 mirrored 网络模式)
- ✅ 真实 MCP 协议(JSON-RPC)下 7 个工具全部通过:create_thread / post_comment / reply_comment / list_threads / get_thread(评论树嵌套正确) / set_status / list_comments_since
- ✅ 只读 HTML 看板 `http://localhost:8765/`

---

## 启动(在 WSL 里)

```bash
cd <REPO_ROOT>
./run.sh
```
首次会自动用项目内 `.venv` 装 `fastmcp`(已装好)。看到 `Uvicorn running on http://127.0.0.1:8765` 就 OK。

**自检**:
- 浏览器开 `http://localhost:8765/` → 看到"📋 MCP Review Board"看板(空)
- `curl http://localhost:8765/mcp` → 返回 406(正常,MCP 端点拒绝裸 GET)

Server 一直跑着就行,三个工具各自连接。

## 三工具配置

配置模板在 `configs/` 下,各自贴到对应位置:

### ZCode (Windows)
把 `configs/zcode.mcp.json` 里的 `mcp` 块合并进 `%USERPROFILE%\.zcode\cli\config.json` 顶层(和你已有的 provider/model/plugins 并列)。重启 ZCode 会话,Settings → MCP 看到 `review-board` 已连。

⚠️ **ZCode 静默丢弃陷阱**:多余 key 会让整个 server 被丢弃。只用 `type` / `url` / `timeoutMs`,**别加** `transport`、`command`。

### Claude Code (WSL)
把 `configs/claude-code.mcp.json` 存为 Claude Code 打开的项目根的 `.mcp.json`。或 CLI:
```bash
claude mcp add --transport http --scope project review-board http://localhost:8765/mcp
```
`localhost` 直通(WSL2 mirrored 模式)。首次加载会让你确认连接。

### Trae CN (Windows)
把 `configs/trae.mcp.json` 存为 Trae 打开的项目根的 `.trae/mcp.json`。

⚠️ **Trae 两个静默丢弃陷阱**:
1. `mcpServers` 是**数组**(`[{name,type,url}]`),不是 ZCode/Claude 那样的对象。
2. `type` 必须**驼峰** `streamableHttp`。写成 `streamable-http`(带横杠)或别的值,Trae 会**静默丢弃整个 mcp.json**,工具列表里啥都不出现。若不出现,先试 `type: "http"` 作为 fallback。

---

## 工作流(三个 agent 怎么用)

身份约定:每个 agent 调写操作时带 `author` = 自己的名字(`zcode` / `claude` / `trae`)。建议在各自的 system prompt / 规则里写死。

1. **发起**:任一 agent 调 `create_thread(title="三方 review: auth 模块", context="<代码片段或PR描述>")` → 拿到 thread_id。告诉另外两个,或它们用 `list_threads` 自己发现。
2. **发评论**:各 agent 读码后调
   `post_comment(thread_id=1, author="claude", body="...", file="auth.py", line=42, severity="major")`。
3. **回应**:看到别人的评论,`list_comments_since(since="1h")` 拉增量,然后 `reply_comment(comment_id=1, author="zcode", body="同意,已修复")` 或 `set_status(comment_id=1, status="resolved", author="zcode")`。
4. **汇总**:你(人类)浏览器开 `http://localhost:8765/` 看全貌;或让任一 agent 调 `get_thread(thread_id=1)` 拿到带缩进的完整评论树。
5. **收尾**:全部 resolved 后 `set_status(thread_id=1, status="resolved", author="claude")`。

## 7 个 MCP 工具

| 工具 | 关键参数 | 作用 |
|---|---|---|
| `create_thread` | `title`, `context?` | 建 review 主题 |
| `post_comment` | `thread_id`, `author`, `body`, `file?`, `line?`, `severity?` | 发顶层评论 |
| `reply_comment` | `comment_id`, `author`, `body` | 回复(靠 parent_id 多级嵌套) |
| `list_threads` | `status?`(默认 open) | 列线程 |
| `get_thread` | `thread_id` | 整棵评论树(缩进显示层级) |
| `set_status` | `thread_id` 或 `comment_id`, `status`, `author` | 标 resolved/wontfix |
| `list_comments_since` | `since`(ISO 或 `30m`/`2h`/`1d`) | 增量拉取(轮询别人说了啥) |

---

## 文件结构
```
mcp-review-board/
  server.py          # FastMCP server:7 个 @mcp.tool + 只读 HTML + main
  schema.sql         # 建表(首次启动自动执行)
  requirements.txt   # fastmcp>=3.4
  run.sh             # WSL 启动(自动建 venv)
  run.bat            # Windows 启动(以后迁移用,需先装 Python)
  test_client.py     # 端到端 MCP 协议测试(验证用,非产品)
  configs/           # 三工具的配置模板
    zcode.mcp.json
    claude-code.mcp.json
    trae.mcp.json
  data/              # SQLite db 自动创建(WAL 模式)
    reviewboard.db
  .venv/             # 项目内 venv
```

## 排坑

**Windows 工具连不上 `localhost:8765`**
- 确认 WSL 跑着且 server 在监听:`wsl -e bash -lc 'ss -ltn | grep 8765'`
- 确认 `.wslconfig` 里 `networkingMode=mirrored`(你这台已开)。若哪天改回 NAT 模式,Windows 就不能用 localhost 访问 WSL,得改用 host IP,并把 server 的 `host` 改成 `0.0.0.0`。
- `.wslconfig` 里 `firewall=true` 偶尔会拦:管理员 PowerShell 跑
  `Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow`

**端口 8765 被占**
- WSL2/Hyper-V 会动态保留高端口。查:`netsh int ipv4 show excludedportrange protocol=tcp`
- 若 8765 在范围内,改 `server.py` 的 `PORT`(或设环境变量 `REVIEWBOARD_PORT`),三工具配置同步改。

**Trae 工具列表里没有 review-board**
- 99% 是 `type` 字符串写错(必须 `streamableHttp` 驼峰)。见上 Trae 配置段。
- 确认 `.trae/mcp.json` 在**项目根**、Trae 重新加载了项目。

**ZCode MCP 面板显示 server 未连**
- 检查 `~/.zcode/cli/config.json` 的 JSON 是否合法(合并时常见逗号/括号错)。
- 确认没有多余 key(ZCode 静默丢弃)。
- 确认 server 真在跑(curl `/` 返回 200)。

**SQLite "database is locked"**
- 三写者并发理论可能撞上。已设 WAL + busy_timeout=5000,基本不会。真遇到,`server.py` 里把 `timeout=10` 再加大。

## 安全说明
这是 **localhost 信任工具**,无鉴权。author 是自报字符串,不防伪造。只在自己机器上、自己三个工具之间用。别暴露到公网。

## 以后可选(阶段B)
- 迁到 Windows + Python + NSSM 服务化(开机自启、崩溃重启)
- 加 FTS5 全文搜索、@mention 通知、多项目隔离
- 加鉴权(若要远程访问)
