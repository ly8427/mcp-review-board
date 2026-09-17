#!/usr/bin/env bash
# Start the MCP Review Board server in WSL.
# Windows-side tools (ZCode, Trae CN) reach it via http://localhost:8765/mcp
# because WSL2 is in mirrored networking mode (see ~/.wslconfig).
#
# Usage:  ./run.sh        (foreground, Ctrl+C to stop)
set -euo pipefail
cd "$(dirname "$0")"

# Make sure deps are present
if ! python3 -c "import fastmcp" 2>/dev/null; then
  echo "fastmcp not found — installing..."
  # plain install first (venvs REJECT --user); then --user (legacy system
  # pythons); PEP-668 systems need pipx — say so instead of failing silently.
  if ! python3 -m pip install -r requirements.txt 2>/dev/null; then
    if ! python3 -m pip install --user -r requirements.txt 2>/dev/null; then
      echo "!! automatic install failed (externally-managed environment?)."
      echo "   try: pipx install git+https://github.com/ly8427/mcp-review-board"
      echo "   or : python3 -m pip install --break-system-packages -r requirements.txt"
      exit 1
    fi
  fi
fi

echo "Starting MCP Review Board on http://127.0.0.1:8765  (Ctrl+C to stop)"
exec python3 server.py
