#!/usr/bin/env python3
# gate: hermetic   (no model, no sandbox run: what a turn is offered, and the sandbox's command)
"""run_python (#1100, #1627): the code_sandbox capability, on for each person until they turn it off, and off for the
house with ALLOW_CODE_EXECUTION=0 (or the switches file). Off, it isn't offered and a call that names it anyway is
refused before anything runs; one person's choice never touches another's. The sandbox's command keeps every
namespace unshared, no writable directory (no tmpfs /tmp), the code read-only, and prlimit's memory, CPU, file-size
and task caps. The sandbox itself was checked on titan (2026-10-15); here subprocess is a spy, so nothing runs.
Fictional people (Robin and Jordan adults, Ava a kid). Zach 10-14: adults only, as a role rule. Run: python3 scripts/test_code_execution_offer.py"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-codeexec-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "SPACES_DB": "spaces.db",
             "CAPABILITY_SWITCHES": "switches.json"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("ALLOW_CODE_EXECUTION", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import governance as G  # noqa: E402

failed = 0


def check(label, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r} want={want!r}"))


def as_person(pid, role="adult"):
    lib._TURN_IO.person = {"id": pid, "name": pid}
    lib._TURN_IO.role = role
    lib._TURN_IO.capability_tiers = None


declared = lambda: {d["name"] for d in lib._gemini_function_declarations()}   # noqa: E731
offered = lambda: (lib._role_allows("run_python"), "run_python" in lib._turn_tool_specs(), "run_python" in declared())  # noqa: E731

as_person("p_robin")
check("on by default (nobody turned it off): offered", offered(), (True, True, True))
G.set_tier("p_robin", "code_sandbox", "never")
as_person("p_robin")
check("Robin turned it off: not offered to Robin", offered(), (False, False, False))
out = lib.execute_tool("run_python", {"code": "print(1)"})
check("…and a call that names it anyway is refused, nothing runs", out.startswith("Not run:") and "1" not in out.split(), True)
as_person("p_jordan")
check("Jordan's is still on", offered(), (True, True, True))
as_person("p_ava", role="kid")
check("Zach 10-14, adults only: a kid is never offered it", offered(), (False, False, False))
check("…and a kid's call is refused before anything runs", lib.execute_tool("run_python", {"code": "print(7)"}),
      lib._ROLE_REFUSAL)
check("…a role rule, not a setting: a kid has no switch for it", "code_sandbox" in G.for_role("kid"), False)
import household_identity as hh  # noqa: E402
check("…the role table: adults yes, kids and shared screens no",
      [hh.role_allows_tool(r, "run_python") for r in ("admin", "adult", "kid", "household")], [True, True, False, False])
G.set_tier("p_jordan", "compute", "never")
as_person("p_jordan")
check("Jordan's Never for compute covers it", lib._role_allows("run_python"), False)
check("…another domain's tools are untouched", lib._role_allows("get_datetime"), True)
G.set_tier("p_jordan", "compute", "always")
os.environ["ALLOW_CODE_EXECUTION"] = "0"
as_person("p_jordan")
check("ALLOW_CODE_EXECUTION=0: off for the house", offered(), (False, False, False))
os.environ.pop("ALLOW_CODE_EXECUTION")

print("── the sandbox's command ──")
cmd = lib._sandbox_cmd("/tmp/abc.py", 100)
check("prlimit caps memory, CPU, file size and tasks",
      cmd[:6], ["prlimit", f"--as={1 << 30}", "--cpu=10", f"--fsize={1 << 20}", "--nproc=164", "--"])
check("every namespace unshared, a read-only root, dies with its parent",
      all(f in cmd for f in ("--unshare-all", "--remount-ro", "--die-with-parent")), True)
check("no writable directory (no tmpfs, no plain bind)", ("--tmpfs" in cmd, "--bind" in cmd), (False, False))
check("only system directories, read-only", [cmd[i + 1] for i, f in enumerate(cmd) if f == "--ro-bind"],
      ["/usr", "/bin", "/lib", "/lib64", "/tmp/abc.py"])
check("nothing of /ai or /home is bound", any(a.startswith(("/ai", "/home")) for a in cmd), False)
seen = {}
real = subprocess.run
subprocess.run = lambda c, **kw: seen.update(cmd=c, kw=kw) or subprocess.CompletedProcess(c, 0, "2\n", "")
try:
    as_person("p_jordan")
    out = lib.execute_tool("run_python", {"code": "print(1+1)"})
finally:
    subprocess.run = real
check("a call runs that command, the code file read-only, and returns its output",
      (seen["cmd"][0], "/code.py" in seen["cmd"], out), ("prlimit", True, "2"))
check("…with no preexec hook (titan's server is threaded)", "preexec_fn" in seen["kw"], False)
check("this user's task count is a number", isinstance(lib._my_tasks(), int), True)
print(f"{'PASS' if not failed else 'FAIL'}: {failed} failed")
sys.exit(1 if failed else 0)
