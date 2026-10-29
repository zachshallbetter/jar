#!/usr/bin/env python3
# gate: hermetic
"""Searching a person's own past conversations (jellybean#827): every word of the query in a message, newest first,
with a snippet; only ever the asker's own stream: never another person's, a kid's only their own, a household device
nothing. Made-up people. Run: python3 scripts/test_conversation_search.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-convsearch-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "SPACES_DB": "spaces.db", "SITTER_DB": "sitter.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


import conversations as cv  # noqa: E402

print("── the search ──")
check("terms: lowercased words, at most 8", cv.terms("  Vet  BILL due "), ["vet", "bill", "due"])
long = "word " * 40 + "Maple Animal Clinic bill is due Friday " + "more " * 40
s = cv.snippet(long, ["clinic"])
check("a snippet around the match, cut at words, with … at both ends",
      ("Maple Animal Clinic" in s, s.startswith("…"), s.endswith("…"), len(s) <= cv.SNIPPET + 2), (True, True, True, True))
check("a short message is its own snippet", cv.snippet("The vet bill", ["vet"]), "The vet bill")

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
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": f"{name}'s device", "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ALEX, alex = pair("Alex Hale", "adult")
ROBIN, robin = pair("Robin Hale", "adult")
AVA, ava = pair("Ava Hale", "kid")
TV, kitchen = pair("Kitchen", "household", platform="tvos")
mem = api._get_memory()
mem.add_message(hh.conv_id_for(alex), "user", "When is the vet bill due for Biscuit?", thread="th-a1", device="Alex's iPhone")
mem.add_message(hh.conv_id_for(alex), "assistant", "The Maple Animal Clinic bill is due Friday.", thread="th-a1")
mem.add_message(hh.conv_id_for(alex), "user", "Plan the coast trip", thread="th-a2")
mem.add_message(hh.conv_id_for(robin), "user", "ROBIN-ONLY: the vet bill for my own records", thread="th-r1")
mem.add_message(hh.conv_id_for(ava), "user", "AVA-ONLY: my science project on the vet", thread="th-k1")
mem.add_message(hh.conv_id_for(kitchen), "user", "KITCHEN-ONLY vet timer", thread="th-h1")
mem.add_message("chat", "user", "ADMIN-ONLY vet appointment", thread="th-z1")

print("\n── through the API ──")
r = c.get("/v1/conversations/search", params={"q": "bill"}, headers=ALEX)
hits = r.json()["hits"]
check("newest first: the reply, then the question", (r.status_code, [h["role"] for h in hits]), (200, ["assistant", "user"]))
check("every word must be there: 'vet bill' finds only the question (the reply says 'Clinic')",
      [h["role"] for h in c.get("/v1/conversations/search", params={"q": "vet bill"}, headers=ALEX).json()["hits"]], ["user"])
check("each hit names its conversation and title", (hits[0]["conversation"], hits[0]["title"]),
      ("th-a1", "When is the vet bill due for Biscuit?"))
check("…with a snippet that holds the match", "bill is due Friday" in hits[0]["snippet"], True)
check("one word missing: no hit", c.get("/v1/conversations/search", params={"q": "vet coast"}, headers=ALEX).json()["hits"], [])
check("limit", len(c.get("/v1/conversations/search", params={"q": "vet", "limit": 1}, headers=ALEX).json()["hits"]), 1)
for bad in ("v", "x" * 101, "   "):
    check(f"q {bad[:5]!r}…: 400", c.get("/v1/conversations/search", params={"q": bad}, headers=ALEX).status_code, 400)

print("\n── only ever the asker's own ──")
everyone = ("ROBIN-ONLY", "AVA-ONLY", "KITCHEN-ONLY", "ADMIN-ONLY")
alex_text = " ".join(h["snippet"] for h in c.get("/v1/conversations/search", params={"q": "vet"}, headers=ALEX).json()["hits"])
check("an adult never sees another person's hits", [m for m in everyone if m in alex_text], [])
robin_hits = c.get("/v1/conversations/search", params={"q": "vet"}, headers=ROBIN).json()["hits"]
check("…and the other adult sees only theirs (the control)", [h["snippet"][:10] for h in robin_hits], ["ROBIN-ONLY"])
kid = c.get("/v1/conversations/search", params={"q": "vet"}, headers=AVA)
check("a kid sees only their own", (kid.status_code, [h["snippet"][:8] for h in kid.json()["hits"]]), (200, ["AVA-ONLY"]))
hh_r = c.get("/v1/conversations/search", params={"q": "vet"}, headers=TV)
check("a household device gets nothing (403)", (hh_r.status_code, "KITCHEN-ONLY" in hh_r.text), (403, False))
admin = c.get("/v1/conversations/search", params={"q": "vet"}, headers=M).json()["hits"]
check("the master token is the admin's own stream only", [h["snippet"][:10] for h in admin], ["ADMIN-ONLY"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
