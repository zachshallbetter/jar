#!/usr/bin/env python3
# gate: hermetic
"""titan-clock-canary (#1515, #1570): scripts/clockshift/canary.sh against a throwaway repo, a stub run_one.sh, a stub gate queue
and a stub alert. The plan is the gate's (listed tests, then other `# gate: hermetic` tests, never the live list or an
unmarked one), each runs once at the shift, the log holds counts and test names only, the alert fires when the failing
set changes (not when it repeats), a test that prints no verdict counts as failing, an eval window skips the run, and
`install` copies the units once. #1570: the plans (+8 days, the repeated and the skipped hour, the household evening), one
variant a night by rotation, `all`, a shared time budget, and the shim's absolute TIME_SHIFT_TO. Nothing live: every path is a temp dir.
Run: python3 scripts/test_clock_canary.py"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE / "clockshift" / "canary.sh"
T = Path(tempfile.mkdtemp(prefix="clock-canary-"))
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.500} expected={expected!r:.500}"))


repo = T / "repo"
(repo / "scripts").mkdir(parents=True)
(repo / "scripts" / "deploy_titan_node.sh").write_text(
    'run_test scripts/test_listed.py "0 failed" || rollback\nrun_test scripts/test_listed_bomb.py || rollback\n')
(repo / "scripts" / "gate_live_tests.txt").write_text("# live\ntest_live.py  drives the real agent\n")
for name, mark in (("test_listed", ""), ("test_listed_bomb", ""), ("test_found", "# gate: hermetic"),
                   ("test_found_bomb", "# gate: hermetic"), ("test_nomark", ""), ("test_live", "# gate: hermetic"),
                   ("test_noverdict", "# gate: hermetic")):
    (repo / "scripts" / f"{name}.py").write_text(f"#!/usr/bin/env python3\n{mark}\n")

calls, alerts, log = T / "calls", T / "alerts", T / "clock-canary.jsonl"
run_one = T / "run_one.sh"
run_one.write_text('#!/usr/bin/env bash\necho "$1 $2" >> "%s"\n' % calls +
                   'case "$1" in *noverdict*) exit 0;; *bomb*) echo "FAIL $1 @$2 rc=1";; *) echo "PASS $1 @$2";; esac\n')
fake_alert = T / "alert.sh"
fake_alert.write_text('#!/usr/bin/env bash\necho "key=[$1]" >> "%s"\necho "$3" >> "%s"\n' % (alerts, T / "alert-labels"))
fake_gq = T / "gq.sh"
fake_gq.write_text('#!/usr/bin/env bash\ncase "$1" in window) [ -f "%s/in-window" ] && { echo "inside the eval window 0325-0350Z nightly"; exit 0; }; exit 1;; esac\n' % T)
for f in (run_one, fake_alert, fake_gq):
    f.chmod(0o755)
RUN_ENV = {**ENV, "CLOCK_CANARY_REPO": str(repo), "CLOCK_CANARY_RUN_ONE": str(run_one), "CLOCK_CANARY_LOG": str(log),
           "CLOCK_CANARY_ALERT": str(fake_alert), "CLOCK_CANARY_QUEUE": str(fake_gq), "CLOCK_CANARY_JOBS": "2", "CLOCK_CANARY_PLANS": "plus8"}


def sh(*args, env=None):
    return subprocess.run(list(args), env=env or RUN_ENV, capture_output=True, text=True, timeout=120)


def lines(p):
    return p.read_text().splitlines() if p.exists() else []


print("── the plan and the first run ──")
r = sh("bash", str(SCRIPT), "run")
ran = sorted(c.split()[0] for c in lines(calls))
check("it runs the listed tests and the marked ones, once each",
      ran, ["test_found.py", "test_found_bomb.py", "test_listed.py", "test_listed_bomb.py", "test_noverdict.py"])
check("…never the live list or an unmarked test", [n for n in ran if n in ("test_live.py", "test_nomark.py")], [])
check("…every one at the +8 shift", sorted({c.split()[1] for c in lines(calls)}), ["8"])
bad = ["test_found_bomb.py", "test_listed_bomb.py", "test_noverdict.py"]
check("the summary names the failing tests", r.stdout.strip(),
      "clock canary: 3 of 5 fail at +8 days: " + " ".join(bad))
rec = json.loads(lines(log)[-1])
check("the log holds counts and names only", (rec["shift"], rec["total"], rec["ok"], rec["failed"], rec["fail"]),
      (8, 5, 2, 3, bad))
check("…and nothing else", sorted(rec), ["fail", "failed", "label", "ok", "secs", "shift", "to", "total", "ts"])
check("…the plan is named, with its shift and no absolute time", (rec["label"], rec["to"], isinstance(rec["secs"], int)), ("plus8", None, True))
check("one alert, keyed by the failing set", lines(alerts), ["key=[" + " ".join(bad) + "]"])

print("── the same set again, then a changed one ──")
sh("bash", str(SCRIPT), "run")
check("a second run appends a log line", len(lines(log)), 2)
(repo / "scripts" / "test_found_bomb.py").write_text("#!/usr/bin/env python3\n# gate: hermetic\n")
run_one.write_text(run_one.read_text().replace("*bomb*", "*listed_bomb*"))   # the found bomb is fixed
r = sh("bash", str(SCRIPT), "run")
check("a fixed test leaves the failing set", "2 of 5" in r.stdout and "test_found_bomb" not in r.stdout, True)
check("…and the alert hears the new set", lines(alerts)[-1], "key=[test_listed_bomb.py test_noverdict.py]")

print("── green, and an eval window ──")
run_one.write_text(run_one.read_text().replace('*noverdict*) exit 0;;', '').replace("*listed_bomb*", "*zzz*"))
r = sh("bash", str(SCRIPT), "run")
check("all passing says so", r.stdout.strip(), "clock canary: 5/5 pass at +8 days")
check("…the log records no failures", json.loads(lines(log)[-1])["fail"], [])
check("…and the alert hears the clear (an empty key)", lines(alerts)[-1], "key=[]")
n = len(lines(log))
(T / "in-window").write_text("")
r = sh("bash", str(SCRIPT), "run")
check("an eval window skips the run, nothing recorded", ("skipped" in r.stdout, len(lines(log))), (True, n))
(T / "in-window").unlink()

print("── the plans: absolute targets, rotation, all, the budget (#1570) ──")
PLAN_CLOCKS = {"plus8": "8", "dst-repeat": "2026-11-01T08:30:00Z", "dst-skip": "2027-03-14T10:30:00Z",
               "evening": "2027-06-15T06:30:00Z"}


def plans_run(*args, **env):
    for p in (calls, log, alerts, T / "alert-labels"):
        p.unlink(missing_ok=True)
    e = {k: v for k, v in RUN_ENV.items() if k != "CLOCK_CANARY_PLANS"}
    r = sh("bash", str(SCRIPT), "run", *args, env={**e, **env})
    return r, [json.loads(x) for x in lines(log)], sorted({c.split()[1] for c in lines(calls)})


r, recs, clocks = plans_run("all")
check("`run all` logs one line per plan, with its label", [x["label"] for x in recs], list(PLAN_CLOCKS))
check("…each test ran at each plan's clock (days, or an absolute UTC time)", clocks, sorted(PLAN_CLOCKS.values()))
check("…the absolute plans record their target and no day shift",
      {x["label"]: (x["shift"], x["to"]) for x in recs},
      {"plus8": (8, None), "dst-repeat": (None, "2026-11-01T08:30:00Z"), "dst-skip": (None, "2027-03-14T10:30:00Z"),
       "evening": (None, "2027-06-15T06:30:00Z")})
check("…the summary lines name the clock", [x for x in r.stdout.splitlines() if "2027-06-15T06:30:00Z" in x][:1] != [], True)
check("…each plan alerts under its own label", lines(T / "alert-labels"), list(PLAN_CLOCKS))
check("the plans do not stop at a failing one: every plan saw all 5 tests", [x["total"] for x in recs], [5, 5, 5, 5])
r, recs, clocks = plans_run("evening", "dst-skip")
check("named plans run in the order given", [x["label"] for x in recs], ["evening", "dst-skip"])
r, recs, clocks = plans_run("nonsense")
check("an unknown plan is refused", (r.returncode, recs), (2, []))
nightly = []
for day in ("001", "002", "003", "004", "365"):
    r, recs, clocks = plans_run(CLOCK_CANARY_DAY=day)
    nightly.append([x["label"] for x in recs])
check("with no list, a night runs +8 and one variant, rotating by day of year",
      nightly, [["plus8", "dst-skip"], ["plus8", "evening"], ["plus8", "dst-repeat"], ["plus8", "dst-skip"],
                ["plus8", "evening"]])
r, recs, clocks = plans_run("all", CLOCK_CANARY_BUDGET="100")
check("a spent time budget skips the plans rather than starting them", (recs, r.stdout.count("skipped")), ([], 4))
# The shim: TIME_SHIFT_TO puts the clock at an absolute instant (in a throwaway interpreter, TZ=UTC like the gate).
SHIM = ("import datetime as d, time\nfrom zoneinfo import ZoneInfo\n"
        "n = d.datetime.now(d.timezone.utc)\nh = n.astimezone(ZoneInfo('America/Los_Angeles'))\n"
        "print(n.strftime('%Y-%m-%dT%H:%M'), h.strftime('%Y-%m-%d %H:%M %Z'), d.date.today(), int(time.time()) // 3600)")


def shim(target):
    e = {**ENV, "TZ": "UTC", "PYTHONPATH": str(HERE / "clockshift"), "TIME_SHIFT_TO": target}
    e.pop("TIME_SHIFT_DAYS", None)
    return subprocess.run([sys.executable, "-c", SHIM], env=e,
                          capture_output=True, text=True, timeout=60).stdout.split()


s = shim("2026-11-01T08:30:00Z")
check("TIME_SHIFT_TO puts the clock at the repeated hour (01:30 PDT, its first pass)", s[:4], ["2026-11-01T08:30", "2026-11-01", "01:30", "PDT"])
s = shim("2027-03-14T10:30:00Z")
check("…and after the skipped hour (03:30 PDT)", s[:4], ["2027-03-14T10:30", "2027-03-14", "03:30", "PDT"])
s = shim("2027-06-15T06:30:00Z")
check("…and in the household's evening: UTC says 06-15, the household says 06-14 23:30",
      (s[0], s[1], s[2], s[4]), ("2027-06-15T06:30", "2027-06-14", "23:30", "2027-06-15"))
check("…a time with no zone is UTC", shim("2027-06-15T06:30:00")[0], "2027-06-15T06:30")
check("run_one.sh hands an ISO time to the shim as TIME_SHIFT_TO and a day count as TIME_SHIFT_DAYS",
      ("TIME_SHIFT_TO" in (HERE / "clockshift" / "run_one.sh").read_text(),
       "TIME_SHIFT_DAYS" in (HERE / "clockshift" / "run_one.sh").read_text()), (True, True))

print("── the deploy's install step ──")
sysd = T / "systemd"
sysd.mkdir()
fake_ctl = T / "systemctl.sh"
fake_ctl.write_text('#!/usr/bin/env bash\necho "$*" >> "%s/ctl"\ncase "$1" in is-enabled|is-active) [ -f "%s/enabled" ];;'
                    ' enable) touch "%s/enabled";; esac\n' % (T, T, T))
fake_ctl.chmod(0o755)
ienv = {**ENV, "SYSTEMD_DIR": str(sysd), "SUDO": "", "SYSTEMCTL": str(fake_ctl)}
r = sh("bash", str(SCRIPT), "install", env=ienv)
check("install copies both units and enables the timer", (r.stdout.strip(), sorted(p.name for p in sysd.iterdir())),
      ("clock canary timer installed", ["titan-clock-canary.service", "titan-clock-canary.timer"]))
r = sh("bash", str(SCRIPT), "install", env=ienv)
check("…a second deploy finds it current", r.stdout.strip(), "clock canary timer current")
check("…the deploy runs it", "bash scripts/clockshift/canary.sh install" in (HERE / "deploy_titan_node.sh").read_text(), True)
unit = (HERE.parent / "server-setup" / "titan-clock-canary.service").read_text()
check("the service runs as zach, niced, idle IO", ("User=zach" in unit, "Nice=19" in unit, "IOSchedulingClass=idle" in unit,
                                                   "canary.sh run" in unit), (True, True, True, True))
# The timer's start is clear of every window in eval_windows.conf (and its lead).
timer = (HERE.parent / "server-setup" / "titan-clock-canary.timer").read_text()
hhmm = [x for x in timer.split() if x.count(":") == 2 and x[:2].isdigit()][0]
start = int(hhmm[:2]) * 60 + int(hhmm[3:5])
lead, wins = 12, []
for line in (HERE / "eval_windows.conf").read_text().splitlines():
    p = line.split()
    if line.startswith("LEAD_MIN"):
        lead = int(p[1])
    elif p and p[0][:1].isdigit() and len(p[0]) == 4:
        wins.append((int(p[0][:2]) * 60 + int(p[0][2:]), int(p[1][:2]) * 60 + int(p[1][2:])))
# RandomizedDelaySec=10min can push it 10 min later.
check("the timer (with its 10 min delay) stays out of the eval windows",
      [w for w in wins if start + 10 + 60 > w[0] - lead and start < w[1]], [])

shutil.rmtree(T, ignore_errors=True)
print("── one plan: the canary runs the tests the deploy gate runs (#1559) ──")
GATE_PLAN = HERE / "gate_plan.sh"


def gate_files(checkout):
    r = subprocess.run(["bash", "-c", '. "$1" && gate_plan "$2"', "x", str(GATE_PLAN), str(checkout)],
                       env=ENV, capture_output=True, text=True, timeout=120)
    return sorted({ln.split("|")[0].rsplit("/", 1)[-1] for ln in r.stdout.splitlines() if ln})


def canary_plan(checkout):   # the canary's own plan function, evaluated against a checkout
    body = SCRIPT.read_text().split("case \"${1:-}\" in")[0]
    r = subprocess.run(["bash", "-c", body + '\nplan | sed "s|.*/||" | sort -u', str(SCRIPT)],
                       env={**ENV, "CLOCK_CANARY_REPO": str(checkout)}, capture_output=True, text=True, timeout=120)
    return r.stdout.split()


check("on a stub checkout the canary's plan is the gate's", canary_plan(repo), gate_files(repo))
check("…on the current tree too", canary_plan(HERE.parent), gate_files(HERE.parent))
check("…and that plan is not empty", len(gate_files(HERE.parent)) > 0, True)
check("the gate script reads the shared plan, with no rules of its own",
      ("gate_plan" in (HERE / "run_deploy_gate.sh").read_text(), "gate: hermetic" in (HERE / "run_deploy_gate.sh").read_text()),
      (True, False))
check("…and so does the canary", ("gate_plan.sh" in SCRIPT.read_text(), "gate: hermetic" in SCRIPT.read_text().replace("`# gate: hermetic`", "")),
      (True, False))

check("the canary's plan is the whole plan: it neither reads nor reorders by GATE_FIRST (#1514)",
      ("GATE_FIRST" in SCRIPT.read_text(), "GATE_FIRST" in (HERE / "gate_plan.sh").read_text()), (False, False))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
