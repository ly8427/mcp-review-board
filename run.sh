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
  echo "fastmcp not found — installing into user site..."
  python3 -m pip install --user -r requirements.txt
fi

echo "Starting MCP Review Board on http://127.0.0.1:8765  (Ctrl+C to stop)"
exec python3 server.py
