#!/usr/bin/env bash
# kit-version: 2.4.1 (drift check: compare with repo copy; update at will)
#
# uninstall-watch.sh — enumerate and clean watcher residue (thread #25 batch B).
# First action is ENUMERATION: a live watcher probing a dead board never stops
# on its own, and if the port is later reused it wakes members against the
# wrong board (#270-3) — so removing scheduler entries comes before anything.
#
# Usage:
#   bash configs/uninstall-watch.sh           # enumerate only (safe, default)
#   bash configs/uninstall-watch.sh --purge   # also delete /tmp residue +
#                                             # the configured-marker file
# Scheduler entries (cron / Task Scheduler / systemd timers) are NEVER
# auto-deleted — they were created by you; the report shows exactly what to
# remove and how.
set -u

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
echo "   Windows Task Scheduler: check Task Scheduler GUI for tasks running"
echo "   host-watch.sh (search: host-watch). Delete them there."

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
echo "== 3. residue under /tmp =="
ls -d /tmp/rb-host-watch-*.lock.d /tmp/rb-host-watch-*.fails \
      /tmp/rb-task-*-live.txt /tmp/rb-pi-events.log 2>/dev/null || echo "   none"
echo "   configured-marker: $([ -f /tmp/rb-host-watch-configured ] && echo present || echo absent)"

echo
echo "== 4. deployed copies you may want to remove =="
for d in "$HOME/opt/rb" "$HOME/rb-watch" /c/Users/liu/ZCodeProject/rb-watch; do
  [ -d "$d" ] && echo "   $d/ exists ($(ls "$d" | wc -l) files) — check for member token files (chmod 600 while alive, delete on uninstall)"
done
echo "   systemd unit: $([ -f "$HOME/.config/systemd/user/review-board.service" ] && echo 'present (see uninstall steps in the unit header)' || echo absent)"

if [ "${1:-}" = "--purge" ]; then
  echo
  echo "== purging /tmp residue and configured-marker =="
  rm -rfv /tmp/rb-host-watch-*.lock.d /tmp/rb-host-watch-*.fails \
          /tmp/rb-task-*-live.txt /tmp/rb-pi-events.log /tmp/rb-host-watch-configured 2>/dev/null
  echo "done. Scheduler entries and deployed copies remain yours to remove (see above)."
else
  echo
  echo "(dry run — re-run with --purge to delete the /tmp residue; scheduler"
  echo " entries and deployed copies are never auto-deleted)"
fi
