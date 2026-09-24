#!/usr/bin/env bash
# kit-version: 2.5.0 (drift check: compare with repo copy; update at will)
#
# uninstall-watch.sh — scan common watcher entry points and clean residue
# (thread #25 batch B; instance-scoped in batch D, thread #27 must-fix 2).
# NOTE: this SCANS COMMON entry-point patterns (cron lines, systemd timers and
# Windows Task Scheduler tasks that mention host-watch/watcher by name) — an
# entry you renamed yourself will NOT be found; the running-process and
# state-dir scans below are exhaustive for what they cover.
#
# First action is enumeration: a live watcher probing a dead board never stops
# on its own, and if the port is later reused it wakes members against the
# WRONG board — removing scheduler entries comes before anything.
#
# Usage:
#   bash configs/uninstall-watch.sh [state-dir]      # scan (safe, default)
#   bash configs/uninstall-watch.sh [state-dir] --purge   # also delete that
#        instance-dir's residue (locks/fails/live tasks/events/marker)
# The state dir defaults to <this dir>/watch-state/default; pass (or export)
# STATE_DIR / EXPECTED_BOARD_ID to target the right board instance. Scheduler
# entries are NEVER auto-deleted — they were created by you; the report shows
# exactly what to remove and how.
set -u
WATCH_DIR="$(cd "$(dirname "$0")" && pwd)"
EXPECTED_BOARD_ID="${EXPECTED_BOARD_ID:-}"
STATE_DIR="${1:-${STATE_DIR:-$WATCH_DIR/watch-state/${EXPECTED_BOARD_ID:-default}}}"
PURGE="${2:-${PURGE:-}}"

echo "== 1. scheduler entries (remove these FIRST, by hand) =="
if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -n "host-watch\|watcher" ; then
  echo "   ^ remove with: crontab -e  (delete the matching lines)"
else
  echo "   no cron entries matching host-watch/watcher"
fi
if systemctl --user list-timers --all 2>/dev/null | grep -i "watch"; then
  echo "   ^ remove with: systemctl --user disable --now <name>"
else
  echo "   no systemd user timers matching watch"
fi
echo "   Windows Task Scheduler: check the GUI for tasks running host-watch.sh"
echo "   (search: host-watch). Delete them there."

echo
echo "== 2. running watcher processes =="
found_proc=0
for p in $(pgrep -f "host-watch.sh" 2>/dev/null); do
  echo "   PID $p: $(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | head -c 120)"
  found_proc=1
done
[ "$found_proc" = "0" ] && echo "   none running"
echo "   (stop with: kill <pid>)"

echo
echo "== 3. per-instance state dir: $STATE_DIR =="
if [ -d "$STATE_DIR" ]; then
  ls -la "$STATE_DIR" | tail -n +2
else
  echo "   (absent)"
fi
echo
echo "   other instances under $WATCH_DIR/watch-state/ :"
ls -d "$WATCH_DIR"/watch-state/*/ 2>/dev/null | sed 's/^/     /' || echo "     (none)"

echo
echo "== 4. deployed copies you may want to remove =="
WATCH_DIRS="${WATCH_DIRS:-$HOME/opt/rb $HOME/rb-watch}"
for d in $WATCH_DIRS; do
  [ -d "$d" ] && echo "   $d/ exists ($(ls "$d" | wc -l) files) — check for member token files (chmod 600 while alive, delete on uninstall)"
done
echo "   systemd unit: $([ -f "$HOME/.config/systemd/user/review-board.service" ] && echo 'present (see uninstall steps in the unit header)' || echo absent)"

if [ "$PURGE" = "--purge" ]; then
  echo
  echo "== purging residue in $STATE_DIR only =="
  rm -rfv "$STATE_DIR" 2>/dev/null
  echo "done. Other instances, scheduler entries and deployed copies remain yours"
  echo "to remove (see above)."
else
  echo
  echo "(dry run — re-run with --purge to delete THIS instance's state dir;"
  echo " other instances are never touched; scheduler entries never auto-deleted)"
fi
