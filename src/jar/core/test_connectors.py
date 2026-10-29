#!/usr/bin/env python3
"""
Unit tests for the household connectors runner (scripts/connectors_runner.py): first write,
no-op resync, update with a timeline correction, upstream delete with a retraction, secret
filtering, forget, and the timeline reader applying retractions/corrections. Hermetic: the
runtimes are a fake and the ledger is a temp file.

Run: python3 scripts/test_connectors.py
"""
import os
import sys
import time
import tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
os.environ["KB_ACL_DB"] = str(Path(tempfile.mkdtemp(prefix="titan-cx-acl-")) / "kb_acl.db")   # never titan's own

import connectors_runner as cx
import agent_runtime_lib as lib


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


class FakeRuntimes:
    def __init__(self):
        self.calls, self.nodes, self.kb, self.events = [], {}, {}, []
        self.edges: set[tuple[str, str, str]] = set()

    def __call__(self, service, path, method, payload):
        self.calls.append((service, method, path))
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
        elif service == "event" and method == "POST":
            self.events.append(payload)
        elif service == "record" and path == "/v1/edges" and method == "POST":
            # like the record runtime: the id comes from (source, target, relation), and a repeat is a
            # unique-key violation answered with 500, not 409
            key = (payload["source_id"], payload["target_id"], payload["relation_type"])
            if key in self.edges:
                raise RuntimeError("HTTP Error 500: Internal Server Error")
            self.edges.add(key)
        elif service == "record" and path.startswith("/v1/edges/trace/") and method == "GET":
            src = path.rsplit("/", 1)[-1]
            return {"edges": [{"source_id": s_, "target_id": t, "relation_type": r} for s_, t, r in self.edges if s_ == src]}
        return {}


