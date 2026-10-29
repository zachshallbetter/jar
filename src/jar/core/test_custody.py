#!/usr/bin/env python3
# gate: hermetic
"""Where the kids are staying (custody.py): the alternating pattern a parent stated, a stretch said in chat, a
calendar event a rule names (a hidden calendar too), the "Kids: …" line and who sees it, and "their dad" never
read as a household adult (09-30: "Kids with Mom" read backwards ~10 times; "with their dad" read as Zach).

Run: python3 scripts/test_custody.py
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-custody-test-"))
os.environ["CUSTODY_DB"] = str(_TMP / "custody.db")
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ["AGENT_TURN_LOG"] = str(_TMP / "turns.jsonl")
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import custody as C  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


PT = ZoneInfo("America/Los_Angeles")
T = lambda *a: datetime(*a, tzinfo=PT)   # noqa: E731
PEOPLE = [{"id": "zach", "name": "Zach Hale", "role": "admin"},
          {"id": "rob", "name": "Robin", "role": "adult"},
          {"id": "leo", "name": "Leo", "role": "kid", "parents": ["zach", "rob"], "other_parent": "their dad"},
          {"id": "ava", "name": "Ava", "role": "kid", "parents": ["zach", "rob"], "other_parent": "their dad"}]

print("\n── nothing stated: nothing said ──")
check("no line", C.line(PEOPLE, "admin", T(2026, 10, 2, 18, 5)), None)

print("\n── the pattern Zach stated (alternate weeks, Thursday 9:00; at their dad's until Thu 10-08) ──")
C.set_pattern(["leo", "ava"], T(2026, 10, 1, 9, 0), 7, "zach", "chat 10-02 18:05")
s = C.state_at("ava", T(2026, 10, 2, 18, 5))
check("away now, until Thu 9:00, then here", (s["state"], s["until"], s["then"]), ("away", T(2026, 10, 8, 9, 0), "here"))
check("here the next week", C.state_at("leo", T(2026, 10, 8, 9, 30))["state"], "here")
check("away again the week after", C.state_at("leo", T(2026, 10, 15, 9, 0))["state"], "away")
line = C.line(PEOPLE, "admin", T(2026, 10, 2, 18, 5))
check("one line, other parent named as the household says it, provenance",
      line, "Kids (where they're staying; their other parent isn't anyone in this household): Leo and Ava are with "
            "their dad until Thu Oct 8, 9 AM, then here (Zach, chat 10-02 18:05) — alternating.")
check("a kid sees it", C.line(PEOPLE, "kid", T(2026, 10, 2, 18, 5)) == line, True)
check("a shared device doesn't", C.line(PEOPLE, "household", T(2026, 10, 2, 18, 5)), None)
check("no label: their other parent",
      "with their other parent" in C.line([{**p, "other_parent": None} for p in PEOPLE], "adult", T(2026, 10, 2, 18, 5)), True)

print("\n── a stretch said in chat beats the pattern ──")
C.set_span(["ava"], "here", T(2026, 10, 3, 12, 0), T(2026, 10, 4, 17, 0), "chat", "rob", "chat 10-03 11:00")
s = C.state_at("ava", T(2026, 10, 3, 15, 0))
check("Ava here for the weekend, then back to the pattern",
      (s["state"], s["until"], s["then"], s["said_by"]), ("here", T(2026, 10, 4, 17, 0), "away", "rob"))
check("Leo unchanged", C.state_at("leo", T(2026, 10, 3, 15, 0))["state"], "away")
two = C.line(PEOPLE, "admin", T(2026, 10, 3, 15, 0))
check("two groups in one line", ("Leo is with their dad until" in two, "Ava is here until Sun Oct 4, 5 PM, then with their dad (Robin" in two),
      (True, True))

print("\n── a calendar event a rule names (hidden calendars too) ──")
ev = {"node_id": "cal-1", "data": {"title": "Kids with Mom", "calendar": "Hamilton",
                                   "start": "2026-10-22T09:00:00-07:00", "end": "2026-10-29T09:00:00-07:00"}}
short = {"node_id": "cal-2", "data": {"title": "Kids with Mom", "start": "2026-10-22T09:00:00-07:00",
                                      "end": "2026-10-22T10:00:00-07:00"}}
check("no rule: nothing read", C.from_calendar([ev], ["leo", "ava"], PT), 0)
C.set_rule("Kids with Mom", "here", "zach")
check("a rule: the week-long event is read, the hour-long one isn't", C.from_calendar([ev, short], ["leo", "ava"], PT), 1)
check("'Kids with Mom' means here (it was read backwards)", C.state_at("leo", T(2026, 10, 23, 12, 0))["state"], "here")
C.set_span(["leo"], "away", T(2026, 10, 23, 0, 0), T(2026, 10, 24, 0, 0), "chat", "zach", "chat")
check("a parent's chat beats the calendar", C.state_at("leo", T(2026, 10, 23, 12, 0))["state"], "away")

print("\n── the people store: other_parent is a kid's, never a household adult ──")
import household_identity as hh  # noqa: E402
st = hh.get_store()
k = st.add_person("Leo", "kid") if hasattr(st, "add_person") else None
kid_id = (k or {}).get("id") if isinstance(k, dict) else None
if kid_id:
    check("set on a kid", st.update_person(kid_id, other_parent="their dad").get("other_parent"), "their dad")
    try:
        st.update_person("zach", other_parent="x")
        check("refused on an adult", False, True)
    except hh.PairingError:
        check("refused on an adult", True, True)
else:
    print("  (no add_person on this store: skipped)")

print("\n── the roster says their dad isn't Zach ──")
import agent_runtime_lib as lib  # noqa: E402
lib._PEOPLE_CACHE["at"] = 1e18
lib._PEOPLE_CACHE["people"] = PEOPLE
r = lib.household_roster()
check("roster", r, "The household: Zach and Robin (adults); Leo and Ava (kids). Leo and Ava also have a "
                   "parent outside the household (“their dad”), who isn't Zach and Robin.")

print("\n── kids_staying: a parent says it in chat ──")
lib._TURN_IO.person, lib._TURN_IO.role = {"id": "zach"}, "admin"
got = lib._kids_staying({"state": "with their dad", "until": "2026-12-03T09:00", "from": "2026-11-26T09:00"})
check("says what it recorded", got.split("\n")[0], "Recorded: Leo with their dad from Thu Nov 26, 9 AM until Thu Dec 3, 9 AM.")
check("in the store, as Zach's chat statement", C.state_at(kid_id or "leo", T(2026, 11, 27, 12, 0))["said_by"], "zach")
check("no end: not recorded", lib._kids_staying({"state": "here"}).startswith("Not recorded"), True)
lib._TURN_IO.person, lib._TURN_IO.role = {"id": "stranger"}, "adult"
check("not their parent: not recorded", lib._kids_staying({"state": "here", "until": "2026-12-01T09:00"}).startswith("Not recorded"), True)
lib._TURN_IO.person, lib._TURN_IO.role = {"id": "zach"}, "admin"
check("an unknown name is refused, never dropped (#807)", lib._kids_staying({"kids": "Leo, Lo", "state": "here",
      "until": "2026-12-01T09:00"}), "Not recorded: I don't know “Lo” among your kids (Leo). Which kids did they mean?")
st.add_person("Hale Jr", "kid")
lib._TURN_IO.person, lib._TURN_IO.role = {"id": "mir_store"}, "adult"
mir_p = st.add_person("Jordan", "adult")["id"]
st.update_person(kid_id, parents=[mir_p]) if kid_id else None
lib._TURN_IO.person = {"id": mir_p}
check("a kid who isn't theirs: says whose call it is", lib._kids_staying({"kids": "Hale", "state": "here",
      "until": "2026-12-01T09:00"}), "Not recorded: only “Hale”'s parents can say where they're staying.")
lib._TURN_IO.person, lib._TURN_IO.role = None, None

print("\n── a correction restates the stretch (#807) ──")
C.set_span(["leo", "ava"], "away", T(2027, 1, 4, 9, 0), T(2027, 1, 14, 9, 0), "chat", "zach", "chat 01-04")
C.set_span(["leo", "ava"], "away", T(2027, 1, 6, 12, 0), T(2027, 1, 12, 9, 0), "chat", "zach", "back Tuesday, not Thursday")
check("away until the corrected end", (C.state_at("ava", T(2027, 1, 8, 12, 0))["until"]), T(2027, 1, 12, 9, 0))
check("…and after it the old chat stretch is gone (only the alternating pattern speaks)",
      (C.state_at("ava", T(2027, 1, 13, 12, 0)) or {}).get("source", "pattern"), "pattern")
check("…while the earlier part still stands", C.state_at("leo", T(2027, 1, 5, 12, 0))["state"], "away")
C.set_span(["leo"], "away", T(2027, 2, 1, 9, 0), T(2027, 2, 11, 9, 0), "chat", "zach", "chat 02-01")
C.set_span(["leo"], "here", T(2027, 2, 5, 17, 0), T(2027, 2, 7, 17, 0), "chat", "zach", "home for the weekend")
check("a break inside a stretch (the other state) trims nothing: away again after it",
      (C.state_at("leo", T(2027, 2, 6, 12, 0))["state"], C.state_at("leo", T(2027, 2, 9, 12, 0))["state"]), ("here", "away"))
C.set_span(["leo", "ava"], "away", T(2027, 3, 1, 9, 0), T(2027, 3, 11, 9, 0), "chat", "zach", "chat 03-01")
C.set_span(["ava"], "away", T(2027, 3, 1, 9, 0), T(2027, 3, 4, 9, 0), "chat", "zach", "Ava's back Thursday")
check("a correction for one kid leaves the other's stretch as it was",
      ((C.state_at("ava", T(2027, 3, 6, 12, 0)) or {}).get("source", "pattern"),
       C.state_at("leo", T(2027, 3, 6, 12, 0))["source"], C.state_at("leo", T(2027, 3, 6, 12, 0))["until"]),
      ("pattern", "chat", T(2027, 3, 11, 9, 0)))

print("\n── custody never guesses: an earlier stretch it may add to is kept, and asked about ──")
old_ref = C.set_span(["leo", "ava"], "away", T(2027, 4, 3, 9, 0), T(2027, 4, 4, 17, 0), "chat", "zach", "away Sat-Sun")
left = []
C.set_span(["leo", "ava"], "away", T(2027, 4, 2, 20, 0), T(2027, 4, 3, 10, 0), "chat", "zach", "Fri night to Sat 10am",
           left=left)
check("starts before it and ends before its end: the Sat-Sun stay is kept (maybe an extra night)",
      (C.state_at("ava", T(2027, 4, 4, 12, 0))["said"], [r["ref"] for r in left]), ("away Sat-Sun", [old_ref]))
check("…and Friday night is recorded too", C.state_at("leo", T(2027, 4, 2, 22, 0))["said"], "Fri night to Sat 10am")
C.set_span(["leo"], "away", T(2027, 4, 10, 9, 0), T(2027, 4, 12, 9, 0), "chat", "zach", "away Sat-Mon")
left = []
C.set_span(["leo"], "away", T(2027, 4, 9, 18, 0), None, "chat", "zach", "away from Friday, no end yet", left=left)
check("no end covers the old one's tail: restated, nothing to ask",
      (C.state_at("leo", T(2027, 4, 11, 12, 0))["said"], left), ("away from Friday, no end yet", []))
if kid_id:
    lib._TURN_IO.person, lib._TURN_IO.role = {"id": "zach"}, "admin"
    C.set_span([kid_id], "away", T(2027, 5, 1, 9, 0), T(2027, 5, 2, 17, 0), "chat", "zach", "away Sat-Sun")
    got = lib._kids_staying({"kids": "Leo", "state": "away", "from": "2027-04-30T20:00", "until": "2027-05-01T10:00"})
    check("the tool's reply asks whether the earlier stretch is still on",
          got.split("\n")[0], "Recorded: Leo with their dad from Fri Apr 30, 8 PM until Sat May 1, 10 AM. They were already "
                               "down as away Sat May 1, 9 AM to Sun May 2, 5 PM. Is that still on?")
    check("…and kept it", C.state_at(kid_id, T(2027, 5, 2, 12, 0))["said"], "away Sat-Sun")
    C.set_span([kid_id], "away", T(2027, 6, 1, 9, 0), T(2027, 6, 10, 9, 0), "chat", "zach", "away until the 10th")
    got = lib._kids_staying({"kids": "Leo", "state": "away", "from": "2027-06-04T12:00", "until": "2027-06-08T09:00"})
    check("said mid-stay with an earlier end (\"back Tuesday, not Thursday\"): restated, nothing asked",
          ("Is that still on" in got, C.state_at(kid_id, T(2027, 6, 9, 12, 0)) is None
           or C.state_at(kid_id, T(2027, 6, 9, 12, 0))["source"] != "chat"), (False, True))
    lib._TURN_IO.person, lib._TURN_IO.role = None, None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
