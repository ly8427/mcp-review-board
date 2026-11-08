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
#   wg_count_wake <tag> [neutral]  持久计数 + 每轮打印；超帽 return 1（停唤醒不停心跳，调用方降级 keepalive）
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
  rm -f "$(wg__state_dir)/neutral_since.${_tag}" 2>/dev/null   # 纪律 2：新进程 ≠ 旧 latch
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

# wg_count_wake <tag> [neutral] — 持久计数 + 每轮打印；超帽返回 1（**不再 exit**）。
# [batch3-watcher-fix 2026-11-09，board #103 #1747–#1751 / GPT 第五轮 P2]：
#   ① 超帽动作从 exit 0（进程整个退出=心跳也停）降级为 return 1——调用方据此跳过
#     本次 LLM 唤醒但**继续循环**（探针照发=心跳不停，dsh #1751 结论 3「帽照数、
#     帽的动作改成停唤醒不停心跳」）。调用方须处理返回 1（四支已同批接线）。
#   ② 第二参为 neutral（非空）时：本轮是 corrupt-await 中性轮——不计数、不查帽、
#     照常放行（pi #1748 §二.1：否则中性轮 24h 后照样被日帽杀闹钟；成本有界，
#     受 reset 窗口约束）。日志行留痕。
# 返回：0=可唤醒；1=超帽（调用方降级）。
wg_count_wake() {
  _tag="${1:-watcher}"
  _dir="$(wg__state_dir)"; _d="$(date +%Y%m%d)"
  _f="$_dir/wakes.${_tag}.${_d}"
  if [ -n "${2:-}" ]; then
    echo "$(date +%T) wake (corrupt-await 中性轮——不计入日帽，board #103 #1748 §二.1)"
    return 0
  fi
  _n=$(( $(cat "$_f" 2>/dev/null || echo 0) + 1 ))
  printf '%s\n' "$_n" > "$_f" 2>/dev/null || echo "$WG_ERR_PREFIX [$_tag] 计数落盘失败（$_f），本轮继续"
  echo "$(date +%T) wake ${_n}/${MAX_WAKES} (今日累计)"
  if [ "$_n" -gt "${MAX_WAKES:-0}" ]; then
    echo "$WG_ERR_PREFIX [$_tag] 已达今日上限 ${MAX_WAKES} → 本轮跳过唤醒（心跳保持；计数保留在 $_f）"
    echo "  恢复：删 $_f，或调大 MAX_WAKES 后重启。"
    return 1
  fi
  return 0
}

# [batch3-watcher-fix] 中性轮判定两件（GPT 第五轮 P2：watcher 失败计数须识别
# 「等待人工 reset」态，board #103 #1747/#1749/#1751 定稿）：
#
# wg__tb_obj <response> — 从紧凑 JSON 响应里**括号配对**提取 token_blocked 对象
#   （含花括号本体）。禁全响应 substring（dsh T6：break_glass 出现在别处不得命中）、
#   禁右邻锚定 sed（dsh #1751 §二.2：右邻从 last_read 换代 meta_unparsable 会静默失效）。
#   已知限界：不做引号内花括号跳过——现行 wire 的 since/exit 值不含花括号
#   （#1751 §二.1 两例原文），fixture T2/T6 亦不含；若未来值引入花括号，本函数
#   须先升级再上量。rc 0=提取成功（对象打到 stdout）；1=键不存在/未闭合。
# wg__tb_obj <response> — 括号配对提取 token_blocked 对象（含花括号本体）。
#   返回：0=提取成功（对象打 stdout）；1=键在但括号不闭合（截断/代理改写）；
#        2=键不存在（健康行常态/键前截断）。紧凑 JSON 观测形态收进本函数
#        （dsh #1756 §三：wire 上该键之前全 ASCII——#1751 §二.2 的右邻换代由
#        调用方按对象判定，不靠右锚）。
wg__tb_obj() {
  _s="$1"
  # B5 (dsh #1757 §一): shortest-prefix strip instead of grep -b byte offset used
  # as a character index — the byte/char mismatch class is deleted, not documented.
  case "$_s" in
    *'"token_blocked":{'*) ;; *) return 2 ;; esac
  _tail=${_s#*'"token_blocked":{'}
  _depth=1; _out="{"
  while [ -n "$_tail" ]; do
    _c=${_tail:0:1}; _tail=${_tail:1}
    case "$_c" in
      '{') _depth=$((_depth+1)); _out="$_out$_c" ;;
      '}') _depth=$((_depth-1)); _out="$_out$_c"
           [ "$_depth" -eq 0 ] && { printf '%s' "$_out"; return 0; } ;;
      *)   _out="$_out$_c" ;;
    esac
  done
  return 1
}

