#!/usr/bin/env python3
"""demo: replay — watch the REAL self-review history of this project,
replayed into a scratch board. No LLM, no API key, no config.

In September 2026 this board reviewed ITSELF for seven rounds. It caught,
before anyone outside saw them: a missing LICENSE, a username leaked across
the entire git history, a regression introduced by the fix itself, a metric
that would have scored the board's best work as failure, and a protocol text
that outsourced reviewer wake-up to humans. Every catch has a thread id, an
objection, a revision and a flip record.

The fixture (demo/replay.json) is a condensed copy of the real audit log —
original posts are quoted in the original language, condensed for terminal
reading. Full story: docs/self-review.md.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _board import Board, REPO, banner, bold, cyan, dim, green, oneline, red, yellow  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "replay.json"


def main() -> None:
    keep = "--keep" in sys.argv
    steps = json.loads(FIXTURE.read_text(encoding="utf-8"))["steps"]
    board = Board("replay", 18766, REPO / "demo" / "replay.db")
    board.start()
    banner("MCP Review Board — replay of the real self-review history",
           board,
           ["7 rounds, 3 defect threads, every flip on the audit log.",
            "Nothing is faked: quotes are from the real threads (#9 #11 #13),",
            "condensed for the terminal. Your live board is not touched."])
    try:
        run(board, steps)
        if keep:
            board.hold()
            print(dim("\n(--keep: server left running; ./demo.sh clean stops it)"))
        else:
            print(dim("\nboard stays up 30s so you can open the dashboard:"))
            print(f"  {bold(board.url + '/')}")
            time.sleep(30)
    except KeyboardInterrupt:
        print()
    finally:
        if not keep:
            board.stop()
    print(green("\n✓ replay complete"))
    print(f"next: {bold('./demo.sh mock')}  (drive the loop yourself, no API key)")
    print(f"      {bold('./demo.sh real')} (a REAL second agent reviews)\n")


def run(board: Board, steps: list[dict]) -> None:
    tokens: dict[str, str] = {}
    tid: int | None = None
    for step in steps:
        if "caption" in step:
            text = step["caption"]
            if text.startswith("··"):
                print(f"\n{yellow(' ' * 8 + text)}")
            elif text.startswith("⚡"):
                print(f"\n{green(' ' * 8 + text)}")
            else:
                print(f"\n{yellow('━' * 8 + ' ' + text)}")
            time.sleep(step.get("pause", 1.6))
            continue

        who, say = step["who"], step["say"]
        tool = step["tool"]
        print(f"{cyan(f'[{who:>5}]')} {say}")

        if tool == "join":  # demo-only shorthand: register + claim + ack
            board.register(step["name"])
            tokens[step["name"]] = board.claim(step["name"])
            print(f"        {dim('└─ registered + token claimed/acked')}")
        else:
            args = {k: subst(v, tid) for k, v in step.get("args", {}).items()}
            if tool in ("set_verdict", "bump_revision", "set_quorum"):
                args.setdefault("token", tokens.get(who))
            out = board.call(tool, **args)
            if tool == "create_thread":
                tid = int(out.split("#")[1].split(":")[0])
            if step.get("expect_error"):
                print(f"        {red('└─ server:')} {red(oneline(out))}")
            elif step.get("assert_status"):
                head = out.splitlines()[0]
                ok = f"[{step['assert_status']}]" in head
                mark = green("✓") if ok else red("✗")
                print(f"        {dim('└─ server:')} {dim(head)} {mark}")
                if not ok:
                    sys.exit(1)
            else:
                print(f"        {dim('└─ server:')} {dim(oneline(out))}")
        if step.get("after"):
            print(f"        {dim('   ⤷ ' + step['after'])}")
        time.sleep(step.get("pause", 1.1))


def subst(v, tid):
    if isinstance(v, str) and "@tid" in v:
        return v.replace("@tid", str(tid)) if not v.startswith("@tid") else tid
    return v


if __name__ == "__main__":
    main()
