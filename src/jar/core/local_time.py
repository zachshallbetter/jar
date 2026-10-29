"""The household's clock. titan's host runs UTC, so anything that says "today", "tomorrow" or a
time to a person (or a model) reads it from here: AGENT_TZ (default America/Los_Angeles).

`now_line()` is the one sentence every model call gets, so no model has to guess the date
(10-01: the weather tool's "Today" was UTC's date all Pacific evening; bill extraction, web
search, summaries and fact extraction had no date at all)."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

DEFAULT_TZ = "America/Los_Angeles"
_warned = False


def parse_iso(value: Any) -> datetime | None:
    """An ISO timestamp from anywhere (the Rust runtimes write nanoseconds, or fewer digits when the tail is zero:
    "…13.243082184+00:00", "…13.2+00:00"), on titan's Python 3.10 too, whose fromisoformat takes only 3 or 6
    fraction digits and no "Z". None when it isn't one."""
    import re
    s = str(value or "").strip().replace("Z", "+00:00")
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    s = re.sub(r"([+-]\d{2})$", r"\1:00", s)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def zone(name: str | None = None) -> tzinfo | None:
    """The household's zone, or None (then the host's local zone is used, loudly: that's UTC on titan)."""
    global _warned
    tz_name = (name or os.environ.get("AGENT_TZ") or DEFAULT_TZ).strip()
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception as exc:  # noqa: BLE001
            if not _warned:
                _warned = True
                print(f"[time] can't load timezone {tz_name!r} ({exc}); falling back to the host clock",
                      file=sys.stderr)
    return None


def now(name: str | None = None) -> datetime:
    """The current time in the household's zone (aware)."""
    z = zone(name)
    return datetime.now(z) if z is not None else datetime.now().astimezone()


def clock(ts: float, fmt: str = "%-I:%M %p", name: str | None = None) -> str:
    """An epoch time as the household reads it on a clock (in zone(); the host's own only when that fails). A card's
    "Until 9:30 PM" must never be titan's UTC (JEV's review of #1285: about 7 h off for the approving adult)."""
    z = zone(name)
    at = datetime.fromtimestamp(float(ts), z) if z is not None else datetime.fromtimestamp(float(ts)).astimezone()
    return at.strftime(fmt)


def today(name: str | None = None) -> date:
    return now(name).date()


def day(ts: float | None = None, name: str | None = None) -> str:
    """YYYY-MM-DD of an epoch time (default: now) on the household's calendar, never titan's UTC day. The key for
    per-day counters and cards; stored in the same format as before, only the day it is derived from differs."""
    z = zone(name)
    if ts is None:
        return now(name).strftime("%Y-%m-%d")
    at = datetime.fromtimestamp(float(ts), z) if z is not None else datetime.fromtimestamp(float(ts))
    return at.strftime("%Y-%m-%d")


def now_line(at: datetime | None = None) -> str:
    """"Now: Wednesday, September 30 2026 — 10:21 PM PDT (tomorrow is Thursday, October 1)". Tomorrow is
    spelled out: the 8B model miscounted it (#255)."""
    n = at or now()
    t = n + timedelta(days=1)
    # No zero-padded day: the model copies it ("Thursday, October 01", the 10-01 truth eval).
    # 12-hour with AM/PM: the 8B model read "02:31" as 2:31 PM (10-01 truth eval).
    clock = n.strftime("%I:%M %p").lstrip("0")
    return f"Now: {n:%A, %B} {n.day} {n:%Y} — {clock} {n:%Z} (tomorrow is {t:%A, %B} {t.day})"


def ago(then: datetime | float, at: datetime | None = None) -> str:
    """"3 min ago", "2 h ago", "yesterday 21:04", "Mon Sep 28 21:04" (household time)."""
    n = at or now()
    t = datetime.fromtimestamp(then, n.tzinfo) if isinstance(then, (int, float)) else then.astimezone(n.tzinfo)
    secs = n.timestamp() - t.timestamp()   # real seconds (subtracting same-zone datetimes ignores a DST change)
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if t.date() == n.date():
        return f"{int(secs // 3600)} h ago"
    if t.date() == n.date() - timedelta(days=1):
        return f"yesterday {t:%H:%M}"
    return f"{t:%a %b} {t.day} {t:%H:%M}"
