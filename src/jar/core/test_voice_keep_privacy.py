#!/usr/bin/env python3
# gate: hermetic
"""A kept voice note is its recipient's alone (titan-node#859; Zach 10-04: "index a transcript only when the recipient
taps Keep, private to them"). Two people and every KB read path: the sender (the admin here) never retrieves the
recipient's kept copy, nor does a kid, a shared screen or another adult; another recipient's Keep is their own item;
forgetting removes it; it lands in the facts zone and nothing expires it. A fake retrieval runtime; no network.
Run: python3 scripts/test_voice_keep_privacy.py"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import urllib.error

_TMP = tempfile.mkdtemp(prefix="voicekeep-")
for k, v in {"HOUSEHOLD_DB": "household.db", "KB_ACL_DB": "kb_acl.db", "HOUSEHOLD_PREFS": "household.json",
             "CONNECTORS_LEDGER": "ledger.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "FENCES_DB": "fences.db", "WORLD_DIR": "world", "LINKS_DB": "links.db", "LINK_SALT_FILE": "salt"}.items():
    os.environ[k] = os.path.join(_TMP, v)
os.environ.setdefault("AGENT_TURN_SOURCE", "test")
os.environ["HELD_ACTIONS_DB"] = os.path.join(_TMP, "held.db")
os.environ["PROACTIVE_DB"] = os.path.join(_TMP, "proactive.db")
os.environ["LINK_TOPICS"] = "0"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_runtime_lib as A  # noqa: E402
import household_identity as hh  # noqa: E402
import kb_privacy as P  # noqa: E402
import kb_query as Q  # noqa: E402
import voice_notes as V  # noqa: E402
import zones  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


class FakeKB:
    """The retrieval runtime's routes the KB uses (word-overlap ranking), as test_kb_privacy's."""
    def __init__(self):
        self.sources, self.clock = {}, 0

    def __call__(self, service, path, method="POST", payload=None, timeout=30):
        if service == "record":
            raise urllib.error.HTTPError(path, 404, "no node", None, None)
        if service != "retrieval":
            return {}
        if path == "/v1/retrieval/index":
            self.clock += 1
            self.sources[payload["source_id"]] = {"source_id": payload["source_id"], "title": payload.get("title", ""),
                                                  "text": payload["text"], "source_type": payload["source_type"],
                                                  "metadata": dict(payload.get("metadata") or {}),
                                                  "updated_at": f"t{self.clock}"}
            return {"source_id": payload["source_id"]}
        if path == "/v1/retrieval/query":
            words = set(re.findall(r"[a-z]{3,}", payload["query"].lower()))
            scored = sorted(((len(words & set(re.findall(r"[a-z]{3,}", (s["title"] + " " + s["text"]).lower()))), s)
                             for s in self.sources.values()), key=lambda x: -x[0])
            res = [{"source_id": s["source_id"], "chunk_text": s["text"], "citation_ref": f"{s['source_id']}#chunk-0000",
                    "score": str(n), "metadata": dict(s["metadata"])} for n, s in scored if n][:payload.get("top_k", 5)]
            return {"results": res, "total_matches": len(res),
                    "context_bundle": {"bundle_text": "\n\n".join(r["chunk_text"] for r in res)}}
        m = re.fullmatch(r"/v1/retrieval/sources/([^/]+)/chunks", path)
        if m:
            s = self.sources.get(m.group(1))
            if not s:
                raise urllib.error.HTTPError(path, 404, "Source not found", None, None)
            return {"chunks": [{"chunk_index": 0, "chunk_text": s["text"], "metadata": dict(s["metadata"])}]}
        if path.startswith("/v1/retrieval/sources?"):
            return {"sources": [{k: s[k] for k in ("source_id", "title", "source_type", "updated_at")}
                                | {"preview": s["text"][:200]} for s in self.sources.values()]}
        m = re.fullmatch(r"/v1/retrieval/sources/([^/]+)", path)
        if m and method == "DELETE":
            if self.sources.pop(m.group(1), None) is None:
                raise urllib.error.HTTPError(path, 404, "Source not found", None, None)
            return {"deleted": True}
        raise AssertionError(f"unexpected call {method} {path}")


KB = FakeKB()
A._runtime_request = KB
A._http_json = lambda url, payload=None, timeout=30: {}
P._META_CACHE.clear()
store = hh.get_store()
ALEX = hh.ADMIN_PERSON_ID                              # the sender: the household's admin
ROBIN = store.add_person("Robin", "adult")["id"]       # a recipient who keeps it
JORDAN = store.add_person("Jordan", "adult")["id"]     # another recipient who keeps it too
HALE = store.add_person("Hale", "adult")["id"]         # an adult it was never sent to
AVA = store.add_person("Ava", "kid")["id"]
TV = store.add_person("Living room", "household")["id"]
KB.sources["hh-dentist"] = {"source_id": "hh-dentist", "title": "Dentist", "text": "The dentist is Lakeside Dental.",
                            "source_type": "household", "metadata": {"owner": None, "audience": "household"},
                            "updated_at": "t0"}

CARD = {"id": "card-vn-1", "detail": "Can you pick up the dentist forms from Lakeside Clinic on Friday, the blue folder",
        "sender": "Alex", "created_at": time.time()}
ROBIN_ID, JORDAN_ID = V.keep_id(CARD["id"], ROBIN), V.keep_id(CARD["id"], JORDAN)
Q_WORDS = "dentist forms blue folder Lakeside Clinic"


def _do_it(owner):
    """The owner's Do it on their newest held card (#1628: kb_forget asks first)."""
    import held_actions
    con = held_actions._db()
    try:
        r = con.execute("SELECT id FROM held WHERE owner=? AND used_at IS NULL ORDER BY created_at DESC LIMIT 1",
                        (str(owner).lower(),)).fetchone()
    finally:
        con.close()
    return A._run_held_action(r["id"]) if r else "no held action"


