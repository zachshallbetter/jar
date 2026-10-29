#!/usr/bin/env python3
# gate: hermetic
"""A repeating calendar event is one KB item (10-02 audit: Ballet ×28 crowded "When is Ava's ballet?"): written from
its occurrences, refreshed when one goes, removed when they all do; the migration does the same for what was synced
before. Counts only.

Run: python3 scripts/test_calendar_series.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

os.environ["AGENT_TZ"] = "America/Los_Angeles"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os_acl, tempfile as _tf_acl  # noqa: E402
_os_acl.environ.setdefault("KB_ACL_DB", _os_acl.path.join(_tf_acl.mkdtemp(prefix="titan-acl-"), "kb_acl.db"))  # #1273: ingest writes sharing rows; never titan's own
import calendar_series_migrate as M  # noqa: E402
import connectors_runner as cx  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


class RT:
    def __init__(self):
        self.kb = {}

    def __call__(self, svc, path, method, payload):
        if svc == "retrieval" and method == "POST":
            self.kb[payload["source_id"]] = payload
        elif svc == "retrieval" and method == "DELETE":
            self.kb.pop(path.rsplit("/", 1)[-1], None)
        return {}


PT = ZoneInfo("America/Los_Angeles")


def occ(day, start="00:15"):
    s = f"2026-{day}T{start}:00Z"
    return {"source_id": f"BALLET-UID@{s}", "title": "Ballet", "kind": "appointment",
            "text": f"Ballet\nMon, Sep 28, 2026, 5:15 PM\nLocation: 12 Adams St\nCalendar: Ava\nNotes: bring shoes",
            "fields": {"start": s, "calendar": "Ava", "location": "12 Adams St"}}


print("\n── the series note ──")
starts = [datetime(2026, 9, d, 17, 15, tzinfo=PT) for d in (14, 16, 21, 23, 28, 30)]
t = cx.series_text("Ballet", starts, datetime(2026, 9, 29, 12, 0, tzinfo=PT), occ("09-29")["text"])
check("when it repeats, the next one, the count; its place and notes kept, its date line not",
      t.splitlines(), ["Ballet", "Repeats: Mondays and Wednesdays at 5:15 PM (6 synced, Sep 14 to Sep 30)",
                       "Next: Wed, Sep 30, 2026, 5:15 PM", "Location: 12 Adams St", "Calendar: Ava", "Notes: bring shoes"])
check("all past: the last one", cx.series_text("Ballet", starts, datetime(2026, 10, 2, tzinfo=PT), "Ballet").splitlines()[2],
      "Last: Wed, Sep 30, 2026, 5:15 PM")
check("one occurrence doesn't claim a rhythm", cx.series_text("Leo", [datetime(2026, 9, 16, 8, 45, tzinfo=PT)],
      datetime(2026, 10, 2, tzinfo=PT), "Leo\nWed, Sep 16, 2026, 8:45 AM\nLocation: McDonald Elementary").splitlines(),
      ["Leo", "Was: Wed, Sep 16, 2026, 8:45 AM", "Location: McDonald Elementary"])
check("an upcoming one: When", cx.series_text("Dentist", [datetime(2026, 10, 9, 9, 0, tzinfo=PT)],
      datetime(2026, 10, 2, tzinfo=PT), "Dentist").splitlines()[1], "When: Fri, Oct 9, 2026, 9:00 AM")
check("a one-off isn't a series", cx.series_key("calendar", "DENTIST-UID"), None)
check("an owner's key keeps its prefix", cx.series_key("calendar", "p_ava:UID@2026-09-28T00:15:00Z"), "p_ava:UID")
check("not calendar: none", cx.series_key("mail", "a@b"), None)

print("\n── ingest: one KB item per series ──")
rt, led = RT(), Path(tempfile.mkdtemp()) / "ledger.db"
out = cx.ingest("calendar", [occ("09-29"), occ("10-01"), occ("10-06")], runtime=rt, ledger=led)
check("three occurrences written, one KB item", (out["written"], len(rt.kb)), (3, 1))
item = next(iter(rt.kb.values()))
check("it's the series, private, with its count", (item["metadata"]["kind"], item["metadata"]["audience"],
                                                   item["metadata"]["occurrences"]), ("series", "private", 3))
import kb_privacy as _P  # noqa: E402
check("…and its sharing-table row: its owner, private (#1273)", _P.acl_rows([item["source_id"]]).get(item["source_id"]),
      _P.acl_of(item["metadata"]))
cx.ingest("calendar", [{"source_id": occ("10-06")["source_id"], "deleted": True, "deleted_reason": "deleted"}], runtime=rt, ledger=led)
check("one goes: the series is rewritten with two", next(iter(rt.kb.values()))["metadata"]["occurrences"], 2)
cx.ingest("calendar", [{"source_id": occ(d)["source_id"], "deleted": True, "deleted_reason": "deleted"} for d in ("09-29", "10-01")], runtime=rt, ledger=led)
check("all gone: the series item goes", rt.kb, {})
one = {"source_id": "DENTIST-UID", "title": "Dentist", "text": "Dentist\nFri, Oct 9, 2026, 9:00 AM", "fields": {"start": "2026-10-09T16:00:00Z"}}
cx.ingest("calendar", [one], runtime=rt, ledger=led)
check("a one-off keeps its own item", [p["metadata"].get("kind") for p in rt.kb.values()], ["calendar"])

print("\n── the same event on two calendars is one item (holidays) ──")
rt3, led3 = RT(), Path(tempfile.mkdtemp()) / "ledger.db"
def hol(uid, cal):
    return {"source_id": f"{uid}@2026-11-26T08:00:00Z", "title": "Thanksgiving Day", "kind": "appointment",
            "text": f"Thanksgiving Day\nThu, Nov 26, 2026\nCalendar: {cal}", "fields": {"start": "2026-11-26T08:00:00Z",
                                                                                   "calendar": cal, "all_day": True}}
cx.ingest("calendar", [hol("HOL-A", "Family"), hol("HOL-B", "Robin")], runtime=rt3, ledger=led3)
check("two calendars, one KB item", [p["metadata"]["series"] for p in rt3.kb.values()], ["HOL-A"])
cx.ingest("calendar", [{"source_id": hol("HOL-A", "x")["source_id"], "deleted": True, "deleted_reason": "deleted"}], runtime=rt3, ledger=led3)
check("the kept one goes: its twin is written", [p["metadata"]["series"] for p in rt3.kb.values()], ["HOL-B"])
other = {**hol("HOL-C", "Ava"), "source_id": "HOL-C@2026-12-03T08:00:00Z", "fields": {"start": "2026-12-03T08:00:00Z"}}
cx.ingest("calendar", [other], runtime=rt3, ledger=led3)
rt4, led4 = RT(), Path(tempfile.mkdtemp()) / "ledger.db"
cx.ingest("calendar", [hol("HOL-B", "Robin")], runtime=rt4, ledger=led4)
cx.ingest("calendar", [hol("HOL-A", "Family")], runtime=rt4, ledger=led4)
check("the later twin first, then the first: still one item", [p["metadata"]["series"] for p in rt4.kb.values()], ["HOL-A"])
check("same title, another day: its own item", sorted(p["metadata"]["series"] for p in rt3.kb.values()), ["HOL-B", "HOL-C"])

print("\n── the migration, for what was synced before ──")
rt2, led2 = RT(), Path(tempfile.mkdtemp()) / "ledger.db"
con = cx._db(led2)
for i, d in enumerate(("09-21", "09-28", "10-05")):
    o = occ(d)
    iid = cx.item_id("calendar", o["source_id"])
    rt2.kb[iid] = {"source_id": iid}
    con.execute("INSERT INTO items (source, source_id, content_hash, item_id, kb, node, synced_at, owner, upstream_id) "
                "VALUES ('calendar',?,?,?,1,1,?,?,?)", (o["source_id"], "h", iid, 0, "zach", o["source_id"]))
    cx._version(con, "calendar", o["source_id"], iid, "created", "h", "Ballet", o["fields"], None, "zach")
con.commit()
con.close()
chunks = lambda iid: (occ("10-05")["text"], {"owner": "zach", "audience": "private", "source": "calendar"})  # noqa: E731
check("dry run: counts only, nothing moved", (M.migrate(rt2, chunks, led2), len(rt2.kb)),
      ({"occurrence_items": 3, "series": 1, "applied": False}, 3))
M.migrate(rt2, chunks, led2, apply=True)
check("applied: the three copies become one series item", [p["metadata"]["occurrences"] for p in rt2.kb.values()], [3])
check("and a re-run finds nothing left", M.migrate(rt2, chunks, led2)["occurrence_items"], 0)

check("refresh rewrites every series", M.refresh(rt2, led2), 1)

print("\n── a row from before the owner column: the default owner's, never nobody's ──")
rt5, led5 = RT(), Path(tempfile.mkdtemp()) / "ledger.db"
con = cx._db(led5)
o = occ("10-12")
iid = cx.item_id("calendar", o["source_id"])
rt5.kb[iid] = {"source_id": iid}
con.execute("INSERT INTO items (source, source_id, content_hash, item_id, kb, node, synced_at, owner, upstream_id) "
            "VALUES ('calendar',?,?,?,1,1,0,NULL,?)", (o["source_id"], "h", iid, o["source_id"]))
cx._version(con, "calendar", o["source_id"], iid, "created", "h", "Ballet", o["fields"], None, None)
con.commit()
con.close()
M.migrate(rt5, lambda iid: (o["text"], {"audience": "private", "source": "calendar"}), led5, apply=True)
check("the series item has an owner", [p["metadata"]["owner"] for p in rt5.kb.values()], [cx.OWNER_DEFAULT])

print("\n── forgetting (#850: 23 series notes outlived their forgotten occurrences, 10-04) ──")
rt3, led3 = RT(), Path(tempfile.mkdtemp()) / "ledger.db"
cx.ingest("calendar", [occ("09-29"), occ("10-01"), occ("10-06")], runtime=rt3, ledger=led3)
note = next(iter(rt3.kb))
one = cx.item_id("calendar", occ("10-06")["source_id"])
cx.tombstone_item(one, ledger=led3, runtime=rt3)
check("forgetting one occurrence rewrites the note with two", rt3.kb[note]["metadata"]["occurrences"], 2)
for d in ("09-29", "10-01"):
    cx.tombstone_item(cx.item_id("calendar", occ(d)["source_id"]), ledger=led3, runtime=rt3)
check("forgetting the last one removes the note", note in rt3.kb, False)
rt4, led4 = RT(), Path(tempfile.mkdtemp()) / "ledger.db"
cx.ingest("calendar", [occ("09-29"), occ("10-01")], runtime=rt4, ledger=led4)
note = next(iter(rt4.kb))
stone = cx.tombstone_item(note, ledger=led4, runtime=rt4)
check("forgetting the series note forgets every occurrence and the note", (stone and stone["occurrences"], note in rt4.kb,
      len(cx.tombstones("zach", ledger=led4)["tombstones"])), ("2", False, 2))
check("…and they don't come back on the next sync", (cx.ingest("calendar", [occ("09-29")], runtime=rt4, ledger=led4)["written"],
      len(rt4.kb)), (0, 0))
rt5, led5 = RT(), Path(tempfile.mkdtemp()) / "ledger.db"
cx.ingest("calendar", [occ("09-29"), occ("10-01")], runtime=rt5, ledger=led5)
cx.forget("calendar", apply=True, runtime=rt5, ledger=led5, owner="zach")
check("forgetting the whole source takes the series note too", rt5.kb, {})

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
