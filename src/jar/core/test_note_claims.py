#!/usr/bin/env python3
# gate: hermetic   (temp world, people and fences; fictional names)
"""#860: a saved note's facts become the saver's world claims, with the note as evidence; saving the note again
replaces them (keyed overwrite, the #814 lesson); a fact goes only where its note could be seen.
Run: python3 scripts/test_note_claims.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-note-claims-"))
os.environ.update({"WORLD_DIR": str(_TMP / "world"), "HOUSEHOLD_DB": str(_TMP / "household.db"),
                   "FENCES_DB": str(_TMP / "fences.db"), "AGENT_TZ": "America/Los_Angeles",
                   "AGENT_TURN_SOURCE": "test"})
sys.path.insert(0, str(Path(__file__).parent))

import note_claims as N  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


PEOPLE = [{"id": "zach", "name": "Zach Hale", "role": "admin"}, {"id": "p_robin", "name": "Robin Hale", "role": "adult"},
          {"id": "p_ava", "name": "Ava Hale", "role": "kid", "parents": ["zach", "p_robin"]}]
w = W.Bubbles(_TMP / "world")
OLD = lambda d: {"ref": f"chat:zach:{d}", "label": "Zach, chat", "observed_at": f"{d}T09:00:00"}  # noqa: E731
W.set_fact(w, "Maple Dental", "phone", "555-0100", "zach", "admin", etype="org", people=PEOPLE, household=True,
           source=OLD("2026-09-01"))
W.set_fact(w, "Swim lessons", "schedule", "every Monday", "zach", "admin", etype="activity", people=PEOPLE, household=True,
           source=OLD("2026-09-01"))
W.set_fact(w, "Juniper Clinic", "phone", "555-0300", "p_robin", "adult", etype="org", people=PEOPLE,   # Robin's own,
           source={"ref": "chat:p_robin:2026-09-01", "label": "Robin, chat 09-01", "observed_at": "2026-09-01T09:00:00"})


def value(name, attr, who="zach", role="admin"):
    ids = w.find(name, who, role)
    for f in w.facts(ids[0], who, role) if ids else []:
        if f.attr == attr:
            return [v.value for v in f.values]
    return None


print("── a household note ──")
got = N.from_note(w, "note-maple-dental", "Maple Dental",
                  "Maple Dental hours: Mon-Fri 8 AM to 5 PM. Their front desk number is 555-0199.",
                  "zach", "admin", PEOPLE, "household", observed_at="2026-10-03T09:00:00")
check("its facts become claims on the thing it names", sorted((a, v) for _, a, v, _ in got),
      [("hours", "Mon-Fri 8 AM to 5 PM"), ("phone", "555-0199")])
check("the newer number is the one titan holds now", value("Maple Dental", "phone"), ["555-0199"])
got = N.from_note(w, "note-swim", "Swim lessons", "Swim lessons moved to every Thursday at 4:30 PM.", "zach", "admin",
                  PEOPLE, "household", observed_at="2026-10-03T09:05:00")
check("a schedule, named by the title", [(a, v) for _, a, v, _ in got], [("schedule", "every Thursday, 4:30 PM")])

print("── saving the note again replaces, never adds ──")
N.from_note(w, "note-maple-dental", "Maple Dental", "Maple Dental front desk number is 555-0177.", "zach", "admin",
            PEOPLE, "household", observed_at="2026-10-03T10:00:00")
check("the note's later number is the one titan holds", value("Maple Dental", "phone"), ["555-0177"])

print("── a fact goes only where its note could be seen ──")
got = N.from_note(w, "note-zach-private", "Maple Dental", "Maple Dental number is 555-0111.", "zach", "admin", PEOPLE,
                  "private")
check("a private note never becomes a household claim", (got, value("Maple Dental", "phone")), ([], ["555-0177"]))
got = N.from_note(w, "note-shared", "Swim lessons", "Swim lessons are every Friday.", "zach", "admin", PEOPLE,
                  ["zach", "p_robin"])
check("…nor a note shared with named people", got, [])
got = N.from_note(w, "note-robin", "Juniper Clinic", "Juniper Clinic number is 555-0333.", "p_robin", "adult", PEOPLE,
                  "private")
check("a private note's fact lands in its author's own bubble", ([(a, v) for _, a, v, _ in got],
      value("Juniper Clinic", "phone", "p_robin", "adult")), ([("phone", "555-0333")], ["555-0333"]))
check("…and stays theirs", value("Juniper Clinic", "phone", "zach", "admin") in (None, []), True)

print("── nothing invented ──")
got = N.from_note(w, "note-new", "Cedar Pharmacy", "Cedar Pharmacy number is 555-0444.", "zach", "admin", PEOPLE,
                  "household")
check("a thing titan doesn't know yet: no new entity, no claim", (got, w.find("Cedar Pharmacy", "zach", "admin")), ([], []))
got = N.from_note(w, "note-vague", "Errands", "Call 555-0555 about the order. Open the box Monday.", "zach", "admin",
                  PEOPLE, "household")
check("a number that isn't about anything known: nothing", got, [])
check("a shared device or no person: nothing", N.from_note(w, "n", "Maple Dental", "number is 555-0100", None,
                                                         "household", PEOPLE, "household"), [])

print("── saving a note through ingest_text ──")
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
st = hh.get_store()
W._WORLD = w                              # the tool reads world.get()
lib._kb_index = lambda *a, **k: {}        # no retrieval runtime here
lib.feed_sources_changed = lambda: None
lib._TURN_IO.person, lib._TURN_IO.role = {"id": "zach", "name": "Zach"}, "admin"
lib._TURN_IO.asker_words = 'save this for the family'   # #1397: the asker named the audience
out = lib.execute_tool("ingest_text", {"title": "Maple Dental", "content": "Maple Dental number is 555-0111.",
                                       "share": "household"})
check("off by default (tests can't reach a live world)", ("Recorded in titan" in out, value("Maple Dental", "phone")),
      (False, ["555-0177"]))
os.environ["AGENT_NOTE_CLAIMS"] = "1"
lib._TURN_IO.asker_words = 'save this for the family'   # #1397: the asker named the audience
out = lib.execute_tool("ingest_text", {"title": "Maple Dental", "content": "Maple Dental number is 555-0122.",
                                       "share": "household"})
check("the reply says what it recorded", "Recorded in titan's records too:" in out and "555-0122" in out, True)
check("…and titan holds it", value("Maple Dental", "phone"), ["555-0122"])
os.environ["AGENT_NOTE_CLAIMS"] = "0"
lib._TURN_IO.asker_words = 'save this for the family'   # #1397: the asker named the audience
out = lib.execute_tool("ingest_text", {"title": "Maple Dental", "content": "Maple Dental number is 555-0133.",
                                       "share": "household"})
check("AGENT_NOTE_CLAIMS=0: the note saves, no claim", ("Recorded in titan" in out, value("Maple Dental", "phone")),
      (False, ["555-0122"]))
lib._TURN_IO.person = lib._TURN_IO.role = None

print("── a note's audience is its facts' audience (10-06) ──")
# Reading club: Zach's own thing (his bubble), private until a household note says otherwise
W.set_fact(w, "Reading club", "schedule", "every Tuesday", "zach", "admin", etype="activity", people=PEOPLE,
           source={"ref": "chat:zach:2026-09-02", "label": "Zach, chat", "observed_at": "2026-09-02T09:00:00"})
W.set_fact(w, "Garden plot", "schedule", "every Saturday", "zach", "admin", etype="activity", people=PEOPLE,
           source={"ref": "chat:zach:2026-09-02b", "label": "Zach, chat", "observed_at": "2026-09-02T09:00:00"})
check("his own thing, said in chat: private (Robin and Ava don't see it)",
      (value("Reading club", "schedule", "p_robin", "adult"), value("Reading club", "schedule", "p_ava", "kid")), (None, None))
N.from_note(w, "note-reading-club", "Reading club", "Reading club is every Wednesday.", "zach", "admin", PEOPLE,
            "household", observed_at="2026-10-04T09:00:00")
check("a household note's fact is the household's: the adults and the kid see it",
      (value("Reading club", "schedule", "p_robin", "adult"), value("Reading club", "schedule", "p_ava", "kid")),
      (["every Wednesday"], ["every Wednesday"]))
N.from_note(w, "note-garden", "Garden plot", "Garden plot is every Sunday.", "zach", "admin", PEOPLE,
            "private", observed_at="2026-10-04T09:00:00")
check("a private note's fact stays private", (value("Garden plot", "schedule"), value("Garden plot", "schedule", "p_robin", "adult")),
      (["every Sunday"], None))
print("── the note's audience changes: its facts follow ──")
check("unshared: back to private", (N.refile("note-reading-club", "zach", "private", w),
                                    value("Reading club", "schedule", "p_robin", "adult")), (1, None))
check("shared with named people only: private, never wider than the note",
      (N.refile("note-reading-club", "zach", '["p_robin"]', w), value("Reading club", "schedule", "p_robin", "adult")), (0, None))
check("shared with the household again: the household's", (N.refile("note-reading-club", "zach", "household", w),
                                                           value("Reading club", "schedule", "p_ava", "kid")),
      (1, ["every Wednesday"]))
check("only that note's own facts move (the chat-said one stays the owner's)",
      N.refile("note-garden", "zach", "household", w, apply=False), 1)
print("── the migration: facts recorded private from a household note before 10-06 ──")
W.set_fact(w, "Chess club", "schedule", "every Thursday", "zach", "admin", etype="activity", people=PEOPLE,
           source={"ref": "chat:zach:2026-09-03", "label": "Zach, chat", "observed_at": "2026-09-03T09:00:00"})
W.set_fact(w, "Chess club", "schedule", "every Friday", "zach", "admin", people=PEOPLE,
           source={"ref": "note:note-chess#x", "label": "the note", "observed_at": "2026-10-01T09:00:00"})  # the old way
check("before: the household note's fact sits private", value("Chess club", "schedule", "p_robin", "adult"), None)
check("dry run counts it and changes nothing", (N.refile("note-chess", "zach", "household", w, apply=False),
                                              value("Chess club", "schedule", "p_robin", "adult")), (1, None))
check("applied: the household sees the note's fact", (N.refile("note-chess", "zach", "household", w),
                                                      value("Chess club", "schedule", "p_robin", "adult")), (1, ["every Friday"]))
check("run again: nothing to change", N.refile("note-chess", "zach", "household", w), 0)
print("── what never widens ──")
W.set_fact(w, "Harbor Gas", "billing", "monthly autopay", "zach", "admin", etype="org", people=PEOPLE,
           source={"ref": "note:note-gas#x", "label": "the note", "observed_at": "2026-10-01T09:00:00"},
           note_audience="household")
check("money from a household note: the adults, never the kid", (value("Harbor Gas", "billing", "p_robin", "adult"),
                                                                 value("Harbor Gas", "billing", "p_ava", "kid")),
      (["monthly autopay"], None))
check("…and a refile keeps it at the adults", (N.refile("note-gas", "zach", "household", w),
                                              value("Harbor Gas", "billing", "p_ava", "kid")), (0, None))
check("a statement in chat (no note) never widens, whatever note_audience says",
      W.set_fact(w, "Garden plot", "schedule", "every Monday", "zach", "admin", people=PEOPLE,
                 note_audience="household")[0] and value("Garden plot", "schedule", "p_robin", "adult"), None)
check("another person's note can't touch Zach's facts", N.refile("note-reading-club", "p_robin", "household", w), 0)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