def run():
    ok = True
    ledger = Path(tempfile.mkdtemp()) / "ledger.db"
    rt = FakeRuntimes()
    ev = {"source_id": "UID-123", "observed_at": "2026-10-02T15:00:00Z", "kind": "appointment",
          "title": "Dentist", "text": "Dentist with Dr. Park, 3pm.", "account": "iCloud",
          "fields": {"start": "2026-10-02T15:00", "calendar": "Home", "pin": "4321", "account": "iCloud",
                     "account_number": "12345678"},
          "links": [["with", "Dr. Park"]], "timeline": "Dentist with Dr. Park"}
    iid = cx.item_id("fixture", "UID-123")   # a source whose items go on the timeline (a calendar's don't, #636)

    print("\n── first write ──")
    out = cx.ingest("fixture", [ev], runtime=rt, ledger=ledger)
    ok &= check("counts", (out["written"], out["unchanged"], out["errors"]), (1, 0, []))
    ok &= check("KB note written with provenance", rt.kb[iid]["metadata"]["source"], "fixture")
    ok &= check("record node written", rt.nodes[iid]["node_type"], "appointment")
    ok &= check("entity linked", "dr-park" in rt.nodes, True)
    ok &= check("secret field kept out", rt.nodes[iid]["data"]["pin"], "(kept out of titan)")
    ok &= check("account number kept out", rt.nodes[iid]["data"]["account_number"], "(kept out of titan)")
    ok &= check("account name kept", rt.nodes[iid]["data"]["account"], "iCloud")
    ok &= check("one timeline event", [e["event_type"] for e in rt.events], ["household.note"])

    ok &= check("its link is in the record runtime", ("" + iid, "dr-park", "with") in rt.edges, True)

    print("\n── its sharing-table row, written at index (#1273) ──")
    import kb_privacy as P
    md = rt.kb[iid]["metadata"]
    ok &= check("the row is what the reads already resolve from its metadata (nobody's view changes)",
                P.acl_rows([iid]).get(iid), P.acl_of(md))
    P.set_acl(iid, P.acl_of(md)[0], "household", by="test", why="share")
    cx.ingest("fixture", [dict(ev, text="Dentist with Dr. Park, 3:30pm now.")], runtime=rt, ledger=ledger)
    ok &= check("…a share made since survives a re-sync of the item", P.acl_rows([iid])[iid][1], "household")
    P.set_acl(iid, *P.acl_of(md), by="test", why="unshare")
    cx.ingest("fixture", [ev], runtime=rt, ledger=ledger)   # back to the first text for the checks below
    good = os.environ["KB_ACL_DB"]
    os.environ["KB_ACL_DB"] = "/dev/null/no/kb_acl.db"
    try:
        out_bad = cx.ingest("fixture", [dict(ev, source_id="UID-999", title="Vet")], runtime=rt, ledger=ledger)
        ok &= check("a failed row write never blocks the item", (out_bad["written"], cx.item_id("fixture", "UID-999") in rt.kb),
                    (1, True))
    finally:
        os.environ["KB_ACL_DB"] = good

    print("\n── an item entering the KB never inherits an old row; leaving drops it (#1274) ──")
    new_ev = dict(ev, source_id="UID-777", title="Plumber")
    new_iid = cx.item_id("fixture", "UID-777")
    P.set_acl(new_iid, "someone-else", "household", by="test", why="stale")   # a row left from before
    cx.ingest("fixture", [new_ev], runtime=rt, ledger=ledger)
    ok &= check("a new item replaces a stale broader row with what its metadata says (its owner, private)",
                P.acl_rows([new_iid]).get(new_iid), P.acl_of(rt.kb[new_iid]["metadata"]))
    cx.ingest("fixture", [{"source_id": "UID-777", "deleted": True}], runtime=rt, ledger=ledger)
    ok &= check("leaving the KB (upstream delete) drops its row", (new_iid in rt.kb, new_iid in P.acl_rows([new_iid])),
                (False, False))

    print("\n── resync without change ──")
    n_calls = len(rt.calls)
    out = cx.ingest("fixture", [dict(ev)], runtime=rt, ledger=ledger)
    ok &= check("unchanged", (out["written"], out["unchanged"]), (0, 1))
    ok &= check("no runtime calls", len(rt.calls), n_calls)

    print("\n── update ──")
    moved = {**ev, "text": "Dentist with Dr. Park, moved to 4pm.", "timeline": "Dentist moved to 4pm"}
    out = cx.ingest("fixture", [moved], runtime=rt, ledger=ledger)
    ok &= check("written, its link already there (10-02: the repeat link's 500 rejected every update)",
                (out["written"], out["errors"]), (1, []))
    ok &= check("KB text updated", rt.kb[iid]["text"], "Dentist with Dr. Park, moved to 4pm.")
    ok &= check("timeline correction appended", rt.events[-1]["event_type"], "household.corrected")
    ok &= check("correction points at the original", rt.events[-1]["payload"]["corrects"], f"{iid}-t")

    print("\n── upstream delete ──")
    out = cx.ingest("fixture", [{"source_id": "UID-123", "deleted": True}], runtime=rt, ledger=ledger)
    ok &= check("removed", out["removed"], 1)
    ok &= check("KB note gone", iid in rt.kb, False)
    ok &= check("node gone", iid in rt.nodes, False)
    ok &= check("retraction appended", rt.events[-1]["event_type"], "household.retracted")

    print("\n── forget ──")
    cx.ingest("downloads", [{"source_id": f"f{i}", "title": f"file {i}", "text": "x", "timeline": f"got {i}"}
                            for i in range(3)], runtime=rt, ledger=ledger)
    dry = cx.forget("downloads", apply=False, runtime=rt, ledger=ledger)
    ok &= check("dry run counts", dry["would_remove"], 3)
    done = cx.forget("downloads", apply=True, runtime=rt, ledger=ledger)
    ok &= check("forget removed all", (done["removed"], done["errors"]), (3, []))
    ok &= check("status reflects it", cx.status(ledger=ledger)["downloads"]["items"], 0)

    print("\n── last_new: the newest item never sent before (#648) ──")
    cx.ingest("mail", [{"source_id": "m1", "title": "Hello", "text": "first"}], runtime=rt, ledger=ledger)
    first = cx.status(ledger=ledger)["mail"]
    time.sleep(1.1)
    cx.ingest("mail", [{"source_id": "m1", "title": "Hello", "text": "first, edited"}], runtime=rt, ledger=ledger)
    again = cx.status(ledger=ledger)["mail"]
    ok &= check("a re-sent or changed old item moves last_sync, not last_new",
                (again["last_new"] == first["last_new"], again["last_sync"] != first["last_sync"]), (True, True))
    time.sleep(1.1)
    cx.ingest("mail", [{"source_id": "m2", "title": "New", "text": "second"}], runtime=rt, ledger=ledger)
    ok &= check("a new item moves last_new", cx.status(ledger=ledger)["mail"]["last_new"] != first["last_new"], True)

    print("\n── validation ──")
    try:
        cx.ingest("nope", [], runtime=rt, ledger=ledger)
        ok &= check("unknown source rejected", False, True)
    except ValueError:
        ok &= check("unknown source rejected", True, True)
    out = cx.ingest("mail", [{"title": "no id"}], runtime=rt, ledger=ledger)
    ok &= check("missing source_id reported", out["errors"][0]["error"], "source_id is required")

    print("\n── a calendar item never goes on the timeline (#636) ──")
    cal = FakeRuntimes()
    cx.ingest("calendar", [{**ev, "source_id": "UID-9@2026-09-30T21:50:00Z"}], runtime=cal,
              ledger=Path(tempfile.mkdtemp()) / "ledger.db")
    ok &= check("KB note, no timeline event", (len(cal.kb), cal.events), (1, []))

    print("\n── timeline reader applies retractions and corrections ──")
    orig = lib._runtime_request
    try:
        lib._runtime_request = lambda *a, **k: {"events": [
            {"event_id": "a", "event_type": "household.note", "payload": {"text": "kept"}},
            {"event_id": "b", "event_type": "household.note", "payload": {"text": "old text"}},
            {"event_id": "c", "event_type": "household.note", "payload": {"text": "gone"}},
            {"event_id": "b-c1", "event_type": "household.corrected", "payload": {"corrects": "b", "text": "new text"}},
            {"event_id": "c-r", "event_type": "household.retracted", "payload": {"retracts": "c"}}]}
        texts = [e["payload"]["text"] for e in lib._timeline_events()]
        ok &= check("reader output", texts, ["kept", "new text"])
    finally:
        lib._runtime_request = orig

    print("\n── daily log connector (log_event) ──")
    import datetime as _dt
    tmp = Path(tempfile.mkdtemp())
    kb, orig = {}, (lib._runtime_request, lib.DAILY_DIR, cx.LEDGER_PATH, os.environ.get("AGENT_TURN_SOURCE"))
    try:
        def fake(svc, path, method="POST", payload=None, timeout=30):
            if svc == "retrieval" and method == "POST":
                kb[payload["source_id"]] = payload
            return {}
        lib._runtime_request, lib.DAILY_DIR, cx.LEDGER_PATH = fake, tmp / "daily", tmp / "ledger.db"
        os.environ["AGENT_TURN_SOURCE"] = "user"
        lib.execute_tool("log_event", {"event": "Changed the furnace filter"})
        lib.execute_tool("log_event", {"event": "Paid the water bill"})
        day = lib._local_now().strftime("%Y-%m-%d")
        lines = (tmp / "daily" / f"{day}.md").read_text().splitlines()
        ok &= check("two lines in today's file", [l.split(" ", 2)[2] for l in lines], ["(titan's summary) Changed the furnace filter", "(titan's summary) Paid the water bill"])   # its own words
        notes = [v for v in kb.values() if v["metadata"].get("source") == "daily"]
        ok &= check("no KB note for the day (10-03: the daily log is titan's own activity)", len(notes), 0)
    finally:
        lib._runtime_request, lib.DAILY_DIR, cx.LEDGER_PATH = orig[0], orig[1], orig[2]
        if orig[3] is None:
            os.environ.pop("AGENT_TURN_SOURCE", None)
        else:
            os.environ["AGENT_TURN_SOURCE"] = orig[3]

    print("\n── stays forgotten (Zach, 10-02; jellybean#233/#305) ──")
    led2 = Path(tempfile.mkdtemp()) / "ledger.db"
    rt2 = FakeRuntimes()
    note = {"source_id": "M-1", "title": "Dentist reminder", "text": "Dentist Tuesday at 3.", "kind": "mail"}
    cx.ingest("mail", [note], runtime=rt2, ledger=led2, owner="p_m")
    cx.ingest("mail", [{**note, "source_id": "Z-1"}], runtime=rt2, ledger=led2)          # Zach's own
    iid = cx.item_id("mail", "p_m:M-1")
    ok &= check("a forgotten item gets a tombstone, per owner", cx.tombstone_item(iid, ledger=led2),
                {"owner": "p_m", "source": "mail", "source_id": "M-1"})
    out = cx.ingest("mail", [{**note, "text": "Dentist moved to Wednesday."}], runtime=rt2, ledger=led2, owner="p_m")
    ok &= check("…changed on the device, it still never comes back", (out["forgotten"], out["written"]), (1, 0))
    t = cx.tombstones("p_m", 0, ledger=led2)
    ok &= check("…and her devices are told to drop it", [(x["source"], x["source_id"]) for x in t["tombstones"]],
                [("mail", "M-1")])
    ok &= check("…nobody else's list has it", cx.tombstones("zach", 0, ledger=led2)["tombstones"], [])
    cx.ingest("mail", [{**note, "source_id": "M-2"}], runtime=rt2, ledger=led2, owner="p_m")
    dry = cx.forget("mail", apply=False, ledger=led2, owner="p_m")
    ok &= check("forgetting a source is per person (dry run counts only hers)", dry["would_remove"], 1)
    cx.forget("mail", apply=True, runtime=rt2, ledger=led2, owner="p_m")
    ok &= check("…applied, the source is off for her", cx.sources_off("p_m", ledger=led2), ["mail"])
    ok &= check("…Zach's mail stays", cx.ingest("mail", [{**note, "source_id": "Z-2"}], runtime=rt2, ledger=led2)["written"], 1)
    out = cx.ingest("mail", [{**note, "source_id": "M-3"}], runtime=rt2, ledger=led2, owner="p_m")
    ok &= check("…anything more from her mail is dropped", (out["forgotten"], out["written"]), (1, 0))
    ok &= check("turning it back on", cx.set_sources_off("p_m", [], ledger=led2), [])
    out = cx.ingest("mail", [{**note, "source_id": "M-3"}, {**note, "text": "again"}], runtime=rt2, ledger=led2, owner="p_m")
    ok &= check("…new mail flows again; the forgotten item still doesn't", (out["written"], out["forgotten"]), (1, 1))

    print("\n── an older ledger: owners backfilled ──")
    import sqlite3 as _sq
    led3 = Path(tempfile.mkdtemp()) / "ledger.db"
    c3 = _sq.connect(led3)
    c3.execute("CREATE TABLE items (source TEXT NOT NULL, source_id TEXT NOT NULL, content_hash TEXT NOT NULL, item_id TEXT "
               "NOT NULL, kb INTEGER NOT NULL DEFAULT 0, node INTEGER NOT NULL DEFAULT 0, event_id TEXT, account TEXT, "
               "observed_at TEXT, synced_at REAL NOT NULL, deleted_at REAL, PRIMARY KEY (source, source_id))")
    c3.execute("CREATE TABLE versions (source TEXT NOT NULL, source_id TEXT NOT NULL, item_id TEXT NOT NULL, change TEXT "
               "NOT NULL, digest TEXT, title TEXT, fields TEXT, observed_at TEXT, owner TEXT, audience TEXT, at REAL NOT NULL)")
    c3.execute("INSERT INTO items VALUES ('mail','p_m:M-9','h','cx-mail-x',1,0,NULL,NULL,NULL,0,NULL)")
    c3.execute("INSERT INTO versions VALUES ('mail','p_m:M-9','cx-mail-x','created',NULL,NULL,NULL,NULL,'p_m',NULL,0)")
    c3.execute("INSERT INTO items VALUES ('mail','Z-9','h','cx-mail-y',1,0,NULL,NULL,NULL,0,NULL)")
    c3.commit()
    c3.close()
    ok &= check("owner and the device's id, from the history or the key",
                sorted(cx._db(led3).execute("SELECT source_id, owner, upstream_id FROM items").fetchall()),
                [("Z-9", "zach", "Z-9"), ("p_m:M-9", "p_m", "M-9")])

    print()
    print("\n── an event already on the stream (409) is recorded, not a failure (#647) ──")
    def conflict(svc, path, method, payload):
        raise RuntimeError("HTTP Error 409: Conflict")
    try:
        cx._post_event(conflict, "cx-calendar-x-c1234abcd", "household.note", {"text": "x"})
        ok &= check("409 is swallowed", True, True)
    except Exception as exc:  # noqa: BLE001
        ok &= check("409 is swallowed", repr(exc), None)
    def boom(svc, path, method, payload):
        raise RuntimeError("HTTP Error 500: Internal Server Error")
    try:
        cx._post_event(boom, "cx-calendar-x-c1234abcd", "household.note", {"text": "x"})
        ok &= check("other errors still raise", False, True)
    except RuntimeError:
        ok &= check("other errors still raise", True, True)

    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
