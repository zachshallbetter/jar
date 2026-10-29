#!/usr/bin/env python3
# gate: hermetic   (a temp package of modules and tests; never the repo's own tests, never titan)
"""#1514: the import graph that picks the tests a change can affect (scripts/affected_tests.py): a test is affected by a
changed module it imports directly or through a chain, by an import inside a function, by a relative import in a
package, by a changed test, and by a changed non-Python file it names (a .sh read as text, a .conf, a .json).
Run: python3 scripts/test_affected_tests.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import affected_tests  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


T = Path(tempfile.mkdtemp(prefix="titan-affected-"))
S = T / "scripts"
(S / "ledgerkit").mkdir(parents=True)
files = {
    "scripts/base_lib.py": "VALUE = 1\n",
    "scripts/mid_lib.py": "import base_lib\n",                                  # mid -> base
    "scripts/top_lib.py": "from mid_lib import VALUE\n",                         # top -> mid -> base
    "scripts/lazy_lib.py": "def f():\n    import other_lib\n    return other_lib\n",   # an import inside a function
    "scripts/other_lib.py": "X = 2\n",
    "scripts/alone_lib.py": "Y = 3\n",
    "scripts/ledgerkit/__init__.py": "from . import core\n",
    "scripts/ledgerkit/core.py": "Z = 4\n",
    "scripts/ledgerkit/extra.py": "from .core import Z\n",
    "scripts/test_direct.py": "import base_lib\n",
    "scripts/test_transitive.py": "import top_lib\n",
    "scripts/test_function_level.py": "import lazy_lib\n",
    "scripts/test_package.py": "from ledgerkit import extra\n",
    "scripts/test_unrelated.py": "import alone_lib\nprint('Robin')\n",
    "scripts/test_reads_sh.py": "from pathlib import Path\nT = Path('scripts/sweep_nightly.sh').read_text()\n",
    "scripts/test_reads_conf.py": "import json\nopen('config/eval_windows.conf').read()\n",
    "scripts/test_runs_script.py": "import subprocess\nsubprocess.run(['python3', 'scripts/alone_lib.py'])\n",
    "scripts/sweep_nightly.sh": "#!/bin/bash\n",
    "config/eval_windows.conf": "lead 12\n",
    "config/other.json": "{}\n",
}
for rel, body in files.items():
    (T / rel).parent.mkdir(parents=True, exist_ok=True)
    (T / rel).write_text(body)


def pick(*changed):
    return affected_tests.affected(T, list(changed))


print("── the import graph ──")
check("a module imported directly: its test", pick("scripts/base_lib.py")[0:1], ["test_direct.py"])
check("…and through a chain (top -> mid -> base): the transitive test too",
      pick("scripts/base_lib.py"), ["test_direct.py", "test_transitive.py"])
check("a module in the middle of the chain: only the test above it", pick("scripts/mid_lib.py"), ["test_transitive.py"])
check("an import inside a function counts", pick("scripts/other_lib.py"), ["test_function_level.py"])
check("a package module imported with a relative import: the test of the package's other module",
      pick("scripts/ledgerkit/core.py"), ["test_package.py"])
check("a changed test is itself affected", pick("scripts/test_unrelated.py"), ["test_unrelated.py"])
check("a module nothing tests by import: its subprocess caller by name",
      pick("scripts/alone_lib.py"), ["test_runs_script.py", "test_unrelated.py"])
check("a change nothing refers to selects nothing", pick("docs/notes.md"), [])

print("── non-Python files a test reads by name ──")
check("a .sh read as text: the test naming it", pick("scripts/sweep_nightly.sh"), ["test_reads_sh.py"])
check("a .conf by its repo path: the test naming it", pick("config/eval_windows.conf"), ["test_reads_conf.py"])
check("a .json nobody names: nothing", pick("config/other.json"), [])
check("a name inside a longer one is not a match (eval_windows.conf vs old_eval_windows.conf2)",
      affected_tests.names_file("read('old_eval_windows.conf2')", "config/eval_windows.conf"), False)
check("a deleted file is still named by the tests that read it", pick("scripts/gone.sh"), [])
(T / "scripts" / "test_reads_gone.py").write_text("open('scripts/gone.sh')\n")
check("…(a test that names a path no longer on disk)", pick("scripts/gone.sh"), ["test_reads_gone.py"])
check("several changes: the union, sorted, each test once",
      pick("scripts/base_lib.py", "scripts/mid_lib.py", "scripts/sweep_nightly.sh"),
      ["test_direct.py", "test_reads_sh.py", "test_transitive.py"])

print("── the command line ──")
r = subprocess.run([sys.executable, str(HERE / "affected_tests.py"), "--root", str(T), "scripts/mid_lib.py",
                    "scripts/sweep_nightly.sh"], capture_output=True, text=True, timeout=60)
check("it prints one test name a line", (r.returncode, r.stdout.split()), (0, ["test_reads_sh.py", "test_transitive.py"]))
r = subprocess.run([sys.executable, str(HERE / "affected_tests.py"), "--root", str(T)], capture_output=True, text=True, timeout=60)
check("no changed paths: no output", (r.returncode, r.stdout), (0, ""))
real = affected_tests.affected(HERE.parent, ["scripts/gate_plan.sh"])
check("on the real tree: a change to gate_plan.sh selects the tests that name it", "test_clock_canary.py" in real, True)
check("…and a test is only ever a test_*.py file in scripts/", all(n.startswith("test_") and n.endswith(".py") for n in real), True)

shutil.rmtree(T, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
