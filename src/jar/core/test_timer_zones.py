#!/usr/bin/env python3
# gate: hermetic
"""Every timer names its zone (#1569). titan's host zone is UTC, but a timer that names no zone follows whatever the host
is set to, and a description that says "PT" over a UTC-pinned OnCalendar is wrong for half the year. Rules: an OnCalendar
with a time of day ends in a zone (UTC or an IANA name); a Description that mentions PT/Pacific/PDT/PST needs an
America/Los_Angeles OnCalendar unless it also says UTC. Run: python3 scripts/test_timer_zones.py"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import eval_windows  # noqa: E402

DIR = Path(__file__).parent.parent / "server-setup"
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


TIME = re.compile(r"\d\d?:\d\d|\*:\d\d|^(hourly|daily|weekly|monthly|minutely|yearly)$")
ZONE = re.compile(r"\s(UTC|[A-Z][A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?)$")
PT = re.compile(r"\b(PT|PDT|PST|Pacific)\b")


def has_time_of_day(spec):
    return bool(TIME.search(spec.strip()))


# the rules, against known-good and known-bad specs
check("zone-less clock time is caught", bool(ZONE.search("*-*-* 02:15:00")), False)
check("UTC suffix is accepted", bool(ZONE.search("*-*-* 02:15:00 UTC")), True)
check("IANA suffix is accepted", bool(ZONE.search("Sun *-*-* 18:00 America/Los_Angeles")), True)
check("bare hourly counts as a time of day", has_time_of_day("hourly"), True)
check("a monotonic interval is not a calendar spec", has_time_of_day("10min"), False)

timers = sorted(DIR.glob("*.timer"))
check("timers found", len(timers) > 20, True)
for f in timers:
    text = f.read_text()
    specs = re.findall(r"^OnCalendar=(.*)$", text, re.M)
    for spec in specs:
        if has_time_of_day(spec):
            check(f"{f.name}: OnCalendar={spec!r} ends in a zone", bool(ZONE.search(spec)), True)
    m = re.search(r"^Description=(.*)$", text, re.M)
    desc = m.group(1) if m else ""
    if PT.search(desc) and "UTC" not in desc:
        check(f"{f.name}: description names Pacific so an America/Los_Angeles OnCalendar is required",
              any(s.strip().endswith("America/Los_Angeles") for s in specs), True)
    # 10-14: three moved timers kept their old "03:30 UTC"-style descriptions. The first time a description names, with
    # its zone, must be the timer's own daily time.
    said = re.search(r"\b(\d\d):(\d\d)\s*(UTC|Z|Pacific|PT|PDT|PST)\b", desc)
    daily = [re.match(r"^\*-\*-\*\s+(\d\d):(\d\d)(?::\d\d)?\s+(\S+)$", x.strip()) for x in specs]
    daily = [d for d in daily if d]
    if said and daily:
        zone = "UTC" if said.group(3) in ("UTC", "Z") else "America/Los_Angeles"
        check(f"{f.name}: the description's {said.group(0)!r} is its OnCalendar time",
              any((d.group(1), d.group(2), d.group(3)) == (said.group(1), said.group(2), zone) for d in daily), True)

# A daily America/Los_Angeles timer must exist every night (#1581): 02:00-03:00 local is skipped on spring-forward, so
# systemd skips that night's run. And it must sit outside the eval windows on both clock-change weekends. The model-eval
# timer defines its own window, so it is the one allowed to be inside it (and 01:35 is before the gap in any case).
PT_DAILY = re.compile(r"^(?:\*-\*-\*\s+)?(\d\d?):(\d\d)(?::\d\d)?\s+America/Los_Angeles$")
OWN_WINDOW = {"titan-model-eval.timer", "titan-household-eval.timer"}   # each eval's timer defines its window
CONF = eval_windows.CONF.read_text()
CHANGE_DAYS = [date(2026, 10, 31), date(2026, 11, 2), date(2027, 3, 13), date(2027, 3, 15)]
PT = ZoneInfo("America/Los_Angeles")


def in_window(h, m, day):
    """The windows of the UTC day that contain the local fire time on Pacific date `day` (None when none)."""
    fire = datetime(day.year, day.month, day.day, h, m, tzinfo=PT).astimezone(ZoneInfo("UTC"))
    for d in (fire.date(), fire.date() - timedelta(days=1)):
        for a, b, name in eval_windows.windows_for(CONF, d):
            lo = datetime(d.year, d.month, d.day, tzinfo=ZoneInfo("UTC")) + timedelta(minutes=int(a[:2]) * 60 + int(a[2:]))
            hi = datetime(d.year, d.month, d.day, tzinfo=ZoneInfo("UTC")) + timedelta(minutes=int(b[:2]) * 60 + int(b[2:]))
            if lo <= fire < hi:
                return name
    return None


check("rule: 03:15 Pacific on 2027-03-14 exists and is outside every window", in_window(3, 15, date(2027, 3, 14)), None)
check("rule: 02:30 Pacific is inside the model-eval window", in_window(2, 30, date(2026, 11, 2)), "model-eval")
for f in timers:
    if f.name in OWN_WINDOW:
        continue
    for spec in re.findall(r"^OnCalendar=(.*)$", f.read_text(), re.M):
        m = PT_DAILY.match(spec.strip())
        if not m:
            continue
        h, mi = int(m.group(1)), int(m.group(2))
        check(f"{f.name}: {spec!r} is not in the 02:00-03:00 spring-forward gap", 2 <= h < 3, False)
        for day in CHANGE_DAYS:
            check(f"{f.name}: {spec!r} on {day} is outside the eval windows", in_window(h, mi, day), None)

print(f"{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
