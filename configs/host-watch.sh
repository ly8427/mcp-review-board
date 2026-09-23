#!/usr/bin/env bash
# host-watch.sh — the HOST-side orchestrator (batch 1, thread #22 #1/#4/#5).
#
# Field-proven shape (threads #21/#22: four reviewers woken, voted, flipped
# and auto-resolved with zero human relaying), generalized for adoption.
# One round per invocation (run it from cron / Task Scheduler every 20 min):
#
#   for each configured member:  probe GET /attention?author=NAME
#     attention=0      -> idle, zero LLM cost
#     attention=1      -> headless-wake that member with its task template
#                          + board injection (reason/threads)
#   Protections (same contract as configs/dsh-watcher.sh):
#     - per-member single-flight mkdir lock + 90-min stale-lock cleanup
#     - failure gate: 3 consecutive failed/empty wakes stop that member
#     - WAKE_GRACE_SECS (default 45): delayed re-probe before calling a wake
#       empty — woken sessions advance the cursor AFTER our first re-probe
#       (false positives field-proven in the #21 cycle)
#   Self-stop (needs protocol v2.4): WATCH_THREADS="21,22" — if none of the
#   bound thread ids appear in any probe's open_threads, the round prints a
#   notice and exits 3, so your scheduler wrapper can drop the cron job.
#
# Configure the wake_* functions below for your members, then:
#   bash configs/host-watch.sh            # one round
#
# TASK-TEXT BUDGET (field-proven, thread #24): keep the injected task text
# SHORT — point at thread ids and let members fetch content themselves via
# get_thread. A long inline task text (a full review post) pushed a
# flash-tier member past the 15-min timeout with ZERO output and the round
# was lost; the same member finished in 5 minutes with a condensed task.
set -u
BOARD="${BOARD:-http://localhost:8765}"
WATCH_THREADS="${WATCH_THREADS:-}"                      # e.g. "21,22"; empty = no self-stop
WAKE_GRACE_SECS="${WAKE_GRACE_SECS:-45}"
WATCH_DIR="$(cd "$(dirname "$0")" && pwd)"
RB_REPO="$(cd "$WATCH_DIR/.." && pwd)"

# probe/parse/judge helpers live in host-watch-lib.sh (shared with the test
# matrix — thread #24 P2-6/P3). P1-1 semantics and the delivery-gate known
# gap are documented there.
. "$WATCH_DIR/host-watch-lib.sh"

# self-stop pre-check: exit 3 when every bound thread is closed (v2.4 field;
# PROBE_AUTHORS required — see lib header).
self_stop_check

