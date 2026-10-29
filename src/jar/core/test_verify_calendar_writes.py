#!/usr/bin/env python3
"""
Tests for the check after a calendar event is created or moved: titan re-reads the day and confirms
the event landed where the user asked (day, time, calendar), exactly once. 09-30: "add it to the
household calendar" landed on the default calendar on Nov 1 at 9 AM, and a second "pickup Leo"
was created. Hermetic: the Mac is faked, in its own wording.

Run: python3 scripts/test_verify_calendar_writes.py
"""
import os
import sys
from datetime import datetime
sys.path.insert(0, os.path.dirname(__file__))

import agent_runtime_lib as lib  # noqa: E402


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


class FakeMac:
    """A day's events as the Mac lists them. Faults: land (a different start), cal (the calendar it
    really goes on), twice (saved twice), lost (reported created, never saved), stuck (a move that
    leaves the old one)."""
    def __init__(self, land=None, cal="Home", twice=False, lost=False, stuck=False, pre=None, calendar_names=True):
        self.events = list(pre or [])   # (datetime, title, calendar)
        self.land, self.cal, self.twice, self.lost, self.stuck = land, cal, twice, lost, stuck
        self.calendar_names = calendar_names

    def __call__(self, name, args):
        if name == "list_calendar_events":
            day = args["start"][:10]
            rows = sorted(e for e in self.events if e[0].strftime("%Y-%m-%d") == day)
            return "\n".join(f"• {lib._stamp(d)} — {t} [calendar: {c}]" for d, t, c in rows) or "No events."
        if name == "create_calendar_event":
            d = self.land or datetime.fromisoformat(args["start"])
            cal = args.get("calendar") if args.get("calendar") and self.cal == "Home" else self.cal
            if not self.lost:
                self.events += [(d, args["title"], cal)] * (2 if self.twice else 1)
            where = f" on the {cal} calendar" if self.calendar_names else ""
            return f"Created '{args['title']}' on {lib._stamp(d)}{where}."
        if name == "update_calendar_event":
            old = next(e for e in self.events if e[1].lower() == args["title"].lower())
            new = (datetime.fromisoformat(args["start"]) if args.get("start") else old[0],
                   args.get("new_title") or old[1], old[2])
            if not self.stuck:
                self.events.remove(old)
            self.events.append(new)
            out = f"Moved “{old[1]}” from {lib._stamp(old[0])} to {lib._stamp(new[0])}."
            return out + (f" Renamed it “{new[1]}”." if args.get("new_title") else "")
        return "ok"


def run():
    ok = True
    saved = (lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed)
    lib._device_bridge_enabled = lambda: True
    lib._device_tool_allowed = lambda n: True

    def call(mac, name, args):
        lib._device_request = mac
        return lib.execute_tool(name, args)

    ask = {"title": "Pickup Leo", "start": "2026-10-07T14:50", "calendar": "Family"}
    try:
        print("\n── the stamp matches the Mac's ──")
        ok &= check("afternoon", lib._stamp(datetime(2026, 10, 7, 14, 50)), "Wed Oct 7, 2:50 PM")
        ok &= check("midnight hour", lib._stamp(datetime(2026, 10, 7, 0, 5)), "Wed Oct 7, 12:05 AM")
        ok &= check("a date only", lib._requested("2026-10-07"), ("2026-10-07", None))

        print("\n── a clean create is verified ──")
        out = call(FakeMac(), "create_calendar_event", ask)
        ok &= check("the Mac's result is kept", out.startswith("Created 'Pickup Leo' on Wed Oct 7, 2:50 PM on the Family calendar."), True)
        ok &= check("verified against a re-read",
                    "Verified: Wed Oct 7, 2:50 PM — Pickup Leo [calendar: Family]." in out, True)
        ok &= check("the reply is told what to say", "say the day, time and calendar it's on." in out, True)

        print("\n── what went wrong on 09-30 ──")
        out = call(FakeMac(land=datetime(2026, 11, 1, 9, 0)), "create_calendar_event", ask)
        ok &= check("landed on another day", "asked for Wed Oct 7, 2:50 PM but landed on Sun Nov 1, 9:00 AM (a different day)" in out, True)
        out = call(FakeMac(cal="Calendar"), "create_calendar_event", ask)
        ok &= check("landed on the default calendar", "asked for the Family calendar but is on Calendar" in out, True)
        out = call(FakeMac(twice=True), "create_calendar_event", ask)
        ok &= check("saved twice", "now shows 2 times at that time" in out, True)
        pre = [(datetime(2026, 10, 7, 14, 50), "Pickup Leo", "Family")]
        out = call(FakeMac(pre=pre), "create_calendar_event", ask)
        ok &= check("a copy of one already there", "now shows 2 times at that time" in out, True)
        out = call(FakeMac(lost=True), "create_calendar_event", ask)
        ok &= check("reported but never saved", "doesn't show on 2026-10-07 after the change" in out, True)
        out = call(FakeMac(land=datetime(2026, 10, 7, 15, 50)), "create_calendar_event", ask)
        ok &= check("an hour off", "(a different time)" in out, True)

        print("\n── moves ──")
        pre = [(datetime(2026, 10, 7, 14, 50), "Pickup Leo", "Family")]
        out = call(FakeMac(pre=pre), "update_calendar_event",
                   {"title": "Pickup Leo", "on": "2026-10-07", "start": "2026-10-08T15:00"})
        ok &= check("a move is verified on the new day", "Verified: Thu Oct 8, 3:00 PM — Pickup Leo" in out, True)
        out = call(FakeMac(pre=list(pre), stuck=True), "update_calendar_event",
                   {"title": "Pickup Leo", "on": "2026-10-07", "start": "2026-10-08T15:00"})
        ok &= check("… and one left behind is flagged", "still shows on 2026-10-07 too" in out, True)
        out = call(FakeMac(pre=list(pre)), "update_calendar_event",
                   {"title": "Pickup Leo", "on": "2026-10-07", "new_title": "Pickup Leo (Robin)"})
        ok &= check("a rename is checked under the new title", "Verified: Wed Oct 7, 2:50 PM — Pickup Leo (Robin)" in out, True)
        out = call(FakeMac(pre=list(pre)), "update_calendar_event", {"title": "Pickup Leo", "on": "2026-10-07", "notes": "gate B"})
        ok &= check("a notes-only change isn't re-read", "[checked]" in out, False)

        print("\n── what isn't checked ──")
        lib._device_request = lambda n, a: "You declined this action."
        ok &= check("declined passes through", lib.execute_tool("create_calendar_event", ask), "You declined this action.")
        out = call(FakeMac(calendar_names=False), "create_calendar_event", {k: v for k, v in ask.items() if k != "calendar"})
        ok &= check("the iPhone's wording (no calendar) is verified too", "Verified:" in out, True)

        def broken(name, args):
            if name == "list_calendar_events":
                raise RuntimeError("device timeout")
            return FakeMac()(name, args)
        lib._device_request = broken
        out = lib.execute_tool("create_calendar_event", ask)
        ok &= check("a failed re-read never breaks the create",
                    (out.startswith("Created 'Pickup Leo'"), "Couldn't re-read 2026-10-07" in out), (True, True))
    finally:
        lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed = saved

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
