#!/usr/bin/env python3
# gate: hermetic   (throwaway stores; the real routes through a TestClient, fake source addresses)
"""A kid's Watch pairing, per source (10-07 review W6). One device on the network can't hold the household's open codes
(it gets WATCH_CODES_PER_SOURCE), and failing on purpose (wrong secrets, wrong codes) locks out only itself: another
Watch still pairs, and a parent's iPhone that typed wrong codes doesn't stop another parent. Fictional people.
Run: python3 scripts/test_watch_pair_caps.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-watch-caps-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "PUSH_DB": "push.db", "CONNECTORS_LEDGER": "ledger.db", "SERVER_IDENTITY_KEY": "id/key"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-watch-caps-0123456789"
api._AGENT_TOKEN = MASTER
store = hh.HouseholdStore(_TMP / "caps.db")
hh.reset_store(store)
M = {"X-Agent-Token": MASTER}
clients = {}


def from_ip(ip):   # a TestClient whose requests come from `ip`
    if ip not in clients:
        clients[ip] = TestClient(api.app, client=(ip, 50000))
    return clients[ip]


home = from_ip("10.0.0.20")


def paired(name, role):
    s = home.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    r = home.post("/v1/pair/claim", json={"code": s["code"], "device_name": f"{name}'s iPhone", "platform": "ios"}).json()
    return s["person"], {"X-Agent-Token": r["token"]}


p_alex, A = paired("Alex", "adult")
p_robin, R = paired("Robin", "adult")
p_ava, _ = paired("Ava", "kid")
store.update_person(p_ava["id"], parents=[p_alex["id"], p_robin["id"]])

print("── open codes per source ──")
bad = from_ip("10.0.0.66")
got = [bad.post("/v1/watch/pair/request", json={"device_name": "x"}).status_code for _ in range(hh.WATCH_CODES_PER_SOURCE + 2)]
check("one source gets WATCH_CODES_PER_SOURCE codes, then 429", got,
      [200] * hh.WATCH_CODES_PER_SOURCE + [429, 429])
real = from_ip("10.0.0.31").post("/v1/watch/pair/request", json={"device_name": "Ava's Watch"})
check("…and a real Watch elsewhere still gets its code", real.status_code, 200)
w = real.json()

print("── failures lock out their own source only ──")
for _ in range(hh.WATCH_FAILURES_PER_SOURCE):
    bad.get(f"/v1/watch/pair/request/{w['request_id']}", headers={"X-Pair-Secret": "wrong"})
check("the guessing source is locked out", bad.get(f"/v1/watch/pair/request/{w['request_id']}",
      headers={"X-Pair-Secret": "wrong"}).status_code, 429)
for _ in range(hh.WATCH_FAILURES_PER_SOURCE):
    home.post("/v1/watch/pair/confirm", json={"code": "000000", "person_id": p_ava["id"]}, headers=A)
check("Alex's iPhone, after wrong codes, is told to wait",
      home.post("/v1/watch/pair/confirm", json={"code": w["code"], "person_id": p_ava["id"]}, headers=A).status_code, 429)
ok = home.post("/v1/watch/pair/confirm", json={"code": w["code"], "person_id": p_ava["id"]}, headers=R)
check("…Robin's iPhone confirms the real code", ok.status_code, 200)
got = from_ip("10.0.0.31").get(f"/v1/watch/pair/request/{w['request_id']}", headers={"X-Pair-Secret": w["secret"]})
check("…and the real Watch collects its token: nobody else was locked out", (got.status_code, bool(got.json().get("token"))),
      (200, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
