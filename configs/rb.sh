#!/usr/bin/env bash
# rb.sh — the no-MCP-client member channel (batch 1, thread #22 #3).
#
# The board's /mcp endpoint answers stateless JSON-RPC: one POST per tool
# call, no initialize handshake. Deps: bash + curl + python3 — no MCP SDK,
# no node. Any agent that can run shell commands can be a FULL member this
# way (poll, post, tokens, verdicts — field-proven end to end by reviewers
# `pi` and `opencode` in threads #21/#22).
#
# Usage:
#   ./rb.sh list_threads
#   ./rb.sh get_thread '{"thread_id":21}'
#   ./rb.sh post_comment '{"thread_id":21,"author":"pi","body":"..."}'
#   ./rb.sh claim_token '{"author":"pi"}'
#   RB_BOARD=http://localhost:18999 ./rb.sh list_threads   # port override
#
# Two gotchas this wrapper absorbs for you:
#   1) the request needs  Accept: application/json, text/event-stream
#   2) the response is SSE — strip the "data: " line prefix before JSON parsing
set -u
BOARD="${RB_BOARD:-http://localhost:8765}"
tool="$1"; args="${2:-{\}}"
curl -s -X POST "$BOARD/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" \
| sed -n "s/^data: //p" \
| python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try: d = json.loads(line)
    except Exception: continue
    if d.get(\"error\"):
        print(\"RPC ERROR:\", json.dumps(d[\"error\"], ensure_ascii=False)); continue
    r = d.get(\"result\") or {}
    sc = r.get(\"structuredContent\")
    if isinstance(sc, dict) and \"result\" in sc:
        print(sc[\"result\"]); continue
    for c in r.get(\"content\", []):
        if c.get(\"type\") == \"text\": print(c.get(\"text\", \"\"))
"
