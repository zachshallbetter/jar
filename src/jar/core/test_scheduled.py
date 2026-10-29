#!/usr/bin/env python3
"""Scheduled actions (scheduled.py, #263): reading times, what can't be scheduled, the card, running
each action once as its owner, failures, cancelling, privacy, a crash mid-run, the tools and the API.
Hermetic: temp stores, fake device hooks; nothing reaches a Mac or Home Assistant.
Run: python3 scripts/test_scheduled.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-scheduled-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "SCHEDULED_DB": "scheduled.db", "PUSH_DB": "push.db", "KB_ACL_DB": "kb_acl.db",
             "PLACES_DB": "places.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha-token")
os.environ.pop("TITAN_DISCOVERY", None)
os.environ.pop("GEMINI_API_KEY", None)
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import cross_device as xd  # noqa: E402
import household_identity as hh  # noqa: E402
import local_time  # noqa: E402
import proactive  # noqa: E402
import scheduled as sc  # noqa: E402

# A pinned household clock (#431): 10:00 AM today, running forward from there. Cases like "11 PM", "in 1 hour"
# and "today at 1 PM" refused as past then read the same at any hour, so a deploy at 1 PM can't flake.
_T0 = time.time()
_BASE = local_time.now().replace(hour=10, minute=0, second=0, microsecond=0).timestamp()


def clock() -> float:
    return _BASE + (time.time() - _T0)


sc.time = type("PinnedTime", (), {"time": staticmethod(clock)})
local_time.now = lambda name=None: datetime.fromtimestamp(clock(), local_time.zone(name))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")
    return good


def as_(role, pid=None, name=None):
    lib._TURN_IO.role = role
    lib._TURN_IO.person = {"id": pid, "name": name} if pid else None


def cards_for(pid, role):
    return proactive.list_cards(include_acked=True, person=pid, role=role)


store = hh.HouseholdStore(_TMP / "people.db")
hh.reset_store(store)
rob = store.add_person("Robin", "adult")["id"]
leo = store.add_person("Leo", "kid")["id"]
store.record_presence("zach", None, xd.clean_presence({"device_key": "mac-zach-1", "name": "Zach's Mac",
                                                       "lan_ip": "10.0.0.20", "wake_port": 50000}))
T = lib.execute_tool

print("── reading times ──")
TZ = local_time.zone()
NOW = datetime(2026, 10, 1, 14, 5, tzinfo=TZ)   # Thursday 2:05 PM
fmt = lambda s: sc.parse_when(s, NOW).strftime("%a %H:%M")   # noqa: E731
for text, want in [("in 30 minutes", "Thu 14:35"), ("in an hour", "Thu 15:05"), ("in 2 hours and 15 minutes", "Thu 16:20"),
                   ("at 4:30 PM", "Thu 16:30"), ("11 PM", "Thu 23:00"), ("tonight at 11", "Thu 23:00"),
                   ("tomorrow at 7am", "Fri 07:00"), ("Friday at 5 PM", "Fri 17:00"), ("next thursday at 9", "Thu 09:00"),
                   ("2026-10-01T23:00", "Thu 23:00"), ("at 4", "Thu 16:00"), ("09:00", "Fri 09:00"), ("noon", "Fri 12:00"),
                   ("11 pm tomorrow", "Fri 23:00"), ("4 p.m.", "Thu 16:00")]:
    check(f"“{text}”", fmt(text), want)
check("an ISO time with an offset is converted", sc.parse_when("2026-10-01T23:00:00-04:00", NOW).strftime("%H:%M"), "20:00")
for text in ("at 1", "tomorrow at 3", "Friday at 5"):   # #382: a bare small hour that lands at night asks
    try:
        check(f"“{text}” asks AM or PM", sc.parse_when(text, NOW).strftime("%a %H:%M"), "asks")
    except ValueError as exc:
        check(f"“{text}” asks AM or PM", "AM or" in str(exc), True)
check("…but “at 1 PM”, “at 7” and “at 4” don't", (sc.parse_when("at 1 PM", NOW).strftime("%a %H:%M"),
      sc.parse_when("tomorrow at 7", NOW).strftime("%a %H:%M"), sc.parse_when("at 4", NOW).strftime("%a %H:%M")),
      ("Fri 13:00", "Fri 07:00", "Thu 16:00"))
check("the Cancel button's words are recognised exactly", (sc.asked_cancel("Cancel scheduled action sa-0123abcd"),
      sc.asked_cancel("cancel scheduled action SA-0123ABCD."), sc.asked_cancel("please cancel scheduled action sa-0123abcd")),
      ("sa-0123abcd", "sa-0123abcd", None))
for text, why in [("today at 1 PM", "1:00 PM today has already passed"), ("tomorrow", "what time tomorrow?"),
                  ("2026-10-02", "that's a date without a time of day"), ("25:00", "can't read the time “25:00”"),
                  ("in the morning", "can't read the time “in the morning”"), ("", "no time given")]:
    try:
        sc.parse_when(text, NOW)
        check(f"“{text}” refused", "parsed", why)
    except ValueError as exc:
        check(f"“{text}” refused", str(exc), why)
check("when, in words", (sc.when_words(sc.parse_when("11 PM", NOW).timestamp(), NOW),
                         sc.when_words(sc.parse_when("tomorrow at 7am", NOW).timestamp(), NOW),
                         sc.when_words(sc.parse_when("Oct 9 at 9:00", NOW).timestamp(), NOW)),
      ("today at 11:00 PM", "tomorrow at 7:00 AM", "Fri Oct 9 at 9:00 AM"))

print("\n── what can't be scheduled ──")
LAMP = {"device": "Living Room Lamp", "action": "off"}
R = lambda tool, args, at="in 30 minutes", owner="zach", role="admin": sc.schedule(   # noqa: E731
    tool, args, at, None, owner=owner, role=role)[1]
check("in the past", (R("home_control", LAMP, "2020-01-01T09:00").startswith("Not scheduled: "),
                      R("home_control", LAMP, "2020-01-01T09:00").endswith("has already passed.")), (True, True))
check("more than 30 days away", R("home_control", LAMP, "in 31 days").startswith("Not scheduled: that's more than 30 days"), True)
check("an unreadable time", R("home_control", LAMP, "whenever").startswith("Not scheduled: can't read the time"), True)
check("a lock", R("home_control", {"device": "Front Door Lock", "action": "lock"}), sc.HANDS_OFF)
check("unlocking, by any name", R("home_control", {"device": "front door", "action": "unlock"}), sc.HANDS_OFF)
check("a lock by entity id", R("home_control", {"device": "lock.front_door", "action": "on"}), sc.HANDS_OFF)
check("the garage", R("home_control", {"device": "Garage Door", "action": "open"}), sc.HANDS_OFF)
check("a camera", R("home_control", {"device": "camera.porch", "action": "off"}), sc.HANDS_OFF)
check("a Shortcut that unlocks", R("run_shortcut", {"name": "Unlock the door"}), sc.HANDS_OFF)
check("an unknown tool", R("teleport", {}), "Not scheduled: there's no action called teleport.")
check("no tool", R("", {}), "Not scheduled: say which action to run (tool_name).")
check("not itself", R("schedule_action", {}), "Not scheduled: a scheduled action can't schedule or cancel others.")
check("a kid: not texting (role)", R("send_message", {"to": "Ava", "text": "hi"}, owner=leo, role="kid"),
      "Not scheduled: " + lib._ROLE_REFUSAL)
check("a kid: not the shell", R("run_shell", {"command": "ls"}, owner=leo, role="kid"), "Not scheduled: " + lib._ROLE_REFUSAL)
check("iPhone-only actions", R("play_music", {"query": "Bluey"}).startswith("Not scheduled: that runs on the iPhone itself"), True)
check("a Mac action without a Mac", R("send_message", {"to": "Ava", "text": "hi"}, owner=rob, role="adult")
      .startswith("Not scheduled: that runs on the Mac, and titan has never connected"), True)
check("nothing was stored", sc.list_for(None, None, include_done=True), [])

print("\n── scheduling: the row and its card ──")
row, reply = sc.schedule("home_control", LAMP, "in 30 minutes", "Turn off the living room lamp", owner="zach", role="admin")
check("the reply says what and when", reply.startswith("Scheduled “Turn off the living room lamp” for today at")
      or reply.startswith("Scheduled “Turn off the living room lamp” for tomorrow at"), True)
check("…and where to cancel it", "a card in Jellybean, where you can cancel it" in reply, True)
check("the row", (row["state"], row["owner"], row["audience"], row["tool"], row["args"], row["card_key"]),
      ("scheduled", "zach", "private", "home_control", LAMP, f"sched-{row['id']}"))
check("it's 30 minutes out", abs(row["run_at"] - (clock() + 1800)) < 5, True)
card = next(c for c in cards_for("zach", "admin") if c["key"] == row["card_key"])
check("the card: title, audience, key", (card["title"], card["to"], card["source"]),
      ("Scheduled: Turn off the living room lamp", '["zach"]', "scheduled"))
check("the card says when", card["detail"].startswith(("Today at", "Tomorrow at")), True)
check("its Cancel button asks titan", [(a["label"], a["kind"], a["value"]) for a in card["actions"]],
      [("Cancel", "prompt", f"Cancel scheduled action {row['id']}")])
check("Robin doesn't see the card", any(c["key"] == row["card_key"] for c in cards_for(rob, "adult")), False)
mrow, _ = sc.schedule("home_control", {"device": "Porch Light", "action": "on"}, "in 2 hours", "Porch light on",
                      owner=rob, role="adult")
crow, _ = sc.schedule("home_control", {"device": "Leo's Lamp", "action": "off"}, "in 40 minutes", None,
                      owner=leo, role="kid")
check("a kid may schedule what a kid may do", (crow["state"], crow["what"]), ("scheduled", "off Leo's Lamp"))

print("\n── privacy: who sees which ──")
ids = lambda rows: sorted(r["id"] for r in rows)   # noqa: E731
check("Zach sees his own only", ids(sc.list_for("zach", "admin")), [row["id"]])
check("Robin sees hers only", ids(sc.list_for(rob, "adult")), [mrow["id"]])
check("Leo sees his only", ids(sc.list_for(leo, "kid")), [crow["id"]])
check("titan itself sees all", len(sc.list_for(None, None)), 3)

print("\n── cancelling ──")
for who, role in ((rob, "adult"), (leo, "kid")):
    try:
        sc.cancel(row["id"], who, role)
        check(f"{who} can't cancel Zach's", "cancelled", "refused")
    except (LookupError, PermissionError) as exc:
        check(f"{who} can't cancel Zach's", type(exc).__name__, "LookupError")
check("…still scheduled", sc.get(row["id"])["state"], "scheduled")
done = sc.cancel(mrow["id"], rob, "adult")
check("Robin cancels her own", (done["state"], sc.get(mrow["id"])["state"]), ("cancelled", "cancelled"))
mcard = next(c for c in cards_for(rob, "adult") if c["key"] == mrow["card_key"])
check("…and her card says so (same card)", mcard["title"], "Cancelled: Porch light on")
try:
    sc.cancel(mrow["id"], rob, "adult")
    check("cancelling twice", "ok", "ValueError")
except ValueError as exc:
    check("cancelling twice", str(exc), "“Porch light on” was already cancelled.")
c2, _ = sc.schedule("home_control", {"device": "Leo's Fan", "action": "off"}, "in 1 hour", "Fan off", owner=leo, role="kid")
check("the admin may cancel a kid's", sc.cancel(c2["id"], "zach", "admin")["state"], "cancelled")

print("\n── running: once, as the owner, and the same card ──")
runs = []


def fake_exec(r, role, name):
    runs.append((r["id"], role, name))
    return True, "Done: Living Room Lamp → turn off."


later = crow["run_at"] + 60   # just after both are due (much later would be too late to run)
res = sc.run_due(later, execute=fake_exec)
check("both due actions ran", sorted(x["id"] for x in res), sorted([row["id"], crow["id"]]))
check("as their owners, with their roles", sorted((r[0], r[1], r[2]) for r in runs),
      sorted([(row["id"], "admin", store.person("zach")["name"]), (crow["id"], "kid", "Leo")]))
check("marked done with the result", (sc.get(row["id"])["state"], sc.get(row["id"])["result"]),
      ("done", "Done: Living Room Lamp → turn off."))
check("never twice", sc.run_due(later + 60, execute=fake_exec), [])
check("…executed exactly twice in all", len(runs), 2)
zc = [c for c in cards_for("zach", "admin") if c["key"] == row["card_key"]]
check("one card, updated in place", (len(zc), zc[0]["id"] == card["id"], zc[0]["title"]),
      (1, True, "Done: Turn off the living room lamp"))
check("…with the one-line result", zc[0]["detail"].startswith("Done: Living Room Lamp → turn off."), True)
check("a cancelled one never runs", sc.get(mrow["id"])["state"], "cancelled")

print("\n── running for real (execute_tool as the owner) ──")
r1 = sc.add("zach", "Count letters", "count_chars", {"text": "hello world"}, clock() - 5)
r2 = sc.add(leo, "titan's files as a kid", "file_list", {"path": "/"}, clock() - 5)   # stored anyway: the run checks again
as_("adult", rob, "Robin")   # whatever turn this thread had is left alone
out = {x["id"]: x for x in sc.run_due()}
check("a titan tool ran", (out[r1["id"]]["state"], out[r1["id"]]["result"].startswith("Total letters (excluding spaces): 10")),
      ("done", True))
check("the role is checked again at run time", (out[r2["id"]]["state"], out[r2["id"]]["result"]), ("failed", lib._ROLE_REFUSAL))
check("the thread's identity is restored", (lib._TURN_IO.role, lib._TURN_IO.person), ("adult", {"id": rob, "name": "Robin"}))
as_(None)

print("\n── a Mac action: through the device hook ──")
seen = []


def hook_says(resp):
    def device(owner, sid):
        def hook(req):
            seen.append((owner, sid, req["tool"]))
            return resp
        return hook
    return device


m1 = sc.add("zach", "Text Ava dinner is at 6", "send_message", {"to": "Ava", "text": "Dinner is at 6"}, clock() - 5)
out = sc.run_due(device=hook_says({"result": "Sent to Ava."}))
check("it went to the owner's Mac", seen, [("zach", m1["id"], "send_message")])
check("done", (out[0]["state"], sc.get(m1["id"])["result"]), ("done", "Sent to Ava."))
m2 = sc.add("zach", "Text Ava again", "send_message", {"to": "Ava", "text": "x"}, clock() - 5)
out = sc.run_due(device=hook_says({"result": xd.NO_WAKE, "completed": False}))
check("a Mac that didn't wake: failed, with why", (out[0]["state"], out[0]["result"]), ("failed", xd.NO_WAKE))
fc = next(c for c in cards_for("zach", "admin") if c["key"] == m2["card_key"])
check("…its card says so and reaches the phone", (fc["title"], fc["urgency"], fc["kind"]),
      ("Couldn't Text Ava again", 2, "alert"))
check("…with a Do it now button", [(a["label"], a["kind"], a["value"]) for a in fc["actions"]][0],
      ("Do it now", "prompt", "Do this now: Text Ava again"))

# mac_hook itself: no Mac → NO_MAC; the Mac wants approval → not done (nobody was there).
xd.reset()
check("mac_hook: no Mac known", sc.mac_hook(rob, "sa-x")({"tool": "send_message", "args": {}}), {"error": sc.NO_MAC})
_route = xd.route


def asks(req, **kw):
    kw["ask_confirm"]({"confirm_id": "c1", "summary": "Send a text to Ava"})
    return {"result": xd.DECLINED, "completed": False}


xd.route = asks
check("mac_hook: the Mac asks → not done, said plainly", sc.mac_hook("zach", "sa-y")({"tool": "send_message", "args": {}}),
      {"result": "Not done: " + sc.NEEDS_OK, "completed": False})
xd.route = lambda req, **kw: {"result": "Sent."}
check("mac_hook: the Mac just does it (trusted there)", sc.mac_hook("zach", "sa-z")({"tool": "send_message", "args": {}}),
      {"result": "Sent."})
xd.route = _route
m3 = sc.add("zach", "Text Ava late", "send_message", {"to": "Ava", "text": "x"}, clock() - 5)
check("the timer's process leaves a Mac action for the API", (sc.run_due(), sc.get(m3["id"])["state"]), ([], "scheduled"))
out = sc.run_due(clock() + sc.LATE_S + 60)
check("…and fails it once it's too late (the API was down)", (sc.get(m3["id"])["state"], out[0]["result"].startswith(
      "titan wasn't running then")), ("failed", True))

print("\n── a failure ──")
f1 = sc.add("zach", "Thermostat to 68", "home_control", {"device": "Thermostat", "action": "set_temperature", "value": 68},
            clock() - 5)
out = sc.run_due(execute=lambda r, ro, nm: (False, "Couldn't reach Home Assistant: timed out"))
check("marked failed", (sc.get(f1["id"])["state"], sc.get(f1["id"])["result"]),
      ("failed", "Couldn't reach Home Assistant: timed out"))
fc = next(c for c in cards_for("zach", "admin") if c["key"] == f1["card_key"])
check("the card: Couldn't …: why", (fc["title"], fc["detail"]),
      ("Couldn't Thermostat to 68", "Couldn't reach Home Assistant: timed out"))
f2 = sc.add("zach", "Boom", "count_chars", {"text": "x"}, clock() - 5)


def boom(r, ro, nm):
    raise RuntimeError("kaboom")


out = sc.run_due(execute=boom)
check("a crash in one action fails it, not the runner", (out[0]["state"], out[0]["result"]),
      ("failed", "it failed (RuntimeError: kaboom)"))
gone = sc.add("ghost", "Ghost's action", "count_chars", {"text": "x"}, clock() - 5)
check("an owner no longer in the household", (sc.run_due(execute=fake_exec)[0]["result"]),
      "whoever scheduled it is no longer in the household.")

print("\n── a crash mid-run: never re-run ──")
k1 = sc.add("zach", "Half done", "count_chars", {"text": "x"}, clock() - 5)
sc._transition(k1["id"], ("scheduled",), "running", runner=f"{__import__('socket').gethostname()}:999999")
k2 = sc.add("zach", "Long ago", "count_chars", {"text": "x"}, clock() - 5)
sc._transition(k2["id"], ("scheduled",), "running", runner="otherhost:1")
runs.clear()
sc.run_due(execute=fake_exec)
check("a dead runner's row: failed, not run", (sc.get(k1["id"])["state"], runs), ("failed", []))
check("…saying what's known", sc.get(k1["id"])["result"].startswith("titan stopped while it was running"), True)
check("another host's fresh row is left alone", sc.get(k2["id"])["state"], "running")
sc.reap(now=clock() + sc.STALE_RUNNING_S + 60)
check("…until it's far too old", sc.get(k2["id"])["state"], "failed")

print("\n── two runners at once ──")
runs.clear()
for i in range(5):
    sc.add("zach", f"Race {i}", "count_chars", {"text": "x"}, clock() - 5)


def slow(r, ro, nm):
    time.sleep(0.05)
    return fake_exec(r, ro, nm)


ths = [threading.Thread(target=sc.run_due, kwargs={"execute": slow}) for _ in range(4)]
[t.start() for t in ths]
[t.join() for t in ths]
check("each ran exactly once", sorted(r[0] for r in runs) == sorted(set(r[0] for r in runs)) and len(runs) == 5, True)

print("\n── the tools ──")
as_("admin", "zach", "Zach")
out = T("schedule_action", {"tool_name": "home_control", "tool_args": '{"device": "Kitchen Lights", "action": "off"}',
                            "at": "in 45 minutes", "what": "Turn off the kitchen lights"})
check("schedule_action (args as a JSON string)", out.startswith("Scheduled “Turn off the kitchen lights” for"), True)
check("…no tool names in the reply", "home_control" in out or "schedule_action" in out, False)
out = T("schedule_action", {"tool_name": "home_control", "tool_args": "not json", "at": "in 45 minutes"})
check("bad tool_args", out.startswith("Not scheduled: tool_args must be a JSON object"), True)
check("a lock, through the tool", T("schedule_action", {"tool_name": "home_control", "at": "11 PM",
                                                      "tool_args": {"device": "Back Door Lock", "action": "lock"}}), sc.HANDS_OFF)
lst = T("list_scheduled", {})
check("list_scheduled shows it", "Turn off the kitchen lights" in lst and "(id sa-" in lst, True)
check("…and how recent ones went", "failed: Couldn't reach Home Assistant: timed out" in lst, True)
check("…pending first", lst.startswith("• ") and "Turn off the kitchen lights" in lst.splitlines()[0], True)
check("cancel_scheduled by what it does", T("cancel_scheduled", {"id": "kitchen lights"}).startswith(
      "Cancelled “Turn off the kitchen lights”"), True)
check("nothing like that", T("cancel_scheduled", {"id": "zzz"}).startswith("Not cancelled: no pending"), True)
# 10-03 audit (#802's shape): "make it 4 hours instead" scheduled a second action and both ran.
DEN = {"device": "Den Lamp", "action": "off"}
first = T("schedule_action", {"tool_name": "home_control", "tool_args": DEN, "at": "in 2 hours", "what": "Den lamp off"})
den = [r for r in sc.list_for("zach", "admin") if r["what"] == "Den lamp off" and r["state"] == "scheduled"]
out = T("schedule_action", {"tool_name": "home_control", "tool_args": DEN, "at": "in 4 hours", "what": "Den lamp off"})
check("the same action again: refused, naming the pending one",
      (out.startswith("Not scheduled: “Den lamp off” is already scheduled"), den[0]["id"] in out,
       len([r for r in sc.list_for("zach", "admin") if r["what"] == "Den lamp off" and r["state"] == "scheduled"])), (True, True, 1))
out = T("schedule_action", {"change": "den lamp", "at": "in 4 hours"})
now_den = [r for r in sc.list_for("zach", "admin") if r["what"] == "Den lamp off" and r["state"] == "scheduled"]
check("change moves it: the new one keeps the tool, its args and what it says; the old one is cancelled",
      (out.startswith("Changed: Scheduled “Den lamp off”"), len(now_den), now_den[0]["args"], sc.get(den[0]["id"])["state"],
       round((now_den[0]["run_at"] - den[0]["run_at"]) / 3600)), (True, 1, DEN, "cancelled", 2))
out = T("schedule_action", {"change": now_den[0]["id"], "what": "Den lamp off (late)"})
moved = [r for r in sc.list_for("zach", "admin") if r["state"] == "scheduled" and r["args"] == DEN]
check("change with only new words keeps the time (to the minute)", (len(moved), moved[0]["what"],
      abs(moved[0]["run_at"] - now_den[0]["run_at"]) < 60), (1, "Den lamp off (late)", True))
check("change to nothing pending says so", T("schedule_action", {"change": "zzz", "at": "in 1 hour"}).startswith(
      "Not changed: no pending scheduled action like that"), True)
out = T("schedule_action", {"tool_name": "home_control", "tool_args": DEN, "at": "in 6 hours", "what": "Den lamp off again",
                            "new": "yes"})
check("new='yes' schedules a second one", (out.startswith("Scheduled “Den lamp off again”"),
      len([r for r in sc.list_for("zach", "admin") if r["state"] == "scheduled" and r["args"] == DEN])), (True, 2))
as_("adult", rob, "Robin")
check("someone else's pending action is never theirs to change", T("schedule_action", {"change": "den lamp", "at": "in 1 hour"})
      .startswith("Not changed: no pending"), True)
as_("admin", "zach", "Zach")
for r in sc.list_for("zach", "admin"):
    if r["args"] == DEN and r["state"] == "scheduled":
        sc.cancel(r["id"], "zach", "admin")
b1, _ = sc.schedule("home_control", LAMP, "in 50 minutes", "Bedroom lamp off", owner="zach", role="admin")
check("the card's button sentence works", T("cancel_scheduled", {"id": f"Cancel scheduled action {b1['id']}"}).startswith(
      "Cancelled “Bedroom lamp off”"), True)
b3, _ = sc.schedule("home_control", LAMP, "in 55 minutes", "Hall lamp off", owner="zach", role="admin")
_real = lib._run_agent_turn_inner
lib._run_agent_turn_inner = lambda *a, **k: (_ for _ in ()).throw(AssertionError("the model was asked"))
try:
    said = lib._run_agent_turn([{"role": "user", "content": f"Cancel scheduled action {b3['id']}"}])
finally:
    lib._run_agent_turn_inner = _real
check("the Cancel button cancels in code, no model (#382)", (said.startswith("Cancelled “Hall lamp off”"),
      sc.get(b3["id"])["state"]), (True, "cancelled"))
as_("adult", rob, "Robin")
b2, _ = sc.schedule("home_control", LAMP, "in 50 minutes", "Zach's other lamp", owner="zach", role="admin")
check("Robin can't cancel Zach's by id", T("cancel_scheduled", {"id": b2["id"]}).startswith("Not cancelled: no pending"), True)
as_("admin", "zach", "Zach")
as_("household")
check("a shared device can't schedule", T("schedule_action", {"tool_name": "home_control", "tool_args": LAMP,
                                                                "at": "in 1 hour"}), lib._ROLE_REFUSAL)
as_("admin", "zach", "Zach")
os.environ["AGENT_TURN_SOURCE"] = "eval"
n = len(sc.list_for(None, None, include_done=True))
out = T("schedule_action", {"tool_name": "home_control", "tool_args": LAMP, "at": "in 1 hour", "what": "Eval lamp"})
check("an eval turn stores nothing", (out.startswith("Test turn: scheduled “Eval lamp”"),
                                      len(sc.list_for(None, None, include_done=True))), (True, n))
os.environ["AGENT_TURN_SOURCE"] = "test"
as_(None)
check("the role table has the tools", [hh.role_allows_tool(r, "schedule_action") for r in ("admin", "adult", "kid", "household")],
      [True, True, True, False])
check("a failure reads as one (honesty check)", bool(lib._FAIL_RX.search("Not scheduled: x")), True)
check("the prompt: titan can act later", "schedule_action" in lib.AGENT_IDENTITY and "can't do something at a later time"
      not in lib.AGENT_IDENTITY, True)
check("plain words for the tool", lib._with_plain_tool_names("I used schedule_action for that."),
      "I used a scheduled action for that.")

print("\n── repeating (#382 item 3) ──")
check("repeats read", [sc.parse_repeat(x) for x in ("every weekday", "daily", "weekends", "Mondays and Thursdays")],
      [[0, 1, 2, 3, 4], list(range(7)), [5, 6], [0, 3]])
check("…and words", (sc.repeat_words({"days": [0, 1, 2, 3, 4], "hm": "16:30"}), sc.repeat_words({"days": [0, 3], "hm": "09:00"})),
      ("every weekday at 4:30 PM", "on Mondays and Thursdays at 9 AM"))
check("a repeat needs a time of day", sc.schedule("home_control", LAMP, "in 30 minutes", None, owner="zach", role="admin",
                                                  repeat="daily")[1].startswith("Not scheduled: a repeating action needs"), True)
check("an unreadable repeat", sc.schedule("home_control", LAMP, "4:30 PM", None, owner="zach", role="admin",
                                          repeat="every other week")[1].startswith("Not scheduled: can't read how it repeats"), True)
rp, said = sc.schedule("home_control", LAMP, "11 PM", "Porch light off", owner="zach", role="admin", repeat="every weekday")
check("scheduled, said so", (said.startswith("Scheduled “Porch light off” every weekday at 11 PM, starting"),
                             rp["repeat"]["days"], rp["repeat"]["hm"]), (True, [0, 1, 2, 3, 4], "23:00"))
first_at = rp["run_at"]
res = sc.run_due(first_at + 30, execute=fake_exec)
check("it ran", rp["id"] in [x["id"] for x in res], True)
pending = [r for r in sc.list_for("zach", "admin") if r["what"] == "Porch light off" and r["state"] == "scheduled"]
check("the next one is scheduled, same series card", (len(pending), pending[0]["card_key"] == rp["card_key"],
      pending[0]["id"] != rp["id"]), (1, True, True))
nxt_local = datetime.fromtimestamp(pending[0]["run_at"], TZ)
check("…on the next weekday, same time", (nxt_local.weekday() in (0, 1, 2, 3, 4), nxt_local.strftime("%H:%M"),
      pending[0]["run_at"] > first_at), (True, "23:00", True))
pc = [c for c in cards_for("zach", "admin") if c["key"] == rp["card_key"]]
check("one card, saying done and when next, with Cancel for the next", (len(pc), pc[0]["title"], "Next " in pc[0]["detail"],
      pc[0]["actions"][0]["value"]), (1, "Done: Porch light off", True, f"Cancel scheduled action {pending[0]['id']}"))
check("cancelling the pending one ends the series", sc.tool("cancel_scheduled", {"id": pending[0]["id"]}).startswith(
      "Cancelled “Porch light off”"), True)
check("…nothing after it", sc.run_due(pending[0]["run_at"] + 30, execute=fake_exec), [])
# the end of its 90 days: no next, an "Ended" card
ended = sc.add("zach", "Hall light off", "home_control", LAMP, clock() - 5,
               repeat={"days": list(range(7)), "hm": "23:00", "until": clock()})
sc.run_due(execute=fake_exec)
check("at its end: no next, an Ended card", ([r for r in sc.list_for("zach", "admin") if r["what"] == "Hall light off"
                                             and r["state"] == "scheduled"],
       [c["title"] for c in cards_for("zach", "admin") if c["key"] == ended["card_key"]]), ([], ["Ended: Hall light off"]))

print("\n── on arriving or leaving a place (#284) ──")
import places  # noqa: E402
P = lambda tool, args, place, on, owner="zach", role="admin", what=None: sc.schedule(   # noqa: E731
    tool, args, None, what, owner=owner, role=role, place=place, on=on)
PORCH = {"device": "Porch Light", "action": "on"}
check("no arrivals reported yet: refused, with how to turn them on", P("home_control", PORCH, "home", "arrive")[1]
      .startswith("Not scheduled: your iPhone hasn't told titan about arriving or leaving yet"), True)
places.record("ev-1", "zach", "left", "home", None)
places.record("ev-2", "zach", "arrived", "Work", None)
places.record("ev-3", rob, "arrived", "home", None)
check("the places titan knows for Zach", places.known("zach"), ["home", "Work"])
check("an unknown place: says which exist", P("home_control", PORCH, "the gym", "arrive")[1],
      "Not scheduled: titan doesn't know a place called “the gym” for you. It knows: home, Work (the places you named "
      "in the iPhone's Settings › Places).")
check("missing on", P("home_control", PORCH, "home", None)[1],
      "Not scheduled: say whether it's on arriving at the place or on leaving it (on: arrive or leave).")
check("a bad on", P("home_control", PORCH, "home", "sometime")[1].startswith("Not scheduled: say whether"), True)
check("a time and a place", sc.schedule("home_control", PORCH, "11 PM", None, owner="zach", role="admin",
                                         place="home", on="arrive")[1], "Not scheduled: give a time or a place, not both.")
check("hands-off still applies", P("home_control", {"device": "Front Door Lock", "action": "unlock"}, "home", "arrive")[1],
      sc.HANDS_OFF)
h1, reply = P("home_control", PORCH, "my house", "arrive", what="Turn on the porch light")
check("“my house” is home; the reply says when", reply.startswith("Scheduled “Turn on the porch light” when you get home."), True)
check("the row: a trigger, no time, lapses in 30 days", (h1["trigger"], h1["run_at"], abs(h1["expires_at"] - clock() - 30 * 86400) < 5),
      ({"place": "home", "on": "arrive"}, None, True))
w1, _ = P("send_message", {"to": "Robin", "text": "On my way"}, "work", "leaving", what="Text Robin I'm on my way")
check("“work” matches the reported “Work”; leaving → leave", w1["trigger"], {"place": "Work", "on": "leave"})
h2, _ = P("home_control", {"device": "Hallway Light", "action": "on"}, "home", "arrived", what="Hallway light on")
m1, _ = P("home_control", PORCH, "home", "arrive", owner=rob, role="adult", what="Robin's porch light")
cz = {c["key"]: c for c in cards_for("zach", "admin")}
check("card: when you get home", cz[h1["card_key"]]["title"], "Scheduled: Turn on the porch light, when you get home")
check("card: when you leave work", cz[w1["card_key"]]["title"], "Scheduled: Text Robin I'm on my way, when you leave Work")
check("…with how long it waits and Cancel", (cz[h1["card_key"]]["detail"].startswith("Any time in the next 30 days (until "),
                                             cz[h1["card_key"]]["actions"][0]["label"]), (True, "Cancel"))
check("listed with its place", any("when you get home — Turn on the porch light" in x for x in
                                   [sc.row_when(r) + " — " + r["what"] for r in sc.list_for("zach", "admin")]), True)
ran = [x["id"] for x in sc.run_due(clock() + 3600, execute=fake_exec)]
check("the time runner never runs a place action", (any(i in ran for i in (h1["id"], h2["id"], w1["id"], m1["id"])),
                                                    sc.get(h1["id"])["state"]), (False, "scheduled"))
runs.clear()
check("the wrong place doesn't fire", sc.on_place_event("zach", "admin", "arrived", "Work", execute=fake_exec), [])
check("the wrong event doesn't fire", sc.on_place_event("zach", "admin", "left", "home", execute=fake_exec), [])
check("someone else's arrival never fires Zach's", [x["id"] for x in sc.on_place_event(rob, "adult", "arrived", "home",
                                                                                         execute=fake_exec)], [m1["id"]])
check("…nor does the place's name alone", (sc.get(h1["id"])["state"], sc.get(h2["id"])["state"]), ("scheduled", "scheduled"))
out = sc.on_place_event("zach", "admin", "arrived", "HOME", execute=fake_exec)
check("one arrival fires every matching action of his", sorted(x["id"] for x in out), sorted([h1["id"], h2["id"]]))
check("…as him", sorted((r[0], r[1]) for r in runs if r[0] in (h1["id"], h2["id"])),
      sorted([(h1["id"], "admin"), (h2["id"], "admin")]))
check("a second arrival: nothing left to fire", sc.on_place_event("zach", "admin", "arrived", "home", execute=fake_exec), [])
check("…each ran once", len([r for r in runs if r[0] in (h1["id"], h2["id"])]), 2)
dc = next(c for c in cards_for("zach", "admin") if c["key"] == h1["card_key"])
check("the same card: done, when you got home", (dc["title"], dc["detail"].endswith("(when you got home)")),
      ("Done: Turn on the porch light", True))
out = sc.on_place_event("zach", "admin", "left", "Work", device=hook_says({"result": "Sent to Robin."}))
check("leaving work: the Mac action goes through the device hook", (out[0]["state"], seen[-1]),
      ("done", ("zach", w1["id"], "send_message")))
e1, _ = P("home_control", PORCH, "Work", "arrive", what="Office fan on")
expired = sc.run_due(clock() + 31 * 86400, execute=fake_exec)
check("after 30 days: failed, never arrived", (sc.get(e1["id"])["state"], sc.get(e1["id"])["result"].startswith("never arrived:")),
      ("failed", True))
ec = next(c for c in cards_for("zach", "admin") if c["key"] == e1["card_key"])
check("…the card says so", (ec["title"], ec["detail"]), ("Couldn't Office fan on (never arrived)",
      "It waited 30 days for you to get to Work; that didn't happen, so it wasn't done."))
check("an expired one never fires", sc.on_place_event("zach", "admin", "arrived", "Work", execute=fake_exec), [])
c3, _ = P("home_control", PORCH, "home", "leave", what="Porch off when I leave")
check("cancelling a place action", sc.cancel(c3["id"], "zach", "admin")["state"], "cancelled")
check("…and it never fires", sc.on_place_event("zach", "admin", "left", "home", execute=fake_exec), [])
as_("admin", "zach", "Zach")
out = T("schedule_action", {"tool_name": "home_control", "tool_args": PORCH, "place": "home", "on": "arrive",
                            "what": "Porch light on"})
check("the tool takes place and on", out.startswith("Scheduled “Porch light on” when you get home."), True)
check("…the tool without on", T("schedule_action", {"tool_name": "home_control", "tool_args": PORCH, "place": "home"})
      .startswith("Not scheduled: say whether"), True)
check("list_scheduled says the place", "• when you get home — Porch light on" in T("list_scheduled", {}), True)
os.environ["AGENT_TURN_SOURCE"] = "eval"
check("an eval turn: no store, any place name", T("schedule_action", {"tool_name": "home_control", "tool_args": PORCH,
      "place": "the beach", "on": "arrive", "what": "x"}), "Test turn: scheduled “x” when you get to the beach (place the beach, on arrive).")
os.environ["AGENT_TURN_SOURCE"] = "test"
as_(None)

print("\n── an older store (#301) gets the trigger columns ──")
import sqlite3  # noqa: E402
old = _TMP / "v1.db"
con = sqlite3.connect(old)
con.execute("""CREATE TABLE scheduled (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, audience TEXT NOT NULL DEFAULT 'private', what TEXT NOT NULL,
    tool TEXT NOT NULL, args TEXT NOT NULL, run_at REAL NOT NULL, state TEXT NOT NULL, result TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, card_key TEXT, runner TEXT)""")
con.execute("INSERT INTO scheduled VALUES ('sa-old00001','zach','private','Old one','count_chars','{}',?, 'scheduled',"
            "NULL,?,?,'sched-sa-old00001',NULL)", (clock() + 600, clock(), clock()))
con.commit(); con.close()
check("the old row survives", (sc.get("sa-old00001", db=old) or {}).get("what"), "Old one")
check("…and a place row can be added", sc.add("zach", "New one", "count_chars", {}, None, db=old, place="home", on="arrive",
                                              expires_at=clock() + 60)["trigger"], {"place": "home", "on": "arrive"})

print("\n── the API ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    tok = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": f"{name}'s iPhone", "platform": "ios"}).json()
    return {"X-Agent-Token": tok["token"]}, s["person"]["id"]


JORDAN, jor = pair("Jordan", "adult")
AVA, ava = pair("Ava", "kid")
z1, _ = sc.schedule("home_control", LAMP, "in 1 hour", "Zach's lamp", owner="zach", role="admin")
s1, _ = sc.schedule("home_control", LAMP, "in 1 hour", "Jordan's lamp", owner=jor, role="adult")
k1, _ = sc.schedule("home_control", LAMP, "in 1 hour", "Ava's lamp", owner=ava, role="kid")
got = c.get("/v1/scheduled", headers=JORDAN).json()
check("GET: the caller's own only", [x["what"] for x in got["scheduled"]], ["Jordan's lamp"])
check("…with when, in words", got["scheduled"][0]["when"].startswith(("today at", "tomorrow at")), True)
check("a kid sees their own", [x["what"] for x in c.get("/v1/scheduled", headers=AVA).json()["scheduled"]], ["Ava's lamp"])
check("Jordan can't cancel Zach's", c.delete(f"/v1/scheduled/{z1['id']}", headers=JORDAN).status_code, 404)
check("a kid cancels their own", c.delete(f"/v1/scheduled/{k1['id']}", headers=AVA).json(),
      {"id": k1["id"], "cancelled": True, "what": "Ava's lamp"})
check("…once (409 after)", c.delete(f"/v1/scheduled/{k1['id']}", headers=AVA).status_code, 409)
check("the admin cancels anyone's", c.delete(f"/v1/scheduled/{s1['id']}", headers=M).status_code, 200)
check("history with include_done", "Jordan's lamp" in [x["what"] for x in c.get(
      "/v1/scheduled?include_done=true", headers=JORDAN).json()["scheduled"]], True)
check("/v1/me says who can schedule", c.get("/v1/me", headers=AVA).json()["can"].get("scheduled"), True)

places.record("ev-s1", jor, "left", "home", None)
s2, _ = sc.schedule("home_control", PORCH, None, "Jordan's porch", owner=jor, role="adult", place="home", on="arrive")
got = [x for x in c.get("/v1/scheduled", headers=JORDAN).json()["scheduled"] if x["id"] == s2["id"]][0]
check("GET: a place action's trigger and when", (got["trigger"], got["when"], got["run_at"]),
      ({"place": "home", "on": "arrive"}, "when you get home", None))
sc_done = []
_real_on_place = sc.on_place_event
sc.on_place_event = lambda *a, **k: sc_done.append(_real_on_place(*a, **{**k, "execute": fake_exec})) or sc_done[-1]
# #1241: the route's place action runs on this test's own long-lived loop (TestClient without `with` cancels a
# background task still pending when the response returns: on a busy titan it was cut off, and the row stayed
# "scheduled"), each with an Event; the checks wait on those, never a clock.
import asyncio as _aio  # noqa: E402
import threading as _th  # noqa: E402

_PLACE_RUNS: list = []
_real_place_actions = api._place_actions
_BG = _aio.new_event_loop()
_th.Thread(target=_BG.run_forever, daemon=True).start()


async def _place_actions(*a, **k):   # the route looks _place_actions up at call time
    done = _th.Event()
    _PLACE_RUNS.append(done)
    _aio.run_coroutine_threadsafe(_real_place_actions(*a, **k), _BG).add_done_callback(lambda _f: done.set())


api._place_actions = _place_actions


def _settle_place(started_before: int = 0, must_start: bool = True, deadman: float = 180.0) -> None:
    """The place action(s) started since `started_before` have run; with must_start, one must have started (a
    duplicate event starts none). A deadman, never a clock, and a loud failure rather than a wrong state."""
    end = time.time() + deadman
    while must_start and len(_PLACE_RUNS) <= started_before:
        if time.time() >= end:
            raise AssertionError("the place route never started its action task")
        time.sleep(0.02)
    for done in _PLACE_RUNS[started_before:]:
        if not done.wait(max(0.0, end - time.time())):
            raise AssertionError("the place task never finished")


before = len(_PLACE_RUNS)
r = c.post("/v1/presence/place", json={"id": "ev-s2", "event": "arrived", "place": "home"}, headers=JORDAN)
_settle_place(before)
check("Jordan's iPhone's arrival runs it (204, then done)", (r.status_code, sc.get(s2["id"])["state"]), (204, "done"))
before = len(_PLACE_RUNS)
r = c.post("/v1/presence/place", json={"id": "ev-s2", "event": "arrived", "place": "home"}, headers=JORDAN)
_settle_place(before, must_start=False)   # the same event again: the route may start nothing
check("the phone's retry of the same event: nothing more", (r.status_code, len(sc_done)), (204, 1))
sc.on_place_event = _real_on_place

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
