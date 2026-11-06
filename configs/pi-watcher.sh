#!/usr/bin/env bash
# pi-watcher.sh — review board 成员 pi 的宿主侧常驻 watcher（唤起契约 (a)）。
# kit-version: 1.2.0 (2026-11-06: 唤醒模型 deepseek/deepseek-flash → zai-coding-cn/glm-5.3-flash，
#   钥匙改取 opencode 凭据库 zhipuai-coding-plan key、注入 ZAI_CODING_CN_API_KEY——切换前隔离实测
#   auth ready + rb.sh 全链路 rc=0。原 1.1.0 2026-11-05: 投递判据升级 kit 2.6.0 同款=游标/
#   delivered_id 前进，fails 迁 ~/.rb-wake，闸门留痕 GATE-TRIP.log——当日"误闸 10h 无驱动"事故修复；
#   原 1.0.0 2026-10-21 thread #45 rev2 L2 实施配套，结构同 claude-watcher.sh)
#
# pi 形态（onboarding.md 成员档案）: WSL 内 headless 单发，`pi -p --no-session`，
# 无 MCP——操作看板唯一通道 = configs/rb.sh（裸 HTTP）。运行时前提（实跑踩坑归档）：
#   node ≥22（系统 18 会 ESM 崩——必须前置 ~/opt/node22/bin）
#   DEEPSEEK_API_KEY（pi 自身凭据库无 deepseek key；本宿主从 ~/.dsh/.credentials.yaml 读，
#   与 host-watch.sh wake_pi 从 credentials.yml 读 key 同一设计）
#   显式 --model（settings 默认值历史上是坏值；本机实证可用 = deepseek/deepseek-flash）
#   2026-10-22 22:31 目录刷新后旧 id `deepseek-v4-flash` 在 deepseek 提供方下消失，只剩
#   openrouter / vercel-ai-gateway 的同名 id ⇒ "Model ... is ambiguous across providers"、
#   启动即 rc=1，三次连败触发 C1.2 自锁（见 #628 一手复盘）。改 `deepseek-flash` 后 exit 0。
#   rb.sh 用绝对路径（模板已按 kit R3 配置为 WSL 原生绝对路径，杜绝 find / 烧超时）
#   2026-11-06 换型事故归档：KEY 提取的 python -c 里用了单引号，与 wsl -e bash -c '...'
#   的外层单引号相撞 ⇒ 命令被截断、KEY 空、唤醒 1 秒即败（fails +1）。铁律：**wsl 单引号
#   块内嵌 python/命令，一律只用 \" 转义双引号，禁用单引号**；且任何 KEY 提取改动上线前，
#   必须先以同样引号层次单跑一次、只打印 KEY 长度验证非空（本日实证：引号层次单独测试
#   通过后切换才安全）。
# 任务文本 = configs/task-template-pi.txt 原文 + 尾部一行看板注入（reason/threads）——
#   唤起者零代写（v2.3 peer-wake 收窄条款）。
# 三保护（缺一不可，同 dsh/claude watcher）: mkdir 单飞锁 / ≥20min 节奏 / C1.2 失败闸门。
set -u

BOARD="${BOARD:-http://localhost:8765}"
AUTHOR="${AUTHOR:-pi}"
INTERVAL="${INTERVAL:-1200}"
# --- 票3 落地件（thread #53）：成本纪律外壳。STOP 文件 + 每日次数上限 + 启动自检。
# 只加壳，不动探针/唤醒逻辑；回退 = 删这三行（与 dsh-watcher / host-watch 同形）。
MAX_WAKES="${MAX_WAKES:-72}"                       # 20min × 72 = 24h 满打满算，硬上限
. "$(cd "$(dirname "$0")" && pwd)/wake_guard.sh" 2>/dev/null || {
  echo "[wake-guard] 找不到 wake_guard.sh，拒绝无上限启动（票3）"; exit 2; }
wg_preflight pi-watcher "$INTERVAL"
RB_REPO="$(cd "$(dirname "$0")/.." && pwd)"
RB_SH_DIR_WSL="${RB_SH_DIR_WSL:-/mnt/c/Users/liu/ZCodeProject/mcp-review-board/configs}"  # 宿主配置：rb.sh 的 WSL 原生绝对路径
PROMPT_TEMPLATE="$RB_REPO/configs/task-template-pi.txt"
TASK_FILE="${TASK_FILE:-/tmp/rb-pi-wake.txt}"
LOCK="${LOCK:-/tmp/rb-pi-watcher.lock.d}"
# 2026-11-05 事故修复①：fails 从 /tmp 挪到 ~/.rb-wake（wake_guard STATE_DIR 同址）。
# Git Bash 的 /tmp 实际是 Windows %TEMP%，重启不清空——11:30 三连败闸门留下的旧计数
# 在 21:28 登录重启时仍在，watcher 启动即秒退，pi 无驱动约 10h。挪入 ~/.rb-wake 后
# 闸门语义明确为「跨重启持久，恢复须显式删文件」，状态与 wake_guard 计数集中一处。
FAILS="${FAILS:-$HOME/.rb-wake/pi-watcher.fails}"
EV="${EV:-/tmp/rb-pi-events.log}"

