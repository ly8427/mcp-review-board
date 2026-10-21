#!/usr/bin/env bash
# ensure-server.sh — 板服务器开机自启守卫（幂等：已在跑则不动）。
# 用法：登录时由 Startup/rb-server.vbs 调起；也可手动跑。
# 日志：data/server-autostart.log
set -u
cd "$(dirname "$0")/.."

if ss -tln 2>/dev/null | grep -q ":8765"; then
  echo "$(date '+%F %T') server already listening on 8765 — no-op"
  exit 0
fi
if pgrep -f "\.venv/bin/python server\.py" >/dev/null 2>&1; then
  echo "$(date '+%F %T') server process exists (port not up yet?) — no-op, watcher probes will retry"
  exit 0
fi
mkdir -p data
nohup .venv/bin/python server.py > data/server.log 2>&1 &
# 大库 WAL 恢复可能要 10s+（2026-10-21 实测：6s 判死是误报）——轮询至 30s
ok=""
for i in $(seq 1 15); do
  sleep 2
  if curl -sf --max-time 3 http://localhost:8765/ping >/dev/null 2>&1; then ok=1; break; fi
done
if [ -n "$ok" ]; then
  echo "$(date '+%F %T') server STARTED after ~$((i*2))s ($(curl -s --max-time 3 http://localhost:8765/ping))"
else
  echo "$(date '+%F %T') server start FAILED (30s no ping) — see data/server.log"
  exit 1
fi
