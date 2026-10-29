#!/usr/bin/env python3
# gate: hermetic
"""The local brain's remaining schedule misses (titan-node#1230, from #402's box run on 10-07):
  - cadence and next-time questions ("How often does the compost go out?", "When does it go out next?") now trigger
    the schedule digest (they matched none of its words, so the 8B answered from an old saved note), and the answer
    line says the calendar read outranks a saved note;
  - timeline questions ("the most recent thing on the timeline", "When did this happen: “…”?") get titan's own
    timeline read and the entry asked for, in code, from what this asker may see only: a private entry of another
    person's never becomes "the most recent thing".
Fictional household; throwaway stores. Run: python3 scripts/test_schedule_facts.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-schedfacts-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import household_identity as hh  # noqa: E402
import schedule_digest as sd  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


store = hh.HouseholdStore(_TMP / "household.db")
hh.reset_store(store)
robin = store.add_person("Robin Hale", "adult")["id"]
ava = store.add_person("Ava Hale", "kid")["id"]

print("── which questions get the digest ──")
for q in ("When does the compost go out next?", "How often does the compost go out?", "When will the bins go out?",
          "Does recycling go out every other week?"):
    check(f"local prefetch: {q!r}", sd.wants_schedule(q, local=True), True)
for q in ("When does the store close?", "When will my package arrive?", "How often should I water the fern?"):
    check(f"Gemini's prefetch trigger stays main's: no digest for {q!r} (JEV: no needless egress)",
          sd.wants_schedule(q), False)
for q in ("What's the weather like?", "Tell me a joke"):
    check(f"not schedule: {q!r}", sd.wants_schedule(q), False)
for q in ("What's the most recent thing on the household timeline?", "When did this happen: “Fed the fish”?"):
    check(f"timeline: {q!r}", sd.wants_timeline(q), True)
check("the quoted entry is read from the question", sd.timeline_about("When did this happen: “Fed the fish”?"),
      "Fed the fish")

import agent_runtime_lib as lib  # noqa: E402

NOW = datetime(2026, 10, 7, 9, 30)
lib._local_now = lambda: NOW
day = NOW + timedelta(days=5)
CAL = "\n".join(f"• {(day + timedelta(weeks=k)):%a %b} {(day + timedelta(weeks=k)).day}, 8:00 PM–8:15 PM — Compost out "
                f"[calendar: Family, shared] [repeats: every week on {day:%A}]" for k in (0, 1, 2))
lib._device_bridge_enabled = lambda: True
lib._device_tool_local = lambda tool: True
lib._execute_tool = lambda name, args: CAL if name == "list_calendar_events" else "No reminders."
lib._TURN_IO.person = {"id": hh.ADMIN_PERSON_ID}
lib._TURN_INFO.info = {}


def hint_for(q):
    out = lib._with_schedule_digest([{"role": "user", "content": q}])
    return next((m["content"] for m in out if m.get("role") == "system" and m["content"].startswith("[From ")), "")


print("── the compost questions, end to end through the local prefetch ──")
h = hint_for("When does the compost go out next?")
check("next: the calendar's next occurrence, weekday and time", (f"{day:%a %b} {day.day} 8 PM" in h,
      f"repeats every week on {day:%A}" in h), (True, True))
check("…and it says the calendar read outranks a saved note", "outranks any saved note" in h, True)
h = hint_for("How often does the compost go out?")
check("how often: the cadence, from the calendar", f"repeats every week on {day:%A}" in h, True)

lm = lib._local_messages(lib._with_schedule_digest([{"role": "system", "content": "sys"},
                                                    {"role": "user", "content": "How often does the compost go out?"}]))
check("the reworded answer line still sits after the question, which keeps /no_think",
      (lm[-1]["content"].startswith("[From the calendar"), lm[-2]["content"].endswith("/no_think"),
       "/no_think" in lm[-1]["content"]), (True, True, False))

print("── the Gemini prefetch: the widened words never send the schedule out ──")
os.environ["AGENT_GEMINI_SCHEDULE_PREFETCH"] = "1"
lib._TURN_IO.person = {"id": hh.ADMIN_PERSON_ID}
sent = []
lib._execute_tool = lambda name, args: (sent.append(name), CAL if name == "list_calendar_events" else "No reminders.")[1]
out = lib._with_schedule_digest_gemini([{"role": "user", "content": "When does the store close?"}])
check("\"When does the store close?\": no calendar read and no digest for Gemini",
      ("list_calendar_events" in sent, any("[SCHEDULE" in str(m.get("content")) for m in out)), (False, False))
sent.clear()
h = hint_for("When does the compost go out next?")
check("…while the local prefetch still reads it for the compost question", ("list_calendar_events" in sent,
      f"{day:%a %b} {day.day} 8 PM" in h), (True, True))
os.environ.pop("AGENT_GEMINI_SCHEDULE_PREFETCH", None)

print("── the timeline, from what this asker may see ──")
t0 = datetime(2026, 10, 7, 8, 0).timestamp()
EVS = [
    {"event_id": "e1", "timestamp": t0 - 3 * 86400, "payload": {"text": "Fed the fish", "audience": "household", "person": ava}},
    {"event_id": "e2", "timestamp": t0 - 3600, "payload": {"text": "Watered the garden", "audience": "household",
                                                            "person": hh.ADMIN_PERSON_ID}},
    {"event_id": "e3", "timestamp": t0, "payload": {"text": "Therapy notes reviewed", "audience": "private", "person": robin}},
]
lib._timeline_events = lambda stream="household-timeline": list(EVS)
for who, role in ((hh.ADMIN_PERSON_ID, "admin"), (ava, "kid")):
    lib._TURN_IO.person = {"id": who}
    _role = lib.turn_role
    lib.turn_role = lambda r=role: r
    h = hint_for("What's the most recent thing on the household timeline?")
    check(f"{role}: the most recent entry they may see, not Robin's private one",
          ("Watered the garden" in h, "Therapy" in h), (True, False))
    h = hint_for("When did this happen: “Fed the fish”?")
    lm = lib._local_messages(lib._with_schedule_digest([{"role": "system", "content": "sys"}, {"role": "user", "content":
                                                        "What's the most recent thing on the household timeline?"}]))
    check(f"{role}: the timeline answer line keeps /no_think on the question", (lm[-2]["content"].endswith("/no_think"),
          "/no_think" in lm[-1]["content"]), (True, False))
    check(f"{role}: a quoted entry gets its own day", ("Fed the fish" in h, "Sun Oct 4" in h or "Oct 4" in h), (True, True))
    lib.turn_role = _role
lib._TURN_IO.person = {"id": robin}
lib.turn_role = lambda: "adult"
h = hint_for("What's the most recent thing on the household timeline?")
check("Robin's own private entry is the most recent for Robin", "Therapy notes reviewed" in h, True)
lib.turn_role = _role
src = Path(lib.__file__).read_text()
body = src[src.index("def _with_schedule_digest("):src.index("def _with_schedule_digest_gemini(")]
check("the timeline read goes through _visible_events (the viewer's filter), nothing else",
      ("_visible_events()" in body, "_timeline_events(" in body), (True, False))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
