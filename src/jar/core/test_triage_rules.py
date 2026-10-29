#!/usr/bin/env python3
# gate: hermetic
"""What a person taught mail triage (#1644): GET /v1/me/triage-rules lists their own rules, DELETE
/v1/me/triage-rules/<sender> takes one back, a shared screen gets 403, and nobody sees or changes anyone else's.
Fictional people (Robin an adult, Ava a kid, a living-room screen); temporary stores; no model.
Run: python3 scripts/test_triage_rules.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-triage-rules-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "SPACES_DB": "spaces.db", "CONNECTORS_LEDGER": "ledger.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "WORLD_DIR": "world",
             "ME_DELETE_DIR": "me-delete", "CLAUDE_CHATS_DB": "claude_chats.db", "MAIL_TRIAGE_DB": "triage.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import mail_triage as mt  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.200} expected={expected!r:.200}"))


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
    return {"X-Agent-Token": tok}, next(p["id"] for p in st.people() if p["name"] == name)


ROBIN, robin = pair("Robin Hale", "adult")
AVA, ava = pair("Ava Hale", "kid")
TV, _ = pair("Living room", "household", "tvos")
mt.learn(robin, "Jordan Ava <jordan@example.org>", "keep")
mt.learn(robin, "news@garden.example", "drop")
mt.learn(ava, "coach@school.example", "keep")
got = c.get("/v1/me/triage-rules", headers=ROBIN).json()["rules"]
check("Robin sees their own rules, newest first", [(r["sender"], r["rule"]) for r in got],
      [("news@garden.example", "drop"), ("jordan@example.org", "keep")])
check("Ava sees only hers", [r["sender"] for r in c.get("/v1/me/triage-rules", headers=AVA).json()["rules"]],
      ["coach@school.example"])
check("a shared screen gets 403", (c.get("/v1/me/triage-rules", headers=TV).status_code,
                                   c.delete("/v1/me/triage-rules/jordan@example.org", headers=TV).status_code), (403, 403))
r = c.delete("/v1/me/triage-rules/coach@school.example", headers=ROBIN)
check("Robin can't take back Ava's rule (it isn't theirs: nothing forgotten)",
      (r.json(), mt.rule_for(ava, "coach@school.example")), ({"forgotten": False}, "keep"))
r = c.delete("/v1/me/triage-rules/JORDAN@example.org", headers=ROBIN)
check("Robin takes back their keep rule (any case)", (r.json(), mt.rule_for(robin, "jordan@example.org")),
      ({"forgotten": True}, None))
check("…their next mail from Jordan is the model's call again", [x["sender"] for x in mt.rules(robin)], ["news@garden.example"])
check("not an address → 400", c.delete("/v1/me/triage-rules/jordan", headers=ROBIN).status_code, 400)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
