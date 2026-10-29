#!/usr/bin/env python3
"""
Tests for the check after a calendar event or reminder is deleted: titan re-reads the day (or the
open reminders), confirms exactly that item is gone and nothing else went with it, and the turn ends
by naming what was removed and how to undo it. Hermetic: the Mac is faked.

Run: python3 scripts/test_verify_deletions.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import agent_runtime_lib as lib


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


class FakeMac:
    """Calendar for 2026-10-01 and three reminders; `sticky` keeps a deleted item (a delete that
    silently failed), `also_drop` loses an extra item (a delete that took too much)."""
    def __init__(self, sticky=False, also_drop=None, broken_list=False):
        self.events = ["Thu Oct 1, 3:00 PM — Piano lesson", "Thu Oct 1, 7:00 PM — Team dinner"]
        self.reminders = ["Buy milk (due today)", "Renew passport (due 2026-10-10)"]
        self.sticky, self.also_drop, self.broken_list, self.calls = sticky, also_drop, broken_list, []

    def __call__(self, name, args):
        self.calls.append(name)
        if name in ("list_calendar_events", "list_reminders"):
            if self.broken_list and self.calls.count(name) > 1:
                raise RuntimeError("device timeout")
            items = self.events if name == "list_calendar_events" else self.reminders
            return "\n".join(f"• {x}" for x in items) or "No events."
        if name == "delete_calendar_event":
            hit = next((e for e in self.events if args["title"].lower() in e.lower()), None)
            if not hit:
                return f"Couldn't find an event “{args['title']}” on {args.get('on')}. Nothing changed."
            if not self.sticky:
                self.events.remove(hit)
            if self.also_drop:
                self.events = [e for e in self.events if self.also_drop not in e]
            return f"Cancelled “{hit.split('— ')[1]}” on Thu Oct 1"
        if name == "delete_reminder":
            hit = next(r for r in self.reminders if args["title"].lower() in r.lower())
            self.reminders.remove(hit)
            return f"Deleted reminder “{hit.split(' (')[0]}”."
        return "ok"


def run():
    ok = True
    saved = (lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed)
    lib._device_bridge_enabled = lambda: True
    lib._device_tool_allowed = lambda n: True

    def delete(mac, name, args):
        lib._device_request = mac
        lib._TURN_IO.deletions = []
        return lib.execute_tool(name, args), list(lib._TURN_IO.deletions)

    try:
        print("\n── a clean delete is verified ──")
        mac = FakeMac()
        out, removed = delete(mac, "delete_calendar_event", {"title": "team dinner", "on": "2026-10-01"})
        ok &= check("the Mac's own result is kept", out.startswith("Cancelled “Team dinner”"), True)
        ok &= check("verified against a re-read", "Verified: “Team dinner” is no longer in 2026-10-01" in out, True)
        ok &= check("what's still there is listed", "Piano lesson" in out.split("Still there:")[1], True)
        ok &= check("the day was read before and after", mac.calls, ["list_calendar_events", "delete_calendar_event", "list_calendar_events"])
        ok &= check("the turn records it", removed, ["Team dinner"])

        print("\n── a delete that didn't take, or took too much ──")
        out, _ = delete(FakeMac(sticky=True), "delete_calendar_event", {"title": "team dinner", "on": "2026-10-01"})
        ok &= check("still there → warning", "WARNING: “Team dinner” still appears" in out, True)
        out, _ = delete(FakeMac(also_drop="Piano"), "delete_calendar_event", {"title": "team dinner", "on": "2026-10-01"})
        ok &= check("something else gone → warning", "WARNING: “Team dinner” is gone, but so is: Thu Oct 1, 3:00 PM — Piano lesson" in out, True)

        print("\n── nothing deleted, nothing claimed ──")
        out, removed = delete(FakeMac(), "delete_calendar_event", {"title": "haircut", "on": "2026-10-01"})
        ok &= check("a miss passes through untouched", out, "Couldn't find an event “haircut” on 2026-10-01. Nothing changed.")
        ok &= check("... and isn't recorded", removed, [])

        print("\n── reminders, and an unknown day ──")
        out, removed = delete(FakeMac(), "delete_reminder", {"title": "milk"})
        ok &= check("reminder verified", "Verified: “Buy milk” is no longer in your open reminders" in out, True)
        mac = FakeMac()
        out, _ = delete(mac, "delete_calendar_event", {"title": "team dinner"})
        ok &= check("no day → no re-read, still reported", "Couldn't re-read that day" in out and mac.calls == ["delete_calendar_event"], True)
        out, _ = delete(FakeMac(broken_list=True), "delete_calendar_event", {"title": "team dinner", "on": "2026-10-01"})
        ok &= check("a failed re-read never breaks the delete", out.startswith("Cancelled “Team dinner”") and "Couldn't re-read" in out, True)

        print("\n── the turn always says what was removed ──")
        lib._TURN_IO.deletions = ["Team dinner", "Piano lesson"]
        ok &= check("appended when the reply doesn't say it",
                    lib._with_deletion_summary("Done!"), "Done!\n\nRemoved: “Team dinner”, “Piano lesson”. Say “undo” to restore.")
        said = "I cancelled Team dinner and Piano lesson. Say undo if that was a mistake."
        ok &= check("left alone when it does", lib._with_deletion_summary(said), said)
        lib._TURN_IO.deletions = []
        ok &= check("no deletions → untouched", lib._with_deletion_summary("Hi"), "Hi")
    finally:
        lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed = saved
        lib._TURN_IO.deletions = None

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
