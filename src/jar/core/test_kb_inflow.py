#!/usr/bin/env python3
# gate: hermetic
"""What reaches the knowledge base at ingest (10-03 cleanup, Zach: "we've pulled in a ton of garbage"): no downloads,
browsing or daily-log notes; no subscribed or holiday calendars (birthdays stay) and no same-day duplicate events; a chat save
only on an explicit request, as the person's own words (never "Assistant: …").
Run: python3 scripts/test_kb_inflow.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-kbinflow-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "CONNECTORS_LEDGER": "ledger.db", "AGENT_MEMORY_DIR": "memory",
             "KB_ACL_DB": "kb_acl.db", "HOUSEHOLD_PREFS": "household.json", "DAILY_LOG_DIR": "daily",
             "PROACTIVE_DB": "proactive.db", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("ITEM_REFERENCES", None)
sys.path.insert(0, str(Path(__file__).parent))
import connectors_runner as cx  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


calls: list[tuple[str, str, str, dict | None]] = []


def rt(service, path, method, payload):
    calls.append((service, path, method, payload))
    return {}


def indexed():
    return [c[3]["source_id"] for c in calls if c[1] == "/v1/retrieval/index"]


def deleted():
    return [c[1].rsplit("/", 1)[1] for c in calls if c[2] == "DELETE" and "/retrieval/sources/" in c[1]]


print("── downloads, browsing and the daily log stay out of the KB ──")
kept_other = {}
for source, item in (("downloads", {"source_id": "d-1", "title": "Robin_resume.pdf", "text": "Robin_resume.pdf in Downloads",
                                    "fields": {"path": "~/Downloads/Robin_resume.pdf"}, "timeline": "Downloaded a file"}),
                     ("browsing", {"source_id": "b-1", "title": "Browsing 10-03", "text": "Visited: a recipe site; a bank"}),
                     ("daily", {"source_id": "2026-10-03", "title": "Daily log", "text": "- 09:00 ran a shell command"})):
    calls.clear()
    st = cx.ingest(source, [item], runtime=rt, owner="zach")
    check(f"{source}: written to the ledger, not indexed", (st["written"], indexed()), (1, []))
    kept_other[source] = sorted({c[1] for c in calls if c[1] in ("/v1/nodes", "/v1/events")})
check("…a download's record node and timeline line stay", kept_other["downloads"], ["/v1/events", "/v1/nodes"])
calls.clear()
cx.ingest("downloads", [{"source_id": "d-1", "title": "Robin_resume.pdf", "text": "Robin_resume.pdf (moved)",
                         "fields": {"path": "~/Downloads/Robin_resume.pdf"}}], runtime=rt, owner="zach")
check("mail still goes in (triage aside)", (cx.ingest("mail", [{"source_id": "m-1", "title": "Dinner Friday?",
      "text": "Robin: dinner at ours Friday at 7?"}], runtime=rt, owner="zach")["written"], len(indexed())), (1, 1))

print("── calendars: subscribed and holiday calendars out, birthdays in; one copy per title and day ──")


def ev(sid, title, start, cal, acct="iCloud"):
    return {"source_id": sid, "title": title, "text": f"{title} on {start[:10]}",
            "fields": {"calendar": cal, "account_name": acct, "start": start, "end": start}}


calls.clear()
cx.ingest("calendar", [ev("c-1", "Labor Day", "2026-09-07T00:00:00", "US Holidays", "Subscribed Calendars"),
                       ev("c-2", "Labor Day", "2026-09-07T00:00:00", "Holidays in United States", "Household"),
                       ev("c-3", "Leo's birthday", "2026-10-01T00:00:00", "Birthdays", "Other"),
                       ev("c-4", "Soccer practice", "2026-10-05T16:00:00", "Family"),
                       ev("c-5", "Soccer practice", "2026-10-05T16:00:00", "Robin"),
                       ev("c-6", "Soccer practice", "2026-10-12T16:00:00", "Family")], runtime=rt, owner="zach")
got = indexed()
check("a subscribed calendar, a holiday calendar under any account: not indexed; a birthdays calendar: indexed",
      [cx.item_id("calendar", s) in got for s in ("c-1", "c-2", "c-3")], [False, False, True])
check("the same event on two calendars the same day: once; next week's: its own",
      [cx.item_id("calendar", s) in got for s in ("c-4", "c-5", "c-6")], [True, False, True])

print("── a re-synced item that no longer belongs in the KB leaves it ──")
calls.clear()
import sqlite3  # noqa: E402
con = sqlite3.connect(os.environ["CONNECTORS_LEDGER"])
con.execute("UPDATE items SET kb=1 WHERE source='browsing'")   # as if indexed before this change
con.commit(); con.close()
cx.ingest("browsing", [{"source_id": "b-1", "title": "Browsing 10-03", "text": "Visited: a recipe site (again)"}],
          runtime=rt, owner="zach")
check("the old KB copy is deleted", deleted(), [cx.item_id("browsing", "b-1")])

print("── a chat save: only on an explicit request, as the person's own words ──")
import agent_runtime_lib as lib  # noqa: E402
check("the statement, without the request words", [lib.save_statement(p) for p in (
    "Remember that the gate code for the pool is 4471", "Note that Robin's flight lands at 6",
    "Please remember: the recycling goes out Tuesdays", "What's the weather?", "remember")],
    ["The gate code for the pool is 4471", "Robin's flight lands at 6", "The recycling goes out Tuesdays", None, None])
saved: list[tuple] = []
saved_kb = lib._kb_index
lib._kb_index = lambda *a, **k: saved.append((a, k))
lib._TURN_IO.role, lib._TURN_IO.person, lib._TURN_IO.source = "admin", {"id": "zach", "name": "Alex"}, "user"
saved_jev = lib._jev_lasting_fact
lib._jev_lasting_fact = lambda prompt: True   # Jev says "a lasting fact": it no longer saves on its own
try:
    lib._maybe_save_fact("The plumber from AquaFix is coming Thursday.", "Got it, I'll keep that in mind for Thursday.")
    check("Jev's lasting-fact verdict alone saves nothing", saved, [])
    lib._maybe_save_fact("Remember that the plumber from AquaFix is coming Thursday.",
                         "Got it. The plumber from AquaFix is coming Thursday.")
    text = saved[0][0][2] if saved else ""
    check("an explicit save: the person's statement only", (text, "Assistant" in text, "household said" in text.lower()),
          ("The plumber from AquaFix is coming Thursday.", False, False))
    saved.clear()
    lib._maybe_save_fact("Remember that the plumber is coming.", "Done.", noted=True)
    check("the turn already saved a note: nothing more", saved, [])
finally:
    lib._kb_index, lib._jev_lasting_fact = saved_kb, saved_jev

print("── the daily log is a file, never a KB note ──")
saved.clear()
lib._kb_index = lambda *a, **k: saved.append((a, k))
try:
    lib._append_daily_log("ran run_shell: ls ~/Downloads", person="zach")
finally:
    lib._kb_index = saved_kb
files = list((_TMP / "daily").glob("*.md"))
check("written to the day's file, no KB note", (len(files), "run_shell" in files[0].read_text() if files else False, saved),
      (1, True, []))

print("── legacy ledger rows (no owner or upstream id) still take a tombstone (10-04: 299 of 326 were skipped) ──")
con = sqlite3.connect(os.environ["CONNECTORS_LEDGER"])
for key, iid in (("m-old", "cx-mail-legacy1"), ("robin:m-old2", "cx-mail-legacy2"), ("m-old3", "cx-mail-legacy3")):
    con.execute("INSERT INTO items (source, source_id, content_hash, item_id, kb, node, synced_at, owner, upstream_id) "
                "VALUES ('mail', ?, 'h', ?, 1, 0, 0, NULL, NULL)", (key, iid))
con.execute("INSERT INTO versions (source, source_id, item_id, change, owner, audience, at) "
            "VALUES ('mail', 'robin:m-old2', 'cx-mail-legacy2', 'created', 'robin', 'private', 0)")
con.commit(); con.close()
check("a legacy row: the default owner, its key as the upstream id", cx.tombstone_item("cx-mail-legacy1"),
      {"owner": "zach", "source": "mail", "source_id": "m-old"})
check("another person's legacy row: owner from its versions, the bare id upstream", cx.tombstone_item("cx-mail-legacy2"),
      {"owner": "robin", "source": "mail", "source_id": "m-old2"})
check("…and the device is told", [t["source_id"] for t in cx.tombstones("robin")["tombstones"]], ["m-old2"])
cx.status()   # any open backfills
con = sqlite3.connect(os.environ["CONNECTORS_LEDGER"])
check("an untouched legacy row is backfilled on open, so forget(owner=) finds it",
      con.execute("SELECT owner, upstream_id FROM items WHERE item_id='cx-mail-legacy3'").fetchone(), ("zach", "m-old3"))
con.close()
import contextlib, io  # noqa: E401,E402
err = io.StringIO()
with contextlib.redirect_stderr(err):
    got = cx.tombstone_item("cx-mail-unknown")
check("an item the ledger doesn't have: None, and said", (got, "tombstone skipped" in err.getvalue()), (None, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
