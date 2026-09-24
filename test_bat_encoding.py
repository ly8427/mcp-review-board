#!/usr/bin/env python3
"""Batch C encoding gate (thread #25, dsh condition: 'this defect has a
recurrence history - without a gate it will come back').

run.bat once hung all of cmd.exe on CP936 Windows because it shipped with LF
line endings plus UTF-8 em-dashes inside an if-block (#230-#232). This test
fails the suite if any .bat file regresses: every .bat must be pure ASCII
and use CRLF line endings in the WORKING TREE.

Run from the repo root:  python test_bat_encoding.py
Intended for CI / pre-commit / the local regression pass alike.
"""
import sys
from pathlib import Path

failures = []
for bat in sorted(Path(".").glob("*.bat")):
    data = bat.read_bytes()
    if b"\r\n" not in data or data.count(b"\n") != data.count(b"\r\n"):
        failures.append(f"{bat}: not CRLF throughout "
                        f"({data.count(b'\\n') - data.count(b'\\r\\n')} bare LF)")
    try:
        data.decode("ascii")
    except UnicodeDecodeError as e:
        bad = data[max(0, e.start - 10):e.start + 10]
        failures.append(f"{bat}: non-ASCII byte at {e.start} (context {bad!r}) — "
                        "cmd.exe on CP936 misparses UTF-8 in if-blocks")

if failures:
    print("❌ BAT ENCODING GATE FAILED (CP936 regression, #230-#232):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("✅ BAT ENCODING GATE PASSED: all .bat files are ASCII + CRLF "
      "(.gitattributes eol=crlf enforces this on checkout)")
