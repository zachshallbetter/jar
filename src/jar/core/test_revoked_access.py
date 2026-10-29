#!/usr/bin/env python3
# gate: hermetic   (throwaway stores; the real routes through a TestClient)
"""A revoked device gets nothing (10-07 red team (a): the apps' re-pair leaks were stale caches on the device; this is
titan's half). Every authenticated GET route of the API, called with a device token that was revoked ("Leave this
household", or the admin removing it), answers 401: never a 200 with what that person could see before. The same
person's other, still-paired device keeps working, and a revoked household screen gets nothing either. The routes are
read from the app itself, so a route added later is covered without editing this test; the public ones are the
documented allowlist. Fictional people (Alex, Robin; Ava, a kid).
Run: python3 scripts/test_revoked_access.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-revoked-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db",
             "SCHEDULED_DB": "scheduled.db", "REFERENCES_DB": "references.db", "WORLD_DIR": "world", "PUSH_DB": "push.db",
             "SITTER_DB": "sitter.db", "LOCK_UP_DB": "lock_up.db", "TV_UNLOCK_DB": "tv_unlock.db", "LINKS_DB": "links.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily",
             "FENCES_DB": "fences.db", "LINK_SALT_FILE": "link_salt", "MEMORY_DB": "memory.db", "TITLES_DB": "titles.db",
             "RETRIEVAL_TRACE_DB": "trace.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL", "APNS_KEY_FILE"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import titan_alert  # noqa: E402
titan_alert.alert = lambda *a, **k: None

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.600} expected={expected!r:.300}"))


from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-revoked-test-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios", person_id=None):
    body = {"person_id": person_id} if person_id else {"new_person": {"name": name, "role": role}}
    s = c.post("/v1/pair/start", json=body, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"], cl.get("device", {}).get("id") or cl.get("device_id")


ALEX, alex, alex_phone = pair("Alex Hale", "adult", "Alex's iPhone")
ALEX_MAC, _, _ = pair(None, None, "Alex's Mac", platform="mac", person_id=alex)
TV, _, tv_dev = pair("Living room", "household", "Living room TV", platform="tvos")


# every GET route the app serves, with path parameters filled with harmless placeholders
PUBLIC = api._PUBLIC_PATHS
PUBLIC_PREFIXES = api._PUBLIC_PREFIXES
paths = []
for r in api.app.routes:
    if "GET" not in getattr(r, "methods", set()):
        continue
    path = getattr(r, "path", "")
    if not path.startswith("/v1/") or path in PUBLIC or path.startswith(PUBLIC_PREFIXES):
        continue
    paths.append(re.sub(r"\{[^}]+\}", "x1", path))
paths = sorted(set(paths))
check("there are authenticated GET routes to check", len(paths) > 50, True)

# before: a few store-only routes answer (never the whole sweep with a live token: some GETs reach runtimes or the web)
SAFE = ["/v1/items?view=now", "/v1/proactive", "/v1/todos"]
check("before: Alex's iPhone is answered on its own lists", [c.get(p, headers=ALEX).status_code for p in SAFE], [200, 200, 200])

# Alex's iPhone leaves the household (revokes itself), as "Leave this household" does
me = c.get("/v1/devices/self", headers=ALEX)
pid_phone = (me.json() or {}).get("id") if me.status_code == 200 else alex_phone
r = c.delete(f"/v1/devices/{pid_phone}", headers=ALEX)
check("Alex's iPhone revokes itself", r.status_code, 200)
after = {p: c.get(p, headers=ALEX).status_code for p in paths}
leaky = sorted(p for p, s in after.items() if s != 401)
check("after: every authenticated GET answers the revoked token 401 (never a 200 of what it saw)", leaky, [])
check("…while Alex's Mac, still paired, is answered as before", c.get("/v1/items?view=now", headers=ALEX_MAC).status_code, 200)

tvme = c.get("/v1/devices/self", headers=TV)
tid = (tvme.json() or {}).get("id") if tvme.status_code == 200 else tv_dev
c.delete(f"/v1/devices/{tid}", headers=M)
tv_after = sorted(p for p in paths if c.get(p, headers=TV).status_code != 401)
check("a revoked household screen gets 401 everywhere too", tv_after, [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
