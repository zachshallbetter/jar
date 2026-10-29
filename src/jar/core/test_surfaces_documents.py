#!/usr/bin/env python3
# gate: hermetic
"""Household documents as a ResolvedSurface (#1060 migration step 4: `collection`, GET /v1/documents?as=surface).
Built from documents.listing's real payload over fictional sources (test_documents.py's), never a hand-made shape.
S1: golden vector scripts/evalsets/surfaces/documents.household.json (`--write` rewrites it; review the diff). Rules:
what opens first, originals still on a device with Open unavailable, the plain row verbatim, no ask (no route that
carries a document reference yet). S2 through the real route: the same household list for every role, never a private
or adults-only note, deterministic, the plain shape unchanged. Fictional (Alex, Robin, Ava); temporary stores.
Run: python3 scripts/test_surfaces_documents.py [--write]"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-documents-surface-"))
for k, v in {"REFERENCES_DB": "references.db", "HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "SURFACES_DB": "surfaces.db", "SITTER_DB": "sitter.db", "WORLD_DIR": "world", "KB_ACL_DB": "kb_acl.db",
             "LINKS_DB": "links.db", "LINK_SALT_FILE": "link_salt", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db",
             "TODOS_DB": "todos.db", "SCHEDULED_DB": "scheduled.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
VECTORS = HERE / "evalsets" / "surfaces"
WRITE = "--write" in sys.argv
import documents as D  # noqa: E402
import file_share as FS  # noqa: E402
import references as R  # noqa: E402
import surfaces as S  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def vector(name, got):
    p = VECTORS / f"{name}.json"
    if WRITE:
        p.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    check(f"S1 golden vector {name}", got, json.loads(p.read_text()) if p.exists() else None)


NOW = 1791331200.0
SOURCES = [{"source_id": "note-recycling", "source_type": "note", "title": "Recycling days", "updated_at": "2026-10-04"},
           {"source_id": "note-alex-private", "source_type": "note", "title": "Alex's own notes", "updated_at": "2026-10-05"},
           {"source_id": "note-adults", "source_type": "note", "title": "Budget", "updated_at": "2026-10-03"},
           {"source_id": "doc-wifi", "source_type": "document", "title": "Guest wifi", "updated_at": "2026-09-30"}]
ACL = {"note-recycling": ("alex", "household"), "note-alex-private": ("alex", "private"),
       "note-adults": ("alex", "adults"), "doc-wifi": ("p_robin", "household")}
acl = lambda sid, md: ACL[sid]  # noqa: E731
con = R._db(None)
for rid, owner, aud, kind, nid, mod in (("rf-0000000000000001", "alex", "household", "scan", "lease.pdf", "2026-10-02"),
                                        ("rf-0000000000000002", "p_robin", "household", "file", "menu.pdf", "2026-10-01"),
                                        ("rf-0000000000000003", "p_robin", "private", "scan", "id.pdf", "2026-10-05")):
    con.execute("INSERT INTO refs (id, owner, audience, device_id, kind, native_id, title, dates, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,1,1)", (rid, owner, aud, "dev-1", kind, nid, nid.split(".")[0].title(),
                                                  json.dumps({"modified": mod})))
con.commit()
con.close()
FS.share("rf-0000000000000001", "alex", "admin", True)
real_listing = D.listing


def listing(role):
    return real_listing(role, SOURCES, acl)


print("── S1: the real household list, resolved ──")
body = listing("adult")
surf = S.documents(body, "alex", "admin", NOW)
vector("documents.household", surf)
G = {g["id"]: g for g in surf["groups"]}
mids = lambda gid: [m["subject"]["id"] for m in G[gid]["members"]]  # noqa: E731
OPS = {o["id"]: o for o in surf["operations"]}
check("profile collection, purpose browse, jb-1", (surf["profile"], surf["purpose"], surf["profileVersion"]),
      ("collection", "browse", "jb-1"))
check("groups: what opens, then what's still on people's devices", [(g["id"], g["role"], g["label"]) for g in surf["groups"]],
      [("g-documents", "primary", {"key": "documents.all", "text": "Documents"}),
       ("g-on-devices", "supporting", {"key": "documents.on_devices", "text": "On people's devices"})])
check("what opens: household notes and documents, and the original its owner shared, newest first",
      mids("g-documents"), ["kb:note-recycling", "rf-0000000000000001", "kb:doc-wifi"])
check("on people's devices: the household original nobody shared as a file", mids("g-on-devices"), ["rf-0000000000000002"])
check("never a private or adults-only note, never someone's private scan",
      [i for i in mids("g-documents") + mids("g-on-devices") if i in ("kb:note-alex-private", "kb:note-adults",
                                                                         "rf-0000000000000003")], [])
check("open is the content route; unavailable, with a reason, for an original still on its owner's device",
      [(OPS[f"op-open-{i}"]["route"]["path"], OPS[f"op-open-{i}"]["exposure"], OPS[f"op-open-{i}"].get("reason"))
       for i in ("kb:note-recycling", "rf-0000000000000002")],
      [("/v1/documents/kb:note-recycling/content", "available", None),
       ("/v1/documents/rf-0000000000000002/content", "unavailable", {"key": "document.on_device"})])
check("open and ask; ask sends fixed words and a reference, never the title (#1322, #473)",
      (sorted({o["verb"] for o in surf["operations"]}), OPS["op-ask-kb:note-recycling"]["params"],
       any("Recycling days" in json.dumps(o) for o in surf["operations"])),
      (["ask", "open"], {"prompt": "Tell me about this.", "about": {"kind": "document", "id": "kb:note-recycling"}}, False))
check("each member keeps its plain row verbatim (the File Provider's parser reads it)",
      {m["subject"]["id"]: m["document"] for g in surf["groups"] for m in g["members"]},
      {d["id"]: d for d in body["documents"]})
check("deterministic (S2-9)", S.documents(listing("adult"), "alex", "admin", NOW)["fingerprint"], surf["fingerprint"])
check("an empty list is two empty groups, sizeClass few",
      ([len(g["members"]) for g in S.documents({}, "ava", "kid", NOW)["groups"]], S.documents({}, "ava", "kid", NOW)["sizeClass"]),
      ([0, 0], "few"))

print("── S2: through the real route ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, platform="ios"):
    s_ = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    tok = c.post("/v1/pair/claim", json={"code": s_["code"], "device_name": f"{name}'s device",
                                         "platform": platform}).json()["token"]
    return {"X-Agent-Token": tok}


D.listing = lambda role, *a, **k: real_listing(role, SOURCES, acl)   # the route's call, over this test's sources
VIEWERS = {"admin": M, "robin": pair("Robin Quarry", "adult"), "ava": pair("Ava Quarry", "kid"),
           "screen": pair("Living room", "household", "tvos")}
got = {}
for nm, h in VIEWERS.items():
    r = c.get("/v1/documents?as=surface", headers=h)
    got[nm] = r.json()
    check(f"{nm}: 200, jb-1 collection", (r.status_code, got[nm].get("profileVersion"), got[nm].get("profile")),
          (200, "jb-1", "collection"))
    check(f"{nm}: never a private or adults-only title (whole JSON)",
          [t for t in ("Alex's own notes", "Budget", "Id") if f'"{t}"' in r.text], [])
check("the same household list for every role", {nm: [m["subject"]["id"] for g in s["groups"] for m in g["members"]]
                                                 for nm, s in got.items()},
      {nm: ["kb:note-recycling", "rf-0000000000000001", "kb:doc-wifi", "rf-0000000000000002"] for nm in got})
check("same state, same fingerprint (S2-9)", c.get("/v1/documents?as=surface", headers=M).json()["fingerprint"],
      got["admin"]["fingerprint"])
check("audience other than household is still 400", c.get("/v1/documents?as=surface&audience=adults", headers=M).status_code, 400)
paths = {r.path for r in api.app.routes if hasattr(r, "path")}
check("open's route is one the app has", "/v1/documents/{doc_id}/content" in paths, True)
old = c.get("/v1/documents", headers=M).json()
check("old clients keep the plain list", (sorted(old), len(old["documents"])), (["documents", "generated_at"], 4))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
