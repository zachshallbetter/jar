#!/usr/bin/env python3
"""Reads scripts/eval_windows.conf (#1518, #1566): the one list of eval windows, each a UTC clock range or, with a 4th
field, a range on a named zone's clock ("0135 0300 model-eval America/Los_Angeles"). A zoned line is converted to UTC for
the day asked about, so it follows the clock change the unit's timer follows. Lines without a zone are UTC.

  python3 scripts/eval_windows.py [--conf FILE] [--date YYYY-MM-DD]   prints "HHMM HHMM name" UTC lines for that UTC day
                                                                      (today when no date); a range past midnight is split
Used by gate_queue.sh, chat_links_cycle.py and autonomous_coder.py."""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CONF = Path(__file__).resolve().parent / "eval_windows.conf"


def parse(text: str) -> list[tuple[str, str, str, str]]:
    """(start, end, name, zone) per window line; zone is "" for UTC. Comments, LEAD_MIN and malformed lines are skipped."""
    rows = []
    for ln in text.splitlines():
        p = ln.split("#", 1)[0].split()
        if len(p) >= 2 and len(p[0]) == len(p[1]) == 4 and p[0].isdigit() and p[1].isdigit():
            rows.append((p[0], p[1], p[2] if len(p) > 2 else "", p[3] if len(p) > 3 else ""))
    return rows


def _mins(s: str) -> int:
    return int(s[:2]) * 60 + int(s[2:])


def _hhmm(m: int) -> str:
    return "%02d%02d" % (m // 60, m % 60)


def utc_ranges(rows: list[tuple[str, str, str, str]], day: date) -> list[tuple[int, int, str]]:
    """(start_min, end_min, name) in UTC minutes of the UTC day `day`, 0..1440, ranges clipped to that day."""
    from zoneinfo import ZoneInfo
    day0 = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    out = []
    for a, b, name, zone in rows:
        if not zone:
            out.append((_mins(a), _mins(b), name))
            continue
        z = ZoneInfo(zone)
        for k in (-1, 0, 1):    # the zone's date may differ from the UTC date by a day either way
            d = day + timedelta(days=k)
            s = datetime(d.year, d.month, d.day, int(a[:2]), int(a[2:]), tzinfo=z).astimezone(timezone.utc)
            e = datetime(d.year, d.month, d.day, int(b[:2]), int(b[2:]), tzinfo=z).astimezone(timezone.utc)
            sm = int((s - day0).total_seconds() // 60)
            em = int((e - day0).total_seconds() // 60)
            sm, em = max(sm, 0), min(em, 1440)
            if sm < em:
                out.append((sm, em, name))
    return out


def windows_for(text: str, day: date) -> list[tuple[str, str, str]]:
    """The windows of `day` (UTC) as ("HHMM", "HHMM", name); an end at midnight is "2400"."""
    return [(_hhmm(s), _hhmm(e), n) for s, e, n in utc_ranges(parse(text), day)]


def windows_at(now: float, conf: Path | None = None) -> list[tuple[str, str, str]]:
    """The windows of the UTC day containing the epoch time `now`, from the conf; [] when it can't be read."""
    try:
        text = (conf or CONF).read_text()
    except OSError:
        return []
    return windows_for(text, datetime.fromtimestamp(now, timezone.utc).date())


def main(argv: list[str]) -> int:
    conf, day = CONF, datetime.now(timezone.utc).date()
    it = iter(argv)
    for arg in it:
        if arg == "--conf":
            conf = Path(next(it))
        elif arg == "--date":
            day = date.fromisoformat(next(it))
    for a, b, n in windows_for(conf.read_text(), day):
        print(a, b, n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