# wg_judge_surface <rc> <pre_att> <pre_reason> <pre_threads> <post_response>
#   → stdout: delivered | neutral | unknown | failed
# 语序写死（dsh #1751 结论 2，中性不得吞 delivered）：
#   1) rc!=0 → failed（crash/hang 计费不豁免，T3）
#   2) 表面动了（attention 掉 0 或 reason/threads 变）→ delivered（T2b）
#   3) 表面未动 且 post 响应含本 author 的 break_glass latch → neutral（等待人工，
#      T1；latch 键=字面 corrupt 非成员名，#1751 §二.1；提取走 wg__tb_obj 括号配对）
#   3.5) 表面未动 且 post 无 token_blocked 键（fixture 失真/键前截断）→ failed；
#        键在但括号不闭合 → unknown（T-140）——边界显式化 dsh #1776 §四.1/§四.2。
#   4) 其余（含 token_blocked 空/tier=manual/缺 tier——服务端已归一 manual，T5 是
#      谓词级防御）→ failed（检测力不变）
wg_judge_surface() {
  case "${1:-}" in ''|*[!0-9]*) printf 'failed'; return 0 ;; esac   # B3: 非数字 rc 不当 0
  [ "$1" -ne 0 ] && { printf 'failed'; return 0; }                 # rc!=0 → failed（语序 1：crash/hang 计费不豁免，T3）
  _p_att="${2:-}"; _p_reason="${3:-}"; _p_threads="${4:-}"; _post="${5:-}"
  [ -z "$_post" ] && { printf 'unknown'; return 0; }                # B2→unknown（dsh #1760 §一）：中性态里探针失败判 failed 会把 C1.2 从网络面重开
  _aatt=$(printf '%s' "$_post" | sed -n 's/.*"attention":\([0-9]\).*/\1/p')
  [ -z "$_aatt" ] && { printf 'unknown'; return 0; }                # B2：缺 attention 字段同判（T2c 同 commit 翻转，N2）
  _areason=$(printf '%s' "$_post" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p')
  _athreads=$(printf '%s' "$_post" | sed -n 's/.*"threads":\[\([^]]*\)\].*/\1/p')
  if [ "${_aatt:-0}" != "1" ] || [ "$_areason" != "$_p_reason" ] || [ "$_athreads" != "$_p_threads" ]; then
    printf 'delivered'; return 0
  fi
  _tb=$(wg__tb_obj "$_post"); _tb_rc=$?   # 单次提取（对象接住，不漏进 stdout）
  case "$_tb_rc" in
    2) printf 'failed'; return 0 ;;    # 键不在（#1776 §四.2 单源）
    1) printf 'unknown'; return 0 ;;   # 键在但括号不闭合（T-140）
    0) : ;;
    *) printf 'failed'; return 0 ;;
  esac
  wg__is_neutral "$_tb" && { printf 'neutral'; return 0; }
  printf 'failed'
}

# wg__is_neutral <token_blocked_object> — 0/1。break_glass 字面唯一住址（B1，dsh
# #1754 §二：三份手抄会让日帽预检与 C1.2 中性判定对同一响应分叉而套件照绿）。
wg__is_neutral() {
  case "$1" in
    *'"tier":"break_glass"'*) return 0 ;;
    *)                        return 1 ;;
  esac
}

