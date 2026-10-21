#!/usr/bin/env bash
# pi-watcher.sh — review board 成员 pi 的宿主侧常驻 watcher（唤起契约 (a)）。
# kit-version: 1.0.0 (2026-10-21, thread #45 rev2 L2 实施配套；结构同 claude-watcher.sh)
#
# pi 形态（onboarding.md 成员档案）: WSL 内 headless 单发，`pi -p --no-session`，
# 无 MCP——操作看板唯一通道 = configs/rb.sh（裸 HTTP）。运行时前提（实跑踩坑归档）：
#   node ≥22（系统 18 会 ESM 崩——必须前置 ~/opt/node22/bin）
#   DEEPSEEK_API_KEY（pi 自身凭据库无 deepseek key；本宿主从 ~/.dsh/.credentials.yaml 读，
#   与 host-watch.sh wake_pi 从 credentials.yml 读 key 同一设计）
#   显式 --model（settings 默认值历史上是坏值；本机实证可用 = deepseek/deepseek-v4-flash）
#   rb.sh 用绝对路径（模板已按 kit R3 配置为 WSL 原生绝对路径，杜绝 find / 烧超时）
# 任务文本 = configs/task-template-pi.txt 原文 + 尾部一行看板注入（reason/threads）——
#   唤起者零代写（v2.3 peer-wake 收窄条款）。
# 三保护（缺一不可，同 dsh/claude watcher）: mkdir 单飞锁 / ≥20min 节奏 / C1.2 失败闸门。
set -u

BOARD="${BOARD:-http://localhost:8765}"
AUTHOR="${AUTHOR:-pi}"
INTERVAL="${INTERVAL:-1200}"
RB_REPO="$(cd "$(dirname "$0")/.." && pwd)"
RB_SH_DIR_WSL="${RB_SH_DIR_WSL:-/mnt/c/Users/liu/ZCodeProject/mcp-review-board/configs}"  # 宿主配置：rb.sh 的 WSL 原生绝对路径
PROMPT_TEMPLATE="$RB_REPO/configs/task-template-pi.txt"
TASK_FILE="${TASK_FILE:-/tmp/rb-pi-wake.txt}"
LOCK="${LOCK:-/tmp/rb-pi-watcher.lock.d}"
FAILS="${FAILS:-/tmp/rb-pi-watcher.fails}"
EV="${EV:-/tmp/rb-pi-events.log}"

gate=$(cat "$FAILS" 2>/dev/null || echo 0)
if [ "${gate:-0}" -ge 3 ]; then
  echo "gate: >=3 consecutive wake failures; heartbeat stopped (C1.2)."
  echo "remove $FAILS and restart this watcher to re-enable."
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

probe() { curl -sf "$BOARD/attention?author=$AUTHOR"; }

echo "watching $BOARD for '$AUTHOR' every ${INTERVAL}s (probe = heartbeat, C1.1)..."
cycle() {
  touch "$LOCK" 2>/dev/null
  resp=$(probe) || { echo "$(date +%T) probe failed (network)"; return 0; }
  att=$(printf '%s' "$resp" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
  if [ "${att:-0}" != "1" ]; then echo "$(date +%T) idle"; return 0; fi
  reason=$(printf '%s' "$resp" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p')
  threads=$(printf '%s' "$resp" | sed -n 's/.*"threads":\[\([^]]*\)\].*/\1/p')
  echo "$(date +%T) attention=1 ($reason) threads=[$threads] — waking pi"

  # 固定模板 + 仅看板注入（零代写）。占位符 <rb.sh 所在目录> 在此解析为宿主配置的
  # WSL 原生绝对路径（kit R3：模板不落盘具体路径，配置由部署方在此完成）。
  { sed "s|<rb.sh 所在目录>|$RB_SH_DIR_WSL|g" "$PROMPT_TEMPLATE"
    echo
    echo "(本次唤醒探针注入: reason=$reason, threads=$threads)"
  } > "$TASK_FILE"

  # WSL 内唤醒：node22 前置 + key 注入 + 180s 停滞看门狗（thread #24 事故修复形态）
  if wsl -e bash -c '
    export PATH=$HOME/opt/node22/bin:$PATH
    KEY=$(grep -i "deepseek" /mnt/c/Users/liu/.dsh/.credentials.yaml | head -1 | sed "s/^.*[:=] *//" | tr -d "\"'"'"' ")
    [ -n "$KEY" ] || exit 1
    export DEEPSEEK_API_KEY="$KEY"
    EV='"$EV"'
    : > "$EV"
    cd ~ && timeout 900 ~/.npm-global/bin/pi -p --no-session --mode json \
      --model deepseek/deepseek-v4-flash "$(cat /mnt/c/Users/liu/AppData/Local/Temp/rb-pi-wake.txt 2>/dev/null || cat /tmp/rb-pi-wake.txt 2>/dev/null)" > "$EV" 2>&1 &
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
