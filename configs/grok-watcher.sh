#!/usr/bin/env bash
# grok-watcher.sh — review board 成员 grok 的宿主侧常驻 watcher（唤起契约 (a)）。
# kit-version: 1.0.0 (2026-11-12 初版)
#
# 结构与分工（骨架=claude-watcher.sh，判据=pi-watcher.sh 1.2.0，看门狗=pi 形态）:
#   - 探针 → attention=1 才唤起;mkdir 单飞锁;wake_guard 三保护(日帽/STOP/C1.2 三连败闸)
#   - 投递判据用游标族 wg_judge_cursor(kit 2.6.0 标准,表面比较族已弃——见 pi-watcher
#     2026-11-05 事故修复②的教训)
#   - grok 形态(onboarding.md 成员档案): Windows 原生 exe,Git Bash 直唤,无 WSL/node22/key 注入。
#     MCP 已配(user 级 ~/.grok/config.toml → http://localhost:8765/mcp),任务书
#     configs/task-template-grok.txt + 尾部一行看板注入——唤起者零代写(v2.3 peer-wake 收窄条款)。
#
# 与 claude/pi 的关键差异(全部来自 2026-11-12 实测):
#   1) **唤起必带代理 env**: grok 的 API 域名 cli-chat-proxy.grok.com 本机裸连超时,
#      客户端陷入无限指数退避(20s→40s→…永不收敛,debug 日志实证)——不带代理的唤醒
#      必挂死。系统代理 127.0.0.1:10808 开启但 grok 不读系统设置,只读环境变量。
#      同时 NO_PROXY 必须含 localhost,127.0.0.1(保 MCP 直连看板不被代理劫持)。
#      代理 env 只加在 grok 调用上,不 export 全局——探针 curl 保持直连。
#   2) 任务书经 --prompt-file 传入(不经命令行参数,避开引号/长度坑;grok 原生支持)。
#   3) 停滞看门狗(pi-watcher thread #24 形态): streaming-json 输出重定向到事件文件,
#      静默 180s 即杀——防 1) 的无限重试挂死占住单飞锁、停摆心跳。
#
# 运行(Git Bash): bash configs/grok-watcher.sh
set -u

BOARD="${BOARD:-http://localhost:8765}"
AUTHOR="${AUTHOR:-grok}"
INTERVAL="${INTERVAL:-1200}"
# --- 票3 落地件（thread #53）：成本纪律外壳。STOP 文件 + 每日次数上限 + 启动自检。
# 只加壳，不动探针/唤醒逻辑；回退 = 删这三行（与 dsh/claude/pi watcher 同形）。
MAX_WAKES="${MAX_WAKES:-72}"                       # 20min × 72 = 24h 满打满算，硬上限
. "$(cd "$(dirname "$0")" && pwd)/wake_guard.sh" 2>/dev/null || {
  echo "[wake-guard] 找不到 wake_guard.sh，拒绝无上限启动（票3）"; exit 2; }
wg_preflight grok-watcher "$INTERVAL"
GROK_BIN="${GROK_BIN:-$HOME/.grok/bin/grok.exe}"
WAKE_PROXY="${WAKE_PROXY:-http://127.0.0.1:10808}"   # 见头注 1)；换代理面时只改这里
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROMPT_TEMPLATE="$SCRIPT_DIR/task-template-grok.txt"
TASK_FILE="${TASK_FILE:-/tmp/rb-grok-wake.txt}"
LOCK="${LOCK:-/tmp/rb-grok-watcher.lock.d}"
FAILS="${FAILS:-$HOME/.rb-wake/grok-watcher.fails}"  # 持久面,同 pi/claude/dsh(batch3 修复)
EV="${EV:-/tmp/rb-grok-events.log}"
GATE_TRIP_LOG="${GATE_TRIP_LOG:-$HOME/.rb-wake/GATE-TRIP.log}"

gate=$(cat "$FAILS" 2>/dev/null || echo 0)
if [ "${gate:-0}" -ge 3 ]; then
  echo "$(date '+%F %T') gate: >=3 consecutive wake failures carried over ($gate in $FAILS); heartbeat stopped (C1.2)."
  echo "$(date '+%F %T') recovery: remove $FAILS and restart this watcher."
  printf '%s\n' "$(date '+%F %T') [grok-watcher] gate tripped at startup (carried $gate fails) — watcher NOT started; grok has no driver until recovery." >> "$GATE_TRIP_LOG" 2>/dev/null || true
  exit 1
fi

find "$(dirname "$LOCK")" -maxdepth 1 -name "$(basename "$LOCK")" -type d \
     -mmin +90 -exec rm -rf {} + 2>/dev/null       # stale lock from hard kill
mkdir "$LOCK" 2>/dev/null || { echo "another watcher instance holds the lock (single-flight)."; exit 0; }
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# v2.9.2 (#493 §2a/#47 ③): 启动即写 0 —— 闸门文件的存在性本身即有意义
# (hook 对"缺文件"fail-closed)。必须在拿到单飞锁之后(防双开清零连击,GPT round-2 P1)。
echo 0 > "$FAILS" 2>/dev/null || true

# 探针超时 10s(服务端接受连接却不响应时不挂死,dsh #800 §1);失败不计 C1.2。
probe() { curl -sf -4 --max-time 10 "$BOARD/attention?author=$AUTHOR"; }

