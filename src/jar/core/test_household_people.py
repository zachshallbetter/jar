#!/usr/bin/env python3
# gate: hermetic
"""Who's in the household, for anyone in it (#1164): GET /v1/household/people → {people: [{id, name, role}]} for the
admin, an adult or a kid alike; a shared screen gets 403; exactly those three fields (never devices, contact, birth
year, parents, aliases or facts); the name they go by; someone mid delete-my-data still listed. Fictional people
(Robin and Jordan adults, Ava a kid, a living-room screen); temporary stores; no model.
Run: python3 scripts/test_household_people.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-hh-people-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "SPACES_DB": "spaces.db", "CONNECTORS_LEDGER": "ledger.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "WORLD_DIR": "world",
             "ME_DELETE_DIR": "me-delete"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


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
    return {"X-Agent-Token": tok}, next(p["id"] for p in st.people() if p["name"] == name)


ROBIN, robin = pair("Robin Hale", "adult")
JORDAN, jordan = pair("Jordan Reyes", "adult")
AVA, ava = pair("Ava Hale", "kid")
TV, _ = pair("Living room", "household", "tvos")
# details only the admin's list carries
st.update_person(ava, parents=[robin])
c.patch("/v1/me", json={"call_me": "Robbie"}, headers=ROBIN)

lists = {nm: c.get("/v1/household/people", headers=h) for nm, h in (("admin", M), ("robin", ROBIN), ("ava", AVA))}
check("the admin, an adult and a kid all get 200", [r.status_code for r in lists.values()], [200, 200, 200])
check("…and the same list", len({str(sorted(map(str, r.json()["people"]))) for r in lists.values()}), 1)
people = lists["robin"].json()["people"]
check("everyone in the household, never the shared screen", sorted(p["role"] for p in people), ["admin", "adult", "adult", "kid"])
check("exactly id, name and role on every entry (the allowlist)", {tuple(sorted(p)) for p in people}, {("id", "name", "role")})
check("the name they go by (call_me), else their name",
      sorted(p["name"] for p in people if p["id"] in (robin, ava, jordan)), ["Ava Hale", "Jordan Reyes", "Robbie"])
body = lists["ava"].text
check("no device, parent or facts in the kid's view", [x for x in ("device", "Ava's device", "parents", robin + '"]', "platform")
                                                       if x in body], [])
check("a shared screen gets 403", c.get("/v1/household/people", headers=TV).status_code, 403)
import json  # noqa: E402
import me_delete  # noqa: E402
_t = me_delete._tomb(jordan, create=True)        # Jordan's data in delete-my-data's 7-day window
_t.execute("INSERT OR REPLACE INTO state VALUES ('state', ?)", (json.dumps("pending"),))
_t.commit()
_t.close()
check("…Jordan is mid delete-my-data", me_delete._state(me_delete._tomb(jordan)).get("state"), "pending")
check("someone mid-deletion is still in the household list", jordan in [p["id"] for p in
                                                                         c.get("/v1/household/people", headers=ROBIN).json()["people"]], True)
check("the admin's /v1/people stays admin-only", c.get("/v1/people", headers=ROBIN).status_code, 403)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
