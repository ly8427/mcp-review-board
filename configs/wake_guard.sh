#!/usr/bin/env bash
# wake_guard.sh — 每条唤醒路径的最低成本纪律（board thread #53 票3 落地件）
#
# 为什么需要：本板所有自动唤醒都是 `while true; do sleep $INTERVAL; done`，
# 没有次数上限、没有停止文件，唯一停法是 `kill <pid>`（uninstall-watch.sh:51 自述）。
# thread #50 §三 的事故形状（"想停停不下来"）在本板自己的 watch 体系里原样存在。
#
# 三个动作，成本各几行：
#   wg_preflight <tag> <interval>  启动自检：节奏过密/无上限 → 拒绝启动（exit 2）
#   wg_stop_check <tag>            停止标志存在 → 干净退出（唯一不花 token 的停法）
#   wg_count_wake <tag>            持久计数 + 每轮打印 wake n/MAX，超帽即停
#
# 环境变量：
#   MAX_WAKES       每日唤醒上限；调用方给默认值（见各 watcher），<=0 或非数字 → 拒启动
#   STOP_FILE       停止标志（默认 $HOME/.rb-wake/STOP）
#   STATE_DIR       计数落点（默认 $HOME/.rb-wake）——必须在进程退出后仍在
#   WG_INTERVAL_MIN 单轮节奏下限秒（默认 600 = 10 分钟，低于此值拒绝启动）
#
# 设计纪律（写下来是为了下次不重犯）：
#   - 停止动作本身不得产生 LLM 调用（thread #50 "止血动作在放血"）；
#   - 计数必须落盘（%TEMP% / /tmp 重启即失，见 thread #50 的 C1.2 计数器教训）；
#   - 本库不改变探针/唤醒逻辑，只在其外面加壳——回退 = 删掉 source 两行。

WG_ERR_PREFIX="[wake-guard]"

wg__state_dir() { printf '%s' "${STATE_DIR:-$HOME/.rb-wake}"; }

wg_preflight() {
  _tag="${1:-watcher}"; _interval="${2:-0}"
  _min="${WG_INTERVAL_MIN:-600}"
  case "$_interval" in
    ''|*[!0-9]*) echo "$WG_ERR_PREFIX [$_tag] INTERVAL 非法: '$_interval' → 拒绝启动"; exit 2 ;;
  esac
  if [ "$_interval" -lt "$_min" ]; then
    echo "$WG_ERR_PREFIX [$_tag] 节奏过密: ${_interval}s < ${_min}s 下限 → 拒绝启动"
    echo "  理由：唤醒成本 = 次数 × 单次成本；低于 10 分钟会先烧钱再谈治理（thread #53 票3）。"
    exit 2
  fi
  case "${MAX_WAKES:-}" in
    ''|*[!0-9]*) echo "$WG_ERR_PREFIX [$_tag] MAX_WAKES 未设或非法: '${MAX_WAKES:-}' → 拒绝启动"; exit 2 ;;
  esac
  if [ "${MAX_WAKES:-0}" -le 0 ]; then
    echo "$WG_ERR_PREFIX [$_tag] MAX_WAKES=${MAX_WAKES} <= 0 → 拒绝启动（无上限=事故形态）"; exit 2
  fi
  if ! mkdir -p "$(wg__state_dir)" 2>/dev/null; then
    echo "$WG_ERR_PREFIX [$_tag] 无法创建 STATE_DIR=$(wg__state_dir) → 拒绝启动"; exit 2
  fi
  echo "$WG_ERR_PREFIX [$_tag] 在岗: INTERVAL=${_interval}s, MAX_WAKES=${MAX_WAKES}/日, STATE_DIR=$(wg__state_dir)"
}

wg_stop_check() {
  _tag="${1:-watcher}"
  _stop="${STOP_FILE:-$HOME/.rb-wake/STOP}"
  if [ -e "$_stop" ]; then
    echo "$(date +%T) STOP 标志存在 ($_stop) → 安静退出（本条不产生任何 LLM 调用）"
    echo "$WG_ERR_PREFIX [$_tag] 已按 STOP 停止；删除该文件并重启即可恢复。"
    exit 0
  fi
}

wg_count_wake() {
  _tag="${1:-watcher}"
  _dir="$(wg__state_dir)"; _d="$(date +%Y%m%d)"
  _f="$_dir/wakes.${_tag}.${_d}"
  _n=$(( $(cat "$_f" 2>/dev/null || echo 0) + 1 ))
  printf '%s\n' "$_n" > "$_f" 2>/dev/null || echo "$WG_ERR_PREFIX [$_tag] 计数落盘失败（$_f），本轮继续"
  echo "$(date +%T) wake ${_n}/${MAX_WAKES} (今日累计)"
  if [ "$_n" -gt "${MAX_WAKES:-0}" ]; then
    echo "$WG_ERR_PREFIX [$_tag] 已达今日上限 ${MAX_WAKES} → 停止唤醒（计数保留在 $_f）"
    echo "  恢复：删 $_f，或调大 MAX_WAKES 后重启。"
    exit 0
  fi
}
