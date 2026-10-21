# automation-guard：定时任务的成本闸（thread #50 机制化）

## 它防什么

2026-10-21 事故（board thread #50）：15 分钟 recurring 定时任务（`max_runs=null`）绑在一个
122K-token 的会话上。定时任务每次触发都会**把绑定的会话全量重放一遍计费**，于是"空转一轮"
实际成本 ≈ 整个会话的 input——≈49 万 tokens/小时，烧穿当天配额；且自动化绑定会话、跨工作区
不可见，关键时刻没有可用停止开关。

本 hook 在**创建/修改定时任务的那一刻**拦住两种形态：

| 规则 | 形态 | 动作 | 依据 |
|------|------|------|------|
| R1 | `recurring=true` 且没设有限 `maxRuns`（无上限常驻） | **阻止**（exit 2，附改法） | #50 事故形态；工作区纪律已定"建 cron 必须设有限 maxRuns"，无合法场景 |
| R2 | 当前会话最近一次请求 input > 阈值（默认 50,000 tokens） | **转人工确认**（ask） | 定时任务每轮重放会话，"最近一次 input"即每轮成本下限（#50 实证 122,373） |

两条都命中最危险的"无上限 × 大会话"组合时按 R1 从严；合法的大会话短促批次（有限次数）走
R2 由人确认放行。

## 安装（每个宿主一次）

`~/.zcode/cli/config.json` 顶层 `hooks`（用户级=所有工作区生效）。与 `quorum_guard_hook.py`
可并存——`PreToolUse` 是数组，追加一个条目即可：

```json
"hooks": {
  "enabled": true,
  "events": {
    "PreToolUse": [
      {
        "matcher": "CronCreate|CronUpdate|OffPeakCreate",
        "hooks": [{
          "type": "command",
          "command": "\"<python>\" \"<repo>/configs/automation_guard_hook.py\"",
          "timeoutMs": 8000,
          "statusMessage": "automation-guard 成本闸"
        }]
      }
    ]
  }
}
```

`<python>` 换成本机 Python 绝对路径，`<repo>` 换成本仓库路径。改完配置需要新会话/重启客户端
才对后续会话生效。

## 调参（全环境变量）

- `AG_MAX_INPUT`：R2 阈值，默认 50000（tokens）。
- `AG_ROLLOUT`：覆盖 rollout 日志路径（默认 `~/.zcode/cli/rollout/model-io-session_<会话ID>.jsonl`；
  也是异构宿主/测试的接入口）。

## 自测（装完跑一遍，预期见注释）

```bash
echo '{"tool_name":"CronCreate","tool_input":{"recurring":true}}' | python automation_guard_hook.py
# → exit 2 + R1 理由（无上限常驻被拒）
echo '{"tool_name":"CronCreate","tool_input":{"recurring":true,"maxRuns":24}}' | AG_ROLLOUT=/nonexistent python automation_guard_hook.py
# → exit 0 静默（有限次数+估算不可用 → 放行）
```

## 设计边界（如实）

- **失败哲学与 quorum_guard 相反**：quorum_guard 守治理完整性，坏了必须关门（fail-closed）；
  本 hook 守成本——R1 是结构性规则（只依赖调用参数）永远可用，R2 是估算规则，
  **日志读不到/格式漂移 → 只放行 R2**（fail-open），R1 不受影响。任何未预期异常也放行。
- **宿主约定依赖**：rollout 日志的路径与用量字段是 2026-10-21 单机实证的约定，宿主升级可能
  改格式——届时 R2 静默失效（fail-open），需要重新对字段。
- **拦的是创建时刻**，不是已运行自动化的紧急停止开关——后者是宿主产品缺陷（#50 §三），
  本 hook 不是它的解。
- CronUpdate 只看得见本次 patch 的字段：更新把 `recurring` 置 true 而未同时给有限 `maxRuns`
  会被拒（宁可多拒一次改法明确的重试）。
