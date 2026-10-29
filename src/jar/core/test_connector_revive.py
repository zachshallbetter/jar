#!/usr/bin/env python3
# gate: hermetic
"""A calendar item deleted on the device and then sent again is revived, not refused (10-09: 24 of the 66 the Mac gave
up on failed with HTTP 404 on the revive path). The fake runtimes answer as the real ones do: a 404 for a KB copy or
a record node that's already gone, a 409 for a node that exists. Fictional events; temporary stores.
Run: python3 scripts/test_connector_revive.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-revive-"))
os.environ["KB_ACL_DB"] = str(_TMP / "kb_acl.db")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import connectors_runner as cx  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


class Strict:
    """The runtimes' answers: a DELETE or PATCH of something that isn't there is HTTP 404; a POST of a node that is
    there is 409."""

    def __init__(self):
        self.kb, self.nodes, self.events, self.calls = {}, {}, [], []

    def __call__(self, service, path, method, payload=None):
        self.calls.append((service, method, path.rsplit("/", 1)[-1]))
        key = path.rsplit("/", 1)[-1]
        if service == "retrieval" and method == "POST":
            self.kb[payload["source_id"]] = payload
        elif service == "retrieval" and method == "DELETE":
            if key not in self.kb:
                raise RuntimeError("HTTP Error 404: Not Found")
            del self.kb[key]
        elif service == "record" and path == "/v1/nodes" and method == "POST":
            if payload["node_id"] in self.nodes:
                raise RuntimeError("HTTP Error 409: Conflict")
            self.nodes[payload["node_id"]] = payload
        elif service == "record" and method == "PATCH":
            if key not in self.nodes:
                raise RuntimeError("HTTP Error 404: Not Found")
            self.nodes[key]["data"] = payload["data"]
        elif service == "record" and method == "DELETE":
            if key not in self.nodes:
                raise RuntimeError("HTTP Error 404: Not Found")
            del self.nodes[key]
        elif service == "event" and method == "POST":
            self.events.append(payload)
        return {}


LEDGER = _TMP / "ledger.db"
rt = Strict()
ev = {"source_id": "ev-swim@2026-10-08T17:00", "observed_at": "2026-10-08", "kind": "appointment",
      "title": "Hale swim meet", "text": "Hale pool, lane 4", "fields": {"start": "2026-10-08T17:00"}}
single = {"source_id": "ev-dentist@2026-10-12T09:00", "observed_at": "2026-10-09", "kind": "appointment",
          "title": "Robin's dentist", "text": "Dr. Hale", "fields": {"start": "2026-10-12T09:00"}}


def ingest(items):
    return cx.ingest("calendar", items, runtime=rt, ledger=LEDGER, owner="robin")


print("── deleted on the device, then sent again ──")
check("first: written", ingest([ev, single])["written"], 2)
out = ingest([{"source_id": ev["source_id"], "deleted": True, "deleted_reason": "deleted"}, {"source_id": single["source_id"], "deleted": True, "deleted_reason": "deleted"}])
check("deleted on the device: removed, no error", (out["removed"], out["errors"]), (2, []))
out = ingest([{**ev, "text": "Hale pool, lane 6 (moved)"}, {**single, "text": "Dr. Hale, room 2"}])
check("re-sent with new content: revived, no 404 (the copies its delete removed aren't removed again)",
      (out["written"], out["errors"]), (2, []))
con = sqlite3.connect(LEDGER)
iid = cx.item_id("calendar", "robin:" + single["source_id"])
check("…live again, the same person's, its record back", (con.execute(
    "SELECT deleted_at IS NULL, owner FROM items WHERE item_id=?", (iid,)).fetchone(), iid in rt.nodes), ((1, "robin"), True))
ingest([{"source_id": single["source_id"], "deleted": True, "deleted_reason": "deleted"}])
check("deleted, then re-sent unchanged from before the delete: revived too", (ingest([single])["written"],), (1,))
check("no ingest error of any class was counted", con.execute("SELECT COUNT(*) FROM ingest_errors").fetchone()[0], 0)

print("── the 10-09 case: a row written before series notes (its own KB copy, kb=1), deleted, then sent again ──")
old_ev = {"source_id": "ev-recital@2026-10-07T18:00", "observed_at": "2026-10-07", "kind": "appointment",
          "title": "Ava's recital", "text": "School hall", "fields": {"start": "2026-10-07T18:00"}}
ingest([old_ev])
old_iid = cx.item_id("calendar", "robin:" + old_ev["source_id"])
rt.kb[old_iid] = {"source_id": old_iid}     # as an older titan wrote it: the event's own KB copy
con.execute("UPDATE items SET kb=1 WHERE item_id=?", (old_iid,))
con.commit()
out = ingest([{"source_id": old_ev["source_id"], "deleted": True, "deleted_reason": "deleted"}])
check("deleted: its KB copy and record removed", (out["removed"], old_iid in rt.kb, old_iid in rt.nodes), (1, False, False))
out = ingest([{**old_ev, "text": "School hall, 6:30 now"}])
check("sent again: revived (no 404 deleting the copy its delete already removed)", (out["written"], out["errors"]), (1, []))
check("…no ingest error counted", con.execute("SELECT COUNT(*) FROM ingest_errors").fetchone()[0], 0)

print("── a KB copy already gone is removed: done ──")
cx._kb_delete(rt, "cx-never-there")
check("_kb_delete of a missing copy: no exception", True, True)
try:
    cx._kb_delete(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP Error 500: Internal")), "x")
    check("…but any other failure still raises", "no raise", "raised")
except RuntimeError:
    check("…but any other failure still raises", "raised", "raised")
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
