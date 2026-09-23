#!/usr/bin/env bash
# host-watch-test.sh — scenario matrix for host-watch.sh judgement logic
# (thread #24 / PR #1 review: P3 + dsh condition (a)).
# Sources configs/host-watch-lib.sh (the exact code host-watch.sh runs) and
# stubs the probe; no board needed. Run: bash configs/host-watch-test.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
BOARD="http://test-stub" WATCH_THREADS="" PROBE_AUTHORS=""
. "$HERE/host-watch-lib.sh"

fails=0
say() { printf '%-46s -> %-10s [%s]\n' "$1" "$2" "$3"; }
expect_eq() { # label got want
  if [ "$2" = "$3" ]; then say "$1" "$2" PASS; else say "$1" "$2" "FAIL(want $3)"; fails=$((fails+1)); fi
}

echo "== unit: python3 JSON parsing (P2-6) =="
expect_eq "jget attention"       "$(jget attention '{"attention":1,"reason":"x"}')"  "1"
expect_eq "jget list join"       "$(jget open_threads '{"open_threads":[20,23]}')"   "20,23"
expect_eq "jget missing field"   "$(jget open_threads '{"attention":0}')"            ""
expect_eq "jget escaped quote"   "$(jget reason '{"reason":"a\"b"}')"               'a"b'
expect_eq "jhasfield present-empty" "$(jhasfield '{"open_threads":[]}')"            "1"
expect_eq "jhasfield absent"     "$(jhasfield '{"attention":0}')"                   "0"
expect_eq "jhasfield bad json"   "$(jhasfield 'not-json')"                          "0"

echo
echo "== judge_wake: wake-outcome scenarios (P3; F split per #257 cond (a)) =="
# args: rc pre_att pre_reason pre_threads post_att post_reason post_threads
judge() { judge_wake "$@" && echo delivered || echo FAILED; }
# A: agent finishes < grace — fingerprint moved
expect_eq "A done-fast (fingerprint moved)" \
  "$(judge 0 1 new_comments 24 0 idle "")" "delivered"
# B: agent finishes 45-90s — grace re-probe catches the move (host-watch.sh sleeps WAKE_GRACE_SECS between probes; final state moved)
expect_eq "B done-in-grace (final moved)" \
  "$(judge 0 1 new_comments 24 0 idle "")" "delivered"
# C: agent finishes > grace — after-probes still show old fingerprint: billed FAILED once; attention stays level (C3) so next round re-wakes and heals
expect_eq "C done-slow (still moved later — billed once, self-heals)" \
  "$(judge 0 1 new_comments 24 1 new_comments 24)" "FAILED"
# D: crash — rc!=0, fingerprint frozen
expect_eq "D crash (rc!=0, frozen)" \
  "$(judge 1 1 awaiting_verdict 23 1 awaiting_verdict 23)" "FAILED"
# E: token failure — rc==0 but nothing consumed
expect_eq "E token-failure (rc==0, frozen)" \
  "$(judge 0 1 awaiting_verdict 23 1 awaiting_verdict 23)" "FAILED"
# F1: SOMEONE ELSE's comment re-raised attention — fingerprint changed -> delivered
expect_eq "F1 others-comment re-raise (moved)" \
  "$(judge 0 1 new_comments 24 1 new_comments 24,25)" "delivered"
# F2: the member's OWN comment re-raised attention — fingerprint identical.
# KNOWN GAP (documented, lib header + #257/#258): fully-compliant round billed
# FAILED; 3 such rounds trip the delivery gate. Next batch: governance-progress gate.
expect_eq "F2 own-comment re-raise (KNOWN GAP)" \
  "$(judge 0 1 new_comments 24 1 new_comments 24)" "FAILED"
# G: attention moved but no governance action — delivered BY DESIGN (P1-1:
# watcher judges delivery, not task completion; awaiting_verdict keeps
# attention level for un-voted members)
expect_eq "G attention-moved-no-action (delivery by design)" \
  "$(judge 0 1 awaiting_verdict 23 0 idle "")" "delivered"

echo
echo "== self_stop_check: bound-thread scenarios =="
OUT_C=""; OUT_OTHER=""
probe() { case "$1" in claude) printf '%s' "$OUT_C";; *) printf '%s' "$OUT_OTHER";; esac; }
ss() { # label want — WATCH_THREADS=21,23
  local got ec=0
  ( WATCH_THREADS="21,23" PROBE_AUTHORS="$PA" self_stop_check >/dev/null 2>&1 ) && ec=$? || ec=$?
  [ "$ec" = "3" ] && got=EXIT3 || got=continued
  expect_eq "$1" "$got" "$2"
}
PA="claude dsh opencode pi"
OUT_C='{"attention":0,"reason":"idle","threads":[],"open_threads":[]}'; OUT_OTHER="$OUT_C"
ss "SS-A all empty lists (all closed)"      EXIT3
OUT_C='{"attention":0,"reason":"idle","threads":[],"open_threads":[20]}'
OUT_OTHER='{"attention":1,"reason":"x","threads":[23],"open_threads":[20,23]}'
ss "SS-B union across members (23 open)"    continued
OUT_C='{"attention":0,"reason":"idle","threads":[],"open_threads":[23]}'; OUT_OTHER="$OUT_C"
ss "SS-C all [23]"                          continued
OUT_C='{"attention":0,"reason":"idle","threads":[]}'; OUT_OTHER="$OUT_C"
ss "SS-D old server (no field)"             continued
PA=""
OUT_C='{"attention":0,"reason":"idle","threads":[],"open_threads":[]}'; OUT_OTHER="$OUT_C"
ss "SS-E adopt default (no PROBE_AUTHORS)"  continued

echo
if [ "$fails" -eq 0 ]; then echo "✅ host-watch-test: all scenarios passed"; exit 0
else echo "❌ host-watch-test: $fails failure(s)"; exit 1; fi
