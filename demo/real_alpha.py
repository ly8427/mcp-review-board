#!/usr/bin/env python3
"""Scripted author side ("alpha") for `./demo.sh real`.

The REVIEWER in real mode is a real agent; the author's submission is
scripted so the demo always has one known, genuinely reviewable flaw —
the real reviewer still has to find it on its own.

Token handling follows the protocol's two-phase ritual for real: the
plaintext is persisted OUTSIDE the repo worktree (OS temp dir, 0700/0600 —
a user zipping or uploading demo/ cannot ship credentials) and read back
across the separate invocations of this script (a fresh claim would hit
the 24h reissue lock).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _board import Board, REPO  # noqa: E402
from mock import PATCH_REV1  # noqa: E402  (same deliberately-flawed patch)


def token_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "mcp-review-board-demo"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)  # effective on POSIX; no-op-ish on Windows
    except OSError:
        pass
    return d


TOKEN_FILE = token_dir() / "alpha.token"
BETA_TOKEN_FILE = token_dir() / "beta-real.token"


def board_for(port: int) -> Board:
    return Board("real", port, REPO / "demo" / "real.db")


def alpha_token(b: Board) -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    out = b.call("claim_token", author="alpha")
    m = re.search(r"^\s{2}([0-9a-f]{32})\s*$", out, re.M)
    if not m:
        raise SystemExit(f"claim_token did not return a token:\n{out}")
    token = m.group(1)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    b.call("ack_token", author="alpha", token=token)
    return token


def cmd_tokendir() -> None:
    print(token_dir())


def cmd_submit(port: int) -> None:
    for f in (TOKEN_FILE, BETA_TOKEN_FILE):
        f.unlink(missing_ok=True)  # fresh run → stale tokens are poison
    b = board_for(port)
    b.register("beta-real")
    b.register("alpha")
    token = alpha_token(b)
    out = b.call("create_thread",
                 title="Add retry to the webhook notifier (fixes dropped pings)",
                 context=PATCH_REV1,
                 author="alpha", quorum=["alpha", "beta-real"])
    tid = int(out.split("#")[1].split(":")[0])
    b.call("set_verdict", thread_id=tid, verdict="pass", author="alpha",
           note="author self-review: happy path works", token=token)
    print(f"TID {tid}")


def cmd_revise(port: int, tid: int, summary: str) -> None:
    b = board_for(port)
    token = alpha_token(b)
    print(b.call("bump_revision", thread_id=tid, author="alpha", token=token))
    print(b.call("post_comment", thread_id=tid, author="alpha", body=summary))
    print(b.call("set_verdict", thread_id=tid, verdict="pass", author="alpha",
                 note=f"self-review of this revision: {summary}", token=token))


def cmd_status(port: int, tid: int) -> None:
    b = board_for(port)
    out = b.call("get_thread", thread_id=tid)
    for line in out.splitlines()[:3]:
        print(line)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tokendir", help="print the demo token dir (owned here, 0700)")
    s = sub.add_parser("submit"); s.add_argument("--port", type=int, required=True)
    r = sub.add_parser("revise"); r.add_argument("--port", type=int, required=True)
    r.add_argument("--tid", type=int, required=True); r.add_argument("--summary", required=True)
    t = sub.add_parser("status"); t.add_argument("--port", type=int, required=True)
    t.add_argument("--tid", type=int, required=True)
    a = p.parse_args()
    {"tokendir": cmd_tokendir,
     "submit": lambda: cmd_submit(a.port),
     "revise": lambda: cmd_revise(a.port, a.tid, a.summary),
     "status": lambda: cmd_status(a.port, a.tid)}[a.cmd]()


if __name__ == "__main__":
    main()
