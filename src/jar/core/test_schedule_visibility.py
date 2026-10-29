#!/usr/bin/env python3
# gate: hermetic
"""Schedule facts only from what the asker may see (titan-node#1230): the digest's day list and every derived fact
(now, next, overlaps, firsts, next occurrence) are computed after the calendar read is cut to the viewer's own
calendars, an adult's own kids', and household (unclaimed) ones; a shared screen gets household ones only. Views ×
viewers: an adult (a parent), a kid and a shared screen, each against a private event on another adult's calendar
that overlaps a household one, comes first, comes next and repeats: adding it changes none of their facts
(noninterference), and its title never appears. Fictional people; a throwaway household store.
Run: python3 scripts/test_schedule_visibility.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-schedvis-"))
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
os.environ["AGENT_MEMORY_DIR"] = str(_TMP / "memory")
os.environ["AGENT_TURN_LOG"] = str(_TMP / "turns.jsonl")
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
alex = hh.ADMIN_PERSON_ID                      # the admin, Ava's parent
robin = store.add_person("Robin Hale", "adult")["id"]
ava = store.add_person("Ava Hale", "kid")["id"]
screen = store.add_person("Living room", "household")["id"]
store.update_person(alex, name="Alex Hale", calendars=["Alex"])
store.update_person(robin, calendars=["Robin"])
store.update_person(ava, calendars=["Ava school"], parents=[alex])

NOW = datetime(2026, 10, 1, 9, 50)
VISIBLE = """• Thu Oct 1, 10:40 AM–11:10 AM — Call with Sam [calendar: Alex]
• Thu Oct 1, 3:30 PM–4:30 PM — Leo soccer [calendar: Family, shared]
• Fri Oct 2, 9:00 AM–10:00 AM — Dentist [calendar: Family, shared]
• Fri Oct 2, 3:00 PM–4:00 PM — Piano lesson [calendar: Ava school, shared] [repeats: every week on Friday]"""
# Robin's own calendar, shared to the household's devices: it overlaps soccer, it's the next event, the first one
# tomorrow, and it repeats. None of that may reach anyone but Robin.
PRIVATE = """• Thu Oct 1, 10:00 AM–10:30 AM — Therapy session [calendar: Robin, shared] [repeats: every week on Thursday]
• Thu Oct 1, 4:00 PM–5:00 PM — Therapy follow-up [calendar: Robin, shared]
• Fri Oct 2, 7:30 AM–8:00 AM — Robin's private call [calendar: Robin, shared]"""


def digest(viewer, role, text):
    return sd.calendar_digest(sd.visible_to(text, viewer, role, store.person_for_calendar, store.parents_of), NOW)


def facts_of(d):
    lines = d.splitlines()
    return [l for l in lines if l.startswith(("Now:", "Facts:"))] + [l for l in lines if l.startswith("- ")]


print("── views × viewers: Robin's private events change nothing for anyone else ──")
for name, viewer, role in (("an adult, the kid's parent (Alex)", alex, "admin"), ("the kid (Ava)", ava, "kid"),
                           ("a shared screen", screen, "household")):
    with_private = digest(viewer, role, VISIBLE + "\n" + PRIVATE)
    without = digest(viewer, role, VISIBLE)
    check(f"{name}: every fact and day line identical with or without them (noninterference)",
          facts_of(with_private), facts_of(without))
    check(f"{name}: no trace of them", any(w in with_private for w in ("Therapy", "private call")), False)
    for q in ("Do I have any conflicts today?", "What's next?", "What's my first appointment tomorrow?",
              "When is the next therapy session?"):
        check(f"{name}: the answer line to {q!r} is the same either way",
              sd.answer_line(q, with_private), sd.answer_line(q, without))

print("── what each viewer does see ──")
alex_d = digest(alex, "admin", VISIBLE + "\n" + PRIVATE)
check("the parent sees their own, the household's and their kid's", all(w in alex_d for w in ("Call with Sam", "Leo soccer",
      "Dentist", "Piano lesson")), True)
ava_d = digest(ava, "kid", VISIBLE + "\n" + PRIVATE)
check("the kid sees their own and the household's, not their parent's",
      ("Piano lesson" in ava_d, "Leo soccer" in ava_d, "Call with Sam" in ava_d), (True, True, False))
