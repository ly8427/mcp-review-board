"""connect/close pairing audit — the executable behind M6 checklist item (i)
(thread #42: dsh #450/#454 forensics, ruling #455/#456 "A+B 并用").

What it does: per (file, function, variable) count of sqlite3.connect sites
vs close() calls, so "a close exists somewhere in the function" cannot be
mistaken for "every connect on that name is closed". Pure stdlib ast, zero
dependencies. This file is the SINGLE source of truth for the audit logic —
the M6 checklist text only points here (pi #456 anti-drift: a second copy of
the logic would be a new drift surface).

M6 item (i), as worded since this file landed: run
    python tools/probe_m6_counts.py
against the then-current HEAD and record the (files, sites, deficit) triple.
Any earlier reading (e.g. #450's "10 files / 40 sites, deficit =
server.py:409 only") is a HISTORICAL reading tied to the tree it was taken
on, not a current fact — the anchor moves with every commit (pi #452/#456).

Boundary (unchanged from #450): no path analysis — a deficit is a REPORT,
not a proof; runtime closure (fix A's _AutoCloseConnection) and the
in-matrix anti-regression case (#440A) keep their own gates. The Windows
unmasked re-run remains the behavioral discriminator.

Self-check (pi #456): `python tools/probe_m6_counts.py --selfcheck` feeds a
known-leaking fixture and a clean fixture; the tool must report deficit>=1 on
the first and 0 on the second. This is what tells "the script broke" apart
from "the tree got clean" — the static-tool analogue of #441's "a masked
exit 0 is no closure evidence".
"""
import ast
import pathlib
import sys
import tempfile
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP = {".git", ".dsh-run", ".dsh-tmp", "__pycache__", "build", "dist",
        "node_modules", ".venv", "venv"}


def is_connect(n):
    return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "connect" and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "sqlite3")


def audit(root: pathlib.Path):
    """-> (files_with_connect, total_connect_sites, deficits) — the triple."""
    deficits = []
    files = 0
    sites = 0
    for p in sorted(root.rglob("*.py")):
        if SKIP & set(p.relative_to(root).parts):
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        file_sites = 0
        pm = {}
        for par in ast.walk(tree):
            for ch in ast.iter_child_nodes(par):
                pm[ch] = par
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            conn = Counter()
            close = Counter()
            lines = {}
            for n in ast.walk(fn):
                if is_connect(n):
                    par = pm.get(n)
                    var = None
                    if isinstance(par, ast.Assign) and isinstance(par.targets[0], ast.Name):
                        var = par.targets[0].id
                    elif isinstance(par, ast.withitem) and isinstance(par.optional_vars, ast.Name):
                        var = par.optional_vars.id
                    if var:
                        conn[var] += 1
                        lines.setdefault(var, []).append(n.lineno)
                elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr == "close" and isinstance(n.func.value, ast.Name)):
                    close[n.func.value.id] += 1
            wrapped = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                          and n.func.attr in ("closing", "enter_context") for n in ast.walk(fn))
            for var, c in conn.items():
                file_sites += c
                if wrapped:
                    continue
                ncl = close[var]
                if ncl < c:
                    deficits.append((str(p.relative_to(root)).replace("\\", "/"),
                                     fn.name, var, c, ncl, lines[var]))
        if file_sites:
            files += 1
            sites += file_sites
    return files, sites, deficits


def selfcheck() -> int:
    # NB: an inline unassigned connect (e.g. contextlib.closing(sqlite3.connect(...)))
    # is invisible to this pairing by design (#450 boundary) — so the clean
    # fixture uses the repo's dominant named-and-closed style.
    leaky = 'import sqlite3\ndef f(db):\n    c = sqlite3.connect(db)\n    return c.execute("SELECT 1").fetchone()\n'  # noqa: E501
    clean = ('import sqlite3\n'
             'def g(db):\n    c = sqlite3.connect(db)\n'
             '    try:\n        return c.execute("SELECT 1").fetchone()\n'
             '    finally:\n        c.close()\n')
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "leaky.py").write_text(leaky, encoding="utf-8")
        (root / "clean.py").write_text(clean, encoding="utf-8")
        f, s, d = audit(root)
        ok = (f, s) == (2, 2) and len(d) == 1 and d[0][0] == "leaky.py"
        print("selfcheck:", "PASS" if ok else f"FAIL ({f} files, {s} sites, deficits={d})")
        return 0 if ok else 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        return selfcheck()
    files, sites, deficits = audit(ROOT)
    print(f"connect/close audit: files={files} sites={sites} deficits={len(deficits)}")
    for f_, fn, var, c, ncl, ln in deficits:
        print(f"  {f_}:{ln[0]}  {fn}()  var={var}  connects={c} closes={ncl}  lines={ln}")
    if not deficits:
        print("  (only runtime-managed sites — fix A's factory owns those)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
