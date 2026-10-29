#!/usr/bin/env python3
# gate: hermetic
"""The eval windows have one home (#1518): scripts/eval_windows.conf. On 10-10 they were written in about 8 places with
three different sets of times (gate_queue 03:05/08:15 with a 20-min lead, chat_links_cycle and ab_run 08:35–09:05, the
runbook 08:35–10:00). gate_queue.sh reads the file (its built-in fallback must equal it), answers `window` for
deploy.sh and ab_run.sh, and chat_links_cycle.py and autonomous_coder.py read it too; a window's times written back into
one of them fails here. A line may carry a zone as a 4th field (#1566: the model eval's timer is on Pacific time, so its
window follows the clock change); every reader converts it to UTC for the day. Reads files and runs gate_queue.sh's
`window` verb only; every time is passed in, none comes from the wall clock. Run: python3 scripts/test_eval_windows.py"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
CONF = HERE / "eval_windows.conf"

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


rows = [ln.split() for ln in CONF.read_text().splitlines() if re.match(r"^\d{4} \d{4}", ln)]
lead = next((int(ln.split()[1]) for ln in CONF.read_text().splitlines() if ln.startswith("LEAD_MIN ")), None)
check("the file lists the nightly and the model eval", [r[2] for r in rows], ["nightly", "model-eval"])
check("…each start before its end", all(r[0] < r[1] for r in rows), True)
check("…and a lead in minutes", isinstance(lead, int) and 0 < lead < 60, True)

import eval_windows as EW  # noqa: E402

q = (HERE / "gate_queue.sh").read_text()
fb = re.search(r"printf '%s\\n' (.+?); fi", q)
fb_rows = [r.split() for r in re.findall(r'"([^"]+)"', fb.group(1))] if fb else []
DAYS = [date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2), date(2027, 3, 13), date(2027, 3, 14), date(2027, 3, 15)]
# The built-in fallback is UTC, so it must cover every file window on every day, either side of both clock changes.
uncovered = []
for d in DAYS:
    for a_, b_, n_ in EW.windows_for(CONF.read_text(), d):
        if not any(f[2] == n_ and f[0] <= a_ and b_ <= f[1] for f in fb_rows):
            uncovered.append((str(d), n_))
check("gate_queue.sh's built-in fallback covers the file's windows on every day", (len(fb_rows), uncovered), (len(rows), []))


def window(hhmm: str, *lead_arg: str, day: str = "2026-10-11") -> int:
    env = {**os.environ, "GATE_QUEUE_HHMM": hhmm, "GATE_QUEUE_DATE": day, "GATE_QUEUE_WINDOWS": str(CONF), "GATE_QUEUE_DIR": "/nonexistent-gq"}
    return subprocess.run(["bash", str(HERE / "gate_queue.sh"), "window", *lead_arg], env=env,
                          capture_output=True, timeout=20).returncode


def hm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}{minutes % 60:02d}"


def mins(s: str) -> int:
    return int(s[:2]) * 60 + int(s[2:])


for a, b, name in EW.windows_for(CONF.read_text(), date(2026, 10, 11)):
    check(f"{name}: inside at its start", window(a), 0)
    check(f"{name}: inside {lead} min before (the lead)", window(hm(mins(a) - lead)), 0)
    check(f"{name}: free {lead + 1} min before", window(hm(mins(a) - lead - 1)), 1)
    check(f"{name}: inside a minute before its end", window(hm(mins(b) - 1)), 0)
    check(f"{name}: free at its end", window(b), 1)
    check(f"{name}: `window 0` ignores the lead", window(hm(mins(a) - 1), "0"), 1)

# The model eval is 01:35–03:00 on Pacific time, whichever side of the clock change the day is on (#1566). The expected
# UTC edges come straight from zoneinfo, not from the helper under test.
import chat_links_cycle as C  # noqa: E402
import autonomous_coder as A  # noqa: E402
LA = ZoneInfo("America/Los_Angeles")
idle = lambda cmd, **kw: type("R", (), {"stdout": "inactive\n", "returncode": 0})()  # noqa: E731
no_hold = Path("/nonexistent-hold")
for d in DAYS:
    s_utc = datetime(d.year, d.month, d.day, 1, 35, tzinfo=LA).astimezone(timezone.utc)
    e_utc = datetime(d.year, d.month, d.day, 3, 0, tzinfo=LA).astimezone(timezone.utc)
    ds = str(d)

    def hhmm_of(t):
        return t.strftime("%H%M")
    label = f"{ds} model-eval ({s_utc:%H:%M}–{e_utc:%H:%M}Z)"
    one = lambda t: t - timedelta(minutes=1)  # noqa: E731
    check(f"{label}: gate_queue inside from the lead to the end",
          [window(hhmm_of(t), day=ds) for t in (s_utc - timedelta(minutes=lead), s_utc, one(e_utc))], [0, 0, 0])
    check(f"{label}: gate_queue free past the lead and at the end",
          [window(hhmm_of(t), day=ds) for t in (s_utc - timedelta(minutes=lead + 1), e_utc)], [1, 1])
    check(f"{label}: the whole 01:35–03:00 local run is covered",
          all(window(hhmm_of(t), "0", day=ds) == 0 for t in (s_utc + timedelta(minutes=m) for m in range(0, 145, 5))
              if t < e_utc), True)
    check(f"{label}: chat_links_cycle blocks from start to end",
          [C.blocked(t.timestamp(), no_hold) is not None for t in (s_utc, one(e_utc), one(s_utc), e_utc)],
          [True, True, False, False])
    check(f"{label}: autonomous_coder blocks from start to end",
          [A.eval_busy(t.timestamp(), idle) is not None for t in (s_utc, one(e_utc), one(s_utc), e_utc)],
          [True, True, False, False])
    pac = ZoneInfo("America/Los_Angeles")   # the nightly is 00:25–01:10 Pacific on every day (10-14: out of the evening)
    check(f"{label}: the nightly covers 00:25–01:10 Pacific",
          [A.eval_busy(datetime(d.year, d.month, d.day, h, m, tzinfo=pac).timestamp(), idle) is not None
           for h, m in ((0, 25), (1, 9), (0, 24), (1, 10))], [True, True, False, False])

check("chat_links_cycle reads the same windows",
      list(C.windows_at(datetime(2026, 10, 11, 12, tzinfo=timezone.utc).timestamp())),
      [(f"{a[:2]}:{a[2:]}", f"{b[:2]}:{b[2:]}") for a, b, _ in EW.windows_for(CONF.read_text(), date(2026, 10, 11))])
check("a line without a zone is UTC on any day",
      EW.windows_for("0325 0350 nightly\n", date(2027, 3, 14)), [("0325", "0350", "nightly")])
check("a zoned range that crosses UTC midnight is split there",
      EW.windows_for("0900 1500 late Pacific/Auckland\n", date(2026, 10, 11)),
      [("0000", "0200", "late"), ("2000", "2400", "late")])

# No window's times written back into the readers (comments and docstrings may name the file, not the times).
TIMES = {t for r in rows for t in (r[0], r[1], f"{r[0][:2]}:{r[0][2:]}", f"{r[1][:2]}:{r[1][2:]}")}
LEGACY = {"0305", "0815", "0905", "03:05", "08:15", "09:05"}
for f in ("gate_queue.sh", "ab_run.sh", "chat_links_cycle.py", "coord/deploy.sh"):
    code = [ln for ln in (HERE / f).read_text().splitlines()
            if not ln.lstrip().startswith("#") and "printf '%s\\n'" not in ln]
    lit = sorted({t for ln in code for t in (TIMES | LEGACY) if re.search(rf"(?<![\d:]){re.escape(t)}(?![\d:])", ln)})
    check(f"{f} holds no window times of its own", lit, [])

# A timer whose job refuses to start inside a window must never fire inside one (10-11: the autonomous coder's
# 03:37Z + 10 min sat inside the nightly window, so after #1517 it would have skipped every night).
for unit in ("titan-autonomous-coder",):
    t = (HERE.parent / "server-setup" / f"{unit}.timer").read_text()
    m = re.search(r"^OnCalendar=\*-\*-\* (\d\d):(\d\d)(?::\d\d)? UTC$", t, re.M)
    check(f"{unit}.timer names its time in UTC", bool(m), True)
    if m:
        start = int(m[1]) * 60 + int(m[2])
        d = re.search(r"^RandomizedDelaySec=(\d+)", t, re.M)
        end = start + (int(d[1]) // 60 if d else 0)
        import eval_windows as EW  # noqa: E402
        from datetime import date as _d
        for day in (_d(2026, 10, 31), _d(2026, 11, 2), _d(2027, 3, 13), _d(2027, 3, 15)):   # both sides of each change
            hit = [n for a, b, n in EW.windows_for(CONF.read_text(), day) if start < mins(b) and mins(a) - lead <= end]
            check(f"{unit}.timer fires outside every window on {day}, lead included", hit, [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
