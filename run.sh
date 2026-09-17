#!/usr/bin/env bash
# Start the MCP Review Board server in WSL.
# Windows-side tools (ZCode, Trae CN) reach it via http://localhost:8765/mcp
# because WSL2 is in mirrored networking mode (see ~/.wslconfig).
#
# Usage:  ./run.sh        (foreground, Ctrl+C to stop)
set -euo pipefail
cd "$(dirname "$0")"

# interpreter: prefer the repo venv (same resolution as demo.sh)
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python
  elif [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe
  elif command -v python3 >/dev/null 2>&1; then PY=python3
  else PY=python; fi
fi

# Make sure deps are present
if ! "$PY" -c "import fastmcp" 2>/dev/null; then
  echo "fastmcp not found — installing..."
  # plain install first (venvs REJECT --user); then --user (legacy system
  # pythons); PEP-668 systems need pipx — say so instead of failing silently.
  if ! "$PY" -m pip install -r requirements.txt 2>/dev/null; then
    if ! "$PY" -m pip install --user -r requirements.txt 2>/dev/null; then
      echo "!! automatic install failed (externally-managed environment?)."
      echo "   try: pipx install git+https://github.com/ly8427/mcp-review-board"
      echo "   or : $PY -m pip install --break-system-packages -r requirements.txt"
      exit 1
    fi
  fi
fi

echo "Starting MCP Review Board on http://127.0.0.1:8765  (Ctrl+C to stop)"
exec "$PY" server.py
