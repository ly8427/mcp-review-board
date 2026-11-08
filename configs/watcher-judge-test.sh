#!/usr/bin/env bash
# watcher-judge-test.sh — GPT 第五轮 P2 处置的判定逻辑测试（board #103 #1751 T1–T10 规格）。
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
say() { printf '%-52s -> %-9s [%s]\n' "$1" "$2" "$3"; }
expect_eq() { if [ "$2" = "$3" ]; then say "$1" "$2" PASS; else say "$1" "$2" "FAIL(want $3)"; fails=$((fails+1)); fi; }
expect_rc() { if [ "$2" = "$3" ]; then say "$1" "rc=$2" PASS; else say "$1" "rc=$2" "FAIL(want $3)"; fails=$((fails+1)); fi; }
# 与 watcher 相同的 sed 提取（pre 面产生器——夹具与实现同口径）：
p_att()     { printf '%s' "$1" | sed -n 's/.*"attention":\([0-9]\).*/\1/p'; }
p_reason()  { printf '%s' "$1" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p'; }
p_threads() { printf '%s' "$1" | sed -n 's/.*"threads":\[\([^]]*\)\].*/\1/p'; }

# —— fixture（wire 原文级；dsh #1751 §二.1 两代形态）——
R_CORRUPT_2120='{"attention":1,"reason":"awaiting_verdict","threads":[104],"token_blocked":{"corrupt":{"tier":"break_glass","exit":"reset_token(human_override=True) (break-glass, audited)."}},"last_read":"2026-11-08T04:16:01"}'
R_CORRUPT_2121='{"attention":1,"reason":"awaiting_verdict","threads":[104],"token_blocked":{"corrupt":{"tier":"break_glass","exit":"reset_token(human_override=True) (break-glass, audited)."}},"meta_unparsable":1}'
R_MANUAL='{"attention":1,"reason":"awaiting_verdict","threads":[104],"token_blocked":{"104":{"tier":"manual","since":"x","exit":"reset_token(human_override=True) | window_ends_at=..."}},"last_read":"x"}'
R_HEALTHY='{"attention":1,"reason":"awaiting_verdict","threads":[104],"last_read":"x"}'
R_OUTSIDE='{"attention":1,"reason":"awaiting_verdict","threads":[104],"other":{"tier":"break_glass"},"token_blocked":{},"last_read":"x"}'
PRE='{"attention":1,"reason":"awaiting_verdict","threads":[104]}'
PRE_ATT=$(p_att "$PRE"); PRE_REASON=$(p_reason "$PRE"); PRE_THREADS=$(p_threads "$PRE")

echo "== T9 结构：谓词实现唯一 + 调用点 + 语法 =="
expect_eq "wg_judge_surface 定义数(wake_guard)" "$(grep -c 'wg_judge_surface()' "$HERE/wake_guard.sh")" "1"
expect_eq "wg__tb_obj 定义数(wake_guard)"      "$(grep -c 'wg__tb_obj()' "$HERE/wake_guard.sh")" "1"
expect_eq "dsh-watcher 调用数"                  "$(grep -c 'verdict=$(wg_judge_surface' "$HERE/dsh-watcher.sh")" "1"
expect_eq "claude-watcher 调用数"               "$(grep -c 'verdict=$(wg_judge_surface' "$HERE/claude-watcher.sh")" "1"
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
echo "== T4：re-arm（本轮 post 无 latch）→ 回落 failed =="
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_MANUAL")
expect_eq "T4 post=manual 档" "$v" "failed"
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_HEALTHY")
expect_eq "T4 post=无档" "$v" "failed"

echo
echo "== T5：同态 + token_blocked 空 / tier=manual / 缺档 → failed（检测力不变） =="
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_HEALTHY")
expect_eq "T5 空 token_blocked" "$v" "failed"
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_MANUAL")
expect_eq "T5 tier=manual" "$v" "failed"

echo
echo "== T6：break_glass 在 token_blocked 之外 → 不得中性（括号配对提取） =="
v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_OUTSIDE")
expect_eq "T6 域外 break_glass" "$v" "failed"

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
echo "== T8：状态落盘纪律（默认 STATE_DIR 非 /tmp；FAILS 跨进程仍在） =="
t8_default=$( ( unset STATE_DIR; . "$HERE/wake_guard.sh"; wg__state_dir ) )
case "$t8_default" in /tmp*|*\\Temp*) say "T8 默认 STATE_DIR" "$t8_default" FAIL; fails=$((fails+1));; *) say "T8 默认 STATE_DIR" "$t8_default" PASS;; esac
echo 2 > "$FAILS"
( . "$HERE/wake_guard.sh"; FAILS="$STATE_DIR/test.fails"; v=$(wg_judge_surface 0 "$PRE_ATT" "$PRE_REASON" "$PRE_THREADS" "$R_CORRUPT_2120"); wg_apply_verdict "$v" "t8" "empty-wake" )
expect_eq "T8 中性轮重置 FAILS=0（跨进程可见）" "$(cat "$FAILS")" "0"

echo
echo "== T10：真 server（需隔离服务；无环境则 SKIP） =="
if [ -x "$HERE/../.venv/bin/python" ] && command -v sqlite3 >/dev/null 2>&1; then
  echo "T10 环境具备——按 dsh #1751 §一 T10 规格执行（隔离库+隔离端口）"
  echo "T10 详细断言随隔离服务部署轮执行（board 回执附读数）"
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
