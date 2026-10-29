#!/usr/bin/env python3
"""Self-verification (evening goal #2): a reply's playback and update claims are checked against what
the tools said this turn. Live 10-01: "I've started playing The White Stripes on the living room TV"
after a resume, and "Updated the T-Mobile bill record" after saving a note.

Run: python3 scripts/test_verified_claims.py
"""
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
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


def run(reply, *outcomes):
    lib._TURN_IO.outcomes = [{"tool": t, "ok": ok, "write": t in lib._WRITE_TOOLS, "text": x} for t, ok, x in outcomes]
    try:
        return lib._with_verified_claims(reply)
    finally:
        lib._TURN_IO.outcomes = None


print("\n── playback ──")
out = run("I've started playing The White Stripes on the living room TV.",
          ("home_control", True, "Done: Living Room TV → media play"))
check("a resume isn't The White Stripes (10-01 live)", ("Correction: I can't confirm “The White Stripes” is playing" in out,
      "media play" in out), (True, True))
check("an album the phone said it's playing stands", run("Now playing Kind of Blue on your iPhone.",
      ("play_music", True, "Playing the album “Kind of Blue” by Miles Davis on this iPhone.")),
      "Now playing Kind of Blue on your iPhone.")
check("what the TV reported stands", run("I'm playing The Fellowship of the Ring on the living room TV.",
      ("home_status", True, "Living Room TV: playing “The Lord of the Rings: The Fellowship of the Ring”")),
      "I'm playing The Fellowship of the Ring on the living room TV.")
check("no tool at all: corrected", "nothing I ran said so" in run("Now playing Abbey Road on the kitchen speaker."), True)
check("not a playback sentence: untouched", run("I've started a check on the Mac.", ("run_check", True, "Queued")),
      "I've started a check on the Mac.")
check("'nothing is playing on Plex' isn't a claim", run("Nothing is playing on Plex right now."), "Nothing is playing on Plex right now.")

for sentence in ("Leo is playing soccer on Saturday.", "Ava's been playing piano for an hour.",
                 "It's playing The Fellowship of the Ring on the living room TV."):
    check(f"not titan's own claim: {sentence[:34]!r}", run(sentence), sentence)

print("\n── updates ──")
check("someone else's update is a household answer", run("Robin updated the calendar yesterday."),
      "Robin updated the calendar yesterday.")
out = run("Updated the T-Mobile bill record to $254.45 due October 27th.", ("ingest_text", True, "Saved 'T-Mobile bill'"))
check("a note isn't a record update (10-01 live)", "Correction: I didn't change the bill itself; I saved what you said as a note." in out, True)
check("a real record update stands", run("Updated the bill record to $254.45.", ("update_record", True, "Updated bill-tmobile")),
      "Updated the bill record to $254.45.")
check("a calendar update needs update_calendar_event", "didn't change the event" in run("I've updated your dentist event to 3 PM."), True)
check("outside a turn: untouched", lib._with_verified_claims("Now playing Abbey Road on the speaker."),
      "Now playing Abbey Road on the speaker.")

print("\n── a content search that stopped at the cap (Spotlight off, 10-01) ──")
CAPPED = ("[no matches for “plain_failure” in ~]\n(searched 20,000 files in 9.1 s)\n"
          "(stopped at 20,000 files — narrow the path or add a glob to search the rest)")


def capped_reply(reply, result=CAPPED):
    lib._TURN_IO.outcomes = []
    lib._record_outcome("search_file_contents", {"query": "plain_failure"}, result)
    try:
        return lib._with_verified_claims(reply)
    finally:
        lib._TURN_IO.outcomes = None


check("'couldn't find' after a capped search says it was partial",
      capped_reply("I couldn't find plain_failure on your Mac.").endswith(
          "Correction: the search inside files under ~ stopped at 20,000 files, so it didn't cover everything; "
          "searching a narrower folder would."), True)
check("'found no mention' is a not-found too",
      "didn't cover everything" in capped_reply("I searched your files and found no mention of a Lenora lease renewal."), True)
check("a reply that already says what was searched is left alone",
      capped_reply("Nothing in the first 20,000 files under ~; want me to look in ~/Projects?"),
      "Nothing in the first 20,000 files under ~; want me to look in ~/Projects?")
check("a complete search's 'not found' is left alone",
      capped_reply("I couldn't find it.", "[no matches for “x” in ~/Projects/app]\n(searched 812 files in 0.4 s)"),
      "I couldn't find it.")
check("a capped search that found something is left alone",
      capped_reply("Found it in agent_runtime_lib.py.", "[“x” in ~: 1 match in 1 file]\n(stopped at 20,000 files — narrow)"),
      "Found it in agent_runtime_lib.py.")
check("the time limit is named too",
      "stopped at the time limit" in capped_reply("No files matched.", "[no matches for “x” in ~]\n"
                                                   "(stopped at the time limit — narrow the path)"), True)

print("\n── wired in ──")
orig = lib._run_agent_turn_inner
lib._run_agent_turn_inner = lambda *a, **k: (lib._record_outcome("home_control", {}, "Done: Living Room TV → media play")
                                             or "Now playing The White Stripes on the living room TV.")
try:
    check("every turn's reply is verified", "Correction:" in lib._run_agent_turn([{"role": "user", "content": "play it"}]), True)
finally:
    lib._run_agent_turn_inner = orig

print("\n── repeating events (10-01: garbage night became one event) ──")
def via_record(reply, *calls, deletions=None):
    lib._TURN_IO.outcomes = []
    lib._TURN_IO.deletions = list(deletions or [])
    for name, args, result in calls:
        lib._record_outcome(name, args, result)
    try:
        return lib._with_deletion_summary(lib._with_verified_claims(reply))
    finally:
        lib._TURN_IO.outcomes = None
        lib._TURN_IO.deletions = []


ONE = ("create_calendar_event", {"title": "Take out garbage"}, "Added “Take out garbage” on Wed Oct 7, 8:00 PM–9:00 PM (Household)")
SERIES = ("create_calendar_event", {"title": "Take out garbage"},
          "Added “Take out garbage” on Wed Oct 7, 8:00 PM–9:00 PM (Household), repeating every week on Wednesday")
check("a single event called repeating gets corrected",
      "Correction: it doesn't repeat" in via_record("I added garbage night, repeating every Wednesday.", ONE), True)
check("a real series stands", "Correction" in via_record("Added garbage night every Wednesday at 8 PM.", SERIES), False)
check("'every Wednesday' with no calendar change isn't a claim",
      "Correction" in via_record("Got it: garbage goes out every Wednesday night.",
                                 ("ingest_text", {"title": "Garbage"}, "Saved “Garbage” to the knowledge base.")), False)
check("a one-off reply that doesn't claim repeating stands",
      "Correction" in via_record("I added it for Wednesday, October 7 at 8 PM.", ONE), False)

print("\n── a move isn't a removal (10-01: \"Removed: …\" after moving it to the household calendar) ──")
MOVED = ("create_calendar_event", {"title": "Take out garbage & recycling"},
         "Added “Take out garbage & recycling” on Wed Oct 7, 8:00 PM (hamilbetter@gmail.com)")
check("deleted then re-added under the same title: no 'Removed' line",
      "Removed:" in via_record("Moved it to the household calendar.", MOVED, deletions=["Take out garbage & recycling"]), False)
check("a real removal still gets its line",
      "Removed: “Dentist”" in via_record("Done.", MOVED, deletions=["Dentist"]), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
