#!/usr/bin/env python3
# gate: hermetic   (a stub checkout in a temp dir: copies of run_deploy_gate.sh, gate_env.sh and deploy_fail_log.py,
#                   a three-line deploy script and three stub tests; never titan's gate, never /ai/logs)
"""The deploy's gate runs 4 tests at a time (#1473 PR 2, after 3 shadow deploys judged identically): any FAIL rolls
back naming the failing test; each failure keeps its last 60 lines, scrubbed, in a 0600 log with its path printed (as
the sequential gate's keep_fail does); a hang is cut at 30 min and rolls back; DEPLOY_GATE_JOBS=1 runs the sequential
block instead. The deploy log carries the verdict lines and log paths only, never a test's output.
Run: python3 scripts/test_deploy_gate_parallel.py
"""
from __future__ import annotations

import os
import shutil
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


T = Path(tempfile.mkdtemp(prefix="titan-gate-parallel-"))
R = T / "checkout"
(R / "scripts").mkdir(parents=True)
for f in ("run_deploy_gate.sh", "gate_plan.sh", "gate_env.sh", "deploy_fail_log.py", "redact.py"):   # redact: deploy_fail_log's scrubber
    shutil.copy(HERE / f, R / "scripts" / f)
(R / "scripts" / "gate_live_tests.txt").write_text("")
(R / "scripts" / "gate_serial_tests.txt").write_text("")
(R / "scripts" / "deploy_titan_node.sh").write_text(
    'export DAILY_LOG_DIR="$TEST_STATE/daily" STUB_PIN=1\n'
    'run_test scripts/test_ok.py || rollback "ok tests failed"\n'
    'run_test scripts/test_bad.py " 0 failed" || rollback "bad tests failed"\n')
(R / "scripts" / "test_ok.py").write_text('print("3 passed, 0 failed")\n')
(R / "scripts" / "test_bad.py").write_text(
    'import sys\nprint("  [ok] the first check")\nprint("  [FAIL] Robin\'s arrival: got=None")\n'
    'print("GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789")\nprint("1 passed, 1 failed")\nsys.exit(1)\n')
(R / "scripts" / "test_found.py").write_text('# gate: hermetic\nprint("1 passed, 0 failed")\n')
LOGS = T / "logs"
LOGS.mkdir()


def gate(jobs: str, keep: bool) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("GATE_KEEP_FAIL", "GATE_FAIL_LOG_DIR")}
    env.update({"GATE_JOBS": jobs, "GATE_FAIL_LOG_DIR": str(LOGS)})
    if keep:
        env["GATE_KEEP_FAIL"] = "1"
    r = subprocess.run(["bash", str(R / "scripts" / "run_deploy_gate.sh"), str(R)], env=env, capture_output=True,
                       text=True, timeout=120)
    return r.returncode, r.stdout


print("── run_deploy_gate as the deploy calls it (GATE_KEEP_FAIL=1) ──")
for jobs in ("4", "1"):
    for p in LOGS.iterdir():
        p.unlink()
    rc, out = gate(jobs, keep=True)
    lines = out.splitlines()
    check(f"GATE_JOBS={jobs}: exit 1, every test judged in plan order (listed sorted, then found), the failing one named",
          (rc, [x for x in lines if x.startswith(("ok", "FAIL"))]),
          (1, ["FAIL scripts/test_bad.py", "ok   scripts/test_ok.py", "ok   scripts/test_found.py (found)"]))
    logs = sorted(LOGS.iterdir())
    check(f"…one failure log, named by stamp and test, 0600", ([p.name.endswith("-test_bad.log") for p in logs],
          [oct(stat.S_IMODE(p.stat().st_mode)) for p in logs]), ([True], ["0o600"]))
    body = logs[0].read_text() if logs else ""
    check("…its last lines kept, the failing check among them, a secret-looking value scrubbed",
          ("[FAIL] Robin's arrival" in body, "ghp_abcdefghij" in body, "GITHUB_TOKEN=" in body), (True, False, True))
    check("…and its path printed, as the sequential gate prints it",
          any(x == f"!!! test_bad.py: its output is in {logs[0]}" for x in lines) if logs else False, True)
