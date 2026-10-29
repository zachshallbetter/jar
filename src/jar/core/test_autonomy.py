#!/usr/bin/env python3
# gate: hermetic
"""Autonomy per operation, first slice (#1645, autonomy.py). Every held operation asks by default. A person may set
one of theirs to act and tell (it runs, then a card says what was done, never its arguments) or act quietly (it runs,
and shows only in their record, with the level it ran under). One person's level never touches another's. An earned
level can't make a permanent or destructive operation act; only the person's own word can. A level that can't be read
means ask. Turns that aren't a person's own still never act. The device-confirmed floors (mail moves, lock up) have no
level here. GET/PUT /v1/me/autonomy reads and sets the caller's own, a screen gets 403, and delete-my-data covers it.
Fictional people (Alex the admin, Robin an adult, Ava a kid, a living-room screen); temporary stores; every acting
call is a spy. Run: python3 scripts/test_autonomy.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-autonomy-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "SPACES_DB": "spaces.db", "CONNECTORS_LEDGER": "ledger.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "WORLD_DIR": "world",
             "ME_DELETE_DIR": "me-delete", "CLAUDE_CHATS_DB": "claude_chats.db", "CAPABILITY_SWITCHES": "switches.json",
             "HELD_ACTIONS_DB": "held.db", "MCP_SERVERS": "none.json"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "user"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import autonomy as AU  # noqa: E402
import capability_audit as A  # noqa: E402
import held_actions  # noqa: E402
import kb_privacy  # noqa: E402
import me_delete  # noqa: E402
import proactive  # noqa: E402

held_actions.DB = _TMP / "held.db"
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


CARDS: list[dict] = []
DELETES: list[str] = []
proactive.add_card = lambda title, detail="", **kw: CARDS.append({"title": title, "detail": detail, **kw}) or {}
lib._runtime_request = lambda svc, path, method="GET", *a, **k: DELETES.append(path) if method == "DELETE" else {}
kb_privacy.access = lambda meta, rid, person, role: "edit"
kb_privacy.source_meta = lambda req, rid: {}
kb_privacy.drop_acl = lambda rid: None


def as_person(pid, role="adult", source="user"):
    lib._TURN_IO.person = {"id": pid, "name": pid} if pid else None
    lib._TURN_IO.scheduled = None
    lib._TURN_IO.role = role
    lib._TURN_IO.source = source
    lib._TURN_IO.client = "mac"
    lib._TURN_IO.capability_tiers = None
    lib._TURN_IO.confirmed_call = None


print("── which operations ──")
ops = AU.operations("admin")
check("the admin's held operations have levels", "kb_forget" in ops, True)
check("the device-confirmed floors have none (mail moves, lock up)", [t for t in ("mail_move", "lock_up") if t in ops], [])
check("nothing that isn't held", [t for t in ("search_mail", "kb_retrieve") if t in ops], [])
check("permanent deletion acts only on the person's own word", AU.explicit_only("kb_forget"), True)

print("── ask by default ──")
as_person("p_alex", "admin")
check("nobody set anything: ask", AU.level("p_alex", "kb_forget"), "ask")
out = lib.execute_tool("kb_forget", {"record_id": "rec-001"})
check("kb_forget is held, nothing deleted", (out.startswith(lib.ALWAYS_ASK_HELD), DELETES), (True, []))

print("── act and tell ──")
AU.set_level("p_alex", "kb_forget", "act_tell")
as_person("p_alex", "admin")
n_cards = len(CARDS)
out = lib.execute_tool("kb_forget", {"record_id": "rec-002"})
check("it runs without asking", (out.startswith("Forgotten"), DELETES), (True, ["/v1/retrieval/sources/rec-002"]))
told = CARDS[n_cards:]
check("…and a card says what was done, never the record", (len(told), told[0]["title"].startswith("Done without asking"),
      "rec-002" in told[0]["title"] + told[0]["detail"]), (1, True, False))
check("…its record says it ran on its own, at that level",
      [(r["tool"], r["outcome"], r["why"]) for r in A.log("p_alex")][:1], [("kb_forget", "ok", "act_tell")])

print("── act quietly ──")
AU.set_level("p_alex", "kb_forget", "act_quiet")
as_person("p_alex", "admin")
n_cards = len(CARDS)
lib.execute_tool("kb_forget", {"record_id": "rec-003"})
check("it runs with no card at all", (DELETES[-1], len(CARDS) - n_cards), ("/v1/retrieval/sources/rec-003", 0))
check("…only its record shows it", A.log("p_alex")[0]["why"], "act_quiet")

print("── whose level ──")
as_person("p_robin", "adult")
n = len(DELETES)
out = lib.execute_tool("kb_forget", {"record_id": "rec-004"})
check("Robin's still asks (Alex's level is Alex's)", (out.startswith(lib.ALWAYS_ASK_HELD), len(DELETES) - n), (True, 0))

print("── only the person's word for what's permanent ──")
try:
    AU.set_level("p_robin", "kb_forget", "act_quiet", source="earned")
    check("an earned level can't make permanent deletion act", "set", "refused")
except ValueError:
    check("an earned level can't make permanent deletion act", "refused", "refused")
con = AU._db()
with con:
    con.execute("INSERT INTO autonomy (person, tool, level, source, updated_at) VALUES ('p_robin','kb_forget','act_quiet','earned',0)")
con.close()
check("…and one that got stored anyway is read as ask", AU.level("p_robin", "kb_forget"), "ask")
check("a level that can't be read is ask", AU.level("p_alex", "kb_forget", path=Path("/dev/null/spaces.db")), "ask")
for bad in ("yolo", ""):
    try:
        AU.set_level("p_alex", "kb_forget", bad)
        check(f"refuses level {bad!r}", "set", "refused")
    except ValueError:
        check(f"refuses level {bad!r}", "refused", "refused")

print("── turns that aren't a person's own ──")
AU.set_level("p_alex", "kb_forget", "act_quiet")
for src in ("eval", "canary", "test"):
    as_person("p_alex", "admin", source=src)
    n = len(DELETES)
    out = lib._always_ask_hold("kb_forget", {"record_id": "rec-005"})
    check(f"a {src} turn never acts, whatever the level", (bool(out), len(DELETES) - n), (True, 0))

print("── only a person's own turn acts (the review's cases) ──")
AU.set_level("p_alex", "kb_forget", "act_quiet")
as_person(None, "admin")
n = len(DELETES)
out = lib.execute_tool("kb_forget", {"record_id": "rec-cli"})
check("a turn with no person (the CLI, a master-token call) never uses the admin's level",
      (out.startswith(lib.ALWAYS_ASK_HELD), len(DELETES) - n), (True, 0))
as_person("p_alex", "admin")
lib._TURN_IO.scheduled = "sch-1"
n = len(DELETES)
out = lib.execute_tool("kb_forget", {"record_id": "rec-sch"})
check("a scheduled action coming due asks, whatever the level", (out.startswith(lib.ALWAYS_ASK_HELD), len(DELETES) - n),
      (True, 0))
lib._TURN_IO.scheduled = None
con = AU._db()
with con:
    con.execute("INSERT OR REPLACE INTO autonomy (person, tool, level, source, updated_at) VALUES ('p_ava','kb_forget','act_quiet','person',0)")
con.close()
as_person("p_ava", "kid")
check("a kid has no levels, and a stored one still asks", (AU.operations("kid"), lib._autonomy_level("kb_forget")), ([], "ask"))
check("a screen has none either", AU.operations("household"), [])
as_person("p_alex", "admin")
con = AU._db()
with con:
    con.execute("INSERT OR REPLACE INTO autonomy (person, tool, level, source, updated_at) VALUES ('p_alex','mcp_old_wipe','act_quiet','person',0)")
con.close()
check("a level for an operation that isn't there now (a server removed, another added) is ask",
      AU.level("p_alex", "mcp_old_wipe", role="admin"), "ask")
outer = []
real_hold = lib._always_ask_hold


def nested(name, args, **kw):
    r = real_hold(name, args, **kw)
    if name == "kb_forget" and not outer:
        outer.append(1)
        lib.execute_tool("get_datetime", {})   # a call inside the held one's own run
    return r


lib._always_ask_hold = nested
lib.execute_tool("kb_forget", {"record_id": "rec-nest"})
lib._always_ask_hold = real_hold
check("a call made inside an acting one doesn't wipe its record", next(r["why"] for r in A.log("p_alex") if r["tool"] == "kb_forget"),
      "act_quiet")
check("…and leaves no slot behind", lib._TURN_IO.acted_stack, [])

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
r = c.get("/v1/me/autonomy", headers=ROBIN).json()
mine = {o["tool"]: o for o in r["operations"]}
check("an adult sees their held operations, each ask", (sorted(mine) == sorted(AU.operations("adult")),
      {o["level"] for o in mine.values()} <= {"ask"}), (True, True))
check("…with words, and which act only on their word", (mine["kb_forget"]["words"], mine["kb_forget"]["explicit_only"]),
      ("forget that record for good", True))
r = c.put("/v1/me/autonomy", json={"kb_forget": "act_tell"}, headers=ROBIN)
check("Robin sets theirs", (r.status_code, {o["tool"]: o["level"] for o in r.json()["operations"]}["kb_forget"],
      AU.levels(robin)), (200, "act_tell", {"kb_forget": {"level": "act_tell", "source": "person"}}))
check("an operation they have no level for is 400, nothing written",
      (c.put("/v1/me/autonomy", json={"titan_service_restart": "act_quiet"}, headers=ROBIN).status_code,
       c.put("/v1/me/autonomy", json={"search_mail": "act_quiet"}, headers=ROBIN).status_code, AU.levels(robin)),
      (400, 400, {"kb_forget": {"level": "act_tell", "source": "person"}}))
check("an unknown level is 400", c.put("/v1/me/autonomy", json={"kb_forget": "always"}, headers=ROBIN).status_code, 400)
check("a kid reads their own", c.get("/v1/me/autonomy", headers=AVA).status_code, 200)
check("a shared screen gets 403 both ways", (c.get("/v1/me/autonomy", headers=TV).status_code,
                                             c.put("/v1/me/autonomy", json={"kb_forget": "ask"}, headers=TV).status_code),
      (403, 403))
r = c.put("/v1/me/autonomy", json={"kb_forget": "ask"}, headers=ROBIN)
check("back to ask removes the row", AU.levels(robin), {})

print("── delete-my-data ──")
stores = me_delete.Parts(mem=None, spaces_db=_TMP / "spaces.db")._stores()
check("delete-my-data covers the levels", [(p, t, col) for p, t, _, col in stores if t == "autonomy"],
      [("capabilities", "autonomy", "person")])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
