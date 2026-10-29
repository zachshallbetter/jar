#!/usr/bin/env python3
# gate: hermetic
"""The contacts routes (#1380): POST v1/contacts/sync and DELETE v1/contacts/{native_id}, from an adult's own paired
device; on for each person until they turn it off (#1627), and off for the house with CONTACTS_CONNECTOR=0. Off: 404. The master token, a shared screen and a kid: 403 (the route itself, not
only ingest). A field titan doesn't keep: 400 naming the field, never a value. A sync keys phones and emails and keeps
them nowhere raw; a full listing deletes what it doesn't list; a delete removes what titan kept. Temp stores only.
Run: python3 scripts/test_contacts_route.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-contacts-route-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "WORLD_DIR": "world", "WORLD_DB": "world.db", "REFERENCES_DB": "refs.db",
             "LINK_SALT_FILE": "salt", "LINKS_DB": "links.db", "PLACES_DB": "places.db", "KB_ACL_DB": "kb_acl.db",
             "CONNECTORS_LEDGER": "ledger.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "PUSH_DB": "push.db",
             "FENCES_DB": "fences.db", "SITTER_DB": "sitter.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
os.environ["SPACES_DB"] = str(_TMP / "spaces.db")
os.environ["CAPABILITY_SWITCHES"] = str(_TMP / "switches.json")
os.environ["CONTACTS_CONNECTOR"] = "0"
sys.path.insert(0, str(Path(__file__).parent))
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import contacts_connector as C  # noqa: E402
import household_identity as hh  # noqa: E402
import references  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.160} expected={expected!r:.160}")


MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def paired(name, role, label):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}, "label": label}, headers=M).json()
    r = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": label, "platform": "mac"}).json()
    return {"X-Agent-Token": r["token"]}, s["person"]["id"]


ALEX, alex_id = paired("Alex", "adult", "Alex's MacBook")
ALEX_DEVICE = c.get("/v1/me", headers=ALEX).json()["device"]["id"]
KID, _ = paired("Ava", "kid", "Ava's MacBook")
SCREEN, _ = paired("Kitchen", "household", "Kitchen iPad")
PHONE, PHONE_KEY = "+1 (555) 010-0077", "5550100077"
BODY = {"contacts": [{"id": "A1", "given": "Morgan", "family": "Reyes", "phones": [PHONE], "emails": ["Morgan@Example.org"]},
                     {"id": "A2", "given": "Dana", "family": "Pike", "nickname": "Dee"}],
        "relations": [{"label": "coach", "name": "Morgan Reyes"}], "full": True}


def sync(headers, body=BODY):
    return c.post("/v1/contacts/sync", json=body, headers=headers)


print("── off ──")
r = sync(ALEX)
check("off: 404, as if the route weren't there", (r.status_code, r.json()), (404, {"detail": "Not Found"}))
r = c.delete("/v1/contacts/A1", headers=ALEX)
check("…but a delete always answers (deleting your own is never off): nothing kept, nothing removed",
      (r.status_code, r.json()), (200, {"removed": False}))
check("…and nothing was kept", (C.observations, list((_TMP / "world").glob("*")) if (_TMP / "world").exists() else []),
      (C.observations, []))

os.environ["CONTACTS_CONNECTOR"] = "1"
print("── who may ──")
for who, h in (("the master token", M), ("a shared screen", SCREEN), ("a kid", KID)):
    check(f"{who}: 403 on sync", sync(h).status_code, 403)
    check(f"{who}: 403 on delete", c.delete("/v1/contacts/A1", headers=h).status_code, 403)
check("no token: 401", c.post("/v1/contacts/sync", json=BODY).status_code, 401)
# defence in depth: a kid that got past the role table is still refused by the route itself
saved = hh.route_allowed
hh.route_allowed = lambda role, method, path: True
check("a kid past the role table: the route still says 403", sync(KID).status_code, 403)
check("…and so does a shared screen", sync(SCREEN).status_code, 403)
hh.route_allowed = saved

print("── what's refused ──")
for label, body in (("an address", {"contacts": [{"id": "X", "given": "Robin", "postalAddresses": ["1 Pine St"]}]}),
                    ("a note", {"contacts": [{"id": "X", "given": "Robin", "note": "gate code 4471"}]}),
                    ("a birthday", {"contacts": [{"id": "X", "given": "Robin", "birthday": "2001-02-03"}]}),
                    ("a relation's extra field", {"contacts": [], "relations": [{"label": "mother", "name": "Jo", "phone": "1"}]}),
                    ("a top-level extra", {"contacts": [], "photos": []})):
    r = sync(ALEX, body)
    text = r.text
    check(f"{label}: 400", r.status_code, 400)
    check(f"…naming the field, never its value", any(v in text for v in ("1 Pine St", "4471", "2001-02-03", "Jo\"")), False)
for label, body in (("not an object", ["A1"]), ("contacts not a list", {"contacts": {"id": "A1"}}),
                    ("a contact not an object", {"contacts": ["A1"]}), ("full not a bool", {"contacts": [], "full": "yes"}),
                    ("a contact without its id", {"contacts": [{"given": "No Id"}]}),
                    ("too many phones", {"contacts": [{"id": "Z", "given": "Z", "phones": ["5550100000"] * 9}]})):
    check(f"{label}: 400", sync(ALEX, body).status_code, 400)

print("── a sync, and deletes ──")
r = sync(ALEX)
check("an adult's own Mac: 200 with counts", (r.status_code, r.json().get("contacts"), r.json().get("deleted")), (200, 2, 0))
raw = [p for p in _TMP.rglob("*") if p.is_file() and any(x in p.read_bytes() for x in (PHONE_KEY.encode(), b"0100077",
                                                                                     b"morgan@example.org", b"Morgan@Example.org"))]
check("the raw phone and email are kept nowhere on titan", raw, [])
check("…a reference per contact, on Alex's own Mac", sorted(references.native_ids(alex_id, ALEX_DEVICE, C.KIND)), ["A1", "A2"])
r = sync(ALEX, {"contacts": [BODY["contacts"][0]], "relations": [], "full": True})
check("a full listing without A2 deletes it", r.json().get("deleted"), 1)
check("a delete: removed", c.delete("/v1/contacts/A1", headers=ALEX).json(), {"removed": True})
check("…again: nothing to remove", c.delete("/v1/contacts/A1", headers=ALEX).json(), {"removed": False})
check("…and no reference is left", references.native_ids(alex_id, ALEX_DEVICE, C.KIND), [])

print("── turned off on titan with contacts kept: a delete still deletes (jellybean#1216 review) ──")
sync(ALEX)
os.environ["CONTACTS_CONNECTOR"] = "0"
check("off: a sync is 404 again", sync(ALEX).status_code, 404)
r = c.delete("/v1/contacts/A2", headers=ALEX)
check("…but a delete removes what was kept while it was on (never a 404 the Mac would take as done)",
      (r.status_code, r.json()), (200, {"removed": True}))
check("…its reference is gone", sorted(references.native_ids(alex_id, ALEX_DEVICE, C.KIND)), ["A1"])
for who, h in (("the master token", M), ("a shared screen", SCREEN), ("a kid", KID)):
    check(f"…off, {who}: still 403 on delete", c.delete("/v1/contacts/A1", headers=h).status_code, 403)
check("…the last one goes too", c.delete("/v1/contacts/A1", headers=ALEX).json(), {"removed": True})
os.environ["CONTACTS_CONNECTOR"] = "1"

print("── per person (#1627): on until each one turns it off ──")
import governance  # noqa: E402
os.environ.pop("CONTACTS_CONNECTOR")
ROBIN, robin_id = paired("Robin", "adult", "Robin's MacBook")
check("the flag unset is on (it's the house's kill switch now)", sync(ROBIN).status_code, 200)
governance.set_tier(robin_id, "contacts", "never")
r = sync(ROBIN)
check("Robin turned contacts off: Robin's sync is refused, and says how to turn it back on",
      (r.status_code, "Settings" in r.json().get("error", "")), (403, True))
check("…Alex's still syncs", sync(ALEX).status_code, 200)
r = c.delete("/v1/contacts/A1", headers=ROBIN)
check("…and Robin can still delete what titan kept", (r.status_code, r.json()), (200, {"removed": True}))
governance.set_tier(robin_id, "contacts", "always")
governance.set_tier(robin_id, "records", "never")
check("Robin's Never for records covers contacts", sync(ROBIN).status_code, 403)
governance.set_tier(robin_id, "records", "always")
os.environ["CONTACTS_CONNECTOR"] = "1"

print("── the route doc ──")
check("the route doc is current", "/v1/contacts/sync" in (Path(__file__).parent.parent / "docs" / "api-routes.md").read_text(), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
