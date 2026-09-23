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
  echo fastmcp not found — nothing is auto-installed. Fix, pick one:
  echo   python -m venv .venv ^&^& .venv\Scripts\python -m pip install -r requirements.txt ^&^& run.bat
  echo   pipx install git+https://github.com/ly8427/mcp-review-board
  exit /b 1
)

if "%REVIEWBOARD_PORT%"=="" (set RBB_PORT=8765) else (set RBB_PORT=%REVIEWBOARD_PORT%)
echo Starting MCP Review Board on http://127.0.0.1:%RBB_PORT%  (Ctrl+C to stop)
python server.py