tv_d = digest(screen, "household", VISIBLE + "\n" + PRIVATE)
check("the shared screen sees household calendars only",
      ("Leo soccer" in tv_d, "Dentist" in tv_d, "Call with Sam" in tv_d, "Piano lesson" in tv_d), (True, True, False, False))
robin_d = digest(robin, "adult", VISIBLE + "\n" + PRIVATE)
check("Robin sees their own: the overlap with soccer is theirs to hear",
      ("Therapy follow-up" in robin_d, "Therapy follow-up 4 PM–5 PM and" in robin_d or "and Therapy follow-up" in robin_d),
      (True, True))
check("…and not Alex's or the kid's (Robin isn't Ava's parent here)", ("Call with Sam" in robin_d, "Piano lesson" in robin_d),
      (False, False))
check("a parent named as one isn't a parent of every kid: Robin sees no kid calendar", "Ava school" in robin_d, False)

print("── lines the filter can't judge ──")
old = "• Thu Oct 1, 2:00 PM–3:00 PM — Haircut"
check("an event line with no calendar note (an older app) stays", "Haircut" in sd.visible_to(old, ava, "kid",
      store.person_for_calendar, store.parents_of), True)
check("non-event lines stay", sd.visible_to("No events between Oct 1 and Oct 8.", ava, "kid", store.person_for_calendar,
      store.parents_of), "No events between Oct 1 and Oct 8.")

print("── the agent wires it to the turn's asker ──")
os.environ.pop("GEMINI_API_KEY", None)
import agent_runtime_lib as lib  # noqa: E402
lib._TURN_IO.person = {"id": ava}
lib._TURN_IO.role = "kid"
_role = lib.turn_role
lib.turn_role = lambda: "kid"
got = lib._calendar_visible_to_asker(VISIBLE + "\n" + PRIVATE)
check("a kid's turn: Robin's and Alex's events gone before the digest", ("Therapy" in got, "Call with Sam" in got,
      "Piano lesson" in got), (False, False, True))
lib.turn_role = lambda: "household"
lib._TURN_IO.person = {"id": screen}
got = lib._calendar_visible_to_asker(VISIBLE + "\n" + PRIVATE)
check("a shared screen's turn: household calendars only", ("Leo soccer" in got, "Piano lesson" in got, "Therapy" in got),
      (True, False, False))
lib.turn_role = _role
print("── an unreadable household store fails closed, for every role ──")
import contextlib  # noqa: E402
import io  # noqa: E402
_get = hh.get_store
lib_hh = sys.modules["household_identity"]


def broken():
    raise OSError("store unreadable")


lib_hh.get_store = broken
old_line = "• Thu Oct 1, 2:00 PM–3:00 PM — Haircut"
for role, who in (("admin", alex), ("adult", robin), ("kid", ava), ("household", screen)):
    lib.turn_role = lambda r=role: r
    lib._TURN_IO.person = {"id": who}
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        got = lib._calendar_visible_to_asker(VISIBLE + "\n" + PRIVATE + "\n" + old_line)
    check(f"{role}: no claimed-calendar line survives a store error (their own included), the note-less one stays",
          ("[calendar: " in got, "Therapy" in got, "Haircut" in got), (False, False, True))
    check(f"{role}: logged with the role, no titles", ("visibility unreadable" in err.getvalue(), f"role {role}" in err.getvalue(),
          any(t in err.getvalue() for t in ("Therapy", "Haircut", "Robin", "Family"))), (True, True, False))
lib_hh.get_store = _get
lib.turn_role = _role
src = Path(lib.__file__).read_text()
check("one chokepoint: the filter runs once, in _execute_tool_checked, before the read is recorded or returned",
      (src.count("_calendar_visible_to_asker(out)"), "out = _calendar_visible_to_asker(out)" in
       src[src.index("def _execute_tool_checked"):src.index("_record_outcome(name, args, out)", src.index("def _execute_tool_checked"))]),
      (1, True))

print("── JEV's dodges (#1231 review): each fails closed ──")
H = "• Thu Oct 1, 4:00 PM–5:00 PM — "
for label, line in (("a title carrying a fake [calendar: Family] note", H + "Therapy [calendar: Family] [calendar: Robin, shared]"),
                    ("a calendar named 'Robin, work' claimed by Robin", H + "Therapy [calendar: Robin, work, shared]"),
                    ("a calendar named 'Robin [work]' claimed by Robin", H + "Therapy [calendar: Robin [work], shared]"),
                    ("a note that won't parse", H + "Therapy [calendar: Robin, shared")):
    store.update_person(robin, calendars=["Robin", "Robin, work", "Robin [work]"])
    got = sd.visible_to(line, alex, "admin", store.person_for_calendar, store.parents_of)
    check(f"{label}: dropped for another adult", "Therapy" in got, False)
