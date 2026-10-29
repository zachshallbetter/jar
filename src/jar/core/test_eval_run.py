#!/usr/bin/env python3
"""An eval run's writes carry its id, and its rollback removes only its own items (#346).

The household eval saves made-up facts through the real agent, then rolls the KB and the record store
back to a snapshot. Before #346 everything chat-made that changed during the run was reverted, so a
note someone really edited at 03:30 went with the eval's. Now eval turns stamp eval_run=<id> on what
they write (one place: _runtime_request), and kb_hygiene rollback/verify --eval-run act on stamped
items only. Hermetic: the runtimes are in-memory fakes.

Run: python3 scripts/test_eval_run.py
"""
# gate: hermetic
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-evalrun-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "KB_ACL_DB": "kb_acl.db", "REFLECTION_DB": "reflection.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "test"
os.environ.pop("AGENT_EVAL_RUN", None)
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


print("── which turns carry a run ──")
os.environ["AGENT_EVAL_RUN"] = "he-1"
check("a user turn: none, whatever the environment", lib.eval_run(), None)
os.environ["AGENT_TURN_SOURCE"] = "eval"
check("an eval turn: its run", lib.eval_run(), "he-1")
os.environ["AGENT_EVAL_RUN"] = "he-2; rm -rf /"
check("…kept to safe characters", lib.eval_run(), "he-2rm-rf")
os.environ["AGENT_EVAL_RUN"] = ""
check("an eval turn without a run: none (the snapshot still covers it)", lib.eval_run(), None)
os.environ["AGENT_EVAL_RUN"] = "he-1"

print("\n── the one place writes are stamped ──")
st = lib._stamped
check("a record created", st("record", "/v1/nodes", "POST", {"node_id": "c1", "data": {"name": "Vet"}})["data"],
      {"name": "Vet", "eval_run": "he-1"})
check("a record changed", st("record", "/v1/nodes/c1", "PATCH", {"data": {"phone": "1"}})["data"], {"phone": "1", "eval_run": "he-1"})
check("a KB source", st("retrieval", "/v1/retrieval/index", "POST", {"source_id": "n1", "metadata": {"owner": "zach"}})["metadata"],
      {"owner": "zach", "zone": "facts", "eval_run": "he-1"})   # every KB write carries its zone (#833)
check("a read, an edge or a delete is untouched",
      (st("record", "/v1/nodes?node_type=contact", "GET", None), st("record", "/v1/edges", "POST", {"a": 1}),
       st("retrieval", "/v1/retrieval/sources/n1", "DELETE", None)), (None, {"a": 1}, None))
sent: list = []


