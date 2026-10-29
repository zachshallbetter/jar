#!/usr/bin/env python3
"""Honest turns (09-30 16:00-16:32): a failed call isn't repeated, a reply can't claim a change
that failed, the next turns see what the tools actually did, routines don't promise actions,
and "this is a todo" isn't a screen question. Hermetic. Run: python3 scripts/test_honest_turns.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-honest-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")
    return good


NO_LIST = "Couldn't add the reminder: No calendar has been set."
calls = []


def fake(name, args):
    calls.append(name)
    if name == "create_reminder":
        return NO_LIST if not args.get("list") else f"Added reminder '{args['title']}' to the {args['list']} list."
    if name == "search_notes":
        return 'Notes didn\'t answer: 875:879: execution error: Notes got an error: Can’t set body to "". (-10006)'
    if name == "list_reminders":
        return "No incomplete reminders."
    return "ok"


saved = lib._execute_tool
lib._execute_tool = fake
os.environ["AGENT_TURN_SOURCE"] = "user"
try:
    def turn():
        lib._TURN_IO.outcomes, lib._TURN_IO.failed_calls = [], {}
        lib._TURN_IO.person, lib._TURN_IO.role = {"id": "zach", "name": "Zach"}, "admin"

    print("── a failed call isn't sent again ──")
    turn()
    a = {"title": "Remind Ava to get ready for ballet", "due": "2026-09-30T16:30:00"}
    check("first try reaches the Mac", lib.execute_tool("create_reminder", a), NO_LIST)
    r = lib.execute_tool("create_reminder", dict(a))
    check("the identical retry is refused", (r.startswith("Not run: this exact call already failed"), calls.count("create_reminder")), (True, 1))
    check("a changed call goes through", lib.execute_tool("create_reminder", {**a, "list": "Family"}),
          "Added reminder 'Remind Ava to get ready for ballet' to the Family list.")
    check("reads aren't blocked when they succeed twice", (lib.execute_tool("list_reminders", {}), lib.execute_tool("list_reminders", {})),
          ("No incomplete reminders.", "No incomplete reminders."))

    print("\n── a reply can't claim a change that failed ──")
    turn()
    lib.execute_tool("create_reminder", a)
    out = lib._with_honest_outcome("I've re-created the reminder \"Remind Ava\" in your Family list.")
    check("false claim gets a correction", ("Correction: that didn't go through" in out, NO_LIST in out), (True, True))
    check("an honest failure is left alone", lib._with_honest_outcome("I couldn't add the reminder: no default list."),
          "I couldn't add the reminder: no default list.")
    lib.execute_tool("create_reminder", {**a, "list": "Family"})
    check("one success: the claim stands", lib._with_honest_outcome("I've added it to your Family list."), "I've added it to your Family list.")
    turn()
    check("no writes this turn: nothing to correct", lib._with_honest_outcome("I've sent it."), "I've sent it.")

    print("\n── a promise to watch needs something set up to watch (#617) ──")
    turn()
    said = "I'll keep an eye out for any location triggers or arrival events as you move around."
    out = lib._with_kept_promises(said)
    check("no tool ran: the promise gets a correction", (out.startswith(said), "nothing is watching for that" in out), (True, True))
    for s in ("I'll let you know when the build finishes.", "I will monitor the garage door tonight.",
              "I'll notify you if Ava's ballet moves.", "I'll be watching for your arrival."):
        check(f"caught: {s[:32]}", "Correction:" in lib._with_kept_promises(s), True)
    for s in ("Let me know when you get home.", "I'll check the calendar now.", "You can watch for it on the card.",
              "I can let you know when you arrive if you want."):
        check(f"left alone: {s[:32]}", lib._with_kept_promises(s), s)
    lib.execute_tool("search_notes", {"query": "ballet"})
    check("a read doesn't back it", "Correction:" in lib._with_kept_promises(said), True)
    lib._record_outcome("schedule_action", {"place": "home", "on": "arrive"}, "Scheduled: Turn on the porch light (sa-1)")
    check("a scheduled action made this turn backs it", lib._with_kept_promises("I'll let you know when you're home."),
          "I'll let you know when you're home.")
    turn()
    lib._record_outcome("run_check", {"name": "jellybean-build"}, "Started jellybean-build on the Mac; the result comes back as an event.")
    check("a run_check made this turn backs it (its result comes back)",
          lib._with_kept_promises("I'll let you know when the build finishes."), "I'll let you know when the build finishes.")
    turn()
    lib._record_outcome("send_to_claude", {"text": "x"}, "Sent to Claude.")
    check("a handoff to Claude doesn't (nothing comes back to titan)", "Correction:" in lib._with_kept_promises("I'll let you know when it's fixed."), True)
    turn()
    lib._record_outcome("schedule_action", {"at": "5 PM"}, "Not scheduled: no such device")
    check("a failed schedule doesn't", "Correction:" in lib._with_kept_promises("I'll let you know at 5."), True)

    print("\n── a claim that it's scheduled needs a scheduling tool this turn (#748) ──")
    turn()
    said = "I've scheduled a recurring action to text Ava to get ready for ballet every weekday at 4:30 PM."
    out = lib._with_kept_schedules(said)
    check("no tool ran: the claim gets a correction", (out.startswith(said), "I didn't set that up" in out), (True, True))
    for s2 in ("I've set up a reminder for 8 AM.", "It's scheduled for Friday at 5.", "I'll text Robin at 4:30 that dinner is at 6.",
               "I'll turn off the lights at 11 PM.", "I have arranged for the porch light to come on when you get home."):
        check(f"caught: {s2[:34]}", "Correction:" in lib._with_kept_schedules(s2), True)
    for s2 in ("Want me to schedule it?", "I can text Robin at 4:30 if you like.", "Your dentist is scheduled for 9 AM.",
               "I'll check the calendar now.", "Say when, and I'll set it up."):
        check(f"left alone: {s2[:34]}", lib._with_kept_schedules(s2), s2)
    lib.execute_tool("search_notes", {"query": "ballet"})
    check("a read doesn't back it", "Correction:" in lib._with_kept_schedules(said), True)
    turn()
    lib._record_outcome("schedule_action", {"at": "4:30 PM", "repeat": "every weekday"},
                        "Test turn: scheduled “Text Ava to get ready for ballet” every weekday, starting today at 4:30 PM.")
    check("true: a repeating schedule_action ran, the reply is untouched", lib._with_kept_schedules(said), said)
    turn()
    lib._record_outcome("schedule_action", {"at": "4:30 PM"}, "Scheduled “Text Ava” today at 4:30 PM. It's a card in Jellybean.")
    out = lib._with_kept_schedules(said)
    check("a once-only schedule_action under a claim that it repeats: corrected", "not to repeat" in out, True)
    check("…and the same claim without repeating words stands", lib._with_kept_schedules("I've scheduled a text to Ava at 4:30 PM."),
          "I've scheduled a text to Ava at 4:30 PM.")
    turn()
    lib._record_outcome("create_reminder", {"title": "Call the vet"}, "Created reminder “Call the vet”, repeats every week.")
    check("a repeating reminder backs 'every week'", lib._with_kept_schedules("I've set up a reminder to call the vet every week."),
          "I've set up a reminder to call the vet every week.")
    turn()
    lib._record_outcome("save_routine", {"name": "Ballet"}, "Saved routine “Ballet” with 2 steps, nudging you at 16:30.")
    check("a routine backs a daily claim (it repeats by itself)", lib._with_kept_schedules("I've set up a daily routine for ballet."),
          "I've set up a daily routine for ballet.")
    turn()
    lib._record_outcome("create_calendar_event", {"title": "Haircut"}, "Created event “Haircut” Sunday 10 AM.")
    check("a calendar event backs 'scheduled'", lib._with_kept_schedules("I've scheduled your haircut for Sunday at 10."),
          "I've scheduled your haircut for Sunday at 10.")
    turn()
    lib._record_outcome("schedule_action", {"at": "5 PM"}, "Not scheduled: no such device")
    check("a failed schedule doesn't back it", "I didn't set that up" in lib._with_kept_schedules("I've scheduled it for 5 PM."), True)

    print("\n── a fact the world model didn't record is never reported as updated (#814) ──")
    turn()
    lib._record_outcome("set_fact", {"entity": "Willow Creek", "attribute": "phone"},
                        "Not recorded: I don't have Willow Creek on record yet. Is it a person, a pet, a place or business, "
                        "or an activity?")
    out = lib._with_honest_outcome("I've updated Willow Creek's phone number to (509) 555-0171.")
    check("set_fact said Not recorded: the reply gets the correction", ("Correction: that didn't go through" in out,
          "Not recorded" in out), (True, True))
    turn()
    lib._record_outcome("set_fact", {"entity": "Willow Creek"}, "Recorded (new: Willow Creek). Willow Creek, phone: 555-0171")
    check("…and a fact it did record stands", lib._with_honest_outcome("I've updated Willow Creek's phone number."),
          "I've updated Willow Creek's phone number.")
    check("a reply that already corrects itself is left alone",
          lib._with_kept_schedules("I've scheduled it.\n\nCorrection: that didn't go through."),
          "I've scheduled it.\n\nCorrection: that didn't go through.")

    print("\n── the next turns see what the tools did ──")
    lib._RECENT_OUTCOMES.clear()
    turn()
    lib.execute_tool("create_reminder", a)
    lib.execute_tool("create_reminder", {"title": "x"})
    lib.execute_tool("search_notes", {"query": "ballet"})
    lib.execute_tool("list_reminders", {})
    lib._remember_outcomes()
    line = lib._recent_outcomes_line()
    check("failures are listed, with the tool", ("create_reminder FAILED ×2" in line or "create_reminder FAILED" in line,
                                                 "search_notes FAILED" in line, "No calendar has been set" in line), (True, True, True))
    check("a successful read isn't", "list_reminders" in line, False)
    check("it says what the user means by 'the errors'", '"the errors"' in line, True)
    check("it reaches the per-turn context", "EARLIER IN THIS CONVERSATION" in lib._situational_context_inner("what caused the recent errors?"), True)
    for _ in range(4):
        turn(); lib.execute_tool("create_reminder", {"title": "later", "list": "Family"}); lib._remember_outcomes()
    check("only the last 3 turns are kept", len(lib._RECENT_OUTCOMES["zach"]), 3)
    lib._TURN_IO.person = {"id": "p_robin", "name": "Robin"}
    check("each person sees only their own", lib._recent_outcomes_line(), None)
    os.environ["AGENT_TURN_SOURCE"] = "eval"
    check("eval turns never record or see them", lib._recent_outcomes_line(), None)
    os.environ["AGENT_TURN_SOURCE"] = "user"
finally:
    lib._execute_tool = saved
    lib._TURN_IO.outcomes = lib._TURN_IO.failed_calls = lib._TURN_IO.person = lib._TURN_IO.role = None

print("\n── titan's own deployed code is never written from chat ──")
lib._DEPLOYED_CHECKOUTS = (os.path.realpath(str(_TMP / "deployed")) + "/",)
(_TMP / "deployed" / "scripts").mkdir(parents=True)
r = lib.execute_tool("file_write", {"path": str(_TMP / "deployed" / "scripts" / "check_findmy.sh"), "content": "#!/bin/bash"})
check("file_write into a deployed checkout is refused", (r.startswith("Not written: that's inside titan's own deployed code"),
      (_TMP / "deployed" / "scripts" / "check_findmy.sh").exists()), (True, False))
check("…and file_edit", lib.execute_tool("file_edit", {"path": str(_TMP / "deployed" / "x.py"), "old_string": "a", "new_string": "b"}).startswith("Not written"), True)
check("a path that only starts the same isn't", lib._in_deployed_checkout(str(_TMP / "deployed-other" / "f")), False)
check("file_write counts as a change the next turns see", "file_write" in lib._WRITE_TOOLS, True)

print("\n── the right machine ──")
check("titan's file_write refuses a Mac path", lib.execute_tool("file_write", {"path": "~/Scripts/check_findmy.sh", "content": "x"}).startswith("Not done: ~/Scripts/check_findmy.sh is a path on the user's Mac"), True)
check("…and /Users/", lib._wrong_machine("file_read", "/Users/zach/Downloads/a.pdf") is not None, True)
check("the Mac's write_file refuses a titan path", lib.execute_tool("write_file", {"path": "/ai/projects/x.sh", "content": "x"}).startswith("Not done: /ai/projects/x.sh is a path on titan"), True)
check("an ordinary titan path is fine for titan's tools", lib._wrong_machine("file_read", "/ai/logs/agent-turns.jsonl"), None)
check("an ordinary Mac path is fine for the Mac's tools", lib._wrong_machine("read_file", "~/Downloads/a.pdf"), None)
check("titan's file tools say where they run", lib._TOOL_REGISTRY["file_write"]["description"].startswith("ON TITAN"), True)
check("the Mac's say where they run", lib._DEVICE_TOOLS["write_file"]["description"].startswith("ON THE USER'S MAC"), True)
saved_device = getattr(lib._TURN_IO, "device", None)
lib._TURN_IO.device = lambda req: {"result": ""}   # a device on the line, as the HTTP API sets it
lib._TURN_IO.role, lib._TURN_IO.person, lib._TURN_IO.client = "admin", {"id": "zach", "name": "Zach"}, "iphone"
m = lib._machines_line() or ""
check("the machine map on a phone turn", ("[MACHINES]" in m, "Zach's iPhone, this conversation" in m, "Your Mac" in m), (True, True, True))
lib._TURN_IO.client = None
m = lib._machines_line() or ""
check("…and on a Mac turn", ("Zach's Mac, this conversation" in m, "create_skill" in m), (True, True))
lib._TURN_IO.role = "kid"
check("a kid's turn has no machine map", lib._machines_line(), None)
lib._TURN_IO.role = lib._TURN_IO.person = None
lib._TURN_IO.device = saved_device
check("the gap protocol is in the prompt (declared routes, 10-01)", ("What you can build: nearly anything, through a declared route" in lib.AGENT_IDENTITY, "never write scripts for the Mac onto titan" in lib.AGENT_IDENTITY), (True, True))

print("\n── try once means once ──")
lib._TURN_IO.outcomes, lib._TURN_IO.failed_calls = [], {}
for i in range(2):
    lib._TURN_IO.outcomes.append({"tool": "run_shell", "ok": True, "write": False, "text": "ok"})
check("a 3rd shell call in a turn isn't run", (lib._try_limit("run_shell", {"command": "lpstat -p"}) or "").startswith("Not run: that would be shell/AppleScript call 3"), True)
check("…AppleScript counts too", lib._try_limit("run_applescript", {"script": "x"}) is not None, True)
check("other tools aren't limited", lib._try_limit("list_files", {"path": "~"}), None)
lib._TURN_IO.outcomes = [{"tool": "add_todo", "ok": True, "write": True, "text": "Added “X” to Claude's list. Within 10 minutes…"}]
check("send_to_claude after add_todo for=claude is skipped", (lib._try_limit("send_to_claude", {"text": "X"}) or "").startswith("Not sent: it's already on Claude's list"), True)
lib._TURN_IO.outcomes = [{"tool": "add_todo", "ok": True, "write": True, "text": "Added “X” to your list."}]
check("…but not after a personal todo", lib._try_limit("send_to_claude", {"text": "X"}), None)
lib._TURN_IO.outcomes = None
check("outside a turn nothing is limited", lib._try_limit("run_shell", {"command": "ls"}), None)
lib._TURN_IO.failed_calls = None

print("\n── what's on the TVs comes from Home Assistant ──")
import home_assistant as _ha
_saved_http = _ha._http
_ha._http = lambda m, p, b: [
    {"entity_id": "media_player.living_room_living_room", "state": "playing",
     "attributes": {"friendly_name": "Living Room", "media_title": "Bluey", "app_name": "TV"}},
    {"entity_id": "media_player.main_bedroom", "state": "idle", "attributes": {"friendly_name": "Main Bedroom"}},
    {"entity_id": "light.porch", "state": "on", "attributes": {}}]
check("the TVs line", lib._media_players_line(), "TVs and speakers (Home Assistant, live): Living Room: playing “Bluey” (TV); Main Bedroom: idle")
def _down(m, p, b):
    raise OSError("no token")
_ha._http = _down
check("HA down: no line", lib._media_players_line(), None)
_ha._http = _saved_http

print("\n── routines, later actions, code paths, the screen ──")
check("save_routine never promises to act", "never tell the user it will" in lib._TOOL_REGISTRY["save_routine"]["description"], True)
check("the prompt says it can act later, with a card to cancel", ("can do one thing at a later time by yourself with "
      "schedule_action" in lib.AGENT_IDENTITY, "card the user can cancel" in lib.AGENT_IDENTITY), (True, True))
check("…and no longer that it can't", "can't do something at a later time" in lib.AGENT_IDENTITY, False)
check("a promise is honest only when scheduled", "unless you scheduled it" in lib.AGENT_IDENTITY, True)
check("live state only from a live check", "Live state only from a live check" in lib.AGENT_IDENTITY, True)
check("read before describing; exact links", ("Read before you describe" in lib.AGENT_IDENTITY,
      "never the site's homepage" in lib.AGENT_IDENTITY), (True, True))
check("a dedicated tool's answer stands", "that is the answer: say it" in lib.AGENT_IDENTITY, True)
check("the prompt keeps tool names out of replies", "never show the user your tool names" in lib.AGENT_IDENTITY, True)
check("the prompt says where the code is", "~/Projects/jellybean" in lib.AGENT_IDENTITY and "/ai/projects/titan-node" in lib.AGENT_IDENTITY, True)
check("'this is a todo' isn't a screen question", lib._wants_screen("This is a todo: fix the garage light."), False)
check("'is this right?' still is", lib._wants_screen("is this right?"), True)
check("'what's this' still is", lib._wants_screen("what's this error"), True)
check("'fix the build' still is", lib._wants_screen("can you fix the build"), True)
check("'fix the garage light' isn't", lib._wants_screen("remind me to fix the garage light"), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
