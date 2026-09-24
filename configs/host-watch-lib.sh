#!/usr/bin/env bash
# kit-version: 2.4.1 (drift check: compare with repo copy; update at will)
# host-watch-lib.sh — probe + parse + judge helpers for host-watch.sh.
# Split out so configs/host-watch-test.sh can source and test them directly
# (thread #24 / PR #1 review: P2-6 python3 parsing, P3 scenario matrix).
#
# P1-1 semantics (thread #24, GPT review): judge_wake's success signal is
# DELIVERY ("the member was woken and consumed the board's attention"), NOT
# task completion. The failure gate is a delivery gate. Known gap
# (field-proven, #257/#258): a member that polls THEN posts in the same wake
# re-raises its own attention (its own comment is newer than its cursor) — a
# fully-compliant round can be billed FAILED; 3 such rounds silently stop the
# member. A governance-progress gate is specced for the next batch.
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
#   exit 4 = BOARD IDENTITY MISMATCH (hard stop): only when EXPECTED_BOARD_ID
#         is set AND the response is valid AND carries a board_id that differs.
#         Probe failures and pre-2.5 servers (no field) NEVER hard-stop.
probe_ok() {
  local r bid
  r=$(probe "$1") || return 1
  [ "$(jhasfield "$r" attention)" = "1" ] || return 1
  if [ -n "${EXPECTED_BOARD_ID:-}" ]; then
    bid=$(jget board_id "$r")
    if [ -n "$bid" ] && [ "$bid" != "$EXPECTED_BOARD_ID" ]; then
      echo "BOARD IDENTITY MISMATCH: expected $EXPECTED_BOARD_ID, this port serves $bid." >&2
      echo "Refusing to wake members against a different board instance." >&2
      echo "Remove or repoint this watcher (configs/uninstall-watch.sh lists entry points)." >&2
      exit 4
    fi
  fi
  printf '%s\n' "$r"
}

# wake_outcome <agent_rc> <post_probe_rc> <post_json> <pre_reason> <pre_threads>
#   → delivered | failed | unverified (thread #27 must-fix 1):
#   probe failure is NEUTRAL — never billed, never touches the gate counter;
#   only a VALID probe with an unchanged fingerprint is an informative FAILED.
wake_outcome() {
  local arc="$1" prc="$2" aj="$3" pr="$4" pt="$5"
  if [ "$prc" != "0" ]; then echo unverified; return; fi
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
#   F2 (the member's OWN comment re-raised it: fingerprint identical)     -> FAILED
#      (known gap, documented — see header; not a task-completion judge)
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
  local field_seen=0 open_union="" r u t any_open=0
  for a in $PROBE_AUTHORS; do
    r=$(probe_ok "$a") || continue   # identity gate + neutral-on-failure (#27)
    [ "$(jhasfield "$r")" = "1" ] && field_seen=1
    u=$(jopen "$r")
    [ -n "$u" ] && open_union="${open_union:+$open_union,}$u"
  done
  [ "$field_seen" = "1" ] || return 0
  for t in ${WATCH_THREADS//,/ }; do
    case ",$open_union," in *",$t,"*) any_open=1; break ;; esac
  done
  if [ "$any_open" = "0" ]; then
    echo "$(date +%T) bound threads [$WATCH_THREADS] all closed (open union=[$open_union]) — host-watch done, drop the cron."
    exit 3
  fi
}
