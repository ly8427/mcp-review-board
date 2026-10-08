#!/usr/bin/env bash
# kit-version: 2.5.0 (drift check: compare with repo copy; update at will)
# host-watch-lib.sh — probe + parse + judge helpers for host-watch.sh.
# Split out so configs/host-watch-test.sh can source and test them directly
# (thread #24 / PR #1 review: P2-6 python3 parsing, P3 scenario matrix).
#
# P1-1 semantics (thread #24, GPT review): judge_wake's success signal is
# DELIVERY ("the member was woken and consumed the board's attention"), NOT
# task completion. The failure gate is a delivery gate.
#
# v2 (thread #31 batch A, quorum 4/4 2026-09-27): delivered = attention
# fingerprint moved OR last_read advanced. Closes the field-proven gap of
# comment #257/#258 (thread #24): a member that polls THEN posts in the same
# wake re-raises its own attention (own comment newer than its cursor) and was
# billed FAILED — 3 such rounds silently blacklisted productive members
# (2026-09-27: pi, opencode). last_read pre/post come from the /attention
# response (server returns the effective cutoff; advanced only by
# list_comments_since(author) — C3, single write point — never by the probe),
# so a compliant poll-then-post round now shows last_read_post > last_read_pre
# and bills delivered regardless of executor stdout shape. rc!=0 is ALWAYS
# failed (crash/hang billing, witness #340 实错2); probe failure stays NEUTRAL.
#
# Executor-agnostic (thread #25 batch B): the watcher often runs under Git
# Bash on Windows, where "python3" is typically the Microsoft Store stub and
# no real Python exists. Interpreter detection probes for a WORKING python
# first, then falls back to node. RB_PY / RB_NODE override the detection.

_RB_BACKEND=""
if [ -z "${RB_PY:-}" ]; then
  for c in python3 python; do
    if "$c" -c "import sys" >/dev/null 2>&1; then RB_PY="$c"; break; fi
  done
fi
if [ -n "${RB_PY:-}" ]; then
  _RB_BACKEND=py
elif command -v node >/dev/null 2>&1 && node -e "1" >/dev/null 2>&1; then
  RB_NODE="${RB_NODE:-node}"
  _RB_BACKEND=node
else
  echo "host-watch-lib: no working python3/python/node on PATH (the Windows Store stub does not count)" >&2
  exit 1
fi

probe() { curl -sf "$BOARD/attention?author=$1"; }

# probe_ok <author> — the ONE probe entry point (thread #27 must-fix 1+2).
#   rc 0 + valid JSON on stdout = a trustworthy response.
#   rc 1 = NEUTRAL: transport failure or invalid/non-JSON body — zero
#         information about delivery; callers must neither bill nor gate.
#   rc 4 = BOARD IDENTITY HARD STOP (GPT round-3 C1: fail-closed once the
#         watcher opts into identity). When EXPECTED_BOARD_ID is SET, a
#         response with a missing OR mismatched board_id returns 4 — an
#         unidentifiable board on this port may be the wrong instance
#         (uninstall + port reuse by a pre-2.5 server). Backward
#         compatibility (no board_id field) applies ONLY when
#         EXPECTED_BOARD_ID is unset.
#   NOTE: probe_ok RETURNS 4 instead of exiting (win-test #11: an exit
#   inside a $(...) substitution dies in the subshell). Callers MUST
#   propagate: [ "$rc" = 4 ] && exit 4.
probe_ok() {
  local r bid
  r=$(probe "$1") || return 1
  [ "$(jhasfield "$r" attention)" = "1" ] || return 1
  if [ -n "${EXPECTED_BOARD_ID:-}" ]; then
    bid=$(jget board_id "$r")
    if [ -z "$bid" ] || [ "$bid" != "$EXPECTED_BOARD_ID" ]; then
      echo "BOARD IDENTITY ${bid:+MISMATCH}${bid:-UNVERIFIABLE}: expected $EXPECTED_BOARD_ID," >&2
      echo "this port ${bid:+serves $bid}${bid:-carries a server with no board_id (pre-2.5?)}." >&2
      echo "Refusing to wake members against an unverified board — remove or repoint" >&2
      echo "this watcher, or unset EXPECTED_BOARD_ID to run without identity pinning." >&2
      return 4
    fi
  fi
  printf '%s\n' "$r"
}

# wake_outcome <agent_rc> <post_probe_rc> <post_json> <pre_reason> <pre_threads>
#              [last_read_pre] [last_read_post]
#   → delivered | failed | unverified (thread #27 must-fix 1; v2 thread #31):
#   probe failure is NEUTRAL — never billed, never touches the gate counter;
#   rc!=0 is ALWAYS failed (crash/hang billing, witness #340 实错2);
#   delivered = fingerprint moved OR last_read advanced (STRICT greater-than;
#   both values are the /attention effective cutoff — 'YYYY-MM-DD HH:MM:SS'
#   compares correctly as a string, never raw NULL). Empty last_read args
#   (pre-2.5 server without the field) degrade to fingerprint-only, so old
#   kit callers keep working unchanged.
wake_outcome() {
  local arc="$1" prc="$2" aj="$3" pr="$4" pt="$5" plr="${6:-}" qlr="${7:-}"
  if [ "$prc" != "0" ]; then echo unverified; return; fi
  if [ "$arc" != "0" ]; then echo failed; return; fi
  if [ -n "$plr" ] && [ -n "$qlr" ] && [[ "$qlr" > "$plr" ]]; then echo delivered; return; fi
  if judge_wake "$arc" "1" "$pr" "$pt" "$(jatt "$aj")" "$(jreason "$aj")" "$(jthreads "$aj")"; then
    echo delivered
  else
    echo failed
  fi
}