def as_(pid, role):
    A._TURN_IO.person = {"id": pid, "name": pid} if pid else None
    A._TURN_IO.role = role


def finds(pid, role, rid):
    """Every read path, as this viewer: an explicit search, the per-turn context, the list, a GET by id."""
    as_(pid, role)
    try:
        search = rid in A.execute_tool("kb_retrieve", {"query": Q_WORDS})
        passive = any(h.get("source_id") == rid for h in A._kb_query(Q_WORDS, 5, passive=True))
        listed = any(r.get("id") == rid or r.get("source_id") == rid for r in Q.kb_list(pid, role))
        got = Q.kb_get(rid, pid, role)
        direct = bool(got) and not got.get("error") and got.get("status", 200) == 200
        return {"search": search, "context": passive, "list": listed, "get": direct}
    finally:
        as_(None, None)


NONE = {"search": False, "context": False, "list": False, "get": False}
ALL = {"search": True, "context": True, "list": True, "get": True}

print("── before Keep: nobody's knowledge base has the words ──")
check("not indexed until someone keeps it", [s for s in KB.sources if s.startswith("voice-")], [])

print("── Robin keeps it ──")
done, said = V.keep(CARD, ROBIN, "adult")
check("kept, and said so", (done, said.startswith("Kept:")), (True, True))
check("Robin finds it on every read path", finds(ROBIN, "adult", ROBIN_ID), ALL)

print("── nobody else does ──")
check("the sender (the admin) never retrieves Robin's copy", finds(ALEX, "admin", ROBIN_ID), NONE)
check("an adult it wasn't sent to doesn't", finds(HALE, "adult", ROBIN_ID), NONE)
check("a kid doesn't", finds(AVA, "kid", ROBIN_ID), NONE)
check("a shared screen doesn't", finds(TV, "household", ROBIN_ID), NONE)
check("the household's own note still shows for the sender (the filter is per item)",
      finds(ALEX, "admin", "hh-dentist")["search"], True)

print("── another recipient's Keep is their own ──")
V.keep(CARD, JORDAN, "adult")
check("two items, one each", (ROBIN_ID != JORDAN_ID, ROBIN_ID in KB.sources, JORDAN_ID in KB.sources), (True, True, True))
check("Robin never sees Jordan's", finds(ROBIN, "adult", JORDAN_ID), NONE)
check("Jordan never sees Robin's", finds(JORDAN, "adult", ROBIN_ID), NONE)
check("keeping twice keeps once", (V.keep(CARD, ROBIN, "adult")[0], sum(1 for s in KB.sources if s == ROBIN_ID)), (True, 1))

print("── refused where Zach's rule says ──")
check("a kid can't keep", V.keep(CARD, AVA, "kid")[0], False)
check("a shared screen can't keep", V.keep(CARD, TV, "household")[0], False)
check("the audio is never in the knowledge base, only the words",
      [s for s in KB.sources.values() if s["source_id"].startswith("voice-") and "file_id" in s["metadata"]], [])

print("── zone and retention ──")
md = KB.sources[ROBIN_ID]["metadata"]
check("it lands in the facts zone (a thing the person chose to keep)", zones.zone_of(md, ROBIN_ID, "voice-note"), "facts")
import kb_lifecycle  # noqa: E402
due, counts = kb_lifecycle.plan([{"source_id": ROBIN_ID, "source_type": "voice-note", "title": "",
                                  "updated_at": "2020-01-01T00:00:00Z"}], lambda sid: md, kb_lifecycle.Ledger(),
                                now=time.time() + 5 * 365 * 86400, pins=set())
check("nothing expires it, even years later: kept is kept until the person forgets it", due, [])

print("── forgetting ──")
as_(ROBIN, "adult")
A._TURN_IO.source = "user"
out = A.execute_tool("kb_forget", {"record_id": ROBIN_ID})
check("Robin's forget asks first: nothing gone yet", out.startswith(A.ALWAYS_ASK_HELD) and ROBIN_ID in KB.sources, True)
out = _do_it(ROBIN)
A._TURN_IO.source = None
as_(None, None)
check("Robin forgets it: gone", ROBIN_ID in KB.sources, False)
check("…Jordan's copy stays Jordan's", JORDAN_ID in KB.sources, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
