#!/usr/bin/env python3
# gate: hermetic   (no Home Assistant, no stores written; the tool executor with a turn's own words set)
"""Asked for later, never done now (#1203; turn_eval lights-later and place-later changed the thermostat on the spot).
A person's latest message that asks for a later time or a place stops any call that acts at once; the model gets
told to use schedule_action. A time the message describes ("dinner is at 6:30") doesn't count, "now" turns it off,
and a scheduled action running at its time, or a background turn, is never stopped. Fictional people.
Run: python3 scripts/test_later_guard.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-later-"))
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "SCHEDULED_DB": "scheduled.db", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import later_guard as G  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── what asks for later ──")
LATER = {
    "Set the thermostat to 66 at 11 PM.": "at 11 PM",
    "When I get home tonight, set the thermostat to 70.": "When I get home",
    "Text Ava at 4:30 that dinner is at 6.": "at 4:30",
    "Turn on the porch light in 20 minutes": "in 20 minutes",
    "turn the heat up in an hour": "in an hour",
    "Once I'm home, put on some jazz": "Once I'm home",
    "Play the playlist later tonight": "later",
    "Set the thermostat to 66 at 11 tonight": "at 11 tonight",
    "Text Robin at noon to bring the cooler": "at noon",
}
check("later times, delays, places and 'later' are found",
      {q: G.later_words(q) for q in LATER}, LATER)
NOT = ["Set the thermostat to 72.", "Text Ava that dinner is at 6:30.", "Tell Robin the game starts at 7 PM",
       "Turn on the lights now, the movie is at 8 PM", "Set it to 66 right now", "What's on at 9 PM?",
       "Turn the lights off; I'll be home at 6", "Text Leo that practice is in an hour"]
check("not later: a plain request, a time the message describes, 'now'",
      {q: G.later_words(q) for q in NOT if q != "What's on at 9 PM?"}, {q: None for q in NOT if q != "What's on at 9 PM?"})
check("a question about what's on at a time describes it (and calls no tool that acts anyway)",
      G.later_words("What's on at 9 PM?"), None)

print("── in the executor, every brain ──")
import agent_runtime_lib as lib  # noqa: E402
io = lib._TURN_IO


def run(tool, args, later, **extra):
    saved = {k: getattr(io, k, None) for k in ("later", "scheduled", "jev_source", "role")}
    try:
        io.later, io.scheduled, io.jev_source, io.role = later, extra.get("scheduled"), extra.get("jev_source"), "adult"
        return lib.execute_tool(tool, args)
    finally:
        for k, v in saved.items():
            setattr(io, k, v)


out = run("home_control", {"device": "Thermostat", "action": "set_temperature", "value": 66}, "at 11 PM")
check("'at 11 PM': home_control doesn't run, and the model is told to schedule it",
      (out.startswith("Not run: they asked for this later (“at 11 PM”)"), "schedule_action with at" in out,
       "tool_name \"home_control\"" in out), (True, True, True))
out = run("home_control", {"device": "Thermostat", "action": "set_temperature", "value": 70}, "When I get home")
check("'when I get home': told to schedule it on arriving", "place and on (\"home\", on arrive)" in out, True)
check("'turn off the lights when I leave': held for leaving",
      run("home_control", {"device": "Living Room Lamp", "action": "off"}, G.later_words("turn off the lights when I leave"))
      .startswith("Not run: they asked for this later (“when I leave”)"), True)
for msg, args in (("Text Robin I'll be home later", {"to": "Robin", "text": "I'll be home later"}),
                  ("Tell Ava pickup is at 5:30", {"to": "Ava", "text": "Pickup is at 5:30"})):
    check(f"a text's own words are content, not when to act: '{msg}' isn't stopped (send_message isn't guarded)",
          str(run("send_message", args, G.later_words(msg))).startswith("Not run: they asked"), False)
check("…nor speak", "speak" in G.LATER_TOOLS or "send_message" in G.LATER_TOOLS, False)
check("a read still runs (the weather, the calendar): only calls that act at once are stopped",
      run("get_datetime", {}, "at 11 PM").startswith("Not run: they asked"), False)
check("schedule_action itself is never stopped", "schedule_action" not in G.LATER_TOOLS, True)
check("a scheduled action running at its time is never stopped (scheduled set)",
      run("home_control", {"device": "Thermostat", "action": "set_temperature", "value": 66}, "at 11 PM",
          scheduled="s1").startswith("Not run: they asked"), False)
check("…nor a background turn (nobody asked)",
      run("home_control", {"device": "Thermostat", "action": "set_temperature", "value": 66}, "at 11 PM",
          jev_source="proactive").startswith("Not run: they asked"), False)
check("…and with no later words, home_control runs as before (Home Assistant isn't set up here)",
      run("home_control", {"device": "Thermostat", "action": "set_temperature", "value": 72}, None)
      .startswith("Not run: they asked"), False)

print("── #1251 B (Zach 10-08): the local brain asks first; it never schedules on its own ──")
real_exec_ = lib._execute_tool
lib._execute_tool = lambda name, args: "STUB: ran"


def run_b(tool, args, later, brain="local", asked=False):
    saved = {k: getattr(io, k, None) for k in ("later", "later_asked", "scheduled", "jev_source", "role")}
    lib._TURN_INFO.info = {"brain": brain}
    try:
        io.later, io.later_asked, io.scheduled, io.jev_source, io.role = later, asked, None, None, "adult"
        return str(lib.execute_tool(tool, args))
    finally:
        for k, v in saved.items():
            setattr(io, k, v)


TH = {"device": "Thermostat", "action": "set_temperature", "value": 66}
out = run_b("home_control", TH, "at 11 PM")
check("local: the action is held, nothing scheduled, and the model is told to ask (never to call schedule_action now)",
      (out.startswith("Not run: they asked for this later"), "nothing is scheduled" in out, "Ask them in one line" in out,
       "Don't call schedule_action now" in out), (True, True, True, True))
check("local: the place form asks too, saying 'when they get there'",
      "when they get there" in run_b("home_control", TH, "When I get home"), True)
SA = {"tool_name": "home_control", "tool_args": TH, "at": "11:00 PM"}
check("local: schedule_action on titan's own say-so in that turn is refused (whether or not it tried the action first)",
      run_b("schedule_action", SA, "at 11 PM").startswith("Not run: ask them first"), True)
check("…counted (later_ask_held)", lib._TURN_INFO.info.get("later_ask_held"), 1)
check("the confirm path: the next turn's 'Yes.' carries no later words, so schedule_action runs",
      run_b("schedule_action", SA, G.later_words("Yes.")), "STUB: ran")
check("the person's own message asked to schedule it: schedule_action runs, and the held action points to it",
      (run_b("schedule_action", SA, "at 11 PM", asked=True),
       "schedule_action with at" in run_b("home_control", TH, "at 11 PM", asked=True)), ("STUB: ran", True))
check("what counts as asking to schedule", [G.asked_to_schedule(m) for m in (
      "Schedule the thermostat to 66 at 11 PM.", "Can you schedule it for tonight at 11?", "Set the thermostat to 66 at 11 PM.",
      "Turn the porch light on when I get home.")], [True, True, False, False])
check("the noun 'schedule' isn't a request to schedule (Codex on #1277)", [G.asked_to_schedule(m) for m in (
      "Check my schedule, then set the thermostat to 66 at 11 PM.", "What's on the schedule? Then lights off at 10.",
      "Is it scheduled?", "Please get it scheduled for 11 PM.", "Go ahead and schedule that for when I get home.",
      "Can you set it up for later?")], [False, False, False, True, True, True])
check("Gemini: unchanged (the held action points to schedule_action; it may schedule)",
      ("schedule_action with at" in run_b("home_control", TH, "at 11 PM", brain="gemini"),
       run_b("schedule_action", SA, "at 11 PM", brain="gemini")), (True, "STUB: ran"))
check("no later words: schedule_action runs on the local brain as before", run_b("schedule_action", SA, None), "STUB: ran")
print("── #1251 B's confirm path: a bare yes to titan's own later question (10-08 A/B: the yes changed it now) ──")
Q = "Set the thermostat to 66 at 11 PM tonight? I'll schedule it if you say yes."
PQ = "Set it to 70 when you get home? Say yes and I will."
check("a bare yes takes the question's time or place (the place in the person's words)",
      [G.confirmed_later(Q, m) for m in ("Yes.", "yes please", "Sure, go ahead", "ok thanks")] + [G.confirmed_later(PQ, "Yep")],
      ["at 11 PM"] * 4 + ["when I get home"])
check("…not a no, not a yes with changes (the model reads those), not a yes to a reply that didn't ask about later",
      [G.confirmed_later(Q, "No."), G.confirmed_later(Q, "Yes, but make it 68 instead"),
       G.confirmed_later("Done, it's 66 now.", "Yes."), G.confirmed_later("What time should I set it?", "Yes.")],
      [None, None, None, None])
mixed = "Yes, and turn on the porch light now"
check("'yes, and turn on the porch light now' is a new request, judged on its own: no carried words, and 'now' means "
      "now", (G.confirmed_later(Q, mixed), G.later_words(mixed)), (None, None))
lib._execute_tool = lambda name, args: "STUB: ran"
check("…so the porch light acts now on the new part", run_b("home_control", {"device": "Porch Light", "action": "on"},
      G.confirmed_later(Q, mixed) or G.later_words(mixed)), "STUB: ran")
check("a yes after an ordinary question (no later words in it) is unchanged: nothing carried, nothing held",
      (G.confirmed_later("Want me to read you the weather?", "Yes."),
       run_b("home_control", TH, G.confirmed_later("Want me to read you the weather?", "Yes."))), (None, "STUB: ran"))
check("only titan's own reply's TIME OR PLACE is carried, never its other words",
      G.confirmed_later("Text Robin 'meet at the bank at 11 PM'? I'll schedule it if you say yes.", "Yes."), "at 11 PM")
yes_words = G.confirmed_later(Q, "Yes.")
out = run_b("home_control", TH, yes_words, asked=True)
check("on the yes turn the action is still never done now: held, and pointed at schedule_action",
      (out.startswith("Not run: they asked for this later"), "schedule_action with at" in out), (True, True))
check("…and schedule_action runs (the yes is the person asking to schedule it)",
      run_b("schedule_action", SA, yes_words, asked=True), "STUB: ran")
check("the place form: held and pointed at schedule_action on arriving",
      "place and on" in run_b("home_control", TH, G.confirmed_later(PQ, "Yep"), asked=True), True)
lib._execute_tool = real_exec_
src_y = (Path(__file__).parent / "agent_runtime_lib.py").read_text()
check("the turn takes it from titan's own previous reply (prior[-1]), only when the message itself has no later words",
      ("yes_to = _lg.confirmed_later(str((prior[-1] if prior else {}).get(\"content\") or \"\")," in src_y
       and "_TURN_IO.later, _TURN_IO.later_asked = yes_to, True" in src_y), True)

lib._execute_tool = real_exec_
src_b = (Path(__file__).parent / "agent_runtime_lib.py").read_text()
check("later_asked is set from the last user message and cleared at the turn's end",
      ("_TURN_IO.later_asked = _lg.asked_to_schedule(_users_words(" in src_b,
       "_TURN_IO.later = _TURN_IO.later_asked = _TURN_IO.everyone" in src_b, '"later_ask_held"' in src_b),
      (True, True, True))

print("── what ran vs what was refused (the eval's `ran`) ──")
lib._TURN_INFO.info = {}
io.outcomes = []
run("home_control", {"device": "Thermostat", "action": "set_temperature", "value": 66}, "at 11 PM")
check("a call the guard refused never counts as ran", lib._TURN_INFO.info.get("ran"), None)
lib._record_outcome("home_control", {}, "Done (eval: not sent): Thermostat → set temperature temperature 66.")
check("…one that ran does", lib._TURN_INFO.info.get("ran"), ["home_control"])
lib._record_outcome("send_message", {}, "Couldn't send: no such contact")
check("…and a failed one doesn't", lib._TURN_INFO.info.get("ran"), ["home_control"])
io.outcomes = None
import turn_eval as te  # noqa: E402
check("turn_eval: 'changed it now' means it ran (titan's ran), not that it was called",
      (te.Turn(called=["home_control"], ran=[]).changed_now("home_control"),
       te.Turn(called=["home_control"], ran=["home_control"]).changed_now("home_control")), (False, True))
check("…and an older titan without `ran` falls back to called", te.Turn(called=["home_control"]).changed_now("home_control"),
      True)
api_src = (Path(__file__).parent / "agent_api_server.py").read_text()
check("the eval done event carries ran (eval turns only)", 'done["ran"] = ran_seen' in api_src and
      'ran_seen = list(_turns[turn_id].get("ran") or []) if is_eval else None' in api_src, True)

print("── the turn sets it from the latest message only ──")
src = (Path(__file__).parent / "agent_runtime_lib.py").read_text()
check("set from the last user message, cleared at the turn's end", (
      "_TURN_IO.later = _lg.later_words(_users_words(str((last or {}).get(\"content\") or \"\")))" in src,
      "            _TURN_IO.later = _TURN_IO.later_asked = _TURN_IO.everyone = _TURN_IO.repeat_send = None" in src), (True, True))
import re as _re  # noqa: E402
_logged = _re.search(r'for k in \("context_ms",(.*?)\)\s+if k in info', src, _re.S)
check("later_held is a field the turn log keeps (counted per turn, for the A/B and the turn report)",
      bool(_logged) and '"later_held"' in _logged.group(1), True)
sched = (Path(__file__).parent / "scheduled.py").read_text()
check("the scheduled runner clears it on its worker thread", '"status", "later"):' in sched, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