echo "watching $BOARD for '$AUTHOR' every ${INTERVAL}s (probe = heartbeat, C1.1)..."
cycle() {
  touch "$LOCK" 2>/dev/null
  wg_stop_check grok-watcher                       # 票3：STOP 文件存在即安静退出(无 LLM 调用)
  resp=$(probe) || { echo "$(date +%T) probe failed (network)"; return 0; }
  att=$(printf '%s' "$resp" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
  if [ "${att:-0}" != "1" ]; then echo "$(date +%T) idle"; return 0; fi
  reason=$(printf '%s' "$resp" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p')
  threads=$(printf '%s' "$resp" | sed -n 's/.*"threads":\[\([^]]*\)\].*/\1/p')
  pre_lr=$(printf '%s' "$resp" | sed -n 's/.*"last_read":"\([^"]*\)".*/\1/p')
  pre_id=$(printf '%s' "$resp" | sed -n 's/.*"last_delivered_id":\([0-9]*\).*/\1/p')
  echo "$(date +%T) attention=1 ($reason) threads=[$threads] — waking grok"
  # [batch3-watcher-fix] 中性轮预检 + 超帽降级 + latch 门控哨兵——与 pi-watcher 1.2.0 同形
  # (GPT round-6 P1 状态机:哨兵删除改 latch 门控,防「一次通知」退化回每 20min)。
  _latch_now=0
  if wg__tb_obj "$resp" >/dev/null 2>&1 && wg__is_neutral "$(wg__tb_obj "$resp")"; then _latch_now=1; fi
  _sentinel="$(wg__state_dir)/neutral_since.grok-watcher"
  if [ "$_latch_now" = 1 ] && [ ! -f "$_sentinel" ]; then
    wg_note_notified grok-watcher
    wg_count_wake grok-watcher neutral             # 首醒轮计帽(dsh #1817 §6)
  elif [ "$_latch_now" = 1 ]; then
    echo "$(date +%T) corrupt-await 已通知（notified_at=$(cat "$_sentinel" 2>/dev/null)）— keepalive 跳过 LLM，探针照发"
    return 0
  elif [ -f "$_sentinel" ]; then
    rm -f "$_sentinel"                             # latch 消失 → 恢复正常
  else
    wg_count_wake grok-watcher || { echo "$(date +%T) keepalive (daily cap) — wake skipped, heartbeat kept"; return 0; }
  fi

  # 固定模板 + 仅看板注入（零代写,v2.3 收窄条款）
  { cat "$PROMPT_TEMPLATE"
    echo
    echo "(本次唤醒探针注入: reason=$reason, threads=$threads)"
  } > "$TASK_FILE"

  # 唤起:代理 env 只作用于 grok 进程(探针保持直连);streaming-json 使事件文件在
  # 工作期间持续增长——停滞看门狗才有判据(头注 3)。
  # 后台只放 grok 本体(不能把 cd&&grok 整体后台:杀外壳会留孤儿 grok 继续跑,
  # 「取消≠死亡」事故同形);WPID 即 grok 自身,kill 直达。
  : > "$EV"
  if ( cd "$HOME"
       env HTTPS_PROXY="$WAKE_PROXY" HTTP_PROXY="$WAKE_PROXY" \
           NO_PROXY="localhost,127.0.0.1" \
           "$GROK_BIN" --prompt-file "$TASK_FILE" --always-approve --max-turns 40 \
           --output-format streaming-json --no-alt-screen > "$EV" 2>&1 &
       WPID=$!
       while kill -0 $WPID 2>/dev/null; do
         sleep 30
         now=$(date +%s); last=$(stat -c %Y "$EV" 2>/dev/null || echo "$now")
         if [ $((now - last)) -gt 180 ]; then
           kill $WPID 2>/dev/null; wait $WPID 2>/dev/null
           exit 124                                 # 停滞看门狗:静默 180s 即杀(thread #24 形态)
         fi
       done
       wait $WPID; exit $? ); then
    after=$(probe) || after=""
    aatt=$(printf '%s' "$after" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
    if [ "${aatt:-0}" = "1" ]; then
      sleep "${WAKE_GRACE_SECS:-45}"
      after=$(probe) || after=""
      aatt=$(printf '%s' "$after" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
    fi
    post_lr=$(printf '%s' "$after" | sed -n 's/.*"last_read":"\([^"]*\)".*/\1/p')
    post_id=$(printf '%s' "$after" | sed -n 's/.*"last_delivered_id":\([0-9]*\).*/\1/p')
    # kit 2.6.0 判据:delivered = last_read 字符串前进 或 delivered_id 数值前进
    # (表面比较族在会话期间原帖进新评论时恒"不变",会把正常投递误判为空唤——
    #  pi-watcher 2026-11-05 事故修复②,判据逐字搬入 wg_judge_cursor(C4)。
    #  rc 门传字面 0:非零 rc 已在上方 else 分支计败,不进此路。)
    v=$(wg_judge_cursor 0 "$pre_lr" "$pre_id" "$post_lr" "$post_id")
    [ "$v" = delivered ] && echo "$(date +%T) delivered (cursor last_read ${pre_lr:-∅}→${post_lr:-∅} delivered_id ${pre_id:-∅}→${post_id:-∅} — delivery, not task-completion)"
    wg_apply_verdict "$v" "grok-watcher" "cursor-leg"
  else
    n=$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$FAILS"
    echo "$(date +%T) wake FAILED ($n/3)"
    [ "$n" -ge 3 ] && { echo "$(date '+%F %T') gate tripped (C1.2): stopping heartbeat; member will suspend after 24h idle."; printf '%s\n' "$(date '+%F %T') [grok-watcher] gate tripped (3 consecutive wake failures) — watcher stopped; recovery: rm $FAILS && restart." >> "$GATE_TRIP_LOG" 2>/dev/null || true; exit 1; }
  fi
}

cycle
while true; do
  sleep "$INTERVAL"
  cycle
done
