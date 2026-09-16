"""Shared helpers for the demo scripts: boot a scratch review-board server,
talk MCP over plain HTTP (stdlib only — no client dependencies), and
pretty-print steps.

The demos NEVER touch your live board (data/reviewboard.db): each one boots
its own server on its own port with its own throwaway SQLite db under demo/.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def demo_python() -> str:
    """Interpreter that can import fastmcp: prefer the repo venv (if the user
    set one up — run.sh / systemd both do), else whatever runs the demo."""
    for cand in (REPO / ".venv" / "bin" / "python",
                 REPO / ".venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return str(cand)
    return sys.executable


def c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s


def bold(s: str) -> str: return c("1", s)
def dim(s: str) -> str: return c("2", s)
def green(s: str) -> str: return c("32", s)
def red(s: str) -> str: return c("31", s)
def yellow(s: str) -> str: return c("33", s)
def cyan(s: str) -> str: return c("36", s)


def rule(title: str = "") -> None:
    bar = "━" * 62
    print(f"\n{yellow(bar)}" if not title else f"\n{yellow('━' * 8 + ' ' + title + ' ' + bar[:max(4, 54 - len(title))])}")


def oneline(text: str, width: int = 96) -> str:
    """First line of a server reply, trimmed — keeps demo output scannable."""
    first = text.splitlines()[0] if text else ""
    return first if len(first) <= width else first[: width - 1] + "…"


class Board:
    """A scratch review-board server process + one MCP client session.

    start() boots server.py as a subprocess with its own port/db; call() then
    speaks the same Streamable-HTTP JSON-RPC any real coding agent uses.
    """

    def __init__(self, name: str, port: int, db: Path):
        self.name = name
        self.port = port
        self.db = db
        self.url = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None
        self._sid: str | None = None
        self._req_id = 0

    # -- server lifecycle ----------------------------------------------------
    def start(self, timeout: float = 60.0) -> None:
        if self.db.exists():
            self.db.unlink()  # idempotent reruns
        env = dict(os.environ,
                   REVIEWBOARD_PORT=str(self.port),
                   REVIEWBOARD_DB=str(self.db),
                   REVIEWBOARD_HOST="127.0.0.1")
        self.proc = subprocess.Popen(
            [demo_python(), "server.py"],
            cwd=str(REPO), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"{self.url}/", timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:
                time.sleep(0.4)
        self.stop()
        raise SystemExit(
            f"demo server failed to start on {self.url} — do you have fastmcp "
            f"installed? (./run.sh installs it; see README Quick Start)")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        pid_file = REPO / "demo" / f"{self.name}.pid"
        pid_file.unlink(missing_ok=True)

    def hold(self) -> None:
        """Leave the server running (writes a pid file for `./demo.sh clean`)."""
        (REPO / "demo" / f"{self.name}.pid").write_text(str(self.proc.pid))

    # -- MCP client (stdlib JSON-RPC over Streamable HTTP) --------------------
    def _rpc(self, method: str, params=None) -> dict:
        self._req_id += 1
        payload = {"jsonrpc": "2.0", "id": self._req_id, "method": method}
        if params is not None:
            payload["params"] = params
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self._sid:
            headers["Mcp-Session-Id"] = self._sid
        req = urllib.request.Request(f"{self.url}/mcp",
                                     data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            self._sid = resp.headers.get("Mcp-Session-Id", self._sid)
            body = resp.read().decode()
            if body.startswith("event:") or body.startswith("data:"):
                for line in body.splitlines():
                    if line.startswith("data:"):
                        body = line[5:].strip()
                        break
        return json.loads(body)

    def call(self, tool: str, **args) -> str:
        res = self._rpc("tools/call", {"name": tool, "arguments": args})
        return res["result"]["content"][0]["text"].strip()

    # -- demo conveniences -----------------------------------------------------
    def register(self, name: str) -> None:
        """Register a participant — any author-carrying call does; a bare poll
        is enough (this IS the onboarding step from the protocol)."""
        self.call("list_comments_since", since="1h", author=name)

    def claim(self, name: str) -> str:
        """The full two-phase identity ritual: claim_token → (persist) →
        ack_token. The demo holds the plaintext in memory; real members write
        it to a file that survives their session."""
        out = self.call("claim_token", author=name)
        m = re.search(r"^\s{2}([0-9a-f]{32})\s*$", out, re.M)
        if not m:
            raise SystemExit(f"could not parse token from:\n{out}")
        token = m.group(1)
        self.call("ack_token", author=name, token=token)
        return token


def banner(title: str, board: Board, lines: list[str]) -> None:
    print(cyan("╭─" + "─" * 60))
    for ln in (title, "") + tuple(lines):
        print(cyan("│") + f" {ln}")
    print(cyan("╰─" + "─" * 60))
    print(dim(f"    board: {board.url}/   mcp: {board.url}/mcp   db: {board.db.name}"))