# wg_apply_verdict <verdict> <tag> <detail> — 判定落账（FAILS 写/日志/C1.2 闸），
#   载体=被 source 的真函数（dsh #1751 结论 1：判据禁抄进测试文件；T1–T8 直接打本函数）。
#   failed 时 $_n 计数走 $FAILS（调用方已指向 STATE_DIR 持久面，T8 禁 /tmp）；
#   3 连败 → 日志 gate tripped 并 exit 1（T3：crash/hang 计费不豁免）。
# wg_judge_cursor <rc> <pre_lr> <pre_id> <post_lr> <post_id> → delivered|failed
#   [batch3-watcher-fix] 判据逐字搬入 wg_judge_cursor（C4，dsh #1757 §3：载体只能落
#   本库——source pi-watcher.sh 会自杀/真唤醒/永不返回）。判据与 pi-watcher 原两腿
#   逐字同（含空串守卫）；rc 门=签名防御、生产恒 0（唯一调用点传字面 0）、勿删——#1766 §4。
wg_judge_cursor() {
  case "${1:-}" in ''|*[!0-9]*) printf 'failed'; return 0 ;; esac
  [ "$1" -ne 0 ] && { printf 'failed'; return 0; }   # rc≠0 ⇒ failed（pi #1761 §四；判别格 T-pi ⑤ 钉红前绿后）
  _pre_lr="${2:-}"; _pre_id="${3:-}"; _post_lr="${4:-}"; _post_id="${5:-}"
  if { [ -n "$_post_lr" ] && [ "$_post_lr" \> "${_pre_lr:-0}" ]; } || \
     { [ -n "$_post_id" ] && [ "${_pre_id:-0}" -lt "$_post_id" ]; }; then
    printf 'delivered'
  else
    printf 'failed'
  fi
}

wg_apply_verdict() {
  _verdict="${1:-failed}"; _vtag="${2:-watcher}"; _vdetail="${3:-empty-wake}"
  _ns="$(wg__state_dir)/neutral_since.${_vtag}"
  case "$_verdict" in
    delivered)
      echo 0 > "$FAILS"
      rm -f "$_ns"   # neutral_since 纪律 1（dsh #1757 §三）：delivered 支也要删
      echo "$(date +%T) delivered (attention consumed — delivery, not task-completion)" ;;
    neutral)
      _pf=$(cat "$FAILS" 2>/dev/null || echo 0)   # C3：prior_fails 上日志
      echo 0 > "$FAILS"
      date -u +%FT%TZ > "$_ns" 2>/dev/null        # neutral_since 哨兵（UTC，#1757 §三.3）
      echo "$(date +%T) corrupt-await neutral (break_glass latch) — heartbeat kept, awaiting human reset (prior_fails=$_pf)" ;;
    unknown)
      echo "$(date +%T) probe unknown — no bookkeeping (FAILS/neutral_since unchanged) (($_vtag/$_vdetail))" ;;
    *)
      rm -f "$_ns"                                 # neutral_since 纪律 1：failed 支同样删
      _n=$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 ))
      echo "$_n" > "$FAILS"
      echo "$(date +%T) wake counted as failure ($_vdetail) ($_n/3)"
      [ "$_n" -ge 3 ] && {
        echo "gate tripped (C1.2): stopping heartbeat; member will suspend after 24h idle."
        # C4（dsh #1763 §三 直读形）：GATE_TRIP_LOG 仅 pi-watcher 定义（普通变量同 shell 可见）；
        # 另三支展开为空=零行为变化。恢复 pi-watcher 主路径（游标不动）跳闸的持久记录。
        [ -n "${GATE_TRIP_LOG:-}" ] && printf '%s\n' "$(date '+%F %T') [$_vtag] gate tripped (C1.2) — $_vdetail; watcher stopped; recovery: rm $FAILS && restart." >> "$GATE_TRIP_LOG" 2>/dev/null || true
        exit 1
      } ;;
  esac
}
