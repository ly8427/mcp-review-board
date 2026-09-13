@echo off
REM Start the MCP Review Board server (Windows — for later migration from WSL).
REM Requires Python 3.11+ on Windows (not currently installed; see README).
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found on PATH. Install Python 3.11+ from https://python.org first.
  exit /b 1
)

python -c "import fastmcp" 2>nul
if errorlevel 1 (
  echo fastmcp not found — installing...
  python -m pip install -r requirements.txt
)

echo Starting MCP Review Board on http://127.0.0.1:8765  (Ctrl+C to stop)
python server.py
