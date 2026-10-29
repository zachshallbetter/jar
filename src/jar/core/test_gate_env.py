#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""The gate tests' environment is an allowlist (titan-node#996): a live switch the deploy's shell got from agent.env
never reaches a test; the export block's throwaway stores and pins do; the deploy and run_deploy_gate.sh run every test
through the same gate_python. A fake agent.env value stands in for the real one (never read here).
Run: python3 scripts/test_gate_env.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


tmp = tempfile.mkdtemp(prefix="titan-gateenv-")
script = f"""
set -euo pipefail
cd {ROOT}
export FICTIONAL_LIVE_SWITCH=1 NEXT_STOP_CARDS=dry GEMINI_API_KEY=fictional-not-a-key   # as if from agent.env
TEST_STATE={tmp}
eval "$(sed -n '/^export DAILY_LOG_DIR=/,/[^\\\\]$/p' scripts/deploy_titan_node.sh)"
. scripts/gate_env.sh
gate_env_build
gate_python -c 'import json, os; print(json.dumps(dict(os.environ)))'
"""
r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
env = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 and r.stdout.strip() else {}
check("the test process starts", (r.returncode, bool(env)), (0, True))
check("a live switch from the deploy's shell never reaches a test", "FICTIONAL_LIVE_SWITCH" in env, False)
check("…nor a credential", "GEMINI_API_KEY" in env, False)
check("a switch the export block pins arrives pinned, not live", env.get("NEXT_STOP_CARDS"), "0")
check("the throwaway stores arrive", (env.get("DAILY_LOG_DIR", "").startswith(tmp), env.get("PROACTIVE_DB", "").startswith(tmp)),
      (True, True))
check("what a process needs arrives", all(k in env for k in ("PATH", "HOME")), True)
names = subprocess.run(["bash", "-c", f"cd {ROOT} && . scripts/gate_env.sh && gate_env_names"], capture_output=True,
                       text=True).stdout.split()
check("the allowlist is the base plus the export block's names (no stray secret-looking name)",
      ("PATH" in names, "NEXT_STOP_CARDS" in names, [n for n in names if any(w in n for w in ("TOKEN", "SECRET", "PASSWORD"))
                                                    and not n.endswith(("_FILE", "_DB", "_DIR"))]), (True, True, []))
dep = (HERE / "deploy_titan_node.sh").read_text()
gate = (HERE / "run_deploy_gate.sh").read_text()
check("the deploy runs every test through gate_python", ('out=$(python3 "$f"' in dep, dep.count("gate_python \"$f\"")),
      (False, 2))
check("…after building the allowlist inside the tests' subshell",
      dep.index("\n(\n") < dep.index("export DAILY_LOG_DIR=") < dep.index("gate_env_build") < dep.index("run_test() {"), True)
check("run_deploy_gate does the same (one judge for both passes)", ('out=$(python3 "$f"' in gate,
      gate.count("gate_python \"$f\""), "gate_env_build" in gate), (False, 1, True))
check("…and a parallel worker (GATE_JOBS) rebuilds the allowlist inside its own subshell, with its own stores",
      'TEST_STATE="$(mktemp -d)"; trap' in gate and 'eval "$EXPORTS"; gate_env_build; judge "$@"' in gate, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
