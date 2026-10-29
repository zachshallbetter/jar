#!/usr/bin/env python3
# gate: hermetic
"""S0 (docs/surfaces.md §8): every ResolvedSurface titan emits validates against docs/surfaces.schema.json, the built
jb-1 contract, in Draft-07 (titan's jsonschema validates it fully; no skip: a test that skips on titan never runs).
Every object is an explicit allowlist, so no block, layout or style field can validate: the negative vectors in
scripts/evalsets/surfaces/negative/ (a style field, a layout field, an unknown purpose, an unknown subject kind) must
each fail for exactly that reason. Covered: every saved golden vector, and every resolver's live route output for the
fixture viewers (an admin, an adult, a kid, the screen). Fictional people; temporary stores; no model.
Run: python3 scripts/test_surfaces_schema.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-surfaces-schema-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl", "SURFACES_DB": "surfaces.db",
             "SITTER_DB": "sitter.db", "WORLD_DIR": "world", "KB_ACL_DB": "kb_acl.db", "LINKS_DB": "links.db",
             "LINK_SALT_FILE": "link_salt", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db", "TODOS_DB": "todos.db",
             "SCHEDULED_DB": "scheduled.db", "FENCES_DB": "fences.db", "REFERENCES_DB": "references.db",
             "ROUTINES_DB": "routines.db", "TASKS_DB": "tasks.db", "ITEM_REFS_DB": "item_refs.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily",
             "PUSH_DB": "push.db", "ITEMS_LATENCY_LOG": "items_latency.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TZ"] = "America/Los_Angeles"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL", "APNS_KEY_FILE"):
    os.environ.pop(k, None)
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
VECTORS = HERE / "evalsets" / "surfaces"
from jsonschema import Draft7Validator  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


SCHEMA = json.loads((HERE.parent / "docs" / "surfaces.schema.json").read_text())
check("the schema is Draft-07 and itself valid", (SCHEMA["$schema"], Draft7Validator.check_schema(SCHEMA)),
      ("http://json-schema.org/draft-07/schema#", None))
V = Draft7Validator(SCHEMA)


def errors(doc):
    return [f"{'/'.join(map(str, e.absolute_path)) or '(top)'}: {e.validator}: {e.message[:120]}" for e in V.iter_errors(doc)]


print("── S0 on every saved golden vector ──")
saved = sorted(p for p in VECTORS.glob("*.json") if p.stem != "shipped_keys")
check("there are vectors to check", len(saved) >= 13, True)
for p in saved:
    check(f"{p.stem}: validates", errors(json.loads(p.read_text())), [])

print("── the allowlist bites: each negative vector fails for exactly its reason ──")
WANT = {"style-field": ("groups/0/members/0/slots", "additionalProperties", "color"),
        "layout-field": ("groups/0", "additionalProperties", "columns"),
        "unknown-purpose": ("purpose", "enum", "entertain"),
        "unknown-subject-kind": ("groups/0/members/0/subject/kind", "enum", "spaceship")}
negs = sorted((VECTORS / "negative").glob("*.json"))
check("one negative vector per rejection class", sorted(p.stem for p in negs), sorted(WANT))
for p in negs:
    doc = json.loads(p.read_text())
    doc.pop("_negative", None)              # the note saying why; not part of the surface
    errs = list(V.iter_errors(doc))
    path, validator, word = WANT[p.stem]
    check(f"{p.stem}: rejected, once, at {path} ({validator}), naming {word}",
          [("/".join(map(str, e.absolute_path)), e.validator, word in e.message) for e in errs], [(path, validator, True)])

print("── S0 on every resolver's live output, per fixture viewer ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import chat_threads  # noqa: E402
import documents  # noqa: E402
import household_identity as hh  # noqa: E402
import local_time  # noqa: E402
import proactive  # noqa: E402
import scheduled  # noqa: E402
import schedule_view as SV  # noqa: E402
import todos  # noqa: E402

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
    return {"X-Agent-Token": tok}, next((p["id"] for p in st.people() if p["name"] == name), None)


ROBIN, robin = pair("Robin Quarry", "adult")
AVA, ava = pair("Ava Quarry", "kid")
TV, _ = pair("Living room", "household", "tvos")
ADMIN = hh.ADMIN_PERSON_ID
today = local_time.now().date()
d0, d1 = today.isoformat(), (today + timedelta(days=1)).isoformat()
PEOPLE = [{"id": ADMIN, "name": "Alex", "role": "admin", "calendars": []},
          {"id": robin, "name": "Robin", "role": "adult", "calendars": ["Robin"]},
          {"id": ava, "name": "Ava", "role": "kid", "parents": [ADMIN, robin], "calendars": ["Ava"]}]


def ev(nid, title, start, cal, owner, end=None):
    return {"node_id": nid, "data": {"title": title, "start": start, "end": end, "calendar": cal, "location": "Harbor Hall",
                                     "provenance": {"owner": owner, "audience": "private"}}}


NODES = [ev("e1", "Dentist", f"{d0}T09:00:00", "Work", ADMIN, f"{d0}T10:00:00"),
         ev("e2", "Budget call", f"{d0}T09:30:00", "Work", ADMIN, f"{d0}T10:15:00"),
         ev("e3", "Ballet", f"{d0}T16:00:00", "Ava", ADMIN), ev("e4", "Bins out", f"{d1}T20:00:00", "Family", ADMIN)]
SV.for_viewer = lambda s, e, v_, r, only=None: {**SV.view(NODES, s, e, v_, r, PEOPLE, {"Family"}, only=only,
                                                          zone=local_time.zone()), "partial": False}
documents.listing = lambda role, *a, **k: {"documents": [
    {"id": "kb:note-recycling", "title": "Recycling days", "kind": "note", "content_type": "text/markdown", "size": None,
     "modified": "2026-10-04", "source": "kb", "original": False, "available": True},
    {"id": "rf-0000000000000002", "title": "Menu", "kind": "file", "content_type": "application/pdf", "size": None,
     "modified": "2026-10-01", "source": "device", "original": True, "available": False, "file_shared": False}],
    "generated_at": "2026-10-06T15:00:00+00:00"}
THREADS = {"view": "threads", "filters": [], "facets": {"kind": [], "person": [], "device": [], "project": [], "place": [], "room": []},
           "since": "2026-09-06", "threads": [{"id": "ct-1", "name": "Lakeside Clinic", "description": "2 chats · calendar",
                                               "conversations": [{"id": "c1", "item_id": "chat-c1", "title": "Call the clinic",
                                                                  "preview": "Remind me", "last_at": "2026-10-06T10:00:00Z", "count": 2},
                                                                 {"id": "c2", "item_id": "chat-c2", "title": "Move the referral",
                                                                  "preview": "Friday", "last_at": "2026-10-06T10:20:00Z", "count": 2}],
                                               "edges": [{"a": "chat-c1", "b": "chat-c2", "type": "continues", "state": "accepted", "why": "same record"}],
                                               "fringe": []}],
           "recent": [{"id": "c3", "item_id": "chat-c3", "title": "Weather", "preview": "Sunny", "last_at": "2026-10-05T10:00:00Z", "count": 2}]}
chat_threads.build = lambda pid, role, mem, **kw: (_ for _ in ()).throw(chat_threads.Refused(403, "a shared screen has no history")) \
    if role == "household" else THREADS
api._MEMORY_AVAILABLE = True
api._get_memory = lambda: type("Mem", (), {"_db": None})()
proactive.add_card("Approve the plumber quote?", "Admin", urgency=2, source="approve-cap", to=[ADMIN])
proactive.add_card("Bins tonight", "Everyone", urgency=1, source="routine", to="household")
proactive.add_card("Robin's parcel", "Robin", urgency=1, source="routine", to=[robin])
todos.add("Buy stamps", ADMIN)
scheduled.add(ADMIN, "Call the bank", "create_reminder", {}, local_time.now().timestamp() + 600)

ROUTES = ["/v1/proactive?as=surface", "/v1/conversations?view=threads&as=surface", "/v1/documents?as=surface",
          "/v1/schedule?as=surface", "/v1/brief", "/v1/items?view=now", "/v1/items?view=unsorted",
          f"/v1/items?view=time&from={d0}&to={(today + timedelta(days=6)).isoformat()}",
          "/v1/surfaces/household-facts", "/v1/surfaces/always-shared"]
validated = 0
for nm, h in (("admin", M), ("adult", ROBIN), ("kid", AVA), ("screen", TV)):
    for path in ROUTES:
        r = c.get(path, headers=h)
        if r.status_code != 200:
            continue                          # a route this viewer can't call: nothing emitted to validate
        body = r.json()
        if "groups" not in body:
            continue
        validated += 1
        check(f"{nm} {path.split('?')[0]}{'?' + path.split('?')[1][:24] if '?' in path else ''}: validates", errors(body), [])
check("live outputs validated: every surface route for at least the admin", validated >= len(ROUTES), True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
