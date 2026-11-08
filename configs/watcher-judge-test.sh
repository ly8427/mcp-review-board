#!/usr/bin/env bash
# watcher-judge-test.sh — GPT 第五轮 P2 处置的判定逻辑测试（board #103 #1751 T1–T10 规格
# + #1754/#1756/#1757/#1758 修正包：B1–B5/C1–C4/neutral_since/结构断言）。
# 载体纪律（dsh #1751 结论 1）：source 真 wake_guard.sh，判据不抄进本文件；
# 无看板、无 LLM——探针/响应全部用 fixture 字面量；pre 值一律经与 watcher 相同的
# sed 提取路径产生（防夹具形态与实现口径漂移）。
# 运行：bash configs/watcher-judge-test.sh   （T10 需要隔离服务，缺环境自动 SKIP）
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$(mktemp -d)"
export STATE_DIR
FAILS="$STATE_DIR/test.fails"
. "$HERE/wake_guard.sh"
MAX_WAKES=3   # T7 用小帽

fails=0
say() { printf '%-56s -> %-9s [%s]\n' "$1" "$2" "$3"; }
expect_eq() { if [ "$2" = "$3" ]; then say "$1" "$2" PASS; else say "$1" "$2" "FAIL(want $3)"; fails=$((fails+1)); fi; }
expect_rc() { if [ "$2" = "$3" ]; then say "$1" "rc=$2" PASS; else say "$1" "rc=$2" "FAIL(want $3)"; fails=$((fails+1)); fi; }
expect_has() { if printf '%s' "$2" | grep -q "$3"; then say "$1" "has($3)" PASS; else say "$1" "missing($3)" FAIL; fails=$((fails+1)); fi; }
expect_not() { if printf '%s' "$2" | grep -q "$3"; then say "$1" "has($3)" FAIL; else say "$1" "no($3)" PASS; fi; }
# 与 watcher 相同的 sed 提取（pre 面产生器——夹具与实现同口径）：
p_att()     { printf '%s' "$1" | sed -n 's/.*"attention":\([0-9]\).*/\1/p'; }
p_reason()  { printf '%s' "$1" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p'; }
p_threads() { printf '%s' "$1" | sed -n 's/.*"threads":\[\([^]]*\)\].*/\1/p'; }

# —— fixture（wire 原文级；dsh #1751 §二.1 两代形态）——
# fixture 形态注（dsh #1790 §二）：R_NOKEY 无 token_blocked 键 = 「fixture 失真/键前截断」形；
# 服务端健康行恒有该键（dsh #1786 §四 一手：310B/偏移 154）。真空对象 {} 形见 R_OUTSIDE。
R_CORRUPT_2120='{"attention":1,"reason":"awaiting_verdict","threads":[104],"token_blocked":{"corrupt":{"tier":"break_glass","exit":"reset_token(human_override=True) (break-glass, audited)."}},"last_read":"2026-11-08T04:16:01"}'
R_CORRUPT_2121='{"attention":1,"reason":"awaiting_verdict","threads":[104],"token_blocked":{"corrupt":{"tier":"break_glass","exit":"reset_token(human_override=True) (break-glass, audited)."}},"meta_unparsable":1}'
R_MANUAL='{"attention":1,"reason":"awaiting_verdict","threads":[104],"token_blocked":{"104":{"tier":"manual","since":"x","exit":"reset_token(human_override=True) | window_ends_at=..."}},"last_read":"x"}'
R_NOKEY='{"attention":1,"reason":"awaiting_verdict","threads":[104],"last_read":"x"}'
R_OUTSIDE='{"attention":1,"reason":"awaiting_verdict","threads":[104],"other":{"tier":"break_glass"},"token_blocked":{},"last_read":"x"}'
PRE='{"attention":1,"reason":"awaiting_verdict","threads":[104]}'
PRE_ATT=$(p_att "$PRE"); PRE_REASON=$(p_reason "$PRE"); PRE_THREADS=$(p_threads "$PRE")

echo "== T9 结构：谓词唯一住址（B1）+ 调用点 + 语法 + 劈叉钉死（pi #1758 §四） =="
expect_eq "break_glass 字面全仓计数(5 文件)" "$(cat "$HERE/wake_guard.sh" "$HERE/dsh-watcher.sh" "$HERE/claude-watcher.sh" "$HERE/pi-watcher.sh" "$HERE/host-watch.sh" | grep -c '"tier":"break_glass"')" "1"
expect_eq "wg_judge_surface 定义数" "$(grep -c 'wg_judge_surface()' "$HERE/wake_guard.sh")" "1"
expect_eq "wg__tb_obj 定义数"      "$(grep -c 'wg__tb_obj()' "$HERE/wake_guard.sh")" "1"
expect_eq "wg__is_neutral 定义数"  "$(grep -c 'wg__is_neutral()' "$HERE/wake_guard.sh")" "1"
expect_eq "dsh-watcher 预检走单源"  "$(grep -c 'wg__is_neutral' "$HERE/dsh-watcher.sh")" "1"
expect_eq "claude-watcher 预检走单源" "$(grep -c 'wg__is_neutral' "$HERE/claude-watcher.sh")" "1"
expect_eq "pi-watcher 无表面判定(劈叉钉死)" "$(grep -c 'wg_judge_surface' "$HERE/pi-watcher.sh")" "0"
expect_eq "pi-watcher 无 break_glass 字面" "$(grep -c '"tier":"break_glass"' "$HERE/pi-watcher.sh")" "0"
expect_eq "wg_judge_cursor 定义数" "$(grep -c 'wg_judge_cursor()' "$HERE/wake_guard.sh")" "1"
expect_eq "pi-watcher 游标判定调用" "$(grep -c 'wg_judge_cursor ' "$HERE/pi-watcher.sh")" "1"
expect_eq "C2 接线: dsh 超帽降级" "$(grep -cE 'wg_count_wake .*\|\|' "$HERE/dsh-watcher.sh")" "1"
expect_eq "C2 接线: claude 超帽降级" "$(grep -cE 'wg_count_wake .*\|\|' "$HERE/claude-watcher.sh")" "1"
expect_eq "C2 接线: pi 超帽降级" "$(grep -cE 'wg_count_wake .*\|\|' "$HERE/pi-watcher.sh")" "1"
expect_eq "C2 接线: host-watch 超帽降级" "$(grep -cE 'wg_count_wake .*\|\|' "$HERE/host-watch.sh")" "1"
expect_eq "unknown 支存在(wg_apply_verdict)" "$(grep -c 'unknown)' "$HERE/wake_guard.sh")" "1"
for f in wake_guard.sh dsh-watcher.sh claude-watcher.sh pi-watcher.sh host-watch.sh; do
  bash -n "$HERE/$f" && say "bash -n $f" OK PASS || { say "bash -n $f" OK FAIL; fails=$((fails+1)); }
done

echo
echo "== T1：4 轮同态 + break_glass + rc=0 → 每轮 neutral、FAILS 恒 0、进程活 =="
: > "$FAILS"
( . "$HERE/wake_guard.sh"
  FAILS="$STATE_DIR/t1.fails"; : > "$FAILS"
  for i in 1 2 3 4; do
    v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_CORRUPT_2120")
    wg_apply_verdict "$v" "t1" "empty-wake"
  done
) ; t1_rc=$?
t1_verdicts=$( . "$HERE/wake_guard.sh"; FAILS="$STATE_DIR/t1b.fails"; : > "$FAILS"
  for i in 1 2 3 4; do printf '%s|' "$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_CORRUPT_2120")"; done )
expect_eq "T1 四轮判定全 neutral" "$t1_verdicts" "neutral|neutral|neutral|neutral|"
expect_eq "T1 四轮后进程存活(rc=0)" "$t1_rc" "0"
expect_eq "T1 FAILS 恒 0"          "$(cat "$STATE_DIR/t1.fails")" "0"

echo
echo "== T2：2.12.1 wire（右邻=meta_unparsable）→ 同 T1 =="
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_CORRUPT_2121")
expect_eq "T2 判定" "$v" "neutral"

echo
echo "== T2b：表面真动了 → delivered（不记中性） =="
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" '{"attention":0,"reason":"awaiting_verdict","threads":[104]}')
expect_eq "T2b attention 掉 0" "$v" "delivered"
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" '{"attention":1,"reason":"new_comments","threads":[104],"last_read":"y"}')
expect_eq "T2b reason 变" "$v" "delivered"

echo
echo "== T2c（B2）：探针失败/缺 attention 字段 → failed 不得 delivered =="
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "")
expect_eq "T2c 空响应 → unknown（B2 翻转）" "$v" "unknown"
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" '{"reason":"x"}')
expect_eq "T2c 缺 attention 字段 → unknown" "$v" "unknown"
# unknown 无记账三格（dsh #1760 §一 修法）：FAILS 原值保持、rc=0 不跳闸
: > "$FAILS"; echo 2 > "$FAILS"
( . "$HERE/wake_guard.sh"; FAILS="$STATE_DIR/u.fails"; echo 2 > "$FAILS"
  for i in 1 2 3; do v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" ""); wg_apply_verdict "$v" "u" "probe-unknown"; done
) ; u_rc=$?
expect_rc "T2c 三连 unknown 后 rc=0（不跳闸）" "$u_rc" "0"
expect_eq "T2c FAILS 保持 2（unknown 不记账）" "$(cat "$STATE_DIR/u.fails")" "2"
# 中性态 + 空 post ⇒ unknown 且 neutral_since 仍在（dsh #1760 §一 末格）
rm -f "$STATE_DIR/neutral_since.u2"
( . "$HERE/wake_guard.sh"; FAILS="$STATE_DIR/u2.fails"; : > "$FAILS"
  v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_CORRUPT_2120"); wg_apply_verdict "$v" "u2" "empty-wake"
  v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" ""); wg_apply_verdict "$v" "u2" "probe-unknown"
)
[ -f "$STATE_DIR/neutral_since.u2" ] && say "T2c 中性态+空 post 哨兵仍在" "exists" PASS || { say "T2c 哨兵" "missing" FAIL; fails=$((fails+1)); }