# apply_billing <outcome> <fails_file> — the ONLY writer of gate state.
#   delivered → reset; failed → increment; unverified → NO change (asserted
#   by the H/I/J matrix: probe failures never move the gate counter).
apply_billing() {
  case "$1" in
    delivered) echo 0 > "$2" ;;
    failed)    echo $(( $(cat "$2" 2>/dev/null || echo 0) + 1 )) > "$2" ;;
    unverified) : ;;
  esac
}

# jget <field> <json> — real JSON parse: empty output for absent fields;
# lists joined with commas. Backend: python if available, else node.
jget() {
  local field="$1" json="$2"
  if [ "$_RB_BACKEND" = py ]; then
    printf '%s' "$json" | "$RB_PY" -c "
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
v = d.get(sys.argv[1])
if isinstance(v, list): print(','.join(str(x) for x in v))
elif v is not None: print(v)
" "$field"
  else
    printf '%s' "$json" | "$RB_NODE" -e '
let d; try { d = JSON.parse(require("fs").readFileSync(0, "utf8")); } catch (e) { process.exit(0); }
const v = d[process.argv[1]];
if (Array.isArray(v)) console.log(v.join(","));
else if (v !== undefined && v !== null) console.log(v);
' "$field"
  fi
}

# jhasfield <json> <field> — distinguishes "empty list" from "field missing"
# (old server). 1 = present, 0 = missing or bad JSON.
jhasfield() {
  local json="$1" field="${2:-open_threads}"
  if [ "$_RB_BACKEND" = py ]; then
    printf '%s' "$json" | "$RB_PY" -c "
import sys, json
try: d = json.load(sys.stdin)
except Exception: print(0); sys.exit(0)
print(1 if sys.argv[1] in d else 0)
" "$field"
  else
    printf '%s' "$json" | "$RB_NODE" -e '
let d; try { d = JSON.parse(require("fs").readFileSync(0, "utf8")); }
catch (e) { console.log(0); process.exit(0); }
console.log(process.argv[1] in d ? 1 : 0);
' "$field"
  fi
}

jatt()     { jget attention "$1"; }
jreason()  { jget reason "$1"; }
jthreads() { jget threads "$1"; }
jopen()    { jget open_threads "$1"; }

# judge_wake <rc> <pre_att> <pre_reason> <pre_threads> <post_att> <post_reason> <post_threads>
#   exit 0 = delivered (attention fingerprint moved), exit 1 = billed FAILED.
# Scenarios F1/F2 in host-watch-test.sh pin the semantics edges:
#   F1 (someone else's comment re-raised attention: fingerprint changed) -> delivered
#   F2 (identical fingerprint, cursor frozen)                              -> FAILED
#      (v2.5: the member's-own-comment re-raise shape is now prevented
#       SERVER-side — /attention excludes the prober's own comments from the
#       wake scan — so this lib-level FAILED remains correct for genuinely
#       frozen rounds and no longer bills compliant poll-then-post members)
judge_wake() {
  [ "$1" -eq 0 ] && { [ "$5" != "1" ] || [ "$6" != "$3" ] || [ "$7" != "$4" ]; }
}

# self_stop_check — exits 3 when every bound thread is absent from the
# open_threads UNION across all probes, and at least one probe response
# carried the open_threads field (v2.4 server). PROBE_AUTHORS must list YOUR
# members — probing an unseen name registers it (pollutes participants).
self_stop_check() {
  [ -n "$WATCH_THREADS" ] || return 0
  if [ -z "$PROBE_AUTHORS" ]; then
    echo "note: self-stop disabled — set PROBE_AUTHORS to your member names."
    return 0
  fi
  local field_seen=0 open_union="" r u t any_open=0 p
  for a in $PROBE_AUTHORS; do
    r=$(probe_ok "$a"); p=$?          # identity gate on EVERY probe (#27)
    [ "$p" = "4" ] && exit 4          # propagate the hard stop (win-test #11)
    [ "$p" != "0" ] && continue       # neutral: no information, no gate
    [ "$(jhasfield "$r")" = "1" ] && field_seen=1
    u=$(jopen "$r")
    [ -n "$u" ] && open_union="${open_union:+$open_union,}$u"
  done
  [ "$field_seen" = "1" ] || return 0
  for t in ${WATCH_THREADS//,/ }; do
    case ",$open_union," in *",$t,"*) any_open=1; break ;; esac
  done
  if [ "$any_open" = "0" ]; then
    echo "$(date +%T) bound threads [$WATCH_THREADS] all closed (open union=[$open_union]) — host-watch done."
    echo "  v2.5 note: drop the cron ONLY if no OTHER open quorum thread depends on this driver (thread #33 B-4)."
    exit 3
  fi
}
