#!/usr/bin/env python3
# gate: hermetic
"""The autonomous coder stays out of evals (#1517): an eval unit active/activating/reloading or an eval window skips the run. Fake systemctl, temp windows file. Run: python3 scripts/test_autonomous_coder_eval_guard.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))

import io  # noqa: E402
import calendar  # noqa: E402
import time  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import autonomous_coder as A  # noqa: E402

with tempfile.TemporaryDirectory() as d:
    conf = Path(d) / "w.conf"
    conf.write_text("# c\n0100 0200 alpha\n1000 1100 beta\nLEAD_MIN 12\n")
    os.environ["EVAL_WINDOWS_CONF"] = str(conf)
    A.LOG_PATH = str(Path(d) / "log")

    def at(hhmm):
        # a UTC wall time as epoch seconds: mktime - time.timezone ignored DST, an hour off on a Mac in PDT
        return calendar.timegm(time.strptime("2030-06-15 " + hhmm, "%Y-%m-%d %H:%M"))

    def sysd(states):
        return lambda cmd, **kw: SimpleNamespace(stdout=states.get(cmd[-1], "inactive") + "\n", returncode=0)

    idle = sysd({})
    check("windows are parsed from the conf", A._eval_windows(), (("01:00", "02:00"), ("10:00", "11:00")))
    check("idle and outside a window: runs", A.eval_busy(at("05:00"), idle), None)
    check("inside a window: skipped with the window", A.eval_busy(at("01:30"), idle), "inside the eval window 01:00-02:00Z")
    check("window end is exclusive", A.eval_busy(at("02:00"), idle), None)
    for st in ("active", "activating", "reloading"):
        for unit in ("titan-household-eval", "titan-model-eval"):
            check(f"{unit} {st}: skipped", A.eval_busy(at("05:00"), sysd({unit: st})), f"{unit} is {st}")
    check("failed units don't block", A.eval_busy(at("05:00"), sysd({"titan-model-eval": "failed"})), None)

    def boom(cmd, **kw):
        raise OSError("no systemctl")
    check("no systemctl (a Mac): falls through to the windows", A.eval_busy(at("05:00"), boom), None)
    conf.write_text("")
    check("an empty window list refuses", A.eval_busy(at("05:00"), idle) is not None, True)

    # main(): skipped means exit 0 and one line, before any token or model check.
    conf.write_text("0000 2400 all\n")
    called = []
    A.load_github_token = lambda: called.append("token") or "t"
    saved = sys.argv
    sys.argv = ["autonomous_coder.py"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = A.main()
    sys.argv = saved
    lines = [x for x in buf.getvalue().splitlines() if x.strip()]
    check("main inside a window: exit 0, one line, no token read", (rc, len(lines), called), (0, 1, []))
    check("…and the line says why", "skipped: " in lines[0] and "eval window" in lines[0], True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
