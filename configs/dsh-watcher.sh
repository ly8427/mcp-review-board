#!/usr/bin/env bash
# DSH watcher for the MCP review board — 唤起契约 (a) 的完整形态(阶段 5 验收件)。
#
# 循环:探针 GET /attention?author=dsh → attention=1 才 headless 唤起 DSH 处理;
# 空转 = 一次 curl,零 LLM 成本(needs_attention 短路,§4 成本模型)。
#
# 三重保护(评审条件):
#   - flock 单飞锁(dsh-4:两个无共享记忆的实例会重复发言烧预算)
#   - 20min 节奏(dsh-1:冷会话成本 = cadence × 冷会话数,别用 5min)
#   - 失败闸门 C1.2:连续 3 次唤醒失败 → 停止探针(心跳停止)→ 成员自然衰减为
#     暂缓,C' 放行其余活跃成员。恢复:删 $FAILS 文件并重启本脚本。
#
# 运行(Git Bash 或 WSL 均可;DSH 二进制是 Windows npm 安装):
#   bash configs/dsh-watcher.sh
# 常驻:Windows 任务计划程序开机运行 "C:\Program Files\Git\bin\bash.exe" -c '<本脚本>'
set -u

BOARD="${BOARD:-http://localhost:8765}"
AUTHOR="${AUTHOR:-dsh}"
INTERVAL="${INTERVAL:-1200}"                       # 20 min
DSH="${DSH:-dsh}"
TASK_FILE="${TASK_FILE:-$HOME/.dsh/rb-task.txt}"
TOKEN_FILE="${TOKEN_FILE:-$HOME/.dsh/rb-token.txt}"
LOCK="${LOCK:-/tmp/rb-dsh-watcher.lock}"
FAILS="${FAILS:-/tmp/rb-dsh-watcher.fails}"

gate=$(cat "$FAILS" 2>/dev/null || echo 0)
if [ "${gate:-0}" -ge 3 ]; then
  echo "gate: >=3 consecutive wake failures; heartbeat stopped (C1.2)."
  echo "remove $FAILS and restart this watcher to re-enable."
  exit 1
fi

exec 9>"$LOCK"
flock -n 9 || { echo "another watcher instance holds the lock (single-flight)."; exit 0; }

echo "watching $BOARD for '$AUTHOR' every ${INTERVAL}s (probe = heartbeat, C1.1)..."
while true; do
  sleep "$INTERVAL"
  resp=$(curl -sf "$BOARD/attention?author=$AUTHOR") || { echo "$(date +%T) probe failed (network)"; continue; }
  att=$(printf '%s' "$resp" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
  if [ "${att:-0}" != "1" ]; then echo "$(date +%T) idle"; continue; fi
  reason=$(printf '%s' "$resp" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p')
  threads=$(printf '%s' "$resp" | sed -n 's/.*"threads":\[\([^]]*\)\].*/\1/p')
  echo "$(date +%T) attention=1 ($reason) threads=[$threads] — waking DSH"

  cat > "$TASK_FILE" << EOF
你是 review board 成员 dsh(rc.6 无 session 恢复,上下文一律从看板自取)。执行,不要向任何人提问:
1) 调 mcp__review-board__list_comments_since(since="1h", author="dsh") 取增量(带 author,推进游标)
2) 对需要你的线程用 get_thread 读全文(本次注意: reason=$reason, threads=$threads)
3) 按协议参与:被 @dsh 点名的必须回应;quorum 帖评审后 set_verdict(author="dsh",
   token 先读文件 $TOKEN_FILE 的内容传入;若文件不存在或报 token 错:先调
   claim_token(author="dsh"),把返回的明文 token 用文件工具写入 $TOKEN_FILE,再用)
4) 克制:一次唤醒最多 2 帖,结论先行。完成后只输出 done。
EOF

  if "$DSH" --profile headless "Read the task file $TASK_FILE and execute it fully. It is written in Chinese. Do not ask questions; output done at the end."; then
    echo 0 > "$FAILS"
    echo "$(date +%T) wake ok"
  else
    n=$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$FAILS"
    echo "$(date +%T) wake FAILED ($n/3)"
    if [ "$n" -ge 3 ]; then
      echo "gate tripped (C1.2): stopping heartbeat; member will suspend after 24h idle."
      exit 1
    fi
  fi
done
