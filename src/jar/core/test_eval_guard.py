#!/usr/bin/env python3
# gate: hermetic
"""No deploy while a nightly eval runs on titan (eval_guard.sh, 10-06: two restarts landed inside the 03:30 nightly).
The guard asks systemd over ssh for titan-household-eval and titan-model-eval's ActiveState: active, activating or
reloading refuses (exit 3, the reason on stderr); --force-during-eval lets it through with a warning; titan out of
reach refuses (exit 1) unless forced. Both deploy scripts source it before touching titan. A fake ssh on PATH: no
host is contacted.
Run: python3 scripts/test_eval_guard.py
"""
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


tmp = Path(tempfile.mkdtemp(prefix="titan-evalguard-"))
fake = tmp / "ssh"
fake.write_text('#!/bin/sh\necho "$@" >> "$FAKE_LOG"\n[ -n "$FAKE_SSH_FAIL" ] && exit 255\nprintf "%b" "$FAKE_STATES"\n')
fake.chmod(fake.stat().st_mode | stat.S_IEXEC)


def guard(states, force="", fail=""):
    env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", "FAKE_STATES": states, "FAKE_SSH_FAIL": fail,
           "FAKE_LOG": str(tmp / "log")}
    r = subprocess.run(["bash", "-c", f'. "{HERE}/eval_guard.sh"; eval_guard titan "$1"', "_", force],
                       capture_output=True, text=True, env=env, timeout=20)
    return r.returncode, r.stderr


print("── the guard ──")
check("both evals inactive: go", guard("inactive\n\ninactive\n")[0], 0)
rc, err = guard("active\n\ninactive\n")
check("the household eval running: refused, naming it", (rc, "titan-household-eval" in err, "titan-model-eval" in err),
      (3, True, False))
rc, err = guard("inactive\n\nactivating\n")
check("the model eval starting: refused, naming it", (rc, "titan-model-eval" in err), (3, True))
check("reloading counts as running", guard("reloading\n\ninactive\n")[0], 3)
check("failed or deactivating doesn't block", guard("failed\n\ndeactivating\n")[0], 0)
rc, err = guard("active\n\nactive\n", force="1")
check("--force-during-eval: through, with a warning", (rc, "--force-during-eval" in err), (0, True))
rc, err = guard("", fail="1")
check("titan out of reach: refused", (rc, "couldn't ask" in err), (1, True))
check("…unless forced", guard("", force="1", fail="1")[0], 0)
log = (tmp / "log").read_text()
check("it asks systemd for exactly the two eval units, read-only",
      all(x in log for x in ("systemctl show -p ActiveState --value", "titan-household-eval.service",
                              "titan-model-eval.service")) and "stop" not in log and "restart" not in log, True)

print("\n── both deploy scripts use it before touching titan ──")
for f, first in (("deploy_titan_node.sh", 'ssh "$SSH_ALIAS" DRY='), ("deploy_rust_runtime.sh", "rsync -az")):
    src = (HERE / f).read_text()
    g = src.find('eval_guard "$SSH_ALIAS" "$FORCE_EVAL" || exit $?')
    check(f"{f}: sources it, takes --force-during-eval, guards before its first change on titan",
          ("eval_guard.sh" in src, "--force-during-eval) FORCE_EVAL=1" in src, -1 < g < src.find(first)), (True, True, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
