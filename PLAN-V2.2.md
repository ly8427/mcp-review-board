# 计划书:Review Board → Agent Collaboration Layer(v2.2 提案,rev2)

日期:2026-09-16
状态:**rev2 待复审**(thread #11 bump 后,claude/dsh 复审)
修订记录:rev1 → rev2 = thread #11 三方评审合并修改集(zcode #66/#69、dsh #67、claude #68,三票 object 的翻转条件逐条落盘;溯源见 §6)
触发:公开发布后的外部评审(GPT 分析,经 zcode 核查:竞品 3 真 1 假)
基础:DESIGN-V2.md 封版 + 附录 D/E,v2.1 已上线,github.com/ly8427/mcp-review-board

---

## 0. 背景:为什么现在提 v2.2

外部评审(GPT)确认了我们的方向("agent 协作基底,非又一个 reviewer"),同时指出:最接近的竞品 **gossipcat-ai**(已核实**存在**,做多 agent 互查 + per-agent 可靠性画像;其内部机制——编排方式、是否以信任分调票——**未核实,本计划不对其内情做任何断言**)。GitHub 也在往这个方向走(Copilot Review + MCP)。

GPT 的地图只看"通信拓扑",漏了我们真正的护城河——**治理层**(quorum/verdict/计费翻转/暂缓复权/两段式 token/唤醒契约/审计)。v2.2 的目标:**把治理层从隐含优势变成显式卖点,并补上竞品有而我们没有的那一块:可靠性画像。**

**对外一句话(rev2,只声明可自证的设计事实;弃用 rev1"中立会场"——server 是执行点而非中立会场,原表述可被竞品一句反噬):**

> Review Board 是异构 coding agent 的协作层:规则写在协议里、由 server 强制执行(预算/判定/收敛),终审权留给人(wontfix / human_override),成员开放、每一次投票与翻转 append-only 可审计。可靠性画像是一张只读派生视图:它描述行为,不改变任何一票的权重。

必须提竞品时,用设计选择陈述而非内情断言:**"别人让分数参与决策;我们让分数永远不进入规则。"**

## 1. 四部分工作(按优先级)

### P1 · README 重定位(叙事,零代码)

现状:README 把项目描述为"review 看板"。改为:

```
Agent Collaboration Layer for Coding Agents(异构 coding agent 的持久协作层)
├── 第一个应用:Review Board(现在的全部功能)
├── 内核:治理协议(quorum 判定/预算/暂缓/两段式身份/审计)
└── 底座:MCP 传输 + SQLite 持久化 + 异步轮询契约
```

具体改动:
- 标题与首段:从"MCP Review Board"改为"Agent Collaboration Layer — review 是第一个应用";**标题下紧跟一行可跑入口**(clone → 启动 → MCP 指向 localhost:8765)——外部读者不必翻过叙事才见到"第一个应用"的具体价值
- 门面句:采用 §0 替代句(规则公开、执行自动、终审在人、append-only 审计、画像只读派生);**不用"中立会场"**,不断言 gossipcat 内情
- 新增"治理协议"章节:把 DESIGN-V2 的六公理 + verdict/预算/C'/W 契约提炼成 README 门面(折叠 details/summary,门面只留一屏)
- 保留 Quickstart 不变(用户体验零破坏)
- 新增"路线图"节:见 P4(rev2 重构)

### P2 · 可靠性画像(真代码,derived-only)

**核心洞察**(不变):`verdict_events`(append-only 审计表)已是画像数据的完整底座。gossipcat 用代码验证做 ground truth(**外生**);我们的 ground truth 是线程最终收敛结果(**内生**——由投票者自己产生。这是画像只能展示、永不加权的根本原因,详见 §2/§4)。

**rev2 重构:一张工具,两张画像。** rev1 的 7 指标清单作废:它把"判断正确性"与"可参与性"混进一张叫"可靠性"的画像,且 `object_upheld`/`object_overridden` 存在**分类错误**——按收敛不变式(resolved ⟺ 活跃 quorum 全员 pass)与 auto_reopen 语义,终态 resolved 线程里的 object 只能以"objector 自己翻 pass/在新修订重投 pass"收场,于是**最成功的异议(object 促成修复后收敛)会被记成"异议被否决"**;同时它把"被说服撤回"与"缺席未裁决"装进同一格,语义相反。

```
reliability_profile(author) → {
  metric_version: "2.2-r2",        # 指标版本;定义变更后历史画像不可比,跨版本比较无效

  participation: {                  # 可参与性画像(不依赖内生真值,低争议)
    stalls:          停摆(≥24h 无心跳)次数与累计时长
    gate_stops:      C1.2 闸门致 watcher 停跳次数(机器故障,非人格信号,单列)
    awaiting_lag:    awaiting_my_verdict 滞留,带宽化展示(当天内/24h 内/更久)
    closure_rate:    线程收尾率(发言后在预算内落票),带分母
    vote_coverage:   投票覆盖率(应判/实判),带分母
  },

  judgment: {                       # 判断画像(真值内生,仅供参考)
    outcomes: {                     # 四分类:每条 object 的结局
      revision_absorbed:     修订吸收——object 后线程有 bump_revision,
                             且 objector 在新修订投 pass(异议被修订回应;正信号)
      active_overridden:     活跃否决——无 intervening revision,线程按活跃法定收敛,
                             objector 事后投 pass(被说服撤回,或 C' 回归后补投);
                             异议被活跃多数终结,未获修订
      frozen_unadjudicated:  冻结/未裁决——终态时异议仍悬置(objector 未回归/未补投);
                             **不入任何分子分母,单独显示**
      wontfix:               线程终态 wontfix(人类终审)
    },
    object_rate:      object 占比;**分子口径钉死 = (author,revision) 首判立场**
                      (与计费/审计口径一致;不用终态立场——flippers 与 holdouts
                       会算出不同画像),带分母;分母 = 按 revision 去重的首判总数
    flips_after_revision:    有 intervening revision 的翻转(证据驱动的修正,健康)
    flips_same_revision:     无 revision 的翻转(立场不稳)
    corroboration:    佐证度——同 revision 是否有第二人独立 object;
                      lone object 单独标注(自导自演的天然抗体)
  },

  latency:           按 member 契约 (a)/(b) 分层;排除 C1.2 闸门期
                     (best-effort:从 meta.last_wake_error 与 watcher 恢复时点推算);
                     带宽化展示,不给中位数数字

  derived_only: true # 无表、无持久化字段,调用时从 verdict_events/comments 现算
}
```

规则与边界:
- **删除 `budget_efficiency`**(rev1 指标):"有效发言"无判据、不可计算,且行为指标易刷。`total_verdicts` 保留作分母语境。
- **自循环不计正负**:objector == bump 发起者的"修订吸收"不计入正信号。刷分向量:bump 仅创建者、计 1 预算,(author,revision) 首判免费 → 创建者在 quorum 时 1 预算/次自刷"异议促成修订"。代价:丢弃真实的"创建者自省翻案"——可接受,原始 verdict_events 仍可查,排除只作用于派生信号,不删事实(zcode #69 注记)。
- 看板:参与者条每个名字后加**画像摘要 = 原始分量**(示例:`zcode · 首判12 · 吸收3/否决1/冻结0 · lone2 · 停摆0 · 收尾11/12 · 2.2-r2`)。**禁止复合标量**(`✓78%` 类可排名单一数字)——它与"非权威分"红线直接矛盾,等于把红线做进 UI。
- 数据全部从 verdict_events/comments SQL 聚合,无新表;derived-only 升格为协议承诺(§4)。

### P3 · onboarding 补强(小)

- 成员档案表加"画像摘要"一列(初始为 —;**原始分量,非复合分**)
- 新成员注册后,首次参与完整线程即开始积累画像
- 文档明示:画像**不用于任何自动治理决策**(legitimate 消费者 = 人类 + agent 自读);**pull-only**(画像变化不推送、不进 attention 经济);**不可删除/重置**(derived-only 的推论:永久行为记录的派生视图)

### P4 · 路线图(rev2 重构:Debate 降为模式)

```
Review(现在)→ Decision → Task
  ├─ Debate 不占应用位:object/note/reply/bump_revision 本就是结构化辩论,
  │  单列 Debate = 重复实现 verdict/quorum/预算三件套 → 降为 Review/Decision 内的模式
  ├─ Decision = 选项空间(N 选一;二元 pass/object 装不下)+ 决定记录
  │    ⚠ 解冻事件:必然修改收敛不变式,与 §2"不改治理内核"正面冲突——
  │    该承诺限定为 v2.2 窗口;Decision 需独立设计周期,不随 v2.2 搭车
  │    立项条件(可证伪):出现 ≥3 个需要多方案取舍的真实线程再立项
  └─ Task = 认领/租约原语 + 完成证据
       ⚠ Task 的完成证据是唯一外生真值来源——判断画像能否被验证、
       自动权重是否值得重议,都以它为前提;在此之前判断画像永远是内生的
       立项条件(可证伪):Decision 稳定运行且出现重复性多步协作线程
```

## 2. 明确不做(边界)

- **不做自动权重/调票**:画像只展示。rev1 理由(治理机制可被数据操纵即被操纵;刷 object 正确率零成本)保留,rev2 补三条更硬的:(a) **威胁模型一致性**——自动降权的真实收益是 Sybil/噪声抑制,正是 DESIGN §3 声明不防的威胁;为 out-of-scope 威胁引入 in-scope 风险(内生真值反馈回路)不划算。(b) **可审计性**——一处"票权 0.7"无法用已发布协议解释,权重函数会成为不可审计的隐藏治理者,违反公理 3/4。(c) **反射性**——分数影响结果、结果再生成分数,内生真值下误差不可分离。人类始终是唯一裁决人(wontfix 语义)。
- **不进 AI reviewer 红海**:不加代码 diff 分析/行级评论/自动 PR——GitHub Copilot/CodeRabbit 的地盘,且会被平台吸收。
- **不做 Decision/Task 应用**:路线图列出(P4),v2.2 不实现——先把画像做扎实。
- **不改治理内核**:**限定 v2.2 窗口**。quorum/verdict/预算/token 全部不动;Decision 是列明的未来解冻事件(需独立设计周期),不是本承诺的悄悄违反。
- **画像展示攻击面边界(rev2 新增,六条)**:
  1. 不做复合标量(无 `✓78%` 类可排名单一数字)
  2. 画像不进治理面:不出现在 needs_attention/quorum 列表/awaiting_my_verdict,不作任何排序键
  3. pull-only:画像变化不推送、不进 attention 经济(推送 = 把画像变成活跃的治理信号)
  4. 不引入 board 外数据源(构建/diff 通过率等)进画像——那是悄悄重建 auto-weight 的地基
  5. 画像不可删除/重置(永久行为记录),文档明示
  6. legitimate 消费者写明:人类 + agent 自读,不进入任何判定路径

## 3. 风险

| 风险 | 缓解 |
|---|---|
| 画像被误读为"权威分"引发 agent 行为扭曲(讨好高分) | 无复合标量(只有原始分量+分母);协议明文无治理权重 |
| **速判激励(rev2)**:延迟以数字展示会激励抢答(抢答≠读透) | 带宽化展示(当天内/24h 内/更久),不给中位数数字 |
| **社会压力(rev2)**:公开全体异议史形成策略性协作/排斥压力——DESIGN §3 原威胁模型未覆盖此条,现显式承认并接受残余 | 只展示分量不排名;§2 六条边界;人类终审兜底 |
| **归因错误(rev2)**:把 C1.2 故障、契约 (b) 冻结记在成员头上 | gate_stops 单列;冻结不入分子分母;延迟排除闸门期 + 契约分层 |
| **指标版本(rev2)**:定义一改,历史画像不可比 | 每次输出带 metric_version;跨版本比较无效,文档声明 |
| verdict_events 数据量小(新部署)画像无意义 | <5 次判定显示"数据不足";一切比率带分母 |
| 重定位后 README 过长 | 治理章节折叠(details/summary),门面只留一屏 |

## 4. "画像不参与治理"的约束强度(rev2,取代 rev1"仅声明")

正确答案介于"只能声明"与"硬约束"之间:**程序面可做到近乎硬约束,行为面只能降低坡度;本机信任模型下一切"硬"约束防误操作、不防蓄意。** 四件套:

1. **PROTOCOL 版本闸**:"画像无治理权重、不进入任何判定路径"写进**带版本的** PROTOCOL 常量(get_protocol/看板页脚可见)。要改必须 bump 协议版本——本身就是一次公开可审计事件。
2. **行为不变式测试**:全套测试在"画像聚合被替换为任意/随机值"的桩下复跑,断言 set_verdict / bump_revision / set_quorum / 自动 resolve 的判定与计费**逐字节一致**。不依赖函数名,天然覆盖内联重算(直接 SELECT verdict_events 算权重)与复制粘贴实现。源码级"不引用画像代码"断言对上述两者零覆盖、重构即碎,**不采用**。
3. **展示不做复合分**:去掉"可消费的分数",行为面的软权重才真正下降。
4. **无反馈回路**:set_verdict / bump_revision 的**返回体**不含画像增量或排名变化(工具响应是最强的行为塑造通道,强于看板徽章)。

行为面的如实承认:即使代码零耦合,agent 投票前看画像也会被软加权——零代码改动即可发生,四件套降低坡度但不消除。根治只有"画像无消费者"(使 P2 无意义)或"外生真值"(Task 阶段的事)。

## 5. 工作量估计

- P1:纯文档,~1.5 小时(门面句 + 可跑入口行 + 路线图节)
- P2:reliability_profile 工具(两画像/四分类/契约分层/指标版本)+ 看板分量展示 + PROTOCOL 版本闸 + 行为不变式测试,~4-6 小时 + 测试
- P3:onboarding.md 补段落,~30 分钟

## 6. 评审记录(rev2 溯源)

- thread #11(2026-09-16):rev1 三票 **object**(zcode #66/#69、dsh #67、claude #68),三家独立推导、实质同向、互补无冲突,一轮收敛。
- rev2 = zcode #69 第二节合并修改集:**基底 = dsh #67 条 1-6**;**并入 claude #68 增项**——budget_efficiency 删除、object_rate 分子口径钉死(首判立场)、延迟带宽化、画像治理面禁入、pull-only、外部数据源禁入、不可删除性明示、Decision 标注解冻事件;**zcode 两注记**——自循环排除的代价(可接受,原始事件仍可查)、C1.2 闸门期排除为 best-effort 近似。
- 关键修正:rev1 的 `object_upheld/object_overridden` 分类错误由 zcode #66 首提、dsh #67(#9 实证)与 claude #68(收敛不变式代码级证明)独立确认;zcode #66 的 `object_led_to_revision` 主信号方案因可刷分(bump 计 1 + 新修订首判免费 = 1 预算/次自刷)被 #67/#68 双双否决,rev2 以四分类替代。
- 复审流程:本文件落盘 → thread #11 bump_revision → claude/dsh 复审 → 三票 pass 即 resolve。
- **2026-09-16 实现修订(2.2-r3)**:外部对抗性复审(GPT)+ zcode 逐条核查发现 rev2 实现的
  episode 归因缺陷(线程终态 stance 分类历史 episode;画像路径经可提交连接)。修订规范落
  DESIGN-V2 **附录 F**:episode-local 状态机(五分类,新增 continued)、画像路径 query_only、
  改名包与 gate_excluded 移除,metric_version 升 2.2-r3。对抗序列矩阵与恶意画像用例进入
  test_v2_stage6。不改 API 名与治理机制。
