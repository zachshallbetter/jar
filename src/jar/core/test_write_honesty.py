#!/usr/bin/env python3
"""
Tests for writes that didn't go as asked: after macOS refuses a privacy area (Calendars, Reminders,
Contacts) in a turn, the shell and AppleScript aren't tried for the same data; and a reminder that
went to a list the user didn't name says which. Hermetic: the Mac is faked.

Run: python3 scripts/test_write_honesty.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import agent_runtime_lib as lib  # noqa: E402


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def run():
    ok = True
    saved = (lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed)
    lib._device_bridge_enabled = lambda: True
    lib._device_tool_allowed = lambda n: True
    io = lib._TURN_IO
    replies = {}
    lib._device_request = lambda name, args: replies.get(name, "ok")
    try:
        io.denied, io.tools_used, io.failed_calls = {}, set(), {}
        print("\n── after a privacy refusal, not the shell ──")
        replies["create_calendar_event"] = "Calendar access not granted."
        lib.execute_tool("create_calendar_event", {"title": "Dentist", "start": "2026-10-02T09:00"})
        ok &= check("the refusal is remembered", io.denied, {"Calendars": "create_calendar_event"})
        out = lib.execute_tool("run_applescript", {"script": 'tell application "Calendar" to make new event'})
        ok &= check("AppleScript for the calendar isn't run", out.startswith("Not run: macOS refused Calendars access"), True)
        ok &= check("… and says how to allow it", "Privacy & Security › Calendars › Jellybean" in out, True)
        out = lib.execute_tool("run_shell", {"command": "sqlite3 ~/Library/Calendars/Calendar.sqlitedb .tables"})
        ok &= check("nor the shell", out.startswith("Not run: macOS refused Calendars access"), True)
        replies["run_shell"] = "up 3 days"
        ok &= check("an unrelated command still runs", lib.execute_tool("run_shell", {"command": "uptime"}), "up 3 days")
        io.denied = {}
        out = lib._shell_redirect("run_applescript", {"script": 'tell application "Calendar" to get name'})
        ok &= check("with no refusal, the usual rule applies", (out or "").startswith("Not run: macOS refused"), False)
        replies["search_contacts"] = "Contacts access denied."
        lib.execute_tool("search_contacts", {"query": "Ava"})
        ok &= check("other areas too", io.denied, {"Contacts": "search_contacts"})
        replies["list_calendar_events"] = "• Fri Oct 2, 9:00 AM — Dentist"
        io.denied = {}
        lib.execute_tool("list_calendar_events", {"start": "2026-10-02"})
        ok &= check("a normal result records nothing", io.denied, {})

        print("\n── a reminder says where it went ──")
        replies["create_reminder"] = ("Added reminder 'Call the vet' to the Reminders list. "
                                      "(There were no reminder lists, so Jellybean made this one.)")
        out = lib.execute_tool("create_reminder", {"title": "Call the vet", "due": "2026-10-01T16:30"})
        ok &= check("a list Jellybean made is named",
                    out.endswith("[say] Jellybean just made that list, because there were none: tell the user it's on the Reminders list."),
                    True)
        replies["create_reminder"] = "Added reminder 'Milk' to the Groceries list."
        out = lib.execute_tool("create_reminder", {"title": "Milk"})
        ok &= check("a list the user didn't pick is named", out.endswith("[say] tell the user it's on the Groceries list."), True)
        out = lib.execute_tool("create_reminder", {"title": "Bread", "list": "Groceries"})
        ok &= check("a list the user named isn't repeated", "[say]" in out, False)
        replies["create_reminder"] = "Not added: there's no reminder list called “Chores”. The lists it can go on: Home."
        out = lib.execute_tool("create_reminder", {"title": "Mow", "list": "Chores"})
        ok &= check("a refusal is left as it is", "[say]" in out, False)
    finally:
        lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed = saved
        io.denied, io.tools_used, io.failed_calls = None, None, None

    print("\n── after \"Which one\", ask before another trip ──")
    saved = (lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed)
    lib._device_bridge_enabled = lambda: True
    lib._device_tool_allowed = lambda n: True
    io = lib._TURN_IO
    try:
        io.travel_which, io.tools_used, io.failed_calls, io.denied = False, set(), {}, {}
        io.client, io.device_tools = "iphone", lib.PHONE_DEVICE_TOOLS   # travel_time runs on the phone
        lib._device_request = lambda n, a: ("Which one: Dentist (today 12:00 PM, Pearl Dental, on=2026-10-01T12:00); "
                                            "Dentist (tomorrow 9:00 AM, Smile Studio, on=2026-10-02T09:00)")
        out = lib.execute_tool("travel_time", {"destination": "Dentist"})
        ok &= check("the choice carries [ask]", out.endswith("[ask] Ask the user which one, naming each; don't pick for them."), True)
        out = lib.execute_tool("travel_time", {"destination": "Dentist", "on": "2026-10-01T12:00"})
        ok &= check("a second trip in the same turn waits for the answer", out.startswith("Not run: you've just asked which"), True)
        io.travel_which = False
        lib._device_request = lambda n, a: "Pearl Dental (…): 14 min by car."
        ok &= check("the next turn (their answer) runs it", lib.execute_tool("travel_time", {"destination": "Dentist", "on": "2026-10-01T12:00"}),
                    "Pearl Dental (…): 14 min by car.")
    finally:
        lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed = saved
        io.travel_which, io.tools_used, io.failed_calls, io.denied = None, None, None, None
        io.client, io.device_tools = None, None

    print("\n── a failed playback is never claimed (10-01 eval: \"I've started playing Kind of Blue\") ──")
    saved = (lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed)
    lib._device_bridge_enabled = lambda: True
    lib._device_tool_allowed = lambda n: True
    io = lib._TURN_IO
    try:
        io.outcomes, io.failed_calls, io.tools_used, io.denied = [], {}, set(), {}
        lib._device_request = lambda n, a: "Device action was not completed: the eval doesn't run this."
        lib.execute_tool("run_applescript", {"script": 'tell application "Music" to play playlist "Kind of Blue"'})
        out = lib._with_honest_outcome("I've started playing Kind of Blue on your device.")
        ok &= check("a failed AppleScript, then 'started playing' → corrected", "Correction: that didn't go through." in out, True)
        ok &= check("'queued' too", "Correction:" in lib._with_honest_outcome("I've queued Kind of Blue to play."), True)
        ok &= check("a true status after a failed AppleScript is left alone (no bare 'playing')",
                    lib._with_honest_outcome("It's playing on the Living Room TV."), "It's playing on the Living Room TV.")
        ok &= check("… nor 'it was playing'", lib._with_honest_outcome("It was playing Kind of Blue earlier."),
                    "It was playing Kind of Blue earlier.")
        ok &= check("an honest 'couldn't' is left alone",
                    lib._with_honest_outcome("I couldn't start it on the Mac."), "I couldn't start it on the Mac.")
        io.outcomes, io.failed_calls = [], {}
        io.client, io.device_tools = "iphone", lib.PHONE_DEVICE_TOOLS
        lib._device_request = lambda n, a: "Playing the album “Kind of Blue” by Miles Davis on this iPhone."
        lib.execute_tool("play_music", {"query": "Kind of Blue"})
        ok &= check("a real playback is left alone", lib._with_honest_outcome("Playing Kind of Blue on your iPhone."),
                    "Playing Kind of Blue on your iPhone.")
    finally:
        lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed = saved
        io.outcomes, io.failed_calls, io.tools_used, io.denied, io.client, io.device_tools = None, None, None, None, None, None
    ok &= check("the prompt says phone music runs only from the iPhone",
                "can't reach it: say to ask from the iPhone" in lib.build_tool_system_prompt(), True)

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
