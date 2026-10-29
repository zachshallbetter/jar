#!/usr/bin/env python3
# gate: hermetic
"""Zone and naive-date lint, production side (#1570): the household's day is not titan's (UTC) day.

Flagged outside tests, each as `file:function:rule`:
  today         date.today()                          (the host's date)
  localtime     time.localtime(...)                   (the host's zone)
  strftime      time.strftime(...) with no gmtime     (formats the host's local time)
  now           datetime.now() with no tz argument    (naive host time)
  fromts        datetime.fromtimestamp(...) with no tz= / second argument (naive host time)
  zoneliteral   ZoneInfo("America/...") anywhere but local_time.py (use local_time.zone(), which reads AGENT_TZ)
Every hit that is not fixed goes in scripts/zone_lint_allow.txt as `file:function:rule  # why`, one reason per line. An entry
that stops matching fails too. Scans scripts/ (and server-setup/) Python plus the ZoneInfo literal in shell files.
Reads files only. Run: python3 scripts/test_zone_lint.py"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
ALLOW = HERE / "zone_lint_allow.txt"
ZONE_HOME = "local_time.py"
SH_ZONE = re.compile(r"ZoneInfo\(.*[\"']America/")

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.900} expected={expected!r:.300}"))


def dotted(n: ast.AST) -> str:
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        return dotted(n.value) + "." + n.attr
    return ""


def has_gmtime(n: ast.AST) -> bool:
    return any(dotted(x).endswith("gmtime") for x in ast.walk(n) if isinstance(x, (ast.Name, ast.Attribute)))


def naive_none(c: ast.Call) -> bool:
    """No tz given: no arguments at all, or a literal None."""
    tz = [k.value for k in c.keywords if k.arg == "tz"]
    args = tz or c.args[:1]
    return not args or (isinstance(args[0], ast.Constant) and args[0].value is None)


def rule_of(c: ast.Call) -> str | None:
    name = dotted(c.func)
    last2 = ".".join(name.split(".")[-2:])
    if last2 in ("date.today",):
        return "today"
    if name.endswith("time.localtime") or name == "localtime":
        return "localtime"
    if name.endswith("time.strftime") or name == "strftime":
        return None if has_gmtime(c) else "strftime"
    if last2 in ("datetime.now", "dt.now", "_dt.now") and naive_none(c):
        return "now"
    if last2 in ("datetime.fromtimestamp", "dt.fromtimestamp", "_dt.fromtimestamp", "date.fromtimestamp"):
        tz = any(k.arg == "tz" for k in c.keywords) or len(c.args) > 1
        return None if tz else "fromts"
    if name.split(".")[-1] == "ZoneInfo":
        for x in ast.walk(c):
            if isinstance(x, ast.Constant) and isinstance(x.value, str) and x.value.startswith("America/"):
                return "zoneliteral"
    return None


def scan(text: str, name: str = "x.py") -> list[str]:
    """`name:function:rule` for each flagged call (function is <module> at module level, the innermost def otherwise)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: set[str] = set()

    def walk(n: ast.AST, fn: str):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = n.name
        if isinstance(n, ast.Call):
            r = rule_of(n)
            if r and not (r == "zoneliteral" and name == ZONE_HOME):
                out.add(f"{name}:{fn}:{r}")
        for c in ast.iter_child_nodes(n):
            walk(c, fn)

    walk(tree, "<module>")
    return sorted(out)


def scan_sh(text: str, name: str) -> list[str]:
    return [f"{name}:<shell>:zoneliteral"] if any(
        SH_ZONE.search(ln) for ln in text.splitlines() if not ln.lstrip().startswith("#")) else []


def allowlist() -> dict[str, str]:
    out = {}
    if ALLOW.exists():
        for line in ALLOW.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, why = line.partition("#")
                out[key.strip()] = why.strip()
    return out


# The lint itself.
check("date.today() is flagged", scan("def f():\n    return date.today()\n"), ["x.py:f:today"])
check("…datetime.date.today() too", scan("x = datetime.date.today()\n"), ["x.py:<module>:today"])
check("time.localtime() is flagged", scan("def f(t):\n    return time.localtime(t)\n"), ["x.py:f:localtime"])
check("time.strftime without gmtime is flagged", scan("def f():\n    return time.strftime('%H')\n"), ["x.py:f:strftime"])
check("…with an explicit struct that is not gmtime too", scan("def f(t):\n    return time.strftime('%H', t)\n"), ["x.py:f:strftime"])
check("…with gmtime it is fine", scan("def f(t):\n    return time.strftime('%H', time.gmtime(t))\n"), [])
check("a bare datetime.now() is flagged", scan("def f():\n    return datetime.now()\n"), ["x.py:f:now"])
check("…datetime.now(None) too", scan("def f():\n    return datetime.now(None)\n"), ["x.py:f:now"])
check("…with a zone or utc it is fine", scan("def f(z):\n    return datetime.now(z), datetime.now(tz=z), dt.now(timezone.utc)\n"), [])
check("datetime.fromtimestamp(t) is flagged", scan("def f(t):\n    return datetime.fromtimestamp(t)\n"), ["x.py:f:fromts"])
check("…with tz= or a second argument it is fine",
      scan("def f(t, z):\n    return datetime.fromtimestamp(t, tz=z), datetime.fromtimestamp(t, z)\n"), [])
check("ZoneInfo with an America literal is flagged",
      scan("z = ZoneInfo(os.environ.get('AGENT_TZ', 'America/Los_Angeles'))\n"), ["x.py:<module>:zoneliteral"])
check("…but not in local_time.py", scan("z = ZoneInfo('America/Los_Angeles')\n", "local_time.py"), [])
check("…and a ZoneInfo of a name that is not a literal is fine", scan("def f(n):\n    return ZoneInfo(n)\n"), [])
check("the innermost def names the hit", scan("def a():\n    def b():\n        return date.today()\n    return b\n"), ["x.py:b:today"])
check("a shell ZoneInfo literal is flagged, a comment is not",
      (scan_sh("python3 -c 'ZoneInfo(\"America/Denver\")'\n", "g.sh"), scan_sh("# ZoneInfo(\"America/Denver\")\n", "g.sh")),
      (["g.sh:<shell>:zoneliteral"], []))

found: set[str] = set()
files = [p for d in (HERE, ROOT / "server-setup") for p in sorted(d.rglob("*.py"))]
for p in files:
    if p.name.startswith("test_") or "__pycache__" in p.parts:
        continue
    rel = p.relative_to(HERE) if HERE in p.parents else p.relative_to(ROOT)
    found.update(scan(p.read_text(errors="replace"), str(rel)))
for d in (HERE, ROOT / "server-setup"):
    for p in sorted(d.rglob("*.sh")):
        rel = p.relative_to(HERE) if HERE in p.parents else p.relative_to(ROOT)
        found.update(scan_sh(p.read_text(errors="replace"), str(rel)))

if os.environ.get("ZONE_LINT_LIST"):
    print("\n".join(sorted(found)))
allowed = allowlist()
check("every allowlist entry says why", [k for k, why in allowed.items() if not why], [])
check("no production code reads the host's date or zone unannounced (fix it, or allowlist it with a reason)",
      sorted(found - set(allowed)), [])
check("every allowlist entry still matches a hit (delete the stale ones)", sorted(set(allowed) - found), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
