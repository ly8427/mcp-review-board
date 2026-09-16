#!/usr/bin/env bash
# One-command demo for mcp-review-board.
#
#   ./demo.sh            replay the REAL self-review history (0 config, no API key)
#   ./demo.sh mock       two scripted agents drive the full loop (~1 min, no API key)
#   ./demo.sh real       a REAL second agent reviews a patch (needs a headless agent)
#   ./demo.sh clean      stop any demo server left running via --keep
#
# All demos run against their own scratch server + throwaway db under demo/ —
# your live board (data/reviewboard.db) is never touched.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python
  elif [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe
  elif command -v python3 >/dev/null 2>&1; then PY=python3
  else PY=python; fi
fi

if ! "$PY" -c "import fastmcp" 2>/dev/null; then
  echo "· fastmcp missing — installing (same as run.sh)…"
  "$PY" -m pip install --user -r requirements.txt
fi

cmd="${1:-replay}"
if [ "$#" -gt 0 ]; then shift; fi

case "$cmd" in
  replay) exec "$PY" demo/replay.py "$@" ;;
  mock)   exec "$PY" demo/mock.py "$@" ;;
  real)   exec bash demo/real.sh "$@" ;;
  clean)
    for f in demo/*.pid; do
      [ -e "$f" ] || continue
      pid=$(cat "$f")
      if kill "$pid" 2>/dev/null; then
        echo "· stopped demo server pid $pid"
      else
        echo "· pid $pid not running"
      fi
      rm -f "$f"
    done
    echo "· clean done"
    ;;
  *)
    sed -n '2,10p' "$0" | sed 's/^# //'
    exit 1
    ;;
esac
