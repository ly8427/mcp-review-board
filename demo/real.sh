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

# -- scratch board: FIXED port 18767, never 8765 -----------------------------
# The live board's port is part of the "demos never touch your live board"
# invariant: occupying 8765 when it happens to be free would collide with a
# board started later and show demo data on the URL users know.
PORT=18767
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
# demo tokens live OUTSIDE the repo worktree (OS temp dir, 0700) — a user
# zipping/uploading demo/ must not be able to ship credentials. The dir is
# owned by real_alpha.py (python tempfile semantics) so bash and the headless
# reviewer agree on the exact absolute path.
TOKENDIR=$("$PY" demo/real_alpha.py tokendir)
TOKENFILE="$TOKENDIR/beta-real.token"

make_prompt() {
  sed -e "s/__PORT__/$PORT/g" -e "s/__TID__/$TID/g" -e "s|__TOKENFILE__|$TOKENFILE|g" \
    demo/reviewer-prompt.txt > demo/reviewer-run.txt
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
  read -rp "│ the reviewer objected. Fix the flaw, then Enter; one-line summary: " SUMMARY || break
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
