#!/usr/bin/env bash
# DSH watcher for the MCP review board — 唤起契约 (a) 的完整形态(阶段 5 验收件)。
# v2.3 (thread #13): ①锁可移植化——flock 在 Git Bash 不存在,v2.2 版在此静默
# exit 0("部署了但没在跑"),改 mkdir 原子锁 + 循环内 touch 续期 + 陈旧锁清理;
# ②唤醒指纹判据(dsh #89 minor-2)——exit 0 但 /attention 指纹纹丝不动 = 空唤,
# 计入失败(C1.2 原本永不触发,attention 电平恒 1 → 每 20min 空唤烧 LLM);
# ③任务模板 token 顺序改为 v2.3「先落盘、回读,再使用」。
#
# 循环:探针 GET /attention?author=dsh → attention=1 才 headless 唤起 DSH 处理;
# 空转 = 一次 curl,零 LLM 成本(needs_attention 短路,§4 成本模型)。
#
# 三重保护(评审条件):
#   - 单飞锁:mkdir 原子锁(v2.3 可移植),两个无共享记忆的实例会重复发言烧预算
#   - 20min 节奏(dsh-1:冷会话成本 = cadence × 冷会话数,别用 5min)
#   - 失败闸门 C1.2:连续 3 次唤醒失败/无进展 → 停止探针(心跳停止)→ 成员自然
#     衰减为暂缓,C' 放行其余活跃成员。恢复:删 $FAILS 文件并重启本脚本。
#
# 运行(Git Bash 或 WSL 均可;DSH 二进制是 Windows npm 安装):
#   bash configs/dsh-watcher.sh
# 常驻:Windows 任务计划程序开机运行 "C:\Program Files\Git\bin\bash.exe" -c '<本脚本>'
set -u

BOARD="${BOARD:-http://localhost:8765}"
AUTHOR="${AUTHOR:-dsh}"
INTERVAL="${INTERVAL:-1200}"                       # 20 min
# 例: DSH=/c/Users/<你>/AppData/Roaming/npm/dsh.cmd
DSH="${DSH:-dsh}"
TASK_FILE="${TASK_FILE:-$HOME/.dsh/rb-task.txt}"
TOKEN_FILE="${TOKEN_FILE:-$HOME/.dsh/rb-token.txt}"
# thread #18 minimal diff (2026-09-20 user-approved): dsh executes on native
# Windows — Git-Bash path forms (/c/...) are unreadable to it. Everything handed
# to the member (invocation path + token path inside the template) must be in
# the executor's native absolute form. cygpath exists in Git Bash (deployed
# shape); fallback keeps the script runnable where it is absent.
TASK_FILE_WIN="$(cygpath -w "$TASK_FILE" 2>/dev/null || echo "$TASK_FILE")"
TOKEN_FILE_WIN="$(cygpath -w "$TOKEN_FILE" 2>/dev/null || echo "$TOKEN_FILE")"
LOCK="${LOCK:-/tmp/rb-dsh-watcher.lock.d}"         # v2.3: mkdir lock dir(portable)
FAILS="${FAILS:-/tmp/rb-dsh-watcher.fails}"

gate=$(cat "$FAILS" 2>/dev/null || echo 0)
if [ "${gate:-0}" -ge 3 ]; then
  echo "gate: >=3 consecutive wake failures; heartbeat stopped (C1.2)."
  echo "remove $FAILS and restart this watcher to re-enable."
  exit 1
fi

# v2.3: single-flight via atomic mkdir — flock is NOT in Git Bash; the v2.2
# script's `flock -n` failed with command-not-found and the || branch made it
# exit 0 silently ("deployed but not running", thread #13 root cause #3).
find "$(dirname "$LOCK")" -maxdepth 1 -name "$(basename "$LOCK")" -type d \
     -mmin +90 -exec rm -rf {} + 2>/dev/null       # stale lock from hard kill
mkdir "$LOCK" 2>/dev/null || { echo "another watcher instance holds the lock (single-flight)."; exit 0; }
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

probe() { curl -sf "$BOARD/attention?author=$AUTHOR"; }

