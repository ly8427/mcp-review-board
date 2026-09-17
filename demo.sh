#!/usr/bin/env bash
# Demo entry for mcp-review-board (needs Python >= 3.10 + fastmcp, one-time
# setup — this script deliberately does NOT auto-install anything; on
# externally-managed (PEP 668) system pythons it fails loudly with the fix
# instead. thread #15 rev3, Q1=A).
#
#   ./demo.sh            replay the REAL self-review history (no API key)
#   ./demo.sh mock       two scripted agents drive the full loop (~1 min, no API key)
#   ./demo.sh real       a REAL second agent reviews a patch (needs a headless agent)
#   ./demo.sh clean      stop any demo server left running via --keep
#
# Run from WSL / Linux / macOS / Git Bash. From PowerShell/cmd a .sh file is
# handed to the Windows file association and may exit silently with code 0 —
# use one of the shells above.
#
# All demos run against their own scratch server + throwaway db under demo/ —
# your live board (data/reviewboard.db) is never touched.
set -euo pipefail
cd "$(dirname "$0")"

# --- interpreter selection (thread #15 rev3): OS-aware + cross-OS venv guard
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) OSTYPE=windows ;;
  *)                    OSTYPE=posix ;;
esac

# A .venv built by the OTHER OS in this checkout (e.g. a Linux venv inside a
# Windows checkout) would make us execute the wrong binary (ELF from Git
# Bash / .exe from WSL) and fail misleadingly. Refuse loudly instead.
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
  echo "   expected on success: progress lines, then the banner"
  echo "   'MCP Review Board — replay of the real self-review history'."
  exit 1
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
    cat <<'EOF'
usage: ./demo.sh [replay|mock|real|clean] [--keep]
  replay (default) — replay the real self-review history (no API key)
  mock             — scripted agents drive the full governance loop (~1 min)
  real             — a REAL headless agent reviews a patch (~15 min)
  clean            — stop any demo server left running via --keep
Prerequisites: Python >= 3.10 + fastmcp (one-time venv setup; see README).
Run from WSL / Linux / macOS / Git Bash (not PowerShell/cmd).
EOF
    exit 1
    ;;
esac
