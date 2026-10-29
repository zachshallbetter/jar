#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""A note keeps the date it was first saved (#627 follow-up, 10-03: 11 KB notes showed their latest re-save as
"saved"). _kb_index reads the note's current metadata: saved stays the earliest date it has had, and updated
records a later re-save. A new note, or one the runtime can't read, is stamped as given.
Run: python3 scripts/test_kb_first_saved.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["HOUSEHOLD_DB"] = str(Path(tempfile.mkdtemp()) / "household.db")
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import kb_privacy  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


existing: dict[str, dict] = {}
posted: list[dict] = []
lib._KB_META_HOOK = lambda sid: dict(existing.get(sid) or {})   # noqa: E731
lib._runtime_post = lambda svc, path, body, **kw: (posted.append(body) or {"ok": True})   # noqa: E731


def save(sid, saved=None, updated=None):
    md = {"origin": "chat"}
    if saved:
        md["saved"] = saved
    if updated:
        md["updated"] = updated
    lib._kb_index(sid, "t", "text", "note", md)
    return {k: posted[-1]["metadata"].get(k) for k in ("saved", "updated")}


print("── a new note: as given ──")
check("no earlier version", save("note-new", "2026-10-02"), {"saved": "2026-10-02", "updated": None})

print("── a re-save keeps the first date ──")
existing["note-a"] = {"saved": "2026-10-01"}
check("re-saved a day later", save("note-a", "2026-10-02"), {"saved": "2026-10-01", "updated": "2026-10-02"})
existing["note-a"] = {"saved": "2026-10-01", "updated": "2026-10-02"}
check("…and again: saved stays, updated moves on", save("note-a", "2026-10-05"),
      {"saved": "2026-10-01", "updated": "2026-10-05"})
check("…the same day as the last update: updated stays", save("note-a", "2026-10-02"),
      {"saved": "2026-10-01", "updated": "2026-10-02"})
existing["note-b"] = {"saved": "2026-10-02"}
check("re-saved the same day: no updated", save("note-b", "2026-10-02"), {"saved": "2026-10-02", "updated": None})

print("── the backfill: an earlier first date than the note shows ──")
existing["note-c"] = {"saved": "2026-10-03"}
check("saved goes back to the first date, updated keeps the later one",
      save("note-c", "2026-09-30", updated="2026-10-03"), {"saved": "2026-09-30", "updated": "2026-10-03"})

print("── an old note without a date, or an unreadable one ──")
existing["note-d"] = {"origin": "seed"}
check("no saved before: stamped as given", save("note-d", "2026-10-03"), {"saved": "2026-10-03", "updated": None})


def broken(sid):
    raise RuntimeError("retrieval down")


lib._KB_META_HOOK = broken
check("unreadable: stamped as given", save("note-e", "2026-10-03"), {"saved": "2026-10-03", "updated": None})
lib._KB_META_HOOK = None
called = []
kb_privacy.source_meta = lambda call, sid, updated_at="": called.append(sid) or {"saved": "2026-01-01"}   # noqa: E731
check("a test turn never reads the live KB for it", (save("note-f", "2026-10-03"), called),
      ({"saved": "2026-10-03", "updated": None}, []))

print("\n── runtime timestamps parse on titan's Python 3.10 too (#766 rollback) ──")
import local_time  # noqa: E402
for raw, want in [("2026-10-02T17:15:13.2+00:00", "2026-10-02T17:15:13.200000+00:00"),
                  ("2026-10-02T17:15:13.25+00:00", "2026-10-02T17:15:13.250000+00:00"),
                  ("2026-10-02T17:15:13.243082184+00:00", "2026-10-02T17:15:13.243082+00:00"),
                  ("2026-10-02T17:15:13+00:00", "2026-10-02T17:15:13+00:00"),
                  ("2026-10-02T17:15:13Z", "2026-10-02T17:15:13+00:00"),
                  ("2026-10-02T17:15:13.5+00", "2026-10-02T17:15:13.500000+00:00")]:
    got = local_time.parse_iso(raw)
    check(f"parse_iso {raw}", got.isoformat() if got else None, want)
check("parse_iso: not a timestamp", (local_time.parse_iso("soon"), local_time.parse_iso(None)), (None, None))

print("\n── the one-off backfill's plan (kb_saved_backfill.py) ──")
import kb_saved_backfill as bf  # noqa: E402
check("the household date of a UTC write (nanoseconds and all)",
      (bf.household_date("2026-10-02T00:05:21.123456789+00:00"), bf.household_date("2026-10-02T17:15:13.2+00:00")),
      ("2026-10-01", "2026-10-02"))
rows = [{"source_id": "n1", "created_at": "2026-09-30T18:00:00+00:00"},   # first saved Sep 30, shows Oct 2
        {"source_id": "n2", "created_at": "2026-10-02T17:15:13+00:00"},   # saved once, Oct 2: right
        {"source_id": "n3", "created_at": "2026-09-29T18:00:00+00:00"},   # no saved: left alone
        {"source_id": "n4", "created_at": "2026-09-28T18:00:00+00:00"}]   # re-saved twice
meta = {"n1": {"saved": "2026-10-02"}, "n2": {"saved": "2026-10-02"}, "n3": {"origin": "seed"},
        "n4": {"saved": "2026-10-01", "updated": "2026-10-03"}}
check("only the re-saved ones, with their first date and the latest as updated", bf.plan(rows, lambda sid: meta[sid]),
      [{"source_id": "n1", "saved_now": "2026-10-02", "first": "2026-09-30", "updated": "2026-10-02"},
       {"source_id": "n4", "saved_now": "2026-10-01", "first": "2026-09-28", "updated": "2026-10-03"}])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
