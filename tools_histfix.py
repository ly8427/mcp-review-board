"""One-shot history-content sanitizer for git filter-branch --tree-filter.

Robust patterns (per board thread #9 #60, dsh's re-review): covers JSON
double-backslash escapes, plain Windows paths, POSIX /c/ and /mnt/c forms,
and hostname — all built WITHOUT literal backslashes in this source (the
wsl.exe transport eats one level), using BS = chr(92).
"""
import pathlib
import re
import sys

BS = chr(92)
DBS = BS + BS          # two literal backslash chars in file bytes
NAME = r"[A-Za-z0-9._-]+"

pats = [
    (re.compile("C:" + re.escape(DBS) + "Users" + re.escape(DBS) + NAME), "%USERPROFILE%"),
    (re.compile("C:" + re.escape(BS) + "Users" + re.escape(BS) + NAME), "%USERPROFILE%"),
    (re.compile("/mnt/c/Users/" + NAME + "/ZCodeProject/mcp-review-board"), "<REPO_ROOT>"),
    (re.compile("/c/Users/" + NAME + "/AppData/Roaming/npm/dsh.cmd"), "dsh"),
    (re.compile(r"(?<![/A-Za-z0-9_.-])Users/" + NAME + r"(?=/AppData)"), "Users/<you>"),
    (re.compile(r"(?<![/A-Za-z0-9_.-])Users/" + NAME + r"(?=/\.zcode|/\.dsh|/\.claude)"), "Users/<you>"),
    (re.compile("DESKTOP-[A-Z0-9]+"), "<host>"),
]

root = pathlib.Path(".")
changed = 0
for f in root.rglob("*"):
    if not f.is_file():
        continue
    parts = set(f.parts)
    if parts & {".git", ".venv", "data", "__pycache__"}:
        continue
    try:
        s = f.read_text(encoding="utf-8")
    except (UnicodeDecodeError, ValueError):
        continue
    o = s
    for pat, rep in pats:
        s = pat.sub(rep, s)
    if s != o:
        f.write_text(s, encoding="utf-8")
        changed += 1
        print("fixed:", f, file=sys.stderr)
print("files changed:", changed)