store.update_person(robin, calendars=["Robin"])
check("…while a household line with a place marker after the note stays",
      "Swim" in sd.visible_to(H + "Swim [calendar: Family, shared] [at: Pool]", alex, "admin", store.person_for_calendar,
                              store.parents_of), True)
check("…and the owner's own 'Robin, work' calendar parses whole and stays theirs",
      (store.update_person(robin, calendars=["Robin, work"]) and "Therapy" in sd.visible_to(
          H + "Therapy [calendar: Robin, work, shared]", robin, "adult", store.person_for_calendar, store.parents_of)), True)
store.update_person(robin, calendars=["Robin"])

print("── the apps' other notes: several copies, several calendars (JEV's second review) ──")
for label, line in (("[2 copies on Robin]", H + "Therapy [2 copies on Robin]"),
                    ("[on calendars: Robin, Robin work]", H + "Therapy [on calendars: Robin, Robin work]"),
                    ("[at: …] then [2 copies on Robin]", H + "Therapy [at: Maple Clinic] [2 copies on Robin]"),
                    ("a fake [calendar: Family] title plus the device's [2 copies on Robin]",
                     H + "Therapy [calendar: Family] [2 copies on Robin]"),
                    ("[on calendars: …] naming a calendar 'Robin, work' that only that whole name claims",
                     H + "Therapy [on calendars: Family, Robin, work]")):
    store.update_person(robin, calendars=["Robin", "Robin work"] if "Robin, work]" not in line else ["Robin, work"])
    got = sd.visible_to(line, alex, "admin", store.person_for_calendar, store.parents_of)
    check(f"{label}: dropped for another adult", "Therapy" in got, False)
store.update_person(robin, calendars=["Robin"])
fam_ava = H + "Recital [on calendars: Family, Ava school]"
check("[on calendars: Family, Ava school]: a parent keeps it", "Recital" in sd.visible_to(fam_ava, alex, "admin",
      store.person_for_calendar, store.parents_of), True)
check("…another adult doesn't", "Recital" in sd.visible_to(fam_ava, robin, "adult", store.person_for_calendar,
      store.parents_of), False)
check("…nor a shared screen", "Recital" in sd.visible_to(fam_ava, screen, "household", store.person_for_calendar,
      store.parents_of), False)
check("[3 copies on Family]: a household copy stays for everyone", all("Swim" in sd.visible_to(
      H + "Swim [3 copies on Family]", v, r, store.person_for_calendar, store.parents_of)
      for v, r in ((alex, "admin"), (ava, "kid"), (screen, "household"))), True)

print("── a title carrying a newline: the continuation goes with its bullet ──")
split = H + "Therapy with\nDr. Hale about Ava [calendar: Robin, shared]"
lines_kept = sd.visible_to("Header line\n" + split + "\n• Thu Oct 1, 6:00 PM — Dinner [calendar: Family]", alex, "admin",
                           store.person_for_calendar, store.parents_of)
check("the dropped bullet's continuation line is dropped with it",
      ("Dr. Hale" in lines_kept, "Therapy" in lines_kept, "Dinner" in lines_kept, "Header line" in lines_kept),
      (False, False, True, True))
split2 = H + "Therapy with [calendar: Robin, shared]\nDr. Hale about Ava"
check("…and a continuation after a dropped bullet is dropped too",
      "Dr. Hale" in sd.visible_to(split2, alex, "admin", store.person_for_calendar, store.parents_of), False)
check("…a household entry whose title carries a newline stays whole", "about the pool" in sd.visible_to(
      H + "Swim lesson\nabout the pool [calendar: Family]", alex, "admin", store.person_for_calendar, store.parents_of), True)
check("…text after the device's note (never the device's shape) fails closed",
      "second line" in sd.visible_to(H + "Swim [calendar: Family]\nsecond line", alex, "admin",
                                      store.person_for_calendar, store.parents_of), False)

