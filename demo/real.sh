#!/usr/bin/env bash
# demo: real — a REAL second coding agent reviews a patch on a scratch board.
#
# You (or your own agent) play the author; a headless agent is woken to play
# the independent reviewer. The reviewer REALLY reviews: it may pass, or it
# may object — then you fix, it re-reviews, and the board converges.
#
# Auto-detected reviewer: claude (Claude Code CLI, headless -p mode).
# No claude? The script prints everything you need to point any other
# MCP-capable agent (dsh headless, codex, opencode, …) at the same board.
#
# Windows note (measured on this project): `claude -p` multi-line prompts
# MUST be fed via stdin — this script already does that. Run from WSL or
# Git Bash.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python
  elif [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe
  elif command -v python3 >/dev/null 2>&1; then PY=python3
  else PY=python; fi
fi
command -v curl >/dev/null 2>&1 || { echo "!! this demo needs curl"; exit 1; }

# -- pick a port: prefer the default 8765, but NEVER touch a live board -------
PORT=8765
if curl -s -m 2 "http://127.0.0.1:8765/" | grep -q "MCP Review Board"; then
  echo "· a live board already answers on 8765 — demos never touch it; scratch port instead"
  PORT=18767
elif curl -s -m 2 -o /dev/null "http://127.0.0.1:8765/"; then
  PORT=18767
fi
URL="http://127.0.0.1:$PORT"
# -- boot the scratch board ---------------------------------------------------
rm -f demo/real.db
REVIEWBOARD_PORT=$PORT REVIEWBOARD_DB=demo/real.db REVIEWBOARD_HOST=127.0.0.1 \
  "$PY" server.py >/dev/null 2>&1 &
SRV=$!
trap 'kill "$SRV" 2>/dev/null || true' EXIT
for _ in $(seq 1 80); do curl -sf -m 2 -o /dev/null "$URL/" && break; sleep 0.5; done
curl -sf -m 2 -o /dev/null "$URL/" || { echo "!! scratch board failed to start"; exit 1; }
echo "✓ scratch board: $URL   (dashboard: $URL/ · mcp: $URL/mcp · db: demo/real.db)"

# -- mcp config for the headless reviewer --------------------------------------
cat > demo/real.mcp.json <<EOF
{
  "mcpServers": {
    "review-board": { "type": "http", "url": "$URL/mcp" }
  }
}
EOF

# -- alpha (scripted author) submits the patch ----------------------------------
TID=$("$PY" demo/real_alpha.py submit --port "$PORT" | awk '/^TID /{print $2}')
echo "✓ alpha submitted the patch → thread #$TID (quorum: alpha + beta-real)"

# -- the reviewer ----------------------------------------------------------------
make_prompt() {
  sed -e "s/__PORT__/$PORT/g" -e "s/__TID__/$TID/g" demo/reviewer-prompt.txt > demo/reviewer-run.txt
}

run_reviewer() {
  make_prompt
  if command -v claude >/dev/null 2>&1; then
    echo "· waking a REAL reviewer (claude, headless one-shot)…"
    if ! claude -p --mcp-config demo/real.mcp.json \
          --allowed-tools "mcp__review-board" "Read" "Write" \
          < demo/reviewer-run.txt; then
      # older claude CLI spell: --allowedTools
      claude -p --mcp-config demo/real.mcp.json \
        --allowedTools "mcp__review-board" "Read" "Write" \
        < demo/reviewer-run.txt
    fi
  else
    echo "!! no headless agent auto-detected (looked for: claude)"
    echo "   Run ANY MCP-capable agent against $URL/mcp with this prompt:"
    echo "   ----------------------------------------------------------------"
    cat demo/reviewer-run.txt
    echo "   ----------------------------------------------------------------"
    read -rp "   press Enter once that agent has voted… "
  fi
}

resolved() {
  "$PY" demo/real_alpha.py status --port "$PORT" --tid "$TID" | grep -q "\[resolved\]"
}

for round in 1 2 3; do
  run_reviewer
  echo "· thread state:"
  "$PY" demo/real_alpha.py status --port "$PORT" --tid "$TID" | sed 's/^/    /'
  if resolved; then break; fi
  [ "$round" = 3 ] && break
  echo
  echo "┌─ your turn (author side — fix it with your OWN editor/agent) ───────"
  read -rp "│ the reviewer objected. Fix the flaw, then Enter; one-line summary: " SUMMARY
  [ -z "$SUMMARY" ] && SUMMARY="revision: flaw fixed per reviewer's flip condition"
  "$PY" demo/real_alpha.py revise --port "$PORT" --tid "$TID" --summary "$SUMMARY" >/dev/null
  echo "└─ revision bumped + posted — reviewer will re-review the new revision"
done

echo
if resolved; then
  echo "✓ CONVERGED: submission → objection → revision → pass."
  echo "  You just watched two DIFFERENT agents review each other on one board."
else
  echo "· thread still open — that's allowed too: objections stand until flipped."
fi
echo "· dashboard (browse before Ctrl+C): $URL/"
echo "· done? press Ctrl+C to stop the scratch board"
wait
