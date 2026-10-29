#!/usr/bin/env python3
# gate: hermetic
"""Clock time bombs, production side (#1515): a function that is handed the clock must not also read the real one.

`def f(..., now=None)` that calls `time.time()`, `datetime.now()`, `date.today()` or `_local_now()` in its body uses two
clocks: a test (or a replay) that passes `now` still gets wall-clock behaviour for the rest (claude_chats._when,
next_stop.on_left and github_stars' "synced ago" did; each was wrong only at a particular date or hour, so no test
saw it). Flagged: a function with a now/today/at/clock/w parameter that reads a real clock. Exempt: the read inside
an SQL INSERT/UPDATE/REPLACE statement (a row's own stamp), a `.tzinfo`-only read, and a fallback for that parameter
(`x if now is None else y`, `now or datetime.now()`, `if now is None: ...`). Anything else justified goes in
scripts/clock_lint_allowlist.txt as `file.py:function  # why`; an entry that no longer matches fails too.
Reads files only. Run: python3 scripts/test_clock_lint_prod.py"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
ALLOWLIST = HERE / "clock_lint_allowlist.txt"
CLOCK_PARAMS = {"now", "today", "at", "clock", "w"}
REAL = {"time.time", "time.time_ns", "datetime.now", "datetime.datetime.now", "dt.datetime.now", "_dt.now", "dt.now",
        "datetime.utcnow", "date.today", "datetime.date.today", "_local_now", "lib._local_now", "agent_runtime_lib._local_now"}
SQL_WRITE = re.compile(r"^\s*(INSERT|REPLACE|UPDATE)\b", re.I)

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


def is_real(n: ast.AST) -> bool:
    return isinstance(n, ast.Call) and dotted(n.func) in REAL


def mentions(n: ast.AST, params: set[str]) -> bool:
    return any(isinstance(x, ast.Name) and x.id in params for x in ast.walk(n))


def is_none_test(t: ast.AST, params: set[str]) -> bool:
    """`now is None`, `now is not None`, `not now`, `now`: a test of the parameter itself."""
    if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not):
        return is_none_test(t.operand, params)
    if isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id in params:
        return all(isinstance(o, (ast.Is, ast.IsNot)) for o in t.ops)
    return isinstance(t, ast.Name) and t.id in params


def exempt_nodes(fn: ast.AST, params: set[str]) -> set[int]:
    """ids of the real-clock calls that are allowed: SQL stamps, .tzinfo-only reads, fallbacks for the parameter."""
    ok: set[int] = set()

    def mark(n):
        for x in ast.walk(n):
            if is_real(x):
                ok.add(id(x))

    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and n.attr == "tzinfo":
            mark(n.value)
        elif isinstance(n, ast.IfExp) and is_none_test(n.test, params):
            mark(n)
        elif isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or) and mentions(n.values[0], params):
            mark(n)
        elif isinstance(n, ast.If) and is_none_test(n.test, params):
            for b in n.body + n.orelse:
                mark(b)
        elif isinstance(n, ast.stmt) and not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.If, ast.For,
                                                           ast.While, ast.With, ast.Try)):
            if any(isinstance(c, ast.Constant) and isinstance(c.value, str) and SQL_WRITE.match(c.value)
                   for c in ast.walk(n)):
                mark(n)
    return ok


def scan(text: str, name: str = "x.py") -> list[str]:
    """`name:function` for each function that takes a clock and reads a real one."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = fn.args
        params = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs} & CLOCK_PARAMS
        if not params:
            continue
        ok = exempt_nodes(fn, params)
        if any(is_real(n) and id(n) not in ok for n in ast.walk(fn)):
            out.append(f"{name}:{fn.name}")
    return sorted(set(out))


def allowlist() -> dict[str, str]:
    out = {}
    if ALLOWLIST.exists():
        for line in ALLOWLIST.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, why = line.partition("#")
                out[key.strip()] = why.strip()
    return out


# The lint itself.
BAD = "def f(x, now=None):\n    return time.time() - x\n"
check("a function with now= that reads time.time() is flagged", scan(BAD), ["x.py:f"])
check("…datetime.now() and date.today() and _local_now() too",
      scan("def g(today):\n    a = date.today()\n\ndef h(at):\n    return lib._local_now()\n\ndef i(w):\n    return datetime.now(tz)\n"),
      ["x.py:g", "x.py:h", "x.py:i"])
check("…a function without a clock parameter is not (that is its job)", scan("def f(x):\n    return time.time()\n"), [])
check("…using the parameter is not", scan("def f(x, now):\n    return now - x\n"), [])
check("a row's own INSERT stamp is exempt",
      scan("def f(con, now=None):\n    con.execute('INSERT INTO t VALUES (?,?)', (1, time.time()))\n"), [])
check("…and an UPDATE stamp", scan("def f(con, now):\n    con.execute('UPDATE t SET at=?', (time.time(),))\n"), [])
check("…but the same read outside SQL is flagged", scan("def f(con, now):\n    x = time.time()\n    con.execute('INSERT INTO t VALUES (?)', (now,))\n"), ["x.py:f"])
check("a .tzinfo-only read is exempt", scan("def f(now):\n    return now.astimezone(lib._local_now().tzinfo)\n"), [])
check("x if now is None else y is exempt", scan("def f(now=None):\n    return datetime.now(tz) if now is None else now\n"), [])
check("now or datetime.now() is exempt", scan("def f(now=None):\n    now = now or datetime.now(timezone.utc)\n    return now\n"), [])
check("if now is None: now = … is exempt", scan("def f(now=None):\n    if now is None:\n        now = time.time()\n    return now\n"), [])
check("a read after the fallback is still flagged",
      scan("def f(now=None):\n    now = now or time.time()\n    return time.time() - now\n"), ["x.py:f"])
check("a method is checked too", scan("class C:\n    def m(self, now):\n        return time.time()\n"), ["x.py:m"])

found: set[str] = set()
for p in sorted(HERE.glob("*.py")):
    if p.name.startswith("test_"):
        continue
    found.update(scan(p.read_text(errors="replace"), p.name))

allowed = allowlist()
check("every allowlist entry says why", [k for k, why in allowed.items() if not why], [])
check("no production function takes a clock and reads the real one (else fix it, or allowlist it with a reason)",
      sorted(found - set(allowed)), [])
check("every allowlist entry still matches a flagged function (delete the stale ones)", sorted(set(allowed) - found), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