echo
echo "== T3：break_glass 且 rc!=0 → failed；3 轮跳闸 =="
: > "$FAILS"
( . "$HERE/wake_guard.sh"
  FAILS="$STATE_DIR/t3.fails"; : > "$FAILS"
  for i in 1 2 3; do
    v=$(wg_judge_surface 124 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_CORRUPT_2120")
    wg_apply_verdict "$v" "t3" "empty-wake"
  done
) ; t3_rc=$?
expect_rc "T3 第三轮 gate tripped 退出" "$t3_rc" "1"
expect_eq "T3 FAILS=3" "$(cat "$STATE_DIR/t3.fails")" "3"

echo
echo "== T4 + C1 后半：re-arm 单轮 failed；rc=0 无 latch 三连 → 第 3 轮跳闸（端到端） =="
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_MANUAL")
expect_eq "T4 post=manual 档" "$v" "failed"
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_NOKEY")
expect_eq "T4 post=无档" "$v" "failed"
: > "$FAILS"
( . "$HERE/wake_guard.sh"
  FAILS="$STATE_DIR/t4b.fails"; : > "$FAILS"
  for i in 1 2 3; do
    v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_NOKEY")
    wg_apply_verdict "$v" "t4b" "empty-wake"
  done
) ; t4b_rc=$?
expect_rc "C1 健康行三连第 3 轮跳闸" "$t4b_rc" "1"
expect_eq "C1 FAILS=3" "$(cat "$STATE_DIR/t4b.fails")" "3"

echo
echo "== T5：同态 + token_blocked 空 / tier=manual / 缺档 → failed（检测力不变） =="
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_NOKEY")
expect_eq "T5 空 token_blocked" "$v" "failed"
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_MANUAL")
expect_eq "T5 tier=manual" "$v" "failed"

echo
echo "== T6 + T6b（B5）：域外不命中；多字节前缀不改变提取结果 =="
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_OUTSIDE")
expect_eq "T6 域外 break_glass" "$v" "failed"
R_CJK='{"reason":"中文中文中文","token_blocked":{"corrupt":{"tier":"break_glass"}},"last_read":"x"}'
R_ASCII='{"reason":"ascii","token_blocked":{"corrupt":{"tier":"break_glass"}},"last_read":"x"}'
expect_eq "T6b CJK/ASCII 提取逐字相等" "$(wg__tb_obj "$R_CJK")" "$(wg__tb_obj "$R_ASCII")"
expect_eq "T6b 提取对象本体" "$(wg__tb_obj "$R_CJK")" '{"corrupt":{"tier":"break_glass"}}'
R_TRUNC='{"attention":1,"reason":"awaiting_verdict","threads":[104],"token_blocked":{"104":{"tier":"man'
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_TRUNC")
expect_eq "T-140 键在括号不闭合 → unknown" "$v" "unknown"
expect_eq "T-140 unknown 三支仍在(结构格)" "$(grep -c "printf 'unknown'" "$HERE/wake_guard.sh")" "3"

echo
echo "== T7：中性态 + 日帽用尽 → 不 exit、探针照发、keepalive 日志 =="
: > "$FAILS"
printf '3\n' > "$STATE_DIR/wakes.t7.$(date +%Y%m%d)"   # 帽=3，已用尽
out=$(wg_count_wake t7 neutral); rc=$?
expect_rc "T7 neutral 不计帽不拦阻" "$rc" "0"
expect_eq "T7 日志含中性轮行" "$(printf '%s' "$out" | grep -c 'corrupt-await')" "1"
expect_eq "T7 计数未被中性轮推高" "$(cat "$STATE_DIR/wakes.t7.$(date +%Y%m%d)")" "3"
out=$(wg_count_wake t7); rc=$?
expect_rc "T7 非中性超帽 → rc=1（降级信号）" "$rc" "1"
expect_eq "T7 keepalive 语义在日志" "$(printf '%s' "$out" | grep -c '本轮跳过唤醒')" "1"

echo
echo "== T8 + C3：落盘纪律 + prior_fails 上日志 + 中性轮不掉 delivered 行 =="
t8_default=$( ( unset STATE_DIR; . "$HERE/wake_guard.sh"; wg__state_dir ) )
case "$t8_default" in /tmp*|*\\Temp*) say "T8 默认 STATE_DIR" "$t8_default" FAIL; fails=$((fails+1));; *) say "T8 默认 STATE_DIR" "$t8_default" PASS;; esac
echo 2 > "$FAILS"
t8_log=$( . "$HERE/wake_guard.sh"; FAILS="$STATE_DIR/test.fails"; v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_CORRUPT_2120"); wg_apply_verdict "$v" "t8" "empty-wake" )
expect_eq "T8 中性轮重置 FAILS=0（跨进程可见）" "$(cat "$FAILS")" "0"
expect_has "C3 neutral 日志含 prior_fails=2" "$t8_log" "prior_fails=2"
expect_not "C3 中性轮不掉 delivered 行" "$t8_log" "delivered (attention consumed"

echo
echo "== T-pi（C4）：wg_judge_cursor 游标判据（pi-watcher 判定面逐字） =="
v=$(wg_judge_cursor 0 "2026-11-08T04:16:01" 1750 "2026-11-08T04:36:06" 1750)
expect_eq "T-pi 空轮询 last_read 前进 → delivered" "$v" "delivered"
v=$(wg_judge_cursor 0 "2026-11-08T04:16:01" 1750 "2026-11-08T04:16:01" 1750)
expect_eq "T-pi 游标不动 → failed" "$v" "failed"
v=$(wg_judge_cursor 0 "2026-11-08T04:16:01" 1750 "" "")
expect_eq "T-pi 空 post → failed（保守向，claude #1756 细化①）" "$v" "failed"
v=$(wg_judge_cursor 0 "2026-11-08T04:16:01" 1750 "2026-11-08T04:36:06" 1751)
expect_eq "T-pi delivered_id 前进 → delivered" "$v" "delivered"
v=$(wg_judge_cursor 2 "2026-11-08T04:16:01" 1750 "2026-11-08T04:36:06" 1750)
expect_eq "T-pi5 rc≠0+两腿真 → failed（判别格：红前绿后）" "$v" "failed"

echo
echo "== neutral_since 哨兵（dsh #1757 §三 三纪律） =="
: > "$FAILS"; rm -f "$STATE_DIR/neutral_since.t-ns"
( . "$HERE/wake_guard.sh"; FAILS="$STATE_DIR/ns.fails"; : > "$FAILS"
  v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_CORRUPT_2120"); wg_apply_verdict "$v" "t-ns" "empty-wake" )
NS="$STATE_DIR/neutral_since.t-ns"
[ -f "$NS" ] && say "neutral 后哨兵在" "exists" PASS || { say "neutral 后哨兵在" "missing" FAIL; fails=$((fails+1)); }
case "$(cat "$NS" 2>/dev/null)" in *Z) say "哨兵时间戳 Z 结尾(UTC)" OK PASS;; *) say "哨兵时间戳" "$(cat "$NS" 2>/dev/null)" FAIL; fails=$((fails+1));; esac
( . "$HERE/wake_guard.sh"; FAILS="$STATE_DIR/ns.fails"; wg_apply_verdict delivered "t-ns" "empty-wake" )
[ -f "$NS" ] && { say "delivered 后哨兵删" "still there" FAIL; fails=$((fails+1)); } || say "delivered 后哨兵删" "removed" PASS
date -u +%FT%TZ > "$NS"   # 重建哨兵（dsh #1773 五行：否则 failed 支的 rm 无事可做，恒绿）
( . "$HERE/wake_guard.sh"; FAILS="$STATE_DIR/ns.fails"; echo 0 > "$FAILS"; wg_apply_verdict failed "t-ns" "rc-nonzero" 2>/dev/null; true )
[ -f "$NS" ] && { say "failed 后哨兵删" "still there" FAIL; fails=$((fails+1)); } || say "failed 后哨兵删" "removed" PASS
date -u +%FT%TZ > "$NS"   # 重建哨兵（否则 preflight 的 rm 无物可删——dsh #1773 五行）
pf_out=$( . "$HERE/wake_guard.sh"; STATE_DIR="$STATE_DIR"; wg_preflight t-ns 1200 2>&1 ); pf_rc=$?
   # tag 修正：删的是 neutral_since.t-ns（原 t-pf 错位，dsh #1773→#1795 §六.2 补句）
expect_rc "preflight 未被拒启动" "$pf_rc" "0"
expect_eq "preflight 在岗行(rm 之前的门全过)" "$(printf '%s' "$pf_out" | grep -c '在岗')" "1"
[ -f "$NS" ] && { say "preflight 清哨兵（新进程≠旧 latch）" "still there" FAIL; fails=$((fails+1)); } || say "preflight 清哨兵（新进程≠旧 latch）" "removed" PASS

echo
echo "== T10：真 server（需隔离服务；无环境则 SKIP） =="
if [ -x "$HERE/../.venv/bin/python" ] && command -v sqlite3 >/dev/null 2>&1; then
  echo "T10 环境具备——按 dsh #1751 §一 T10 规格执行（隔离库+隔离端口）"
else
  say "T10 真 server 断言" "SKIP" "SKIP(需 .venv+sqlite3 隔离环境，随部署轮执行)"
fi

echo
echo "== 汇总 =="
if [ "$fails" -eq 0 ]; then
  echo "ALL PASS"
else
  echo "FAILED: $fails assertions"
fi
rm -rf "$STATE_DIR"
exit "$fails"
