#!/usr/bin/env bash
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

probe() { curl -sf "$BOARD/attention?author=$1"; }

jget() {  # jget <field> <json> — python3 JSON parse (P2-6): empty output for
          # absent fields; lists joined with commas
  printf '%s' "$2" | python3 -c "
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
v = d.get(sys.argv[1])
if isinstance(v, list): print(','.join(str(x) for x in v))
elif v is not None: print(v)
" "$1"
}

jhasfield() {  # distinguishes "empty open_threads list" from "field missing" (old server)
  printf '%s' "$1" | python3 -c "
import sys, json
try: d = json.load(sys.stdin)
except Exception: print(0); sys.exit(0)
print(1 if 'open_threads' in d else 0)
"
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
    r=$(probe "$a") || continue
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
