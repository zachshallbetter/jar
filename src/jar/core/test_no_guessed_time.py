#!/usr/bin/env python3
"""Never book a time nobody gave (#388; 09-30: "we'll connect again tomorrow" became a 2:00 PM meeting).
A calendar create with a time of day the user's recent words don't contain is refused back to the model.
Run: python3 scripts/test_no_guessed_time.py"""
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(__file__))
os.environ["AGENT_TURN_LOG"] = os.path.join(tempfile.mkdtemp(), "turns.jsonl")
os.environ["AGENT_TURN_SOURCE"] = "test"
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.160} expected={expected!r:.160}")


def refused(said, start):
    lib._TURN_IO.said = said
    try:
        return lib._guessed_time("create_calendar_event", {"title": "Catch up with Tim", "start": start}) is not None
    finally:
        lib._TURN_IO.said = None


print("── refused: a time nobody said ──")
check("'we'll connect again tomorrow' → 2 PM", refused("Tim and I will connect again tomorrow", "2026-10-02T14:00:00"), True)
check("a day but no time", refused("put the dentist on Friday", "2026-10-02T09:00:00"), True)
print("\n── allowed ──")
for said in ("Lunch with Tim tomorrow", "at 3", "3:30 PM tomorrow", "tomorrow at 9am", "Friday 7 p.m.", "after school Thursday",
             "every Wednesday night", "tonight", "noon tomorrow", "9 o'clock", "15:00"):
    check(f"said {said!r}", refused(said, "2026-10-02T14:00:00"), False)
check("a time given two messages back still counts", refused("Dinner with Jordan Friday at 7\nactually make it the patio", "2026-10-03T19:00:00"), False)
check("all-day / date-only is never refused", refused("put the field trip on Friday", "2026-10-02"), False)
check("midnight start (all-day form) isn't a guessed time", refused("field trip Friday", "2026-10-02T00:00:00"), False)
lib._TURN_IO.said = None
check("not a chat turn (scheduled/background): no check", lib._guessed_time("create_calendar_event", {"start": "2026-10-02T14:00:00"}), None)
check("other tools untouched", (setattr(lib._TURN_IO, "said", "hi"), lib._guessed_time("create_reminder", {"due": "2026-10-02T14:00:00"}))[1], None)
lib._TURN_IO.said = None

print("\n── a time the assistant just stated, then asked to book (#610) ──")


def refused_shown(said, shown, start):
    lib._TURN_IO.shown = shown
    try:
        return refused(said, start)
    finally:
        lib._TURN_IO.shown = None


DENTIST = "Your dentist appointment is today at 10:15 AM at Cedar Dental."
check("'That needs to be in my calendar' after '10:15 AM' → booked", refused_shown("That needs to be in my calendar", DENTIST, "2026-10-02T10:15:00"), False)
check("…a different time than the one shown is still refused", refused_shown("That needs to be in my calendar", DENTIST, "2026-10-02T14:00:00"), True)
check("…pm reads as pm", refused_shown("add it", "Practice is at 5:30 pm.", "2026-10-02T17:30:00"), False)
check("no reply before: refused as ever", refused_shown("Tim and I will connect again tomorrow", "", "2026-10-02T14:00:00"), True)

print("\n── a held card's Do it (#610): the person confirmed the card that showed the time ──")
args = {"title": "Dentist", "start": "2026-10-02T10:15:00"}
lib._TURN_IO.said, lib._TURN_IO.confirmed_call = "Yes, do held action h-7c2548a1e118", ("create_calendar_event", lib.json.dumps(args, sort_keys=True))
check("Do it books the held time", lib._guessed_time("create_calendar_event", args), None)
check("…only that exact call", lib._guessed_time("create_calendar_event", {**args, "start": "2026-10-02T11:00:00"}) is not None, True)
lib._TURN_IO.said = lib._TURN_IO.confirmed_call = None

print("\n── wired in: a device create refused before it reaches the Mac ──")
calls = []
orig = lib._device_request_inner
lib._device_request_inner = lambda n, a: calls.append(n) or "Created"
try:
    lib._TURN_IO.said = "Tim and I will connect again tomorrow"
    out = lib._device_request("create_calendar_event", {"title": "Catch up with Tim", "start": "2026-10-02T14:00:00"})
    check("refused, and the Mac was never asked", (out.startswith("Not created: nobody said a time"), calls), (True, []))
finally:
    lib._device_request_inner = orig
    lib._TURN_IO.said = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
