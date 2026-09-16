#!/usr/bin/env python3
"""demo: mock — two scripted agents drive the FULL governance loop against a
REAL review-board server. No LLM, no API key, ~1 minute.

You will watch:

  alpha submits a patch → alpha passes its own work → beta objects
  (beta first tries objecting WITHOUT a flip condition — the server rejects
  it: governance rules live in the server, not in prompt etiquette) →
  alpha revises (bump_revision clears ALL verdicts — stale passes can't
  auto-resolve a new revision) → beta re-reviews → pass → pass →
  the server auto-resolves when the whole active quorum passes.

Nothing here touches your live board: scratch server, scratch db.
This is the exact loop the 30-second GIF on the README records.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from _board import Board, REPO, banner, bold, cyan, dim, green, oneline, red, yellow  # noqa: E402

PATCH_REV1 = """patch under review (rev 1) — notifier.py

    def deliver(url, event, attempts=5):
        for i in range(attempts):
            try:
                return post(url, event, timeout=2)
            except TransientError:
                time.sleep(1.0)          # retry: sleep 1s, go again

Fixes the dropped-ping bug from issue #42. Happy path tested manually."""

OBJECTION = """TransientError retry sleeps a FIXED 1.0s for all 5 attempts.

1. When the webhook endpoint recovers, every queued client reconnects in the
   same second — thundering herd. I've seen exactly this pattern take down a
   receiver during an incident.
2. A hard cap of 5 fixed-interval attempts gives up on slow recoveries
   (>5s of downtime = dropped events), and the cap is undocumented.

Line ref: time.sleep(1.0) in the patch above."""

FLIP_CONDITION = ("pass once retry uses exponential backoff WITH jitter, the "
                  "attempt cap is documented, and a test simulates a "
                  "reconnect storm.")

PATCH_REV2 = """rev 2 — fixes per beta's flip condition:

    def deliver(url, event, attempts=5):
        for i in range(attempts):
            try:
                return post(url, event, timeout=2)
            except TransientError:
                time.sleep(min(2 ** i, 30) * random.uniform(0.5, 1.5))
                                          # exponential backoff + jitter

+ test_reconnect_storm.py: 200 simulated clients against a recovering
  receiver, reconnect spread > 20s, zero drops. Cap documented in the
  module docstring (attempts=5, backoff ceiling 30s)."""


def step(who: str, what: str) -> None:
    print(f"\n{cyan(f'[{who:>5}]')} {bold(what)}")


def reply(text: str) -> None:
    print(f"        {dim('└─ server:')} {dim(oneline(text))}")


def main() -> None:
    keep = "--keep" in sys.argv
    board = Board("mock", 18765, REPO / "demo" / "mock.db")
    board.start()
    banner(
        "MCP Review Board — mock demo",
        board,
        ["two scripted agents (alpha: author, beta: reviewer)",
         "one REAL server enforcing the governance protocol",
         "",
         "watch for: object-without-flip-condition gets REJECTED,",
         "bump_revision clears verdicts, unanimous quorum auto-resolves."])

    try:
        run(board)
        print(f"\n{dim('board stays up 60s so you can open the dashboard ')}"
              f"{dim('(Ctrl+C to quit early, --keep to hold):')}")
        print(f"  {bold(board.url + '/')}")
        if keep:
            board.hold()
            print(dim("    (--keep: server left running; ./demo.sh clean stops it)"))
        else:
            for i in range(60, 0, -10):
                print(dim(f"    {i}s…"), end="\r", flush=True)
                time.sleep(10)
            print()
    except KeyboardInterrupt:
        print()
    finally:
        if not keep:
            board.stop()
    print(green("\n✓ demo complete — server stopped, scratch db kept at demo/mock.db"))
    print(f"next: {bold('./demo.sh real')}  — have a REAL second agent do beta's job\n")


def run(board: Board) -> None:
    step("board", "members register (a bare poll with author= is enough)")
    board.register("alpha")
    board.register("beta")
    reply("registered: alpha, beta")

    step("board", "identity ritual — claim token → persist → ack (two-phase)")
    tok_alpha = board.claim("alpha")
    tok_beta = board.claim("beta")
    print(f"        {dim('└─ tokens claimed + acked:')} "
          f"{dim(tok_alpha[:8] + '… / ' + tok_beta[:8] + '…')}")

    step("alpha", 'opens a review thread: "Add retry to the webhook notifier"')
    out = board.call("create_thread",
                     title="Add retry to the webhook notifier (fixes dropped pings)",
                     context=PATCH_REV1,
                     author="alpha", quorum=["alpha", "beta"])
    reply(out)
    tid = int(out.split("#")[1].split(":")[0])

    step("alpha", "self-review verdict: pass (authors vote too — quorum is everyone)")
    reply(board.call("set_verdict", thread_id=tid, verdict="pass",
                     author="alpha", note="author self-review: happy path works",
                     token=tok_alpha))

    step("beta", "reviews the patch → finds a real flaw → posts it (major)")
    reply(board.call("post_comment", thread_id=tid, author="beta",
                     body=OBJECTION, file="notifier.py", line=7, severity="major"))

    step("beta", "objects — but WITHOUT stating a flip condition …")
    out = board.call("set_verdict", thread_id=tid, verdict="object",
                     author="beta", token=tok_beta)
    print(f"        {red('└─ server:')} {red(oneline(out))}")
    print(f"        {red('   ⤷ rejected: every objection must state what would')}")
    print(f"        {red('     change the verdict — a server-enforced rule, not')}")
    print(f"        {red('     prompt etiquette.')}")

    step("beta", "objects — this time WITH the flip condition")
    reply(board.call("set_verdict", thread_id=tid, verdict="object",
                     author="beta", note=FLIP_CONDITION, token=tok_beta))

    step("alpha", "revises the patch → bump_revision (ALL verdicts cleared)")
    reply(board.call("bump_revision", thread_id=tid, author="alpha",
                     token=tok_alpha))
    step("alpha", "posts rev 2 (backoff + jitter + documented cap + storm test)")
    reply(board.call("post_comment", thread_id=tid, author="alpha",
                     body=PATCH_REV2))

    step("beta", "re-reviews rev 2 → flip condition met → pass")
    reply(board.call("set_verdict", thread_id=tid, verdict="pass",
                     author="beta", note="flip condition met: exp backoff + "
                     "jitter + documented cap + storm test. pass.",
                     token=tok_beta))

    step("alpha", "re-votes on the new revision (first verdict after a bump is free)")
    reply(board.call("set_verdict", thread_id=tid, verdict="pass",
                     author="alpha", note="rev2 self-review: ok", token=tok_alpha))

    out = board.call("get_thread", thread_id=tid)
    status_line = out.splitlines()[0]
    if "[resolved]" not in status_line:
        print(red(f"\n✗ expected thread to auto-resolve, got: {status_line}"))
        print(out)
        sys.exit(1)
    print(f"\n{green('⚡ server: active quorum unanimous → thread RESOLVED')}")
    print(f"        {dim('└─ ' + status_line)}")

    step("board", "aftermath — beta's read-only reliability profile (derived view)")
    prof = board.call("reliability_profile", author="beta")
    for line in prof.splitlines()[:10]:
        print(f"        {dim(line)}")

    step("board", "the audit trail — every verdict and flip, append-only")
    reply(board.call("get_thread", thread_id=tid))


if __name__ == "__main__":
    main()
