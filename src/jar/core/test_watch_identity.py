#!/usr/bin/env python3
# gate: hermetic   (throwaway stores and identity key; the real routes through a TestClient)
"""titan's identity for a kid's Watch (10-07 review W1; the contract is server_identity.py's docstring): the pairing
answer carries titan's public key with the token, POST /v1/watch/hello signs the Watch's nonce, and only titan's key
verifies it, so the Watch can refuse to send its token to anything else at the same address. Fictional people.
Run: python3 scripts/test_watch_identity.py
"""
from __future__ import annotations

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-watch-id-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "PUSH_DB": "push.db", "CONNECTORS_LEDGER": "ledger.db", "ROUTINES_DB": "routines.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "TODOS_DB": "todos.db", "PLACES_DB": "places.db",
             "SPACES_DB": "spaces.db", "PRESENCE_DB": "presence.db", "CONTINUITY_DB": "continuity.db",
             "SERVER_IDENTITY_KEY": "id/server-identity.key"}.items():
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
import server_identity as sid  # noqa: E402

MASTER = "master-watch-id-0123456789"
api._AGENT_TOKEN = MASTER
store = hh.HouseholdStore(_TMP / "routes.db")
hh.reset_store(store)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def paired(name, role):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    r = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": f"{name}'s iPhone", "platform": "ios"}).json()
    return s["person"], {"X-Agent-Token": r["token"]}


p_alex, A = paired("Alex", "adult")
p_ava, _ = paired("Ava", "kid")
store.update_person(p_ava["id"], parents=[p_alex["id"]])

print("── pairing pins titan's identity ──")
w = c.post("/v1/watch/pair/request", json={"device_name": "Ava's Watch"}).json()
c.post("/v1/watch/pair/confirm", json={"code": w["code"], "person_id": p_ava["id"]}, headers=A)
got = c.get(f"/v1/watch/pair/request/{w['request_id']}", headers={"X-Pair-Secret": w["secret"]}).json()
ident = got.get("server_identity") or {}
check("the token comes with titan's identity: ed25519, a 32-byte key, its sha256", (
      ident.get("alg"), len(base64.b64decode(ident.get("key", ""))), len(ident.get("fp", ""))), ("ed25519", 32, 64))
check("the key file is titan's own, 0600", oct(sid.key_path().stat().st_mode & 0o777), "0o600")
check("…and stays the same key (made once)", sid.identity()["key"], ident.get("key"))

print("── hello: only titan's key verifies ──")
nonce = secrets.token_bytes(32)
h = c.post("/v1/watch/hello", json={"nonce": base64.b64encode(nonce).decode()})
check("hello needs no token and answers a signature", (h.status_code, sorted(h.json())), (200, ["alg", "fp", "key", "sig"]))
pinned = ident["key"]
check("the Watch's check: the signature verifies against the key it pinned", sid.verify(pinned, nonce, h.json()["sig"]), True)
check("…not for another nonce (no replay of an old answer)", sid.verify(pinned, secrets.token_bytes(32), h.json()["sig"]), False)
from cryptography.hazmat.primitives import serialization as ser  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
impostor = Ed25519PrivateKey.generate()
forged = base64.b64encode(impostor.sign(sid.DOMAIN + nonce)).decode()
check("an impostor's signature, with its own key in the answer, fails against the pinned key",
      sid.verify(pinned, nonce, forged), False)
check("…a signature over the bare nonce (another protocol's) doesn't verify: the domain is bound",
      sid.verify(pinned, nonce, base64.b64encode(sid._private().sign(nonce)).decode()), False)
bad = [c.post("/v1/watch/hello", json=b).status_code for b in ({}, {"nonce": "!!"}, {"nonce": base64.b64encode(b"x").decode()})]
check("a missing, non-base64 or too-short nonce: 400", bad, [400, 400, 400])
check("…and bad nonces never lock anyone out (no failure counted)", c.post("/v1/watch/hello", json={
      "nonce": base64.b64encode(secrets.token_bytes(16)).decode()}).status_code, 200)
check("the hello route is public (listed), the pairing route still is", ("/v1/watch/hello" in api._PUBLIC_PATHS,
      "/v1/watch/pair/request" in api._PUBLIC_PATHS), (True, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
