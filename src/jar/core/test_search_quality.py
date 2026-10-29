#!/usr/bin/env python3
"""Knowledge-base hits worth reading (09-30 review, F-02/03/04): a score floor, downloads only for
file questions, a recurring event once at its next date, and sync lines kept out of the per-turn
recent-activity block.

Run: python3 scripts/test_search_quality.py
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


def cal(day, score, title="Ballet", who="Ava"):
    return {"score": str(score), "metadata": {"source": "calendar", "kind": "appointment"}, "source_id": f"cx-{day}",
            "chunk_text": f"{title}\n{day}, 6:15 PM – 7:45 PM\nLocation: Spokane Ballet Studio\nCalendar: {who}"}


now = lib._local_now()
fut = lambda d: (now + __import__("datetime").timedelta(days=d)).strftime("%a, %b %-d, %Y")   # noqa: E731
past = lambda d: (now - __import__("datetime").timedelta(days=d)).strftime("%a, %b %-d, %Y")   # noqa: E731

print("\n── the floor (\"when does the trash go out?\", nothing stored) ──")
noise = [{"score": "0.438", "metadata": {}, "chunk_text": "Rule: events posted to /v1/triggers…"},
         {"score": "0.428", "metadata": {"source": "mail"}, "chunk_text": "From: Superhuman…"}]
check("passive context, below 0.5: nothing", lib._tidy_hits("when does the trash go out", noise, 5, passive=True), [])
check("an explicit search keeps 0.43 (floor 0.35), drops below it",
      len(lib._tidy_hits("when does the trash go out", noise + [{"score": "0.30", "metadata": {}, "chunk_text": "x"}], 5)), 2)
check("a real hit stays", len(lib._tidy_hits("T-Mobile bill", [{"score": "0.71", "metadata": {}, "chunk_text": "October bill $254.45"}], 5)), 1)

print("\n── downloads only for file questions ──")
dl = {"score": "0.62", "metadata": {"source": "downloads"}, "chunk_text": "Downloaded IMG_2041.HEIC"}
check("passive \"what does Robin like?\": no downloads", lib._tidy_hits("what does Robin like", [dl], 5, passive=True), [])
tax = {"score": "0.6", "metadata": {"source": "downloads"}, "chunk_text": "Downloaded 2025-Form-1040.pdf"}
check("an explicit search for \"2025 tax return\" finds the downloaded PDF (the owner's review)",
      len(lib._tidy_hits("2025 tax return", [tax], 5)), 1)
check("\"find the photo I downloaded\": downloads", len(lib._tidy_hits("find the photo I downloaded", [dl], 5)), 1)

print("\n── a recurring event once, at its next date ──")
hits = [cal(past(9), 0.672), cal(past(2), 0.671), cal(fut(20), 0.670), cal(fut(5), 0.669), cal(fut(12), 0.668),
        cal(fut(3), 0.60, title="Leo skate night", who="Leo"),
        {"score": "0.58", "metadata": {"source": "mail"}, "chunk_text": "Ballet recital tickets"}]
out = lib._tidy_hits("Ava ballet schedule", hits, 5)
check("one ballet, one skate night, the mail: three hits", len(out), 3)
check("the ballet shown is the next one, with how many more", (fut(5) in out[0]["chunk_text"], out[0]["chunk_text"].endswith("(+4 more dates of this event)")), (True, True))
check("Leo's event stays its own (different calendar)", "Leo skate night" in out[1]["chunk_text"], True)
only_past = lib._tidy_hits("ballet", [cal(past(9), 0.7), cal(past(2), 0.69)], 5)
check("all past: the latest one", past(2) in only_past[0]["chunk_text"], True)

print("\n── recent activity skips sync lines (236 of 253 were downloads and calendar) ──")
orig = lib._visible_events
evs = [{"timestamp": "2026-10-01T05:00:00Z", "payload": {"text": "Downloaded IMG_1.PNG", "source": "downloads"}},
       {"timestamp": "2026-10-01T05:01:00Z", "payload": {"text": "Ballet (Ava)", "source": "calendar"}},
       {"timestamp": "2026-10-01T05:02:00Z", "payload": {"text": "Leftover turkey chili for dinner", "person": "zach"}}]
lib._visible_events = lambda stream="household-timeline": evs
real_time = __import__("time").time
__import__("time").time = lambda: 1790831000.0
try:
    ctx = lib._situational_context("anything new?")
    check("only the real event is in the context", ("turkey chili" in ctx, "IMG_1" in ctx, "Ballet (Ava)" in ctx), (True, False, False))
finally:
    lib._visible_events = orig
    __import__("time").time = real_time

print("\n── privacy first: the floor and grouping only see what this person may see ──")
import kb_privacy  # noqa: E402
seen_args = {}
orig_q = kb_privacy.query
kb_privacy.query = lambda call, text, k, person, role, timeout=30: seen_args.update(person=person, role=role, k=k) or [
    {"score": "0.9", "metadata": {}, "chunk_text": "visible to this person"}]
lib._TURN_IO.role, lib._TURN_IO.person = "adult", {"id": "p_mir", "name": "Robin"}
try:
    got = lib._kb_query("anything", 2)
    check("kb_privacy.query runs with the asker, over-fetching, and only its results are tidied (passive default off)",
          (seen_args, [h["chunk_text"] for h in got]), ({"person": "p_mir", "role": "adult", "k": 6}, ["visible to this person"]))
finally:
    kb_privacy.query = orig_q
    lib._TURN_IO.role = lib._TURN_IO.person = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
