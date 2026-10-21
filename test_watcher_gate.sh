#!/usr/bin/env bash
# v2.9.2 回归(GPT round-2 P1 on 7f06517): 启动时的 FAILS 写 0 必须发生在
# 拿到单飞锁之后 —— 双开实例在 mkdir 失败退出时,绝不能触碰运行中实例的
# 失败连击,否则 3 次闸门(C1.2)可被"重复启动"绕过(不需要拿到锁、不需要
# 真正执行唤醒)。
#
# 场景覆盖(对三个 watcher 同构跑): 连击=1/2 时双开不得清零;连击=3 时闸门
# 拒启且文件不动;owner 首启(拿到锁)才合法重置连击。
#
# Run: bash test_watcher_gate.sh
# 无需真实看板/agent 二进制: BOARD 指向死端口;探针网络失败不计入唤醒
# 失败(watcher 语义),owner 循环只会空转。

set -u
cd "$(dirname "$0")"

WATCHERS="configs/claude-watcher.sh configs/dsh-watcher.sh configs/pi-watcher.sh"
PASS=0; FAIL=0

check() { # name expected actual
  if [ "$2" = "$3" ]; then
    PASS=$((PASS+1)); echo "  ✅ $1"
  else
    FAIL=$((FAIL+1)); echo "  ❌ $1 (expected [$2] got [$3])"
  fi
}

for W in $WATCHERS; do
  echo "=== $W ==="
  T=$(mktemp -d); LOCKD="$T/lock.d"; FAILSF="$T/fails"
  # 脚本内的陈锁清理只删 >90min 的同名目录;测试锁是新建的,不受影响。

  # 1. THE P1: 有运行中实例(锁被持)+ 连击=2 → 双开必须原样保留连击
  mkdir "$LOCKD"; echo 2 > "$FAILSF"
  out=$(LOCK="$LOCKD" FAILS="$FAILSF" BOARD="http://127.0.0.1:1" bash "$W" 2>&1); rc=$?
  check "duplicate launch exits 0 (single-flight)" "0" "$rc"
  case "$out" in
    *"single-flight"*) check "duplicate reports single-flight" ok ok;;
    *) check "duplicate reports single-flight" ok "missing in: $out";;
  esac
  check "streak preserved after duplicate launch (P1)" "2" "$(cat "$FAILSF")"
  rmdir "$LOCKD"

  # 2. 连击=3 → 闸门拒启(exit 1),FAILS 不被触碰(读闸在一切之前)
  echo 3 > "$FAILSF"
  out=$(LOCK="$LOCKD" FAILS="$FAILSF" BOARD="http://127.0.0.1:1" bash "$W" 2>&1); rc=$?
  case "$out" in
    *"gate:"*) check "gate message on 3 strikes" ok ok;;
    *) check "gate message on 3 strikes" ok "missing in: $out";;
  esac
  check "gated launch exits nonzero" "1" "$rc"
  check "gated FAILS untouched" "3" "$(cat "$FAILSF")"

  # 3. owner 首启(无锁,连击=2 残留)→ 拿到锁后合法重置为 0,随后空转
  echo 2 > "$FAILSF"
  LOCK="$LOCKD" FAILS="$FAILSF" BOARD="http://127.0.0.1:1" INTERVAL=3600 \
      timeout 2 bash "$W" >/dev/null 2>&1
  check "owner start resets streak (lock held)" "0" "$(cat "$FAILSF")"

  rm -rf "$T"
done

echo "============================================================"
echo "watcher gate: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