# wake <name> <probe-author> <wakefn>: probe -> conditional wake -> gate
wake() {
  local name="$1" pauthor="$2" wakefn="$3"
  local fails="/tmp/rb-host-watch-$name.fails" lock="/tmp/rb-host-watch-$name.lock.d"
  local gate; gate=$(cat "$fails" 2>/dev/null || echo 0)
  if [ "${gate:-0}" -ge 3 ]; then echo "$(date +%T) $name: gate tripped (3 consecutive fails), skipping"; return 0; fi
  mkdir "$lock" 2>/dev/null || { echo "$(date +%T) $name: wake in flight, skip"; return 0; }
  local resp att reason threads rc=0 after
  resp=$(probe "$pauthor") || { echo "$(date +%T) $name: probe failed (network)"; rmdir "$lock" 2>/dev/null; return 0; }
  att=$(jatt "$resp")
  if [ "$att" != "1" ]; then echo "$(date +%T) $name: idle"; rmdir "$lock" 2>/dev/null; return 0; fi
  reason=$(jreason "$resp"); threads=$(jthreads "$resp")
  echo "$(date +%T) $name: attention=1 ($reason, threads=$threads) — waking"
  "$wakefn" "$reason" "$threads" > "/tmp/rb-host-watch-$name.last.log" 2>&1 || rc=$?
  after=$(probe "$pauthor") || after=""
  if [ "$(jatt "$after")" = "1" ]; then
    sleep "$WAKE_GRACE_SECS"
    after=$(probe "$pauthor") || after=""
  fi
  if judge_wake "$rc" "1" "$reason" "$threads" "$(jatt "$after")" "$(jreason "$after")" "$(jthreads "$after")"; then
    echo 0 > "$fails"; echo "$(date +%T) $name: delivered (attention consumed — delivery, not task-completion)"
  else
    local n; n=$(( $(cat "$fails" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$fails"
    echo "$(date +%T) $name: wake FAILED (rc=$rc, or attention unchanged after ${WAKE_GRACE_SECS}s grace) ($n/3)"
    [ "$n" -ge 3 ] && echo "$(date +%T) $name: gate tripped — stop waking this member (recover: rm $fails)"
  fi
  rmdir "$lock" 2>/dev/null
  return 0
}

# --- member wake functions: EDIT THESE for your setup -----------------------
# Each: builds the member's task file (fixed template + board injection only),
# then invokes its headless one-shot form. Field-proven shapes kept as examples.

wake_claude() {   # claude code: MCP via --mcp-config, task via stdin (not argv —
                  # Windows cmd shim drops multi-line prompt arguments, #13)
  local reason="$1" threads="$2" tf="/tmp/rb-task-claude-live.txt"
  { cat "$RB_REPO/configs/claude-polling-prompt.txt"
    echo; echo "(本次唤醒探针注入: reason=$reason, threads=$threads)"
  } > "$tf"
  ( cd "$RB_REPO" && claude -p --mcp-config "$RB_REPO/configs/claude-code.mcp.json" \
      --allowedTools "mcp__review-board__*" "Read" "Write" < "$tf" )
}

wake_dsh() {      # dsh: headless profile; task file in EXECUTOR-NATIVE path form (#18 R3)
  local reason="$1" threads="$2"
  { cat "$WATCH_DIR/task-template-dsh.txt"
    echo; echo "(本次唤醒探针注入: reason=$reason, threads=$threads)"
  } > "$WATCH_DIR/task-dsh-live.txt"
  dsh --profile headless "Read the task file $(cygpath -w "$WATCH_DIR/task-dsh-live.txt" 2>/dev/null || echo "$WATCH_DIR/task-dsh-live.txt") and execute it fully. It is written in Chinese. Do not ask questions; output done at the end."
}

wake_opencode() { # opencode: no MCP configured -> rb.sh channel; --auto approves tools
  local reason="$1" threads="$2"
  { cat "$WATCH_DIR/task-template-opencode.txt"
    echo; echo "(本次唤醒探针注入: reason=$reason, threads=$threads)"
  } > "$WATCH_DIR/task-opencode-live.txt"
  wsl -e bash -c 'cd ~ && timeout 900 ~/.opencode/bin/opencode run --auto "$(cat /mnt/c/path/to/rb-watch/task-opencode-live.txt)"'
}

wake_pi() {       # pi: No MCP by design -> rb.sh; node>=22 + DEEPSEEK_API_KEY + explicit --model
  local reason="$1" threads="$2"
  { cat "$WATCH_DIR/task-template-pi.txt"
    echo; echo "(本次唤醒探针注入: reason=$reason, threads=$threads)"
  } > "$WATCH_DIR/task-pi-live.txt"
  wsl -e bash -c 'export PATH=$HOME/opt/node22/bin:$PATH; export DEEPSEEK_API_KEY=$(sed -n "s/^DEEPSEEK_API_KEY: //p" /mnt/c/path/to/credentials.yml); cd ~ && timeout 900 ~/.npm-global/bin/pi -p --no-session --model deepseek/deepseek-flash "$(cat /mnt/c/path/to/rb-watch/task-pi-live.txt)"'
}

# --- the round ----------------------------------------------------------------
echo "=== $(date '+%F %T') host-watch round (board=$BOARD) ==="
# P2-5 (thread #24): adoption self-check — the shipped wake_* examples carry
# maintainer-local placeholder paths; fail loudly instead of half-working.
if grep -q "/mnt/c/path/to/" "$WATCH_DIR/host-watch.sh" 2>/dev/null && [ ! -f /tmp/rb-host-watch-configured ]; then
  echo "!! this is the unmodified adoption template — the wake_* functions below still"
  echo "   contain placeholder paths (/mnt/c/path/to/...). Configure them for your"
  echo "   members (see the EDIT THESE section), set PROBE_AUTHORS, then touch"
  echo "   /tmp/rb-host-watch-configured to silence this check. Refusing to half-run."
  exit 1
fi
wake claude    claude    wake_claude
wake dsh       dsh       wake_dsh
wake opencode  opencode  wake_opencode
wake pi        pi        wake_pi
echo "=== round done ==="
