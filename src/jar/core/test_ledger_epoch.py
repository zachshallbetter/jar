#!/usr/bin/env python3
# gate: hermetic
"""The ledger epoch and ingest error classes (10-09 household reset): a device resends everything when the epoch it
last saw changes (bumped only by household_reset.py --apply); every per-item ingest error is counted by its class
(the text before the first ':'), per day, source and owner, never a title or an id; and a device-deleted item that
comes back with new content is revived for the same person (only a person's forget, a tombstone, keeps it out).
Hermetic: fake runtimes, temporary stores, fictional people. Run: python3 scripts/test_ledger_epoch.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-ledger-epoch-"))
for k, v in {"KB_ACL_DB": "kb_acl.db", "HOUSEHOLD_DB": "household.db", "LINKS_DB": "links.db",
             "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "TODOS_DB": "todos.db",
             "PLACES_DB": "places.db", "PUSH_DB": "push.db", "SPACES_DB": "spaces.db", "LOCK_UP_DB": "lock_up.db",
             "TV_UNLOCK_DB": "tv_unlock.db", "SITTER_DB": "sitter.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import connectors_runner as cx  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


class Fake:
    def __init__(self):
        self.nodes, self.kb, self.fail = {}, {}, None

    def __call__(self, service, path, method, payload=None):
        if self.fail and service == "retrieval" and method == "POST":
            raise RuntimeError(self.fail)
        if service == "retrieval" and method == "POST":
            self.kb[payload["source_id"]] = payload
        elif service == "retrieval" and method == "DELETE":
            self.kb.pop(path.rsplit("/", 1)[-1], None)
        elif service == "record" and path == "/v1/nodes" and method == "POST":
            if payload["node_id"] in self.nodes:
                raise RuntimeError("HTTP 409 Conflict")
            self.nodes[payload["node_id"]] = payload
        elif service == "record" and method == "PATCH":
            self.nodes[path.rsplit("/", 1)[-1]]["data"] = payload["data"]
        elif service == "record" and method == "DELETE":
            self.nodes.pop(path.rsplit("/", 1)[-1], None)
        return {}


LEDGER = _TMP / "ledger.db"
rt = Fake()
ev = {"source_id": "ev-1@2026-10-08T17:00", "observed_at": "2026-10-08", "kind": "appointment",
      "title": "Hale Swim Meet", "text": "Hale pool, lane 4", "fields": {"start": "2026-10-08T17:00"}}

print("── the epoch ──")
e1 = cx.ledger_epoch(LEDGER)
check("an epoch exists from the first read: 16 hex", (len(e1), all(c in "0123456789abcdef" for c in e1)), (16, True))
check("…and stays the same", cx.ledger_epoch(LEDGER), e1)
out = cx.ingest("calendar", [ev], runtime=rt, ledger=LEDGER, owner="robin")
check("every ingest answer carries it", out.get("ledger_epoch"), e1)
e2 = cx.bump_epoch(LEDGER)
check("bumped: a new one", (e2 != e1, cx.ledger_epoch(LEDGER)), (True, e2))
check("…and the next ingest answer says so", cx.ingest("calendar", [], runtime=rt, ledger=LEDGER, owner="robin")
      .get("ledger_epoch"), e2)
check("state: the epoch", cx.state("robin", ledger=LEDGER)["ledger_epoch"], e2)
check("the tombstones answer the Mac polls carries it too (jellybean#1157)", cx.tombstones("robin", ledger=LEDGER)
      .get("ledger_epoch"), e2)

print("── ingest errors, by class ──")
rt.fail = "HTTP Error 500: Internal Server Error for Hale Swim Meet"
bad = {**ev, "source_id": "ev-2@2026-10-09T09:00", "title": "Robin's dentist", "text": "Dr. Hale"}
out = cx.ingest("calendar", [bad, {**bad, "source_id": "ev-3@2026-10-09T10:00", "title": "Ava's recital"}, {"title": "no id"}],
                runtime=rt, ledger=LEDGER, owner="robin")
check("the answer still lists each error (for the device)", len(out["errors"]), 3)
st = cx.state("robin", ledger=LEDGER)["errors"]
check("counted by class: the text before the first ':', per source",
      sorted((e["source"], e["class"], e["count"]) for e in st),
      [("calendar", "HTTP Error 500", 2), ("calendar", "source_id is required", 1)])
check("never a title or an id in a class", any(w in e["class"] for e in st for w in ("Hale", "Robin", "dentist", "ev-")),
      False)
check("another person sees none of these", cx.state("alex", ledger=LEDGER)["errors"], [])
check("error_class keeps only a short, plain head", (cx.error_class("ValueError"), cx.error_class(""),
                                                     len(cx.error_class("x" * 200))), ("ValueError", "unknown", 60))
rt.fail = None

print("── deleted on the device, then back: revived ──")
iid = cx.item_id("calendar", "robin:" + ev["source_id"])
cx.ingest("calendar", [{"source_id": ev["source_id"], "deleted": True, "deleted_reason": "deleted"}], runtime=rt, ledger=LEDGER, owner="robin")
con = sqlite3.connect(LEDGER)
check("deleted: gone from the KB and records, the ledger row marked", (iid in rt.kb, iid in rt.nodes, con.execute(
    "SELECT deleted_at IS NOT NULL FROM items WHERE item_id=?", (iid,)).fetchone()[0]), (False, False, 1))
out = cx.ingest("calendar", [{**ev, "text": "Hale pool, lane 6 (moved)"}], runtime=rt, ledger=LEDGER, owner="robin")
check("re-sent with new content: written, no error", (out["written"], out["errors"]), (1, []))
check("…revived: live again, the same person's, its record back",
      (con.execute("SELECT deleted_at IS NULL, owner FROM items WHERE item_id=?", (iid,)).fetchone(), iid in rt.nodes),
      ((1, "robin"), True))
cx.ingest("calendar", [{"source_id": ev["source_id"], "deleted": True, "deleted_reason": "deleted"}], runtime=rt, ledger=LEDGER, owner="robin")
out = cx.ingest("calendar", [ev], runtime=rt, ledger=LEDGER, owner="robin")
check("re-sent with its earlier content after a delete: revived too", (out["written"], out["errors"]), (1, []))
check("a person's forget (a tombstone) still keeps it out: counted as forgotten, not an error",
      (cx.tombstone_item(iid, ledger=LEDGER, runtime=rt) is not None,
       cx.ingest("calendar", [{**ev, "text": "again"}], runtime=rt, ledger=LEDGER, owner="robin")["forgotten"]),
      (True, 1))

print("── the route ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
cx.LEDGER_PATH = LEDGER
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)


def pair(name, role):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers={"X-Agent-Token": MASTER}).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": f"{name}'s Mac", "platform": "mac"}).json()
    return {"X-Agent-Token": cl["token"]}


ROBIN = pair("Robin Quarry", "adult")
r = c.get("/v1/connectors/state", headers=ROBIN)
check("GET /v1/connectors/state: the epoch and a list of errors", (r.status_code, r.json()["ledger_epoch"],
                                                                     isinstance(r.json()["errors"], list)), (200, e2, True))
r = c.get("/v1/connectors/tombstones", headers=ROBIN)
check("GET /v1/connectors/tombstones carries ledger_epoch", (r.status_code, r.json().get("ledger_epoch")), (200, e2))
SCREEN = pair("Hall screen", "household")
check("…for a shared screen too", c.get("/v1/connectors/tombstones", headers=SCREEN).json().get("ledger_epoch"), e2)
r = c.get("/v1/connectors/state", headers=SCREEN)
check("a shared screen gets the epoch, never anyone's errors", (r.status_code, r.json()["ledger_epoch"], r.json()["errors"]),
      (200, e2, []))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