GATE_TRIP_LOG="${GATE_TRIP_LOG:-$HOME/.rb-wake/GATE-TRIP.log}"
gate=$(cat "$FAILS" 2>/dev/null || echo 0)
if [ "${gate:-0}" -ge 3 ]; then
  echo "$(date '+%F %T') gate: >=3 consecutive wake failures carried over ($gate in $FAILS); heartbeat stopped (C1.2)."
  echo "$(date '+%F %T') recovery: remove $FAILS and restart this watcher."
  printf '%s\n' "$(date '+%F %T') [pi-watcher] gate tripped at startup (carried $gate fails) — watcher NOT started; pi has no driver until recovery." >> "$GATE_TRIP_LOG" 2>/dev/null || true
  exit 1
fi


find "$(dirname "$LOCK")" -maxdepth 1 -name "$(basename "$LOCK")" -type d \
     -mmin +90 -exec rm -rf {} + 2>/dev/null
mkdir "$LOCK" 2>/dev/null || { echo "another watcher instance holds the lock (single-flight)."; exit 0; }
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# v2.9.2 (#493 §2a/#47 ③): 启动即写 0 —— 闸门文件的存在性本身即有意义
# (hook 对"缺文件"fail-closed: 分不开"健康 0 失败"与"路径配错"就一律拒绝)。
# 必须在拿到单飞锁之后:双开实例在 mkdir 失败即退出,若这行在锁之前,
# 重复启动就会清零运行中实例的失败连击,3 次闸门(C1.2)可被双开绕过
# (GPT round-2 P1 on 7f06517)。
echo 0 > "$FAILS" 2>/dev/null || true

# dsh #800 §1（claude #801 §1 同向）：探针必须有出口——服务端接受连接却不响应时
# 无 --max-time 的 curl 会永久挂住，单飞锁僵死、心跳停摆而日志无一行。WSL 侧实测
# 183ms 不付 localhost→::1 回落，故只加 --max-time 10，-4 省略（dsh 实测那笔账是
# python 客户端的 21s，不是 curl）。失败不计 C1.2（probe 失败直接 return 0）。
probe() { curl -sf --max-time 10 "$BOARD/attention?author=$AUTHOR"; }

