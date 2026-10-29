#!/usr/bin/env python3
# gate: hermetic
"""Per-person capabilities (#1627, Zach 2026-10-15: "This is a per user setting"; governance.CAPABILITIES): each one is
on for every person until that person turns it off; one person's choice never touches another's; a domain's Never
covers its capabilities; the house can still kill one (the switches file by id or domain, or its old env flag set to
exactly "0"); decide() applies a tool's capability; GET/PUT /v1/me/capabilities lists and sets only the caller's own,
and only the ones their role has a setting for; a shared screen gets 403. No behaviour change yet: the old flags still
gate their code paths. Fictional people (Alex admin, Robin and Jordan adults, Ava and Leo kids, a living-room screen);
temporary stores; no model. Run: python3 scripts/test_person_capabilities.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-capabilities-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "SPACES_DB": "spaces.db", "CONNECTORS_LEDGER": "ledger.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "WORLD_DIR": "world",
             "ME_DELETE_DIR": "me-delete", "CLAUDE_CHATS_DB": "claude_chats.db",
             "CAPABILITY_SWITCHES": "switches.json"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import governance as G  # noqa: E402
import tool_descriptors as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def switch(**kw):
    p = Path(os.environ["CAPABILITY_SWITCHES"])
    p.write_text(json.dumps(kw))
    t = time.time() + len(json.dumps(kw)) % 13 + 1
    os.utime(p, (t, t))


print("── the registry ──")
check("every capability's domain is a domain", all(c.domain in T.DOMAINS for c in G.CAPABILITIES.values()), True)
check("every capability's tools are titan tools", all(t in T.DESCRIPTORS for c in G.CAPABILITIES.values() for t in c.tools), True)
check("no tool in two capabilities", len(G._TOOL_CAPABILITY), sum(len(c.tools) for c in G.CAPABILITIES.values()))
check("ids never collide with a domain", set(G.CAPABILITIES) & set(T.DOMAINS), set())
check("each old flag belongs to one capability",
      len([c.flag for c in G.CAPABILITIES.values() if c.flag]), len({c.flag for c in G.CAPABILITIES.values() if c.flag}))
check("remote exec isn't a capability here", any(c.flag == "TITAN_REMOTE_EXEC" for c in G.CAPABILITIES.values()), False)

print("── on until each person turns it off ──")
PEOPLE = ("p_alex", "p_robin", "p_jordan", "p_ava", "p_leo")
MOVED = [c for c, v in G.CAPABILITIES.items() if v.moved]
check("nobody has turned anything off: every moved capability on for everyone",
      {(p, c): G.allowed(p, c).run for p in PEOPLE for c in MOVED} == {(p, c): True for p in PEOPLE for c in MOVED}, True)
check("…one whose code still needs its old flag reads as off until the flag is on (the truth, not a dead switch)",
      [G.killed(c) for c in G.CAPABILITIES if not G.CAPABILITIES[c].moved],
      [True] * sum(not v.moved for v in G.CAPABILITIES.values()))
os.environ["TASK_CHAIN"] = "1"
check("…and on once it is", G.killed("task_chain"), False)
os.environ.pop("TASK_CHAIN")
check("the scans' model switches aren't capabilities yet (no live scan source)",
      [c for c in G.CAPABILITIES if c.startswith("scan")], [])
G.set_tier("p_robin", "mail_moves", "never")
check("Robin's off is Robin's only", [G.allowed(p, "mail_moves").run for p in PEOPLE], [True, False, True, True, True])
r = G.allowed("p_robin", "mail_moves")
check("…and says what to do", (r.why, "turn it back on in Settings" in r.message), ("person_never", True))
check("…other capabilities stay on for Robin", G.allowed("p_robin", "contacts").run, True)
G.set_tier("p_jordan", "records", "never")
check("Jordan's Never for records covers its capabilities",
      sorted(c for c in MOVED if not G.allowed("p_jordan", c).run),
      sorted(c for c in MOVED if G.CAPABILITIES[c].domain == "records"))
G.set_tier("p_robin", "mail_moves", "always")
check("turning it back on removes the row", G.tiers("p_robin"), {})
check("a system job (no person): only the house's switches", G.allowed(None, "inbox_proposals").run, True)
check("a person's settings that can't be read: closed, never their Off ignored",
      G.allowed("p_robin", "contacts", path=Path("/dev/null/spaces.db")).why, "unreadable")

print("── the house can still kill one ──")
switch(off=["code_sandbox"])
check("by id in the switches file", [G.allowed(p, "code_sandbox").why for p in ("p_alex", None)], ["capability_off"] * 2)
switch(off=["mail"])
check("by its domain", (G.killed("mail_moves"), G.killed("mail_push"), G.killed("contacts")), (True, True, False))
switch(all=True)
check("everything off kills every capability", all(G.killed(c) for c in G.CAPABILITIES), True)
os.remove(os.environ["CAPABILITY_SWITCHES"])
for v, expect in (("0", True), (" 0 ", True), ("", False), ("1", False), ("false", False)):
    os.environ["MAIL_SERVER_WRITES"] = v
    check(f"a moved one's old flag {v!r}: {'killed' if expect else 'not killed'}", G.killed("mail_moves"), expect)
os.environ.pop("MAIL_SERVER_WRITES", None)

print("── decide applies a tool's capability ──")
check("mail_move refused when the person turned mail moves off, search_mail not",
      [G.decide(t, person_tiers={"mail_moves": "never"}).run for t in ("mail_move", "mail_move_undo", "search_mail")],
      [False, False, True])
os.environ["ALLOW_CODE_EXECUTION"] = "0"
check("run_python refused when the house killed the sandbox", G.decide("run_python").why, "capability_off")
os.environ.pop("ALLOW_CODE_EXECUTION")
check("nothing off: moved ones run (mail moves, the sandbox); lock up waits for its flag until its code moves",
      [G.decide(t).run for t in ("mail_move", "run_python", "lock_up")], [True, True, False])

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
JORDAN, jordan = pair("Jordan Hale", "adult")
AVA, ava = pair("Ava Hale", "kid")
TV, _ = pair("Living room", "household", "tvos")
caps = c.get("/v1/me/capabilities", headers=ROBIN).json()["capabilities"]
check("an adult sees every capability, all on",
      (sorted(x["id"] for x in caps), {x["tier"] for x in caps}), (sorted(G.for_role("adult")), {"always"}))
check("…and that is all of them", sorted(G.for_role("adult")), sorted(G.CAPABILITIES))
kid = [x["id"] for x in c.get("/v1/me/capabilities", headers=AVA).json()["capabilities"]]
check("a kid sees only what their role has a setting for (no lock up)", ("lock_up" in kid, sorted(kid) == sorted(G.for_role("kid"))),
      (False, True))
r = c.put("/v1/me/capabilities", json={"mail_moves": "never"}, headers=ROBIN)
check("Robin turns mail moves off",
      (r.status_code, next(x["tier"] for x in r.json()["capabilities"] if x["id"] == "mail_moves"), G.tiers(robin)),
      (200, "never", {"mail_moves": "never"}))
check("Jordan's still on", next(x["tier"] for x in c.get("/v1/me/capabilities", headers=JORDAN).json()["capabilities"]
                                if x["id"] == "mail_moves"), "always")
check("a kid can't set one their role has no setting for (nothing written)",
      (c.put("/v1/me/capabilities", json={"lock_up": "never"}, headers=AVA).status_code, G.tiers(ava)), (400, {}))
check("an unknown name is 400", c.put("/v1/me/capabilities", json={"teleport": "never"}, headers=ROBIN).status_code, 400)
check("a domain still works", c.put("/v1/me/capabilities", json={"web": "never"}, headers=JORDAN).status_code, 200)
check("a shared screen gets 403 both ways",
      (c.get("/v1/me/capabilities", headers=TV).status_code,
       c.put("/v1/me/capabilities", json={"mail_moves": "never"}, headers=TV).status_code), (403, 403))
shown = {x["id"]: x["switched_off"] for x in c.get("/v1/me/capabilities", headers=ROBIN).json()["capabilities"]}
check("Settings shows what's really off: not-yet-moved ones switched off, moved ones on",
      (shown["task_chain"], shown["mail_push"], shown["mail_moves"]), (True, True, False))
os.environ["CONTACTS_CONNECTOR"] = "0"
check("a killed moved one shows switched off",
      next(x["switched_off"] for x in c.get("/v1/me/capabilities", headers=ROBIN).json()["capabilities"] if x["id"] == "contacts"),
      True)
os.environ.pop("CONTACTS_CONNECTOR")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
