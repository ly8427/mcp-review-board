#!/usr/bin/env bash
# Start the MCP Review Board server in WSL.
# Windows-side tools (ZCode, Trae CN) reach it via http://localhost:8765/mcp
# because WSL2 is in mirrored networking mode (see ~/.wslconfig).
#
# Usage:  ./run.sh        (foreground, Ctrl+C to stop; WSL/Linux/macOS/Git Bash)
#         ./run.bat       (Windows cmd/PowerShell equivalent)
#
# Deps: Python >= 3.10 + fastmcp. This script does NOT auto-install — on
# externally-managed (PEP 668) system pythons it fails loudly with the fix
# (thread #15 rev3, Q1=A).
set -euo pipefail
cd "$(dirname "$0")"

# --- interpreter selection (thread #15 rev3): OS-aware + cross-OS venv guard
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) OSTYPE=windows ;;
  *)                    OSTYPE=posix ;;
esac

# A .venv built by the OTHER OS would make us execute the wrong binary and
# fail misleadingly. Refuse loudly instead.
# (thread #15 rev3: detect via existence + pyvenv.cfg home= — the -x test is
# unreliable across the WSL/NTFS boundary for foreign-OS binaries.)
if [ -f .venv/pyvenv.cfg ] && [ "$OSTYPE" = "windows" ] && [ ! -e .venv/Scripts/python.exe ]; then
  if [ -e .venv/bin/python ] || grep -qi '^home *= */' .venv/pyvenv.cfg; then
    echo "!! this checkout contains a Linux-built .venv (no .venv/Scripts/)"
    echo "   using it from Windows would execute an ELF binary and fail misleadingly."
    echo "   rebuild it for THIS side:"
    echo "     rm -rf .venv && python -m venv .venv        # python.org Python, not the Store alias"
    exit 1
  fi
fi
if [ -f .venv/pyvenv.cfg ] && [ "$OSTYPE" = "posix" ] && [ ! -e .venv/bin/python ]; then
  if [ -e .venv/Scripts/python.exe ] || grep -qi '^home *= *[A-Za-z]:[\\/]' .venv/pyvenv.cfg; then
    echo "!! this checkout contains a Windows-built .venv (no .venv/bin/)"
    echo "   rebuild it for THIS side:"
    echo "     rm -rf .venv && python3 -m venv .venv"
    exit 1
  fi
fi

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ "$OSTYPE" = "windows" ]; then
    # never pick .venv/bin/python here — in a checkout that also carries a
    # Linux venv it is an ELF binary Git Bash cannot execute
    if [ -e .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe
    elif command -v python3 >/dev/null 2>&1; then PY=python3
    else PY=python; fi
  else
    if [ -e .venv/bin/python ]; then PY=.venv/bin/python
    elif [ -e .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe
    elif command -v python3 >/dev/null 2>&1; then PY=python3
    else PY=python; fi
  fi
fi

# --- dependency check: loud failure, no auto-install, no workarounds (Q1=A)
if ! "$PY" -c "import sys" 2>/dev/null; then
  echo "!! no working Python interpreter (tried: $PY)"
  echo "   on Windows: install Python >= 3.10 from python.org (the Microsoft"
  echo "   Store 'python' alias is a stub) or use WSL, then retry."
  exit 1
fi
if ! "$PY" -c "import fastmcp" 2>/dev/null; then
  echo "!! fastmcp is missing for interpreter: $PY"
  echo "   fix (pick one):"
  echo "     python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt && $0"
  echo "     pipx install git+https://github.com/ly8427/mcp-review-board"
  echo "   (Windows venv variant: python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt)"
  exit 1
fi

echo "Starting MCP Review Board on http://127.0.0.1:8765  (Ctrl+C to stop)"
exec "$PY" server.py
