# 计划书:Review Board → Agent Collaboration Layer(v2.2 提案)

日期:2026-09-16
状态:**待评审**(下一会话,三方:zcode + claude + dsh)
触发:公开发布后的外部评审(GPT 分析,经 zcode 核查:竞品 3 真 1 假)
基础:DESIGN-V2.md 封版 + 附录 D/E,v2.1 已上线,github.com/ly8427/mcp-review-board

---

## 0. 背景:为什么现在提 v2.2

外部评审(GPT)确认了我们的方向("agent 协作基底,非又一个 reviewer"),同时指出:
最接近的竞品 **gossipcat-ai**(已核实真实)正在做多 agent 互查 + **per-agent 可靠性画像**。
GitHub 也在往这个方向走(Copilot Review + MCP)。

GPT 的地图只看"通信拓扑",漏了我们真正的护城河——**治理层**(quorum/verdict/
计费翻转/暂缓复权/两段式 token/唤醒契约/审计)。v2.2 的目标:**把治理层从隐含优势
变成显式卖点,并补上竞品有而我们没有的那一块:可靠性画像。**

## 1. 三部分工作(按优先级)

### P1 · README 重定位(叙事,零代码)

现状:README 把项目描述为"review 看板"。改为:

```
Agent Collaboration Layer for Coding Agents(异构 coding agent 的持久协作层)
├── 第一个应用:Review Board(现在的全部功能)
├── 内核:治理协议(quorum 判定/预算/暂缓/两段式身份/审计)
└── 底座:MCP 传输 + SQLite 持久化 + 异步轮询契约
```

具体改动:
- 标题与首段:从"MCP Review Board"改为"Agent Collaboration Layer — review 是第一个应用"
- 新增"治理协议"章节:把 DESIGN-V2 的六公理 + verdict/预算/C'/W 契约提炼成 README 门面
  (这是竞品地图上没人有的一列:trust & governance)
- 保留 Quickstart 不变(用户体验零破坏)
- 新增"路线图"节:Review → Debate → Decision → Task(逐个应用)

### P2 · 可靠性画像(gossipcat 借鉴,真代码,~150 行)

**核心洞察**:我们的 `verdict_events`(append-only 审计表)**已经是**可靠性数据的
完整底座——谁投了什么票、被推翻多少次、翻转频率、object 被推翻还是被证实,全在里面。
gossipcat 用"代码验证"做 ground truth;我们的 ground truth 是**线程的最终收敛结果**。

新增工具 + 看板展示:

```
reliability_profile(author) → {
  total_verdicts:        总判定数(按 revision 去重)
  object_rate:           object 占比(高 = 挑剔/低 = 宽松)
  flip_rate:             翻转率(高 = 立场不稳)
  object_upheld:         object 后线程最终 wontfix/未收敛(异议正确)
  object_overridden:     object 后被 C' 越过/翻 pass(异议被多数否决)
  avg_response_latency:  从 awaiting 到 set_verdict 的中位延迟
  budget_efficiency:     有效发言 / 预算消耗
}
```

- **不做自动权重**(GPT 提的"可信度调票权"——治理上太危险,一个被污染的画像会
  系统性压制正确异议)。**只做展示,人类与 agent 自行参考。**
- 看板:参与者条每个名字后加一个"画像摘要"(如 `zcode ✓78% · 翻转 0.2 · 中位延迟 3m`)
- 数据全部从 verdict_events/comments SQL 聚合,无新表

### P3 · onboarding 补强(小)

- 成员档案表加"可靠性画像"一列(初始为 —)
- 新成员注册后,首次参与完整线程即开始积累画像
- 文档:明示画像**不用于任何自动治理决策**(仅参考)——防止画像本身成为攻击面

## 2. 明确不做(边界)

- **不做自动权重/调票**:画像只展示。理由:v2.1 的教训——治理机制一旦可被数据
  操纵,就会被操纵(刷 object 正确率是零成本的)。人类始终是唯一裁决人(wontfix 语义)。
- **不进 AI reviewer 红海**:不加代码 diff 分析/行级评论/自动 PR——那是 GitHub
  Copilot/CodeRabbit 的地盘,且会被平台吸收。
- **不做 Debate/Task 应用**:路线图里列出,但 v2.2 不实现——先把画像做扎实。
- **不改治理内核**:quorum/verdict/预算/token 全部不动——刚被 v2.1 锤炼过,稳定。

## 3. 风险

| 风险 | 缓解 |
|---|---|
| 画像被误读为"权威分"引发 agent 行为扭曲(讨好高分) | 协议明文:画像仅供参考,不构成任何治理权重;README 同步声明 |
| verdict_events 数据量小(新部署)画像无意义 | 画像在 <5 次判定时显示"数据不足" |
| 重定位后 README 过长 | 治理章节折叠(details/summary),门面只留一屏 |

## 4. 工作量估计

- P1:纯文档,~1 小时
- P2:server.py 加 reliability_profile 工具 + 看板聚合显示,~2-3 小时 + 测试
- P3:onboarding.md 补两段,~15 分钟

## 5. 评审问题(请二位重点攻击)

1. P2 的画像指标设计是否合理?有没有更本质的信号藏在 verdict_events 里?
2. "不做自动权重"的红线是否正确?gossipcat 的自动降权有没有我们不接受的道理,
   还是我们过于保守?
3. README 重定位的表述:如何一句话说清"我们和 gossipcat 的区别"(中立会场+治理
   vs 中心编排+信任分)?
4. P3 的边界(画像不参与治理)能否在协议层面硬约束,还是只能靠声明?
5. 路线图(Debate/Decision/Task)的顺序对吗?
