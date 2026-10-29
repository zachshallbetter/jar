#!/usr/bin/env python3
# gate: hermetic
"""Ports and gate-job counts have one home each (#1518). The agent API's port and the code model's were written as
literals in about 25 files, and run_deploy_gate.sh defaulted to 1 test at a time while gate_run.sh and the deploy used 4.
Now scripts/ops_ports.py / ops_ports.sh define the ports and scripts/ops_defaults.sh the gate-job count; the rest import or
source them. An endpoint literal (an int, "7770", or a host:port URL) or a GATE_JOBS default written again in a script
fails here; comments, docstrings, prose and tests (which bind their own ports and may pin literals) are not scanned.
Reads files and runs two tiny sourcing shells only. Run: python3 scripts/test_ops_constants.py"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import ops_ports  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


PORTS = {7770: "AGENT_API_PORT", 8198: "CODE_MODEL_PORT"}
DEFINING = {"scripts/ops_ports.py", "scripts/ops_ports.sh", "scripts/ops_defaults.sh", "scripts/test_ops_constants.py"}
SKIP_DIRS = {"node_modules", "venv", "__pycache__", "worktrees", "site-packages"}
ENDPOINT = re.compile(r"(127\.0\.0\.1|localhost|\[::1\]|\d{1,3}(\.\d{1,3}){3}|[\w-]+(\.[\w-]+)*\.ts\.net):(%s)\b"
                      % "|".join(map(str, PORTS)))


def files(suffix: str):
    for p in sorted(ROOT.rglob(f"*{suffix}")):
        rel = p.relative_to(ROOT)
        if any(part.startswith(".") or part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.as_posix() in DEFINING or p.name.startswith("test_"):
            continue   # tests bind their own ports and pin literals on purpose
        yield rel.as_posix(), p


def py_hits(tree: ast.AST) -> list[tuple[int, str]]:
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Constant) or id(n) in docs:
            continue
        v = n.value
        if isinstance(v, int) and not isinstance(v, bool) and v in PORTS:
            out.append((n.lineno, str(v)))
        elif isinstance(v, str) and (v.strip() in map(str, PORTS) or ENDPOINT.search(v)):
            out.append((n.lineno, v[:60]))
    return out


GATE_DEFAULT_PY = re.compile(r"""GATE_JOBS["']?\s*[,:]\s*["']?\d""")
GATE_DEFAULT_SH = re.compile(r"GATE_JOBS(:-|=)\s*[\"']?\d")


def code_lines(text: str):
    for n, ln in enumerate(text.splitlines(), 1):
        if not ln.lstrip().startswith("#"):
            yield n, ln


port_hits, job_hits = [], []
for rel, p in files(".py"):
    text = p.read_text(errors="replace")
    try:
        port_hits += [f"{rel}:{n} {v}" for n, v in py_hits(ast.parse(text))]
    except SyntaxError:
        port_hits.append(f"{rel}: does not parse")
    job_hits += [f"{rel}:{n}" for n, ln in code_lines(text) if GATE_DEFAULT_PY.search(ln)]
for rel, p in files(".sh"):
    for n, ln in code_lines(p.read_text(errors="replace")):
        code = re.sub(r"\s#\s.*$", "", ln)   # a trailing comment is prose
        if re.search(r"\b(%s)\b" % "|".join(map(str, PORTS)), code):
            port_hits.append(f"{rel}:{n} {code.strip()[:60]}")
        if GATE_DEFAULT_SH.search(code):
            job_hits.append(f"{rel}:{n}")

print("── ports ──")
check("no script writes the agent API's or the code model's port again (import ops_ports / source ops_ports.sh)",
      port_hits, [])
check("the Python twin holds the values the code always used", (ops_ports.AGENT_API_PORT, ops_ports.CODE_MODEL_PORT), (7770, 8198))
check("…and builds loopback URLs from them", (ops_ports.AGENT_API_URL, ops_ports.CODE_MODEL_URL, ops_ports.local_url(8010)),
      ("http://127.0.0.1:7770", "http://127.0.0.1:8198", "http://127.0.0.1:8010"))


def sourced(script: str, *names: str) -> list[str]:
    r = subprocess.run(["bash", "-c", f'. {HERE / script}; printf "%s\\n" ' + " ".join(f'"${n}"' for n in names)],
                       capture_output=True, text=True, timeout=20)
    return r.stdout.split()


check("the shell twin agrees with the Python one", sourced("ops_ports.sh", "OPS_AGENT_API_PORT", "OPS_CODE_MODEL_PORT"),
      [str(ops_ports.AGENT_API_PORT), str(ops_ports.CODE_MODEL_PORT)])

print("── gate jobs ──")
check("no script defaults GATE_JOBS to a number of its own (read OPS_GATE_JOBS from ops_defaults.sh)", job_hits, [])
check("the one default is 4 (10-10: 507 s one at a time, 194 s at 4, same verdicts)", sourced("ops_defaults.sh", "OPS_GATE_JOBS"), ["4"])
reads = {n: "ops_defaults.sh" in (HERE / n).read_text() for n in
         ("run_deploy_gate.sh", "gate_run.sh", "deploy_titan_node.sh", "main_health.sh")}
check("the gate, the queue runner, the deploy and main-health all read it", reads, {n: True for n in reads})
check("run_deploy_gate.sh's default is that value", 'JOBS="${GATE_JOBS:-$OPS_GATE_JOBS}"' in (HERE / "run_deploy_gate.sh").read_text(), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
