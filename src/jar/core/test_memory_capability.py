#!/usr/bin/env python3
# gate: hermetic
"""Memory is a per-person capability (#1690; governance "memory", on by default per #1627).

What it checks, over the real routes and the real turn path (_remember_turn) with a fake extractor model:
  listing   admin, adults and kids have a memory setting in /v1/me/capabilities; a shared screen and the engineering
            agent don't (the memory routes' role rule); a kid may turn their own off
  off       Robin turns memory off: Robin's turns store no facts and no dev notes (the model isn't even asked), POST
            /v1/memory/facts and a PATCH rewrite are 403 and change nothing, a needs-a-look choice that writes is
            refused; Alex and Jordan are unaffected. Robin's existing facts and dev notes still read (list, recall,
            dev_notes), a pin still sets, forget and restore still work, delete-my-data still takes them. Back on:
            stored again
  kill      "memory" in the switches file's "off" stops everyone's writes and shows as switched off; reads still work;
            cleared, writes resume. Everything off kills it too
People: Alex (the admin), Robin and Jordan (adults), Ava (a kid), a living-room screen. Temporary stores; the clock is
pinned and ticks one second per read. Run it under TZ=UTC and TZ=America/Los_Angeles.
Run: python3 scripts/test_memory_capability.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time as _real_time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-memcap-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "SPACES_DB": "spaces.db", "CONNECTORS_LEDGER": "ledger.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "WORLD_DIR": "world",
             "ME_DELETE_DIR": "me-delete", "CLAUDE_CHATS_DB": "claude_chats.db", "CAPABILITY_SWITCHES": "switches.json",
             "HELD_ACTIONS_DB": "held.db", "MCP_SERVERS": "none.json", "CHAT_TITLES_DB": "titles.db",
             "MEMORY_EXTRACT_LOG": "memory-extract.jsonl", "EXPORT_DIR": "exports", "LINKS_DB": "links.db",
             "LINK_SALT_FILE": "link_salt", "CONTINUITY_DB": "continuity.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["MEMORY_GROUP_FACTS"] = "0"
os.environ["MEMORY_L1_EVERY_N"] = "1"      # every turn extracts, so one turn is enough to prove what's stored
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL", "AGENT_WORLD_MODEL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_memory as am  # noqa: E402
import governance as G  # noqa: E402
import household_identity as hh  # noqa: E402
import local_time  # noqa: E402


class _Clock:
    """A pinned clock that ticks one second per read."""
    def __init__(self, start: float):
        self.t = start

    def time(self) -> float:
        self.t += 1.0
        return self.t

    def __getattr__(self, name):
        return getattr(_real_time, name)


NOW = 1_792_000_000.0                       # 2026-10-15, fixed
am.time = _Clock(NOW)
local_time.now_line = lambda at=None: "Now: Thursday, October 15, 2026, 9:00 AM."

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


def switch(**kw):
    p = Path(os.environ["CAPABILITY_SWITCHES"])
    p.write_text(json.dumps(kw))
    t = NOW + len(json.dumps(kw)) % 13 + switch.n
    switch.n += 1
    os.utime(p, (t, t))


switch.n = 1

from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402

api._lib.world_on = lambda: False
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
mem = am.AgentMemory()
api._get_memory = lambda: mem
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, platform="ios"):
    s_ = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    tok = c.post("/v1/pair/claim", json={"code": s_["code"], "device_name": f"{name}'s device",
                                         "platform": platform}).json()["token"]
    return {"X-Agent-Token": tok}, next(p["id"] for p in st.people() if p["name"] == name)


RH, ROBIN = pair("Robin Hale", "adult")
JH, JORDAN = pair("Jordan Hale", "adult")
AH, AVA = pair("Ava Hale", "kid")
TV, _ = pair("Living room", "household", "tvos")
ALEX = hh.ADMIN_PERSON_ID
ROLE = {ALEX: "admin", ROBIN: "adult", JORDAN: "adult", AVA: "kid"}

CALLS: list[str] = []
SAID = ["I pay for downtown parking with ParkMobile.", "I wish the assistant could read the school portal for me."]
MODEL = ["The user pays for downtown parking with ParkMobile.",
         "The user wants the assistant to be able to read the school portal."]
mem._llm_call = lambda prompt, max_tokens=512: (CALLS.append(prompt), "\n".join(MODEL))[1]


def turn(person):
    """One chat turn through the real path: the message kept, then (every turn here) extraction for its person."""
    ident = hh.Identity(person={"id": person, "name": person, "role": ROLE[person]}, device=None, role=ROLE[person],
                        via="device")
    for i, said in enumerate(SAID):
        tid = f"t-{person}-{len(CALLS)}-{i}-{am.time.time()}"
        api._turns[tid] = {"remember": True}
        api._remember_turn(tid, ident, said, "Got it.", extract=True)
        api._turns.pop(tid, None)


def facts(person):
    return sorted(mem.get_facts(limit=1000, person_id=person))


def notes(person):
    return [n["text"] for n in mem.dev_notes(person_id=person)]


def wipe(person):
    with mem._lock:
        for t in ("facts", "dev_notes"):
            mem._db.execute(f"DELETE FROM {t} WHERE person_id=?", (person,))
        mem._db.commit()
        mem._bm25 = None


def caps(h):
    r = c.get("/v1/me/capabilities", headers=h)
    return {x["id"]: (x["tier"], x["switched_off"]) for x in r.json()["capabilities"]} if r.status_code == 200 else r.status_code


PARK = "You pay for downtown parking with ParkMobile."
WISH = "The user wants the assistant to be able to read the school portal."

print("── who has a memory setting ──")
check("the capability: the assistant's domain, no env flag (the switches file is its kill switch), moved",
      (G.CAPABILITIES["memory"].domain, G.CAPABILITIES["memory"].flag, G.CAPABILITIES["memory"].moved), ("assistant", None, True))
check("for_role: admin, adult and kid have it; a shared screen and the engineering agent don't",
      ["memory" in G.for_role(r) for r in ("admin", "adult", "kid", "household", hh.ENGINEERING)],
      [True, True, True, False, False])
check("…the same rule as the memory routes", ["memory" in G.for_role(r) for r in ("adult", "kid", "household")],
      [hh.route_allowed(r, "GET", "/v1/memory/facts") for r in ("adult", "kid", "household")])
check("listed, on, for the admin, an adult and a kid", [caps(h).get("memory") for h in (M, RH, AH)],
      [("always", False)] * 3)
check("a shared screen: 403, and it can't set one", (caps(TV), c.put("/v1/me/capabilities", json={"memory": "never"},
                                                                      headers=TV).status_code), (403, 403))
check("other capabilities' listing unchanged for a kid (no lock up)", "lock_up" in caps(AH), False)
r = c.put("/v1/me/capabilities", json={"memory": "never"}, headers=AH)
check("a kid turns their own memory off", (r.status_code, G.tiers(AVA), G.allowed(AVA, "memory").run),
      (200, {"memory": "never"}, False))
c.put("/v1/me/capabilities", json={"memory": "always"}, headers=AH)
check("…and back on", (G.tiers(AVA), G.allowed(AVA, "memory").run), ({}, True))

print("── on by default: everyone's turns are remembered ──")
for p in (ALEX, ROBIN, JORDAN):
    turn(p)
check("Alex, Robin and Jordan: the fact and the dev note, each their own",
      [(facts(p), notes(p)) for p in (ALEX, ROBIN, JORDAN)], [([PARK], [WISH])] * 3)

print("── Robin turns memory off ──")
mem.add_fact("c", "You keep the spare key under the blue pot.", person_id=ROBIN)
spare = mem._db.execute("SELECT id FROM facts WHERE person_id=? AND text LIKE 'You keep the spare key%'", (ROBIN,)).fetchone()[0]
before_r = (facts(ROBIN), notes(ROBIN))
r = c.put("/v1/me/capabilities", json={"memory": "never"}, headers=RH)
check("PUT memory never: Robin's own tier", (r.status_code, G.tiers(ROBIN), caps(RH)["memory"]),
      (200, {"memory": "never"}, ("never", False)))
check("…Jordan's and Alex's still on", (caps(JH)["memory"], caps(M)["memory"]), (("always", False), ("always", False)))
wipe(ALEX)
wipe(JORDAN)
SAID = ["I take the 7:40 bus on weekdays.", "I wish titan could text me when the bus is late."]
MODEL = ["The user takes the 7:40 bus on weekdays.", "The user wants titan to text them when the bus is late."]
BUS, BUS_WISH = "You take the 7:40 bus on weekdays.", "The user wants titan to text them when the bus is late."
n0 = len(CALLS)
turn(ROBIN)
check("Robin's turns store nothing new: no fact, no dev note", (facts(ROBIN), notes(ROBIN)), before_r)
check("…and the extractor model isn't even asked", len(CALLS) - n0, 0)
check("…the chat itself is still kept (Past conversations), only memory is off",
      any("7:40" in str(m["content"]) for m in mem.get_messages(hh.conv_id_for(ROBIN), limit=50)), True)
turn(ALEX)
turn(JORDAN)
check("Alex and Jordan unaffected", [(facts(p), notes(p)) for p in (ALEX, JORDAN)], [([BUS], [BUS_WISH])] * 2)
r = c.post("/v1/memory/facts", json={"text": "You like window seats."}, headers=RH)
check("POST /v1/memory/facts: 403, says why, nothing stored",
      (r.status_code, "turned off" in r.text, "You like window seats." in facts(ROBIN)), (403, True, False))
check("…Jordan's POST still stores", (c.post("/v1/memory/facts", json={"text": "You like aisle seats."}, headers=JH).status_code,
                                      "You like aisle seats." in facts(JORDAN)), (201, True))
r = c.patch(f"/v1/memory/facts/{spare}", json={"text": "You keep the spare key with Leo's teacher."}, headers=RH)
check("PATCH rewrite: 403, the fact unchanged", (r.status_code, "You keep the spare key under the blue pot." in facts(ROBIN)),
      (403, True))
check("…a pin is still Robin's to set", c.patch(f"/v1/memory/facts/{spare}", json={"pinned": True}, headers=RH).json(),
      {"id": spare, "pinned": True})
check("reads still work: the list, recall and dev notes",
      ("You keep the spare key under the blue pot." in [f["text"] for f in c.get("/v1/memory/facts?detail=1", headers=RH)
                                                       .json()["facts"]],
       "spare key" in c.post("/v1/memory/recall", json={"query": "where is the spare key"}, headers=RH).json()["prependContext"],
       [n["text"] for n in c.get("/v1/memory/dev_notes", headers=RH).json()["notes"]]),
      (True, True, [WISH]))
import memory_review  # noqa: E402
memory_review._ensure(mem._db)
park_id = mem._db.execute("SELECT id FROM facts WHERE person_id=? AND text=?", (ROBIN, PARK)).fetchone()[0]
mem._db.execute("INSERT INTO memory_review (id, person, kind, a, created_at) VALUES ('rv-wish', ?, 'wish', ?, ?)",
                (ROBIN, park_id, NOW))
mem._db.execute("INSERT INTO memory_review (id, person, kind, a, created_at) VALUES ('rv-one', ?, 'one_off', ?, ?)",
                (ROBIN, park_id, NOW))
mem._db.commit()
r = c.post("/v1/memory/review/rv-wish", json={"choice": "to_dev_note"}, headers=RH)
check("needs a look: a choice that writes (a wish to dev notes) is refused, nothing moved",
      (r.status_code, notes(ROBIN), PARK in facts(ROBIN)), (403, [WISH], True))
check("…keeping resolves", c.post("/v1/memory/review/rv-wish", json={"choice": "keep"}, headers=RH).status_code, 200)
r = c.post("/v1/memory/review/rv-one", json={"choice": "forget"}, headers=RH)
check("…forgetting still works", (r.status_code, PARK in facts(ROBIN)), (200, False))
check("forget and restore one fact still work",
      (c.delete(f"/v1/memory/facts/{spare}", headers=RH).status_code, "You keep the spare key under the blue pot." in facts(ROBIN),
       c.post(f"/v1/memory/facts/{spare}/restore", headers=RH).status_code,
       "You keep the spare key under the blue pot." in facts(ROBIN)), (200, False, 200, True))
import me_delete  # noqa: E402
parts = me_delete.Parts(mem)
tomb = me_delete._tomb(ROBIN, create=True)
got = parts.stash_memory(ROBIN, hh.conv_id_for(ROBIN), tomb)
parts.drop_memory(ROBIN, hh.conv_id_for(ROBIN), tomb)
tomb.commit()
check("delete-my-data still takes Robin's facts and dev notes", (got.get("dev_notes"), facts(ROBIN), notes(ROBIN)),
      (1, [], []))
parts.restore_memory(tomb)
check("…and Cancel brings them back", ("You keep the spare key under the blue pot." in facts(ROBIN), notes(ROBIN)), (True, [WISH]))

c.put("/v1/me/capabilities", json={"memory": "always"}, headers=RH)
turn(ROBIN)
check("Robin turns it back on: the next turn is remembered", (BUS in facts(ROBIN), BUS_WISH in notes(ROBIN)), (True, True))

print("── the house's kill switch ──")
for p in (ALEX, ROBIN, JORDAN):
    wipe(p)
SAID = ["Our recycling goes out on Tuesdays.", "I wish the assistant could remind me about recycling."]
MODEL = ["The user's recycling goes out on Tuesdays.", "The user wants the assistant to remind them about recycling."]
switch(off=["memory"])
check("memory in the switches file's off: killed, shown switched off to everyone",
      (G.killed("memory"), [caps(h)["memory"] for h in (M, RH, JH, AH)]), (True, [("always", True)] * 4))
n0 = len(CALLS)
for p in (ALEX, ROBIN, JORDAN):
    turn(p)
check("nobody's turns store anything; the model isn't asked",
      ([(facts(p), notes(p)) for p in (ALEX, ROBIN, JORDAN)], len(CALLS) - n0), ([([], [])] * 3, 0))
r = c.post("/v1/memory/facts", json={"text": "You like window seats."}, headers=M)
check("…the admin's POST too: 403, says it's switched off on titan", (r.status_code, "switched off on titan" in r.text,
                                                                      facts(ALEX)), (403, True, []))
mem.add_fact("c", "You water the plants on Sundays.", person_id=JORDAN)
check("…what's there still reads", [f["text"] for f in c.get("/v1/memory/facts?detail=1", headers=JH).json()["facts"]],
      ["You water the plants on Sundays."])
switch(off=[])
for p in (ALEX, JORDAN):
    turn(p)
check("cleared: writes resume", [("You" in " ".join(facts(p)) and len(notes(p)) == 1) for p in (ALEX, JORDAN)], [True, True])
switch(all=True)
check("everything off (only talking works) kills memory too", G.killed("memory"), True)
switch(off=["assistant"])
check("the assistant's domain off kills it", G.killed("memory"), True)
switch(off=[])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
