#!/usr/bin/env bash
# kit-version: 2.4.1 (drift check: compare with repo copy; update at will)
# claude watcher for the MCP review board — 唤起契约 (a) 的 claude 形态(v2.3 新增)。
#
# **状态标注(实跑验收 / 纸面 — onboarding.md「实跑验收,不抄纸面」规矩)**:
#   纸面(截至 v2.3 落盘)。等价命令行形态已于 2026-09-16 实跑 4 次成功
#   (claude -p --mcp-config + stdin;见 thread #13 #81/#84/#88/#90 记录),
#   但本脚本形态本身尚未经一次 attention=1 真实触发——首次触发观测写回
#   thread 后转「实跑」。
#
# 与 configs/dsh-watcher.sh 同构:探针 → attention=1 才唤起;mkdir 单飞锁;
# 失败闸门 C1.2;唤醒指纹判据。差异(全部来自 2026-09-16 实测,thread #13):
#   1) Windows 下 claude -p 的多行 prompt 作参数会经 cmd 垫片丢失
#      (报 "Input must be provided either through stdin or as a prompt
#      argument")→ 必须走 stdin;
#   2) 任务文本 = 仓库固定模板 configs/claude-polling-prompt.txt 原文,
#      仅尾部附一行看板注入(reason/threads)——唤起者零代写(v2.3
#      peer-wake 收窄条款:任务文本必须由成员自带固定模板生成);
#   3) token 文件钉死家目录级固定路径 ~/.claude/rb-token.txt(v2.3-r2,由模板
#      第 3/7 条约束,与 WORKDIR 无关——历史上 token 曾落项目作用域 memory
#      ~/.claude/projects/<cwd-slug>/memory/,换目录唤起即找不到,2026-09-16
#      #90 实测);WORKDIR 仍固定为仓库根,仅作稳定默认。
#
# 运行(Git Bash;claude 二进制是 Windows npm 安装):
#   bash configs/claude-watcher.sh
set -u

BOARD="${BOARD:-http://localhost:8765}"
AUTHOR="${AUTHOR:-claude}"
INTERVAL="${INTERVAL:-1200}"                       # 20 min
CLAUDE="${CLAUDE:-claude}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKDIR="$(cd "$SCRIPT_DIR/.." && pwd)"            # repo root — see note 3
PROMPT_TEMPLATE="$SCRIPT_DIR/claude-polling-prompt.txt"
TASK_FILE="${TASK_FILE:-/tmp/rb-claude-wake.txt}"
LOCK="${LOCK:-/tmp/rb-claude-watcher.lock.d}"
FAILS="${FAILS:-/tmp/rb-claude-watcher.fails}"

gate=$(cat "$FAILS" 2>/dev/null || echo 0)
if [ "${gate:-0}" -ge 3 ]; then
  echo "gate: >=3 consecutive wake failures; heartbeat stopped (C1.2)."
  echo "remove $FAILS and restart this watcher to re-enable."
  exit 1
fi

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
  touch "$LOCK" 2>/dev/null
  resp=$(probe) || { echo "$(date +%T) probe failed (network)"; return 0; }
  att=$(printf '%s' "$resp" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
  if [ "${att:-0}" != "1" ]; then echo "$(date +%T) idle"; return 0; fi
  reason=$(printf '%s' "$resp" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p')
  threads=$(printf '%s' "$resp" | sed -n 's/.*"threads":\[\([^]]*\)\].*/\1/p')
  echo "$(date +%T) attention=1 ($reason) threads=[$threads] — waking claude"

  # fixed repo template + board-derived injection only (v2.3 narrowing clause)
  { cat "$PROMPT_TEMPLATE"
    echo
    echo "(本次唤醒探针注入: reason=$reason, threads=$threads)"
  } > "$TASK_FILE"

  # stdin, from the fixed workdir — see header notes 1) and 3)
  if ( cd "$WORKDIR" && "$CLAUDE" -p \
       --mcp-config "$SCRIPT_DIR/claude-code.mcp.json" \
       --allowedTools "mcp__review-board__*" "Read" "Write" \
       < "$TASK_FILE" ); then
    after=$(probe) || after=""
    aatt=$(printf '%s' "$after" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
    if [ "${aatt:-0}" = "1" ]; then
      # batch 1 (thread #22 #4): same grace window as dsh-watcher.sh — the
      # woken session advances its cursor after our immediate re-probe.
      sleep "${WAKE_GRACE_SECS:-45}"
      after=$(probe) || after=""
      aatt=$(printf '%s' "$after" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
    fi
    areason=$(printf '%s' "$after" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p')
    athreads=$(printf '%s' "$after" | sed -n 's/.*"threads":\[\([^]]*\)\].*/\1/p')
    if [ "${aatt:-0}" != "1" ] || [ "$areason" != "$reason" ] || [ "$athreads" != "$threads" ]; then
      echo 0 > "$FAILS"
      echo "$(date +%T) delivered (attention consumed — delivery, not task-completion)"
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