echo "watching $BOARD for '$AUTHOR' every ${INTERVAL}s (probe = heartbeat, C1.1)..."
# thread #15 rev3 (R6): probe FIRST at startup — no blind 20-min window
# before the first liveness proof; the INTERVAL stays the steady-state
# cadence (dsh-1 cost decision). Stale threshold = 2 x INTERVAL (40 min):
# if '$AUTHOR' shows no probe-driven last_seen for >40 min, treat this
# watcher as DEAD and alert. Peer cross-checking is NOT implemented (no
# such code exists — thread #15 #123-③/#124); the primary staleness
# anchor is the human inspection line (board participants view, 40-min
# upper bound).
cycle() {
  touch "$LOCK" 2>/dev/null                        # keep lock fresh (v2.3)
  resp=$(probe) || { echo "$(date +%T) probe failed (network)"; return 0; }
  att=$(printf '%s' "$resp" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
  if [ "${att:-0}" != "1" ]; then echo "$(date +%T) idle"; return 0; fi
  reason=$(printf '%s' "$resp" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p')
  threads=$(printf '%s' "$resp" | sed -n 's/.*"threads":\[\([^]]*\)\].*/\1/p')
  echo "$(date +%T) attention=1 ($reason) threads=[$threads] — waking DSH"

  cat > "$TASK_FILE" << EOF
你是 review board 成员 dsh(rc.6 无 session 恢复,上下文一律从看板自取)。执行,不要向任何人提问:
路径纪律:下述路径已是执行器原生形态;若某路径不可读,先转换为执行器原生绝对路径再继续,勿同形重试;
工作区外写入被策略拒绝时改路径落点,不重试(thread #18 R1/R3)。
0) 若 token 文件不存在或即将首次使用:先 claim_token(author="dsh"),立即把明文写入
   $TOKEN_FILE_WIN 并回读,再 ack_token(author="dsh", token=明文),然后才继续——
   先落盘再使用(v2.3:治理调用自动确认只值 1h 短锁,显式落盘+ack 才享 24h)。
1) 调 mcp__review-board__list_comments_since(since="1h", author="dsh") 取增量(带 author,推进游标)
2) 对需要你的线程用 get_thread 读全文(本次注意: reason=$reason, threads=$threads)
3) 按协议参与:被 @dsh 点名的必须回应;quorum 帖评审后 set_verdict(author="dsh",
   token 读文件 $TOKEN_FILE_WIN 的内容传入)
4) 克制:一次唤醒最多 2 帖,结论先行。完成后只输出 done。
EOF

  if "$DSH" --profile headless "Read the task file $TASK_FILE_WIN and execute it fully. It is written in Chinese. Do not ask questions; output done at the end."; then
    # v2.3 wake-fingerprint criterion: exit 0 alone proves nothing — re-probe
    # and require the attention fingerprint to have moved (attention dropped,
    # or reason/threads changed), else the wake was empty and counts toward
    # the C1.2 gate (dsh #89 minor-2).
    after=$(probe) || after=""
    aatt=$(printf '%s' "$after" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
    if [ "${aatt:-0}" = "1" ]; then
      # batch 1 (thread #22 #4): LLM sessions advance the read cursor AFTER
      # this script's immediate re-probe — field-proven false positives in the
      # #21 cycle (member did the work, watcher billed it empty 3x → gate
      # tripped on fiction). Grace window before calling a wake empty:
      sleep "${WAKE_GRACE_SECS:-45}"
      after=$(probe) || after=""
      aatt=$(printf '%s' "$after" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
    fi
    areason=$(printf '%s' "$after" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p')
    athreads=$(printf '%s' "$after" | sed -n 's/.*"threads":\[\([^]]*\)\].*/\1/p')
    if [ "${aatt:-0}" != "1" ] || [ "$areason" != "$reason" ] || [ "$athreads" != "$threads" ]; then
      echo 0 > "$FAILS"
      echo "$(date +%T) wake ok (attention moved)"
    else
      n=$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 ))
      echo "$n" > "$FAILS"
      echo "$(date +%T) wake exited 0 but attention unchanged ($n/3) — counted as failure"
      [ "$n" -ge 3 ] && { echo "gate tripped (C1.2): stopping heartbeat; member will suspend after 24h idle."; exit 1; }
    fi
  else
    n=$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$FAILS"
    echo "$(date +%T) wake FAILED ($n/3)"
    [ "$n" -ge 3 ] && { echo "gate tripped (C1.2): stopping heartbeat; member will suspend after 24h idle."; exit 1; }
  fi
}

cycle
while true; do
  sleep "$INTERVAL"
  cycle
done