for p in LOGS.iterdir():
    p.unlink()
rc, out = gate("4", keep=False)
check("a PR gate (no GATE_KEEP_FAIL): the same verdicts, and no log written", (rc, list(LOGS.iterdir())), (1, []))

print("── the deploy's parallel branch: rollback names the failing test; a hang rolls back too ──")
dep = (HERE / "deploy_titan_node.sh").read_text()
branch = dep[dep.index("else\n  # Too few tests judged") + len("else\n"):dep.index("\nfi\ngate_s=")]


SHIM = T / "bin"   # no GNU timeout (the Mac): a stand-in that runs the command (titan's real one cuts at 1800 s)
if subprocess.run(["bash", "-c", "command -v timeout"], capture_output=True).returncode != 0:
    SHIM.mkdir()
    (SHIM / "timeout").write_text('#!/usr/bin/env bash\nshift\nexec "$@"\n')
    (SHIM / "timeout").chmod(0o755)


def deploy_branch(runner_body: str | None, min_tests: str | None = "1", last: int | None = None,
                  no_log: bool = False, extra_env: dict | None = None) -> tuple[int, str, str]:
    if runner_body is not None:   # a stand-in gate, for the cases a stub checkout can't make (a hang, a crash)
        (R / "scripts" / "run_deploy_gate.sh").write_text("#!/usr/bin/env bash\n" + runner_body + "\n")
    else:
        shutil.copy(HERE / "run_deploy_gate.sh", R / "scripts" / "run_deploy_gate.sh")
        shutil.copy(HERE / "ops_defaults.sh", R / "scripts" / "ops_defaults.sh")
        shutil.copy(HERE / "gate_plan.sh", R / "scripts" / "gate_plan.sh")
    history = T / "deploy-gate.jsonl"
    history.unlink(missing_ok=True)
    if not no_log:
        history.write_text("" if last is None else
                           '{"ts": 1, "commit": "a", "jobs": 4, "gate_s": 9, "tests": null}\n'
                           f'{{"ts": 2, "commit": "b", "jobs": 4, "gate_s": 323, "tests": {last}}}\n')
    floor = "" if min_tests is None else f"DEPLOY_GATE_MIN_TESTS={min_tests}; "
    script = ('set -euo pipefail\nrollback() { echo "ROLLBACK: $1"; exit 1; }\n'
              f'{floor}gate_t0=$SECONDS; gate_tests=null; gate_log={history}; DEPLOY_GATE_JOBS=4; '
              f'export GATE_FAIL_LOG_DIR={LOGS}\n' + branch + '\necho "GATE_TESTS=$gate_tests"\n')
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "PATH": f"{SHIM}:{os.environ['PATH']}",
           "GATE_PASSED_FILE": str(T / "gate-passed.tsv"), **(extra_env or {})}
    r = subprocess.run(["bash", "-c", script], cwd=R, env=env, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout, r.stderr


rc, out, err = deploy_branch(None)
check("a failing test: rolled back, the message naming it", (rc, "ROLLBACK: test_bad.py failed" in out), (1, True))
check("…the deploy log shows the verdict and the log path, never the test's output",
      ("FAIL scripts/test_bad.py" in err, "!!! test_bad.py: its output is in" in err, "ghp_" in out + err,
       "Robin" in out + err), (True, True, False, False))
rc, out, err = deploy_branch("echo 'ok   scripts/test_ok.py'; echo 'ok   scripts/test_bad.py'")
check("every test ok: no rollback, the count and seconds said", (rc, "ROLLBACK" in out, "GATE_TESTS=2" in out,
      "==> gate: 2 tests passed, 4 at a time" in out), (0, False, True, True))
rc, out, err = deploy_branch("echo 'ok   scripts/test_ok.py'; exit 3")
check("a gate that fails without naming a test (a crash): still rolled back, saying so",
      (rc, "ROLLBACK: the gate failed (exit 3) without naming a test" in out), (1, True))
rc, out, err = deploy_branch("exit 124")   # what timeout returns when it cuts the gate
check("a gate cut at its timeout: rolled back, saying so", (rc, "ROLLBACK: the gate timed out" in out), (1, True))

print("── too few tests judged is a failure too (Coordination's review of #1486) ──")
oks = lambda n: "; ".join(f"echo 'ok   scripts/test_{i}.py'" for i in range(n)) or "true"
rc, out, _ = deploy_branch("exit 0", min_tests=None)
check("an empty plan (nothing judged, exit 0): rolled back, 'the gate judged only 0 tests'",
      (rc, "ROLLBACK: the gate judged only 0 tests (at least 300 expected)" in out), (1, True))
rc, out, _ = deploy_branch(oks(5), min_tests=None)
check("a handful judged (an early stop): rolled back", (rc, "ROLLBACK: the gate judged only 5 tests" in out), (1, True))
rc, out, _ = deploy_branch(oks(350), min_tests=None, last=426)
check("below 90% of the last deploy's 426 (350 < 383): rolled back", (rc, "judged only 350 tests (at least 383" in out),
      (1, True))
rc, out, _ = deploy_branch(oks(400), min_tests=None, last=426)
check("…at or above it: deployed", (rc, "ROLLBACK" in out, "==> gate: 400 tests passed" in out), (0, False, True))
rc, out, _ = deploy_branch(oks(310), min_tests=None, last=None)
check("no history yet: the 300 floor alone", (rc, "ROLLBACK" in out), (0, False))
rc, out, _ = deploy_branch(oks(310), min_tests=None, no_log=True)
check("no timing log at all (the first parallel deploy, 10-10 15:2xZ): the 300 floor, and the script carries on",
      (rc, "ROLLBACK" in out, "GATE_TESTS=310" in out), (0, False, True))
rc, out, _ = deploy_branch(oks(320), min_tests=None, last=200)
check("…a smaller last count never lowers the floor below 300", (rc, "ROLLBACK" in out), (0, False))
rc, out, _ = deploy_branch(oks(290), min_tests=None, last=200)
check("…290 against a last of 200: still below 300, rolled back", (rc, "judged only 290 tests (at least 300" in out), (1, True))
check("the timing line goes to the same log the floor reads", ('>> "$gate_log" 2>/dev/null || true' in dep,
      'gate_log="${DEPLOY_GATE_LOG:-/ai/logs/deploy-gate.jsonl}"' in dep), (True, True))
shutil.copy(HERE / "run_deploy_gate.sh", R / "scripts" / "run_deploy_gate.sh")
shutil.copy(HERE / "ops_defaults.sh", R / "scripts" / "ops_defaults.sh")
shutil.copy(HERE / "gate_plan.sh", R / "scripts" / "gate_plan.sh")

print("── #1514: gate once per tree (a recorded pass of this same clean tree is reused) ──")
shutil.copy(HERE / "gate_passed.sh", R / "scripts" / "gate_passed.sh")
FAILS = "echo 'FAIL scripts/test_bad.py'; exit 1"   # a runner that would roll back if it were run
(R / "scripts" / "run_deploy_gate.sh").write_text("#!/usr/bin/env bash\n" + FAILS + "\n")   # as deploy_branch writes it
GENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT = ["git", "-c", "user.name=Alex", "-c", "user.email=alex@example.invalid"]
subprocess.run([*GIT, "init", "-q", str(R)], env=GENV, check=True)
subprocess.run([*GIT, "-C", str(R), "add", "-A"], env=GENV, check=True)
subprocess.run([*GIT, "-C", str(R), "commit", "-qm", "stub"], env=GENV, check=True)
tree = subprocess.run(["git", "-C", str(R), "rev-parse", "HEAD^{tree}"], env=GENV, capture_output=True, text=True).stdout.strip()
PASSED = T / "gate-passed.tsv"
gp = lambda *a, **kw: subprocess.run(["bash", str(HERE / "gate_passed.sh"), *a], capture_output=True, text=True,
                                     env={**GENV, "GATE_PASSED_FILE": str(PASSED), **kw})
check("gate_passed: nothing recorded, nothing found", gp("find", tree).returncode, 1)
check("…bad arguments refuse", (gp("add", "nothex", "430", "x").returncode, gp("add", tree, "0", "x").returncode), (2, 2))
gp("add", tree, "430", "pr-1514")
check("…a recorded pass is found with its count and who gated it", gp("find", tree).stdout.strip(), "430\tpr-1514")
check("…another tree isn't", gp("find", "0" * 40).returncode, 1)
check("…a stale pass isn't (max age 0 h)", gp("find", tree, GATE_PASSED_MAX_H="0").returncode, 1)
rc, out, _ = deploy_branch(FAILS, min_tests="300")
check("a deploy of a tree that passed reuses it: no run, no rollback, the count kept",
      (rc, "ROLLBACK" in out, "reused pr-1514's pass of this tree" in out, "GATE_TESTS=430" in out), (0, False, True, True))
rc, out, _ = deploy_branch(FAILS, min_tests="300", extra_env={"DEPLOY_GATE_REUSE": "0"})
check("…DEPLOY_GATE_REUSE=0 runs it anyway", (rc, "ROLLBACK: test_bad.py failed" in out), (1, True))
rc, out, _ = deploy_branch(FAILS, min_tests="500")
check("…a pass with fewer tests than the floor is run again", (rc, "ROLLBACK: test_bad.py failed" in out), (1, True))
rc, out, _ = deploy_branch(FAILS, min_tests="300", extra_env={"GATE_PASSED_MAX_H": "0"})
check("…a stale pass is run again", (rc, "ROLLBACK: test_bad.py failed" in out), (1, True))
(R / "scripts" / "test_ok.py").write_text('print("4 passed, 0 failed")\n')   # a tracked file changed: dirty
rc, out, _ = deploy_branch(FAILS, min_tests="300")
check("…a dirty checkout is run again", (rc, "ROLLBACK: test_bad.py failed" in out), (1, True))
subprocess.run(["git", "-C", str(R), "checkout", "-q", "--", "scripts/test_ok.py"], env=GENV)
shutil.rmtree(R / ".git")
check("gate_run.sh records a pass by the tree it shipped", ("TREE=$(git rev-parse 'HEAD^{tree}')" in (HERE / "gate_run.sh").read_text(),
      "gate_passed.sh add $TREE" in (HERE / "gate_run.sh").read_text()), (True, True))
check("the timing line says whether the gate was reused", '"reused": %s' in dep, True)

print("── #1514: the affected tests run first, the whole plan after; per-test seconds in the log ──")
R2 = T / "affected"
(R2 / "scripts").mkdir(parents=True)
for f in ("run_deploy_gate.sh", "gate_plan.sh", "gate_env.sh", "deploy_fail_log.py", "redact.py", "ops_defaults.sh"):
    shutil.copy(HERE / f, R2 / "scripts" / f)
(R2 / "scripts" / "gate_serial_tests.txt").write_text("")
(R2 / "scripts" / "gate_live_tests.txt").write_text("test_live_one.py   drives the real agent\n")
(R2 / "scripts" / "deploy_titan_node.sh").write_text(
    'export DAILY_LOG_DIR="$TEST_STATE/daily"\nrun_test scripts/test_listed.py || rollback "listed failed"\n')
RAN = T / "ran"
RAN.mkdir()


def stub_test(name, secs=0.0, fail=False, hermetic=True):
    body = ("# gate: hermetic\n" if hermetic else "") + "import sys, time\n" \
        f"time.sleep({secs})\nopen({str(RAN / name)!r}, 'w').close()\n" + \
        ('print("1 passed, 1 failed"); sys.exit(1)\n' if fail else 'print("1 passed, 0 failed")\n')
    (R2 / "scripts" / name).write_text(body)


stub_test("test_listed.py", hermetic=False)
for i, nm in enumerate("abcdefghijkl"):   # twelve hermetic tests, each a little slower than the one before
    stub_test(f"test_{nm}.py", secs=0.05 * (i + 1))
stub_test("test_live_one.py", hermetic=True)   # on the live list: never part of the plan
PLAN_ORDER = ["scripts/test_listed.py"] + [f"scripts/test_{n}.py" for n in "abcdefghijkl"]


def gate2(first, jobs="1", extra=()):
    for p in RAN.iterdir():
        p.unlink()
    ff = T / "first.txt"
    env = {k: v for k, v in os.environ.items() if k not in ("GATE_KEEP_FAIL", "GATE_FIRST")}
    env["GATE_JOBS"] = jobs
    if first is not None:
        ff.write_text("".join(x + "\n" for x in first))
        env["GATE_FIRST"] = str(ff)
    r = subprocess.run(["bash", str(R2 / "scripts" / "run_deploy_gate.sh"), str(R2)], env=env, capture_output=True,
                       text=True, timeout=120)
    verdicts = [x.split()[1] for x in r.stdout.splitlines() if x.startswith(("ok ", "FAIL "))]
    return r.returncode, r.stdout, verdicts, sorted(p.name for p in RAN.iterdir())


rc, out, base, ran = gate2(None)
check("no first list: the plan as it is, all thirteen, live test not run", (rc, base, "test_live_one.py" in ran),
      (0, PLAN_ORDER, False))
for jobs in ("1", "4"):
    rc, out, got, ran = gate2(["test_k.py", "test_c.py", "test_live_one.py", "test_nowhere.py", "test_k.py"], jobs)
    check(f"GATE_JOBS={jobs}: a first list orders without dropping or duplicating (affected first, plan order kept in both)",
          (rc, got), (0, ["scripts/test_c.py", "scripts/test_k.py"] + [x for x in PLAN_ORDER if not x.endswith(("_c.py", "_k.py"))]))
    check("…a live test or an unknown name in the list is never added", ("test_live_one.py" in ran, len(got), len(set(got))),
          (False, 13, 13))
    check("…and says how many ran first", "affected first: 2 of 13 tests" in out, True)
rc, out, got, ran = gate2(["test_listed.py"])
check("a first list holding the plan's first test: the order is the plan's", got, PLAN_ORDER)

stub_test("test_e.py", fail=True)
rc, out, got, ran = gate2(["test_c.py", "test_e.py", "test_g.py"], "1")
check("an affected test fails (one at a time): the gate stops there, the rest never runs",
      (rc, got, ran, "affected tests failed: stopped before the rest of the plan" in out),
      (1, ["scripts/test_c.py", "scripts/test_e.py"], ["test_c.py", "test_e.py"], True))
rc, out, got, ran = gate2(["test_c.py", "test_e.py"], "4")
check("…at 4 at a time the affected phase finishes and the rest never runs",
      (rc, got, "test_a.py" in ran), (1, ["scripts/test_c.py", "scripts/test_e.py"], False))
rc, out, got, ran = gate2(["test_c.py"], "4")
check("the failure is in the rest: the affected pass, then the whole plan runs and the gate fails with the same verdicts",
      (rc, got[0], len(got), [x for x in out.splitlines() if x.startswith("FAIL")]),
      (1, "scripts/test_c.py", 13, ["FAIL scripts/test_e.py (found)"]))
rc, out, base2, ran = gate2(None, "4")
check("…the same verdict lines as without a first list", sorted(got), sorted(base2))
stub_test("test_e.py", secs=0.05 * 5)

print("── #1514: one line per test with its seconds in the gate log, the 10 slowest at the end ──")
rc, out, got, ran = gate2(["test_b.py"], "1")
tl = [x.split() for x in out.splitlines() if x.startswith("time ")]
check("a time line for each of the 13 tests, each with seconds and the file", (len(tl), all(len(x) == 3 and x[2].startswith("scripts/") for x in tl)), (13, True))
check("…the seconds are numbers, and the 0.6 s test took longer than the 0.05 s one",
      (all(float(x[1]) >= 0 for x in tl), float(dict((x[2], x[1]) for x in tl)["scripts/test_l.py"]) >
       float(dict((x[2], x[1]) for x in tl)["scripts/test_a.py"])), (True, True))
tail = out.split("slowest 10 (seconds):\n")[1].splitlines() if "slowest 10 (seconds):" in out else []
secs = [float(x.split()[0]) for x in tail]
check("the summary lists 10, slowest first, ending the log", (len(tail), secs == sorted(secs, reverse=True), tail[0].split()[1] if tail else ""),
      (10, True, "scripts/test_l.py"))
check("…the verdict lines are unchanged by the time lines (count of ok lines)", len([x for x in out.splitlines() if x.startswith("ok ")]), 13)

print("── the deploy script ──")
check("4 at a time by default, passed to titan; DEPLOY_GATE_JOBS=1 runs the sequential block",
      ('DEPLOY_GATE_JOBS="${DEPLOY_GATE_JOBS:-$OPS_GATE_JOBS}" bash -s <<' in dep,
       dep.index('if [ "${DEPLOY_GATE_JOBS}" = 1 ]; then\n(\n') < dep.index("keep_fail() {")), (True, True))
check("the parallel branch keeps failure logs and is cut at 30 min",
      'GATE_JOBS="${DEPLOY_GATE_JOBS}" GATE_KEEP_FAIL=1 timeout 1800 bash scripts/run_deploy_gate.sh' in branch, True)
check("both branches end before the restart", dep.index("\nfi\ngate_s=") < dep.index("sudo systemctl restart titan-agent-api\nfor _ in"),
      True)
check("the shadow is gone; one timing line per deploy instead, never a reason to stop",
      ("deploy_gate_shadow" in dep, ">> \"$gate_log\" 2>/dev/null || true" in dep), (False, True))
check("the sequential block is still the plan run_deploy_gate reads (its run_test lines, unindented)",
      dep.count("\nrun_test scripts/test_") > 100, True)

print("── an unexpected stop between the checkout move and the canary rolls back ──")
lines = dep.splitlines()
t_on = next(i for i, x in enumerate(lines) if x.startswith("trap 'rollback") and "ERR" in x)
t_off = next(i for i, x in enumerate(lines) if x.startswith("trap - ERR"))
merge = next(i for i, x in enumerate(lines) if "git merge --ff-only" in x)
canary = next(i for i, x in enumerate(lines) if "agent_canary.py" in x and "rollback" in x)
check("the ERR trap is set after the checkout moves and before the gate, and cleared right after the canary",
      merge < t_on < dep.splitlines().index(next(x for x in lines if x.startswith("gate_t0=$SECONDS"))) and t_off == canary + 1, True)
r = subprocess.run(["bash", "-c", 'set -euo pipefail\nrollback() { echo "ROLLBACK: $1"; exit 1; }\n' + lines[t_on] +
                    '\nx=$(false | cat)\necho survived\n'], capture_output=True, text=True, timeout=30)
check("…and a failed assignment under pipefail rolls back instead of ending silently",
      (r.returncode, "ROLLBACK: the deploy stopped unexpectedly" in r.stdout, "survived" in r.stdout), (1, True, False))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