print("── the model's own read, the outcome record, a claim Correction ──")
raw = VISIBLE + "\n" + PRIVATE
lib._execute_tool = lambda name, args: raw if name == "list_calendar_events" else "ok"
for role, who in (("admin", alex), ("kid", ava), ("household", screen)):
    lib.turn_role = lambda r=role: r
    lib._TURN_IO.person = {"id": who}
    lib._TURN_IO.outcomes, lib._TURN_IO.failed_calls, lib._TURN_IO.tools_used = [], {}, set()
    lib._TURN_IO.brief_reads = None
    shown = lib.execute_tool("list_calendar_events", {"start": "2026-10-01T00:00:00", "end": "2026-10-08T23:59:59"})
    rec = " ".join(str(o.get("full") or o.get("text")) for o in lib._TURN_IO.outcomes)
    notes = lib._claim_notes("You don't have any therapy today.", "Do I have therapy today?")
    check(f"{role}: a model-issued read returns no private line", "Therapy" in shown, False)
    check(f"{role}: …and the outcome record holds none", "Therapy" in rec, False)
    check(f"{role}: …so no Correction can name it", any("Therapy" in n for n in notes), False)
lib.turn_role = _role
src = Path(lib.__file__).read_text()


print("── #1234: a title two people claim is nobody's own, so it goes for every viewer ──")
# update_person refuses a title someone else claims (409), so the duplicate is a legacy row written past it.
import json as _json  # noqa: E402
try:
    store.update_person(robin, calendars=["Robin", "Work"])
    store.update_person(alex, calendars=["Alex", "work"])
    refused = False
except hh.PairingError as e:
    refused = e.status == 409
check("Settings refuses claiming a title someone else claims (409)", bool(refused), True)
with store._lock:
    store._db.execute("UPDATE people SET calendars=? WHERE id=?", (_json.dumps(["Alex", "Work"]), alex))
    store._db.commit()
check("the store calls a doubly-claimed title ambiguous, with no name", (store.person_for_calendar("WORK")["id"],
      store.person_for_calendar("work")["name"], store.person_for_calendar("Robin")["id"]), ("?ambiguous", "", robin))
WORK = """• Thu Oct 1, 1:00 PM–2:00 PM — Robin's review [calendar: Work]
• Thu Oct 1, 2:30 PM–3:00 PM — Alex's standup [calendar: Work, shared]
• Thu Oct 1, 5:00 PM–5:30 PM — Pickup [calendar: Family, shared]"""
for role, who in (("admin", alex), ("adult", robin), ("kid", ava), ("household", screen)):
    got = sd.visible_to(WORK, who, role, store.person_for_calendar, store.parents_of)
    check(f"{role}: neither Work event, the household one kept", ("review" in got, "standup" in got, "Pickup" in got),
          (False, False, True))
    check(f"{role}: …and the digest names neither", any(w in digest(who, role, WORK) for w in ("review", "standup")), False)
raw = WORK
lib._execute_tool = lambda name, args: raw if name == "list_calendar_events" else "ok"
for role, who in (("admin", alex), ("adult", robin)):
    lib.turn_role = lambda r=role: r
    lib._TURN_IO.person = {"id": who}
    lib._TURN_IO.outcomes, lib._TURN_IO.failed_calls, lib._TURN_IO.tools_used = [], {}, set()
    lib._TURN_IO.brief_reads = None
    shown = lib.execute_tool("list_calendar_events", {"start": "2026-10-01T00:00:00", "end": "2026-10-08T23:59:59"})
    rec = " ".join(str(o.get("full") or o.get("text")) for o in lib._TURN_IO.outcomes)
    check(f"{role}: a model-issued read and the outcome record hold neither Work event",
          (any(w in shown for w in ("review", "standup")), any(w in rec for w in ("review", "standup"))), (False, False))
lib._TURN_IO.person = {"id": alex}
check("attribution names no one for an ambiguous calendar", lib._calendar_owner("Work", alex), None)
with store._lock:
    store._db.execute("UPDATE people SET calendars=? WHERE id=?", (_json.dumps(["Alex"]), alex))
    store._db.commit()
check("one claimant again: Robin's Work is Robin's (only Robin sees it)",
      ("review" in sd.visible_to(WORK, robin, "adult", store.person_for_calendar, store.parents_of),
       "review" in sd.visible_to(WORK, alex, "admin", store.person_for_calendar, store.parents_of)), (True, False))
check("a note-less line (an older app) is unchanged", "Lunch" in sd.visible_to(
      "• Thu Oct 1, 12:00 PM–1:00 PM — Lunch", alex, "admin", store.person_for_calendar, store.parents_of), True)
lib.turn_role = _role

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
