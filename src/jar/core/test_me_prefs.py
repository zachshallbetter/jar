#!/usr/bin/env python3
# gate: hermetic
"""A person's look across devices (#1159): GET/PUT /v1/me/prefs. Each person's own (a kid's too), a shared screen gets
403, only the catalogue's values, partial updates keep the rest, and delete-my-data stashes, drops and restores it.
Fictional people (Robin an adult, Ava a kid, a living-room screen); temporary stores; no model.
Run: python3 scripts/test_me_prefs.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-me-prefs-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "SPACES_DB": "spaces.db", "CONNECTORS_LEDGER": "ledger.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "WORLD_DIR": "world",
             "ME_DELETE_DIR": "me-delete", "CLAUDE_CHATS_DB": "claude_chats.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import me_prefs as P  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.200} expected={expected!r:.200}"))


print("── the store ──")
check("nothing set: look and accent null, follow true", P.get("robin"), {"look": None, "accent": None, "follow": True,
                                                                         "updated_at": None})
r = P.put("robin", {"look": "Paper", "accent": "Slate"})
check("set look and accent (names in any case)", (r["look"], r["accent"], r["follow"], bool(r["updated_at"])),
      ("paper", "slate", True, True))
r2 = P.put("robin", {"follow": False})
check("a partial update keeps the rest", (r2["look"], r2["accent"], r2["follow"]), ("paper", "slate", False))
check("a custom accent is #RRGGBB (stored upper-case)", P.put("robin", {"accent": "#a1b2c3"})["accent"], "#A1B2C3")
check("…and every look in the catalogue is accepted", all(P.put("robin", {"look": lk})["look"] == lk for lk in P.LOOKS), True)
for label, body in (("an unknown look", {"look": "neon"}), ("an unknown accent", {"accent": "magenta"}),
                    ("a short hex", {"accent": "#abc"}), ("follow as a string", {"follow": "yes"}),
                    ("an unknown field", {"look": "cool", "font": "x"}), ("nothing", {}), ("not an object", ["cool"])):
    try:
        P.put("robin", body)
        check(f"{label} → refused", "kept", "Invalid")
    except P.Invalid:
        check(f"{label} → refused", "Invalid", "Invalid")
check("another person's are their own", P.get("ava")["look"], None)

print("── the routes ──")
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
r = c.put("/v1/me/prefs", json={"look": "cool", "accent": "grove"}, headers=ROBIN)
check("Robin sets theirs", (r.status_code, r.json()["look"], r.json()["accent"]), (200, "cool", "grove"))
check("…and reads it back", c.get("/v1/me/prefs", headers=ROBIN).json()["look"], "cool")
check("Ava (a kid) has her own, untouched by Robin's", c.get("/v1/me/prefs", headers=AVA).json()["look"], None)
check("Ava sets hers", c.put("/v1/me/prefs", json={"look": "evening"}, headers=AVA).json()["look"], "evening")
check("…Robin's is unchanged", c.get("/v1/me/prefs", headers=ROBIN).json()["look"], "cool")
check("a shared screen gets 403 both ways", (c.get("/v1/me/prefs", headers=TV).status_code,
                                             c.put("/v1/me/prefs", json={"look": "cool"}, headers=TV).status_code), (403, 403))
check("a bad value → 400 with the reason", (lambda r_: (r_.status_code, "look is one of" in r_.json()["error"]))(
    c.put("/v1/me/prefs", json={"look": "neon"}, headers=ROBIN)), (400, True))

print("── delete-my-data covers it ──")
import me_delete  # noqa: E402
parts = me_delete.Parts(None, spaces_db=_TMP / "spaces.db", titles_db=_TMP / "titles.db",
                        claude_db=_TMP / "claude_chats.db")
check("theme_prefs is one of the stores", ("theme", "theme_prefs") in [(p_, t) for p_, t, _o, _c in parts._stores()], True)
tomb = me_delete._tomb(robin, create=True)
counts = parts.stash_stores(robin, tomb)
check("stash: Robin's one row", counts.get("theme.theme_prefs"), 1)
parts.drop_stores(robin)
check("drop: gone; Ava's stays", (P.get(robin)["look"], P.get(ava)["look"]), (None, "evening"))
parts.restore_stores(tomb)
check("cancel restores it exactly", (P.get(robin)["look"], P.get(robin)["accent"]), ("cool", "grove"))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