class _Resp:
    def __init__(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


_urlopen, _mint = lib.urllib.request.urlopen, lib._mint_coordinator_token
lib.urllib.request.urlopen = lambda req, timeout=30: (sent.append(json.loads(req.data)), _Resp())[1]
lib._mint_coordinator_token = lambda cid, ttl_seconds=300: "t"
lib._runtime_request("record", "/v1/nodes", "POST", {"node_id": "c1", "node_type": "contact", "data": {"name": "Vet"}})
check("_runtime_request sends the stamp", sent[-1]["data"].get("eval_run"), "he-1")
os.environ["AGENT_TURN_SOURCE"] = "user"
lib._runtime_request("record", "/v1/nodes", "POST", {"node_id": "c2", "node_type": "contact", "data": {"name": "Vet"}})
check("…and never on a real turn", "eval_run" in sent[-1]["data"], False)
lib.urllib.request.urlopen, lib._mint_coordinator_token = _urlopen, _mint

print("\n── rollback: an eval note and a real note change in the same window ──")
import kb_hygiene as kh  # noqa: E402


class Fake:
    """The retrieval and record runtimes, in memory."""

    def __init__(self):
        self.src: dict = {}    # id → {title, text, source_type, metadata, updated_at}
        self.nodes: dict = {}  # id → node
        self.tick = 0

    def put(self, sid, text, md=None, stype="note"):
        self.tick += 1
        self.src[sid] = {"title": sid, "text": text, "source_type": stype, "metadata": dict(md or {}),
                         "updated_at": f"t{self.tick}"}

    def __call__(self, service, path, method="GET", payload=None, timeout=30):
        payload = lib._stamped(service, path, method, payload)   # the real chokepoint, as an eval turn's
        if service == "retrieval":
            if path.startswith("/v1/retrieval/sources?"):
                return {"sources": [{"source_id": k, "title": v["title"], "source_type": v["source_type"],
                                     "updated_at": v["updated_at"]} for k, v in self.src.items()]}
            sid = path.split("/v1/retrieval/sources/")[1].split("/")[0]
            if method == "DELETE":
                self.src.pop(sid, None)
                return {}
            s = self.src[sid]
            return {"chunks": [{"chunk_index": 0, "chunk_text": s["text"], "metadata": s["metadata"]}]}
        if path.startswith("/v1/nodes?"):
            t = path.split("node_type=")[1].split("&")[0]
            return {"nodes": [n for n in self.nodes.values() if n["node_type"] == t]}
        if path == "/v1/nodes" and method == "POST":
            self.nodes[payload["node_id"]] = {"node_id": payload["node_id"], "node_type": payload["node_type"],
                                              "data": dict(payload["data"])}
            return {}
        nid = path.split("/v1/nodes/")[1]
        if method == "DELETE":
            self.nodes.pop(nid, None)
            return {}
        if method == "PATCH":
            d = self.nodes[nid]["data"]
            d.update(payload["data"])
            for k in [k for k, v in d.items() if v is None]:
                d.pop(k)
            return {}
        if nid not in self.nodes:
            raise RuntimeError("HTTP 404")
        return self.nodes[nid]


fake = Fake()
_rr, _ki = lib._runtime_request, lib._kb_index
lib._runtime_request = fake


def kb_index(sid, title, text, stype, md=None, **kw):
    fake.put(sid, text, lib._stamped("retrieval", "/v1/retrieval/index", "POST", {"metadata": dict(md or {})})["metadata"], stype)


lib._kb_index = kb_index
real = {"provenance": {"source": "chat", "owner": "zach", "audience": "private"}}
fake.put("note-vet", "Vet: Dr. Lee, 555-0100")
fake.put("note-wifi", "Wifi is upstairs")
fake.nodes["contact-plumber"] = {"node_id": "contact-plumber", "node_type": "contact", "data": {"name": "Luis", **real}}
fake.nodes["contact-dentist"] = {"node_id": "contact-dentist", "node_type": "contact", "data": {"name": "Dr. Kim", **real}}
snap = _TMP / "snap.json"
kh.snapshot(snap)

os.environ["AGENT_TURN_SOURCE"] = "eval"          # the eval's turns
lib._kb_index("note-fixture", "Fixture", "Door code is 4321", "note", {"owner": "zach"})
lib._kb_index("note-vet", "Vet", "Vet: Dr. Fake, 555-9999", "note", {"owner": "zach"})
fake("record", "/v1/nodes", "POST", {"node_id": "contact-fixture", "node_type": "contact", "data": {"name": "Fixture", **real}})
fake("record", "/v1/nodes/contact-plumber", "PATCH", {"data": {"phone": "555-0000"}})
os.environ["AGENT_TURN_SOURCE"] = "user"          # Robin, during the run
lib._kb_index("note-soccer", "Soccer", "Practice moved to 5", "note", {"owner": "robin"})
lib._kb_index("note-wifi", "Wifi", "Wifi is upstairs; password on the fridge", "note", {"owner": "zach"})
fake("record", "/v1/nodes", "POST", {"node_id": "contact-coach", "node_type": "contact", "data": {"name": "Coach Dan", **real}})
fake("record", "/v1/nodes/contact-dentist", "PATCH", {"data": {"phone": "555-0200"}})

kh.rollback(snap, "he-1")
check("the eval's new note is gone; the real new note stays", ("note-fixture" in fake.src, "note-soccer" in fake.src),
      (False, True))
check("the note the eval changed is restored", fake.src["note-vet"]["text"], "Vet: Dr. Lee, 555-0100")
check("a note someone really edited meanwhile keeps the edit", fake.src["note-wifi"]["text"],
      "Wifi is upstairs; password on the fridge")
check("the eval's new record is gone; the real new record stays",
      ("contact-fixture" in fake.nodes, "contact-coach" in fake.nodes), (False, True))
check("the record the eval changed is restored (and unstamped)", fake.nodes["contact-plumber"]["data"],
      {"name": "Luis", **real})
check("a record someone really edited meanwhile keeps the edit", fake.nodes["contact-dentist"]["data"].get("phone"), "555-0200")
check("verify --eval-run: the real items written meanwhile are UNSTAMPED, so a person looks (never deleted)",
      (kh.verify(snap, False, "he-1"), "note-soccer" in fake.src, "contact-coach" in fake.nodes), (1, True, True))
fake.src.pop("note-soccer")
fake.nodes.pop("contact-coach")   # a person checked them (here: the test removes them to see a clean verify)
check("verify --eval-run: nothing of the run's remains", kh.verify(snap, False, "he-1"), 0)
fake("record", "/v1/nodes", "POST", {"node_id": "contact-bypass", "node_type": "contact", "data": {"name": "X", **real}})
check("an eval write that bypassed the stamp is UNSTAMPED: verify fails, nothing deleted",
      (kh.verify(snap, False, "he-1"), "contact-bypass" in fake.nodes), (1, True))
fake.nodes.pop("contact-bypass")
os.environ["AGENT_TURN_SOURCE"] = "eval"
lib._kb_index("note-left", "Left", "A fixture the rollback missed", "note", {})
os.environ["AGENT_TURN_SOURCE"] = "user"
check("…and fails loudly when something of it does", kh.verify(snap, False, "he-1"), 1)
check("without a run, the old behaviour (every new chat-made item)", kh.verify(snap, False), 1)

lib._runtime_request, lib._kb_index = _rr, _ki

print("\n── the household eval passes its run ──")
src = (Path(__file__).parent / "test_household_agent.py").read_text()
check("its turns carry AGENT_EVAL_RUN, its rollback and verify --eval-run",
      ('"AGENT_EVAL_RUN": EVAL_RUN' in src, src.count('"--eval-run", EVAL_RUN'), "AGENT_EVAL_RUN=${eval_run}"
       in (Path(__file__).parent / "titan.sh").read_text()), (True, 2, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