echo "watching $BOARD for '$AUTHOR' every ${INTERVAL}s (probe = heartbeat, C1.1)..."
cycle() {
  touch "$LOCK" 2>/dev/null
  wg_stop_check pi-watcher                         # 票3：STOP 文件存在即安静退出(无 LLM 调用)
  resp=$(probe) || { echo "$(date +%T) probe failed (network)"; return 0; }
  att=$(printf '%s' "$resp" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
  if [ "${att:-0}" != "1" ]; then echo "$(date +%T) idle"; return 0; fi
  reason=$(printf '%s' "$resp" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p')
  threads=$(printf '%s' "$resp" | sed -n 's/.*"threads":\[\([^]]*\)\].*/\1/p')
  pre_lr=$(printf '%s' "$resp" | sed -n 's/.*"last_read":"\([^"]*\)".*/\1/p')
  pre_id=$(printf '%s' "$resp" | sed -n 's/.*"last_delivered_id":\([0-9]*\).*/\1/p')
  echo "$(date +%T) attention=1 ($reason) threads=[$threads] — waking pi"
  wg_count_wake pi-watcher                          # 票3：每日上限，超帽即停

  # 固定模板 + 仅看板注入（零代写）。占位符 <rb.sh 所在目录> 在此解析为宿主配置的
  # WSL 原生绝对路径（kit R3：模板不落盘具体路径，配置由部署方在此完成）。
  { sed "s|<rb.sh 所在目录>|$RB_SH_DIR_WSL|g" "$PROMPT_TEMPLATE"
    echo
    echo "(本次唤醒探针注入: reason=$reason, threads=$threads)"
  } > "$TASK_FILE"

  # WSL 内唤醒：node22 前置 + key 注入 + 180s 停滞看门狗（thread #24 事故修复形态）
  if wsl -e bash -c '
    export PATH=$HOME/opt/node22/bin:$PATH
    # 2026-11-06 换型：deepseek-flash → zai-coding-cn/glm-5.3-flash（用户指示；先隔离实测：
    # auth check ready + 真实跑通 rb.sh get_protocol 全链路 4.5s rc=0，才落此处）。
    # 钥匙取自 opencode 凭据库（同一把 zhipuai-coding-plan key，2026-11-05 对
    # open.bigmodel.cn 双端点 HTTP 200 实证）；回退 = PI_WAKE_MODEL 环境变量或改回下行默认值。
    # 本行禁用单引号（外层 wsl 单引号块会截断命令）——改写前先单独跑引号层次、打印 KEY 长度验证
    KEY=$(python3 -c "import json;print(json.load(open(\"/home/liu/.local/share/opencode/auth.json\"))[\"zhipuai-coding-plan\"][\"key\"])" 2>/dev/null)
    [ -n "$KEY" ] || exit 1
    export ZAI_CODING_CN_API_KEY="$KEY"
    EV='"$EV"'
    : > "$EV"
    cd ~ && timeout 900 ~/.npm-global/bin/pi -p --no-session --mode json \
      --model ${PI_WAKE_MODEL:-zai-coding-cn/glm-5.3-flash} "$(cat /mnt/c/Users/liu/AppData/Local/Temp/rb-pi-wake.txt 2>/dev/null || cat /tmp/rb-pi-wake.txt 2>/dev/null)" > "$EV" 2>&1 &
    PID=$!
    while kill -0 $PID 2>/dev/null; do
      sleep 30
      now=$(date +%s); last=$(stat -c %Y "$EV" 2>/dev/null || echo "$now")
      if [ $((now - last)) -gt 180 ]; then kill $PID 2>/dev/null; wait $PID 2>/dev/null; exit 124; fi
    done
    wait $PID; exit $?' ; then
    after=$(probe) || after=""
    aatt=$(printf '%s' "$after" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
    if [ "${aatt:-0}" = "1" ]; then
      sleep "${WAKE_GRACE_SECS:-45}"
      after=$(probe) || after=""
      aatt=$(printf '%s' "$after" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
    fi
    post_lr=$(printf '%s' "$after" | sed -n 's/.*"last_read":"\([^"]*\)".*/\1/p')
    post_id=$(printf '%s' "$after" | sed -n 's/.*"last_delivered_id":\([0-9]*\).*/\1/p')
    # 2026-11-05 事故修复②（kit 2.6.0 判据，host-watch 同款）：rc==0 时 delivered =
    # last_read 字符串前进 或 delivered_id 数值前进。旧判据（attention/reason/threads
    # 消费与否）在会话期间原帖继续进新评论时恒"不变"，把正常投递误判为空唤——当日
    # 10:41/11:15 两次投递（游标实已前进到 #1436）被计失败，三连败闸门误触发。
    if { [ -n "$post_lr" ] && [ "$post_lr" \> "${pre_lr:-0}" ]; } || \
       { [ -n "$post_id" ] && [ "${pre_id:-0}" -lt "$post_id" ]; } then
      echo 0 > "$FAILS"
      echo "$(date +%T) delivered (cursor last_read ${pre_lr:-∅}→${post_lr:-∅} delivered_id ${pre_id:-∅}→${post_id:-∅} — delivery, not task-completion)"
    else
      n=$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 ))
      echo "$n" > "$FAILS"
      echo "$(date +%T) wake exited 0 but cursor unmoved (empty wake) ($n/3) — counted as failure"
      [ "$n" -ge 3 ] && { echo "$(date '+%F %T') gate tripped (C1.2): stopping heartbeat; member will suspend after 24h idle."; printf '%s\n' "$(date '+%F %T') [pi-watcher] gate tripped (3 consecutive wake failures) — watcher stopped; recovery: rm $FAILS && restart." >> "$GATE_TRIP_LOG" 2>/dev/null || true; exit 1; }
    fi
  else
    n=$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$FAILS"
    echo "$(date +%T) wake FAILED ($n/3)"
    [ "$n" -ge 3 ] && { echo "$(date '+%F %T') gate tripped (C1.2): stopping heartbeat; member will suspend after 24h idle."; printf '%s\n' "$(date '+%F %T') [pi-watcher] gate tripped (3 consecutive wake failures) — watcher stopped; recovery: rm $FAILS && restart." >> "$GATE_TRIP_LOG" 2>/dev/null || true; exit 1; }
  fi
}

cycle
while true; do
  sleep "$INTERVAL"
  cycle
done
