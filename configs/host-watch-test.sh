#!/usr/bin/env bash
# kit-version: 2.4.1 (drift check: compare with repo copy; update at will)
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
echo "== probe_ok: validity gate + board identity (thread #27 must-fix 1+2) =="
EXPECT_SAVED="${EXPECTED_BOARD_ID:-}"
p_ok() { ( probe_ok x >/dev/null 2>&1 ); echo "rc=$?"; }
probe() { return 1; }
expect_eq "probe_ok transport failure"     "$(p_ok)" "rc=1"
probe() { printf '%s' "$PO"; }
PO='not-json'
expect_eq "probe_ok invalid JSON (J)"      "$(p_ok)" "rc=1"
PO='{"reason":"x"}'
expect_eq "probe_ok missing attention"     "$(p_ok)" "rc=1"
PO='{"attention":1,"board_id":"bbb"}'      # no EXPECTED set → no identity gate
expect_eq "probe_ok mismatch no-EXPECTED"  "$(p_ok)" "rc=0"
EXPECTED_BOARD_ID="aaa"
PO='{"attention":1,"board_id":"bbb"}'
expect_eq "probe_ok mismatch → hard stop"  "$(p_ok)" "rc=4"
PO='{"attention":1,"board_id":"aaa"}'
expect_eq "probe_ok match → proceed"       "$(p_ok)" "rc=0"
PO='{"attention":1}'                       # pre-2.5 server: no field.
expect_eq "probe_ok no-field+EXPECTED → fail-closed (GPT C1)" "$(p_ok)" "rc=4"
EXPECTED_BOARD_ID=""
PO='{"attention":1}'
expect_eq "probe_ok no-field, no pin → compat" "$(p_ok)" "rc=0"
EXPECTED_BOARD_ID="$EXPECT_SAVED"

echo
echo "== wake_outcome + apply_billing: H/I/J — probe failure NEVER moves the gate =="
wo() { wake_outcome "$@"; }
expect_eq "H post-probe network failure"  "$(wo 0 1 '{}' new_comments 24)" "unverified"
expect_eq "I grace re-probe failure"      "$(wo 0 1 '{}' new_comments 24)" "unverified"
expect_eq "J invalid JSON body"           "$(wo 0 1 garbage new_comments 24)" "unverified"
expect_eq "A valid delivered path"        "$(wo 0 0 '{"attention":0,"reason":"idle","threads":[]}' new_comments 24)" "delivered"
expect_eq "E valid failed path"           "$(wo 0 0 '{"attention":1,"reason":"new_comments","threads":[24]}' new_comments 24)" "failed"
TF=$(mktemp); echo 2 > "$TF"
apply_billing unverified "$TF"
expect_eq "billing unverified keeps 2"    "$(cat "$TF")" "2"
apply_billing unverified "$TF"; apply_billing unverified "$TF"
expect_eq "billing 3x unverified still 2" "$(cat "$TF")" "2"
apply_billing delivered "$TF"
expect_eq "billing delivered resets"      "$(cat "$TF")" "0"
apply_billing failed "$TF"; apply_billing failed "$TF"
expect_eq "billing failed increments"     "$(cat "$TF")" "2"
rm -f "$TF"

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
