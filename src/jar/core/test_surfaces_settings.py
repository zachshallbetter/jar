#!/usr/bin/env python3
# gate: hermetic
"""Settings lists as ResolvedSurfaces (#1060): Household facts and Privacy › Always shared.

S1: a fixed fictional snapshot, viewer and `now` resolve to exactly the golden vectors in
scripts/evalsets/surfaces/ (`--write` rewrites them; review the diff). S2: the viewer matrix through the real routes
and stores (an admin, an adult, a kid, a shared screen), each with a never-see list checked against the whole JSON.
Batches: one undo token puts back exactly what the batch changed, and only its owner can use it. Fictional people
(Alex the admin, Robin an adult, Ava a kid, a living-room screen); temporary stores; no model.
Run: python3 scripts/test_surfaces_settings.py [--write]"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-surfaces-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "WORLD_DIR": "world", "SURFACES_DB": "surfaces.db",
             "ITEM_REFS_DB": "item_refs.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "SPACES_DB": "spaces.db", "PLACES_DB": "places.db", "LINKS_DB": "links.db", "LINK_SALT_FILE": "link_salt"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TZ"] = "America/Los_Angeles"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
VECTORS = HERE / "evalsets" / "surfaces"
WRITE = "--write" in sys.argv

import item_open  # noqa: E402
import surfaces as S  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def obs(entity, etype, name, attr, value, when, audience="household", ref=None):
    return W.Obs(entity=entity, etype=etype, name=name, attr=attr, value=value, observed_at=when, kind="household",
                 ref=ref or f"hh#{entity}#{attr}", origin="household.yaml", label="the household file", owner=None,
                 audience=audience, bubble="household")


SNAPSHOT = [obs("maple-animal-clinic", "org", "Maple Animal Clinic", "phone", "(555) 0142", "2019-03-01"),
            obs("maple-animal-clinic", "org", "Maple Animal Clinic", "address", "12 Elm Road", "2026-09-01"),
            obs("biscuit", "pet", "Biscuit", "vet", "Maple Animal Clinic", "2020-01-10"),
            obs("biscuit", "pet", "Biscuit", "species", "dog", "2024-05-01"),
            obs("harbor-gas", "org", "Harbor Gas", "billing", "autopay on the 3rd", "2026-08-01", audience="adults")]
NOW = 1791331200.0     # 2026-10-07T00:00:00Z


def vector(name, got):
    p = VECTORS / f"{name}.json"
    if WRITE:
        VECTORS.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    want = json.loads(p.read_text()) if p.exists() else None
    check(f"S1 golden vector {name}", got, want)


print("── S1: golden vectors (fixed snapshot, viewer, now) ──")
gw = W.Bubbles(_TMP / "golden-world", clock=lambda: NOW)
gw.sync("golden", SNAPSHOT)
vector("household_facts.admin", S.household_facts(gw, "alex", "admin", NOW))
vector("household_facts.kid", S.household_facts(gw, "ava", "kid", NOW))
# #1510: the Places group, from the real registry reader over fixed rows (ids, clocks and people fixed)
import sqlite3  # noqa: E402
import places as PL  # noqa: E402
import places_seed as PS  # noqa: E402
_pc = sqlite3.connect(_TMP / "places.db")
PL.ensure_registry(_pc)
for _r in (("pl-g1", "Willow Studio", "venue", "alex", '["alex"]', '[]', 0, "alex"),
           ("pl-g2", "Harbor Pool", "candidate", "alex", '["alex"]', '[]', 4, None),
           ("pl-g3", "Cedar School", "school", "ava", '["alex", "ava"]', '["Cedar Elementary"]', 3, "ava"),
           ("pl-g4", "Shore Cafe", "shop", "robin", "household", '[]', 0, "robin"),
           ("pl-g5", "Robin's Gym", "named", "robin", '["robin"]', '[]', 0, "robin"),
           ("pl-g6", "Old Depot", "named", "alex", '["alex"]', '[]', 0, "alex")):
    _pc.execute("INSERT INTO places (id, label, kind, owner, audience, aliases, sightings, confirmed_by, updated_at, "
                "archived_at) VALUES (?,?,?,?,?,?,?,?,0,?)", (*_r, 1.0 if _r[0] == "pl-g6" else None))
_pc.commit()
_pc.close()
_PF = [{"id": "alex", "role": "admin", "parents": []}, {"id": "robin", "role": "adult", "parents": []},
       {"id": "ava", "role": "kid", "parents": ["alex"]}]
_people0, PS.people = PS.people, lambda household=None: _PF
vector("household_facts.places.admin", S.household_facts(gw, "alex", "admin", NOW, places=PS.listing("alex", "admin")))
vector("household_facts.places.kid", S.household_facts(gw, "ava", "kid", NOW, places=PS.listing("ava", "kid")))
_pl = {nm: next(g for g in S.household_facts(gw, v, r, NOW, places=PS.listing(v, r))["groups"] if g["id"] == "g-places")
       for nm, v, r in (("alex", "alex", "admin"), ("robin", "robin", "adult"), ("ava", "ava", "kid"))}
check("Places: each viewer sees their own, the household's and (a parent) their kid's; never another's private, never archived",
      {k: sorted(m["slots"]["title"] for m in g["members"]) for k, g in _pl.items()},
      {"alex": ["Cedar School", "Harbor Pool", "Shore Cafe", "Willow Studio"],
       "robin": ["Robin's Gym", "Shore Cafe"], "ava": ["Cedar School", "Shore Cafe"]})
PS.people = _people0
G = [{"asker": "robin", "kind": "mail", "at": NOW - 120 * 86400, "used_at": None},
     {"asker": "robin", "kind": "scan", "at": NOW - 200 * 86400, "used_at": NOW - 2 * 86400},
     {"asker": "jordan", "kind": "mail", "at": NOW - 30 * 86400, "used_at": None}]
PEOPLE = {"alex": {"name": "Alex Quarry"}, "robin": {"name": "Robin Quarry"}, "jordan": {"name": "Jordan Quarry"}}
vector("always_shared.alex", S.always_shared("alex", "admin", PEOPLE, NOW, grants_of=lambda o: G if o == "alex" else []))
vector("always_shared.empty", S.always_shared("robin", "adult", PEOPLE, NOW, grants_of=lambda o: []))

print("── the rules, on the golden snapshot ──")
hf = S.household_facts(gw, "alex", "admin", NOW)
att = next(g for g in hf["groups"] if g["role"] == "attention")
check("needs a look: the stale facts, oldest first", [m["subject"]["id"] for m in att["members"]],
      ["household:maple-animal-clinic#phone", "household:biscuit#vet"])
check("then one group per entity, most recently confirmed first",
      [g["label"]["text"] for g in hf["groups"] if g["role"] == "primary"], ["Maple Animal Clinic", "Harbor Gas", "Biscuit"])
check("sizeClass counts distinct facts (the queue repeats members)", (hf["sizeClass"], len({m["subject"]["id"] for g in
      hf["groups"] for m in g["members"]})), ("few", 5))
kid = S.household_facts(gw, "ava", "kid", NOW)
check("a kid never sees the adults' billing fact (not the entity, not a count)",
      ("Harbor Gas" in json.dumps(kid), "billing" in json.dumps(kid)), (False, False))
check("a kid's confirm operations are all unavailable, with a reason",
      {(o["exposure"], (o.get("reason") or {}).get("key")) for o in kid["operations"]}, {("unavailable", "kids.propose_only")})
check("an adult's are available; confirm-all only where something is stale",
      sorted(o["id"] for o in hf["operations"] if o["contract"] == "jellybean.fact.confirm_all@1"),
      ["op-confirm-all-household:biscuit", "op-confirm-all-household:maple-animal-clinic"])
big = W.Bubbles(_TMP / "big-world", clock=lambda: NOW)
big.sync("big", [obs(f"shop-{i:03d}", "org", f"Shop {i:03d}", "phone", f"(555) {1000 + i}", "2018-01-01" if i % 3 == 0
                     else "2026-09-01") for i in range(120)])
hb = S.household_facts(big, "alex", "admin", NOW)
check("hundreds: sizeClass, and the needs-a-look queue is capped and first",
      (hb["sizeClass"], hb["groups"][0]["role"], len(hb["groups"][0]["members"])), ("hundreds", "attention", 5))
check("…q narrows by name", [g["label"]["text"] for g in S.household_facts(big, "alex", "admin", NOW, q="Shop 007")["groups"]
                             if g["role"] == "primary"], ["Shop 007"])


def refused(fn):
    try:
        fn()
        return "served"
    except S.Refused as exc:
        return exc.status


aw = W.Bubbles(_TMP / "assistant-world", clock=lambda: NOW)
aw.sync("golden", SNAPSHOT + [obs("titan", "assistant", "Titan", "capability", "timers: yes", "2026-10-06")])
check("titan's own capabilities aren't a household fact",
      [g["label"]["text"] for g in S.household_facts(aw, "alex", "admin", NOW)["groups"]] ==
      [g["label"]["text"] for g in hf["groups"]], True)
check("a shared screen gets none", refused(lambda: S.household_facts(gw, None, "household", NOW)), 403)
al = S.always_shared("alex", "admin", PEOPLE, NOW, grants_of=lambda o: G if o == "alex" else [])
check("always-shared: needs a look is the unused grants (never used and given 90+ days ago), least recently first",
      [m["slots"]["subtitle"] + ":" + m["slots"]["title"] for m in al["groups"][0]["members"]], ["Robin:mail"])
check("…a grant used 2 days ago, or given 30 days ago, isn't in it",
      sorted(m["slots"]["subtitle"] + ":" + m["slots"]["title"] for g in al["groups"][1:] for m in g["members"]
             if (m["slots"].get("status") or {}).get("text") != "grant.unused"), ["Jordan:mail", "Robin:scan"])
check("one group per asker, the most recently used first; first names only",
      [g["label"]["text"] for g in al["groups"][1:]], ["Robin", "Jordan"])
check("…no surname anywhere", "Quarry" in json.dumps(al), False)
check("stop-all per asker carries one undo", sorted((o["id"], tuple(o.get("recovery") or ())) for o in al["operations"]
                                                     if o["contract"] == "jellybean.grant.stop_all@1"),
      [("op-stop-jordan", ("undo",)), ("op-stop-robin", ("undo",))])
check("a kid's own (empty) page is served; a screen's isn't",
      (S.always_shared("ava", "kid", PEOPLE, NOW, grants_of=lambda o: [])["sizeClass"],
       refused(lambda: S.always_shared(None, "household", PEOPLE, NOW))), ("few", 403))
f1 = S.always_shared("alex", "admin", PEOPLE, NOW, grants_of=lambda o: G)["fingerprint"]
f2 = S.always_shared("alex", "admin", PEOPLE, NOW, grants_of=lambda o: G if o == "alex" else G[:1])["fingerprint"]
check("someone else's grants changing never changes Alex's fingerprint", f1 == f2, True)

print("── batches: one undo token, exactly what the batch did ──")
item_open.restore("alex", [dict(g) for g in G])
item_open.restore("robin", [{"asker": "alex", "kind": "mail", "at": NOW - 10 * 86400, "used_at": None}])
before = item_open.grants_of("alex")
r = S.stop_all("alex", "robin")
check("stop all for Robin: both of Robin's grants, one token", (r["stopped"], bool(r["token"])), (2, True))
check("…Jordan's stays, and Robin's own grant to Alex is untouched",
      ([g["asker"] for g in item_open.grants_of("alex")], [g["asker"] for g in item_open.grants_of("robin")]),
      (["jordan"], ["alex"]))
check("someone else can't use the token", refused(lambda: S.undo("robin", r["token"])), 404)
check("undo puts back exactly the same rows (dates too)", (S.undo("alex", r["token"])["restored"],
      sorted(map(str, item_open.grants_of("alex"))) == sorted(map(str, before))), (2, True))
check("…once", refused(lambda: S.undo("alex", r["token"])), 404)
check("stopping nothing gives no token", S.stop_all("alex", "nobody"), {"stopped": 0, "token": None})
item_open.touch("alex", "jordan", "mail")
check("a grant that lets a read through records its use", bool(next(g for g in item_open.grants_of("alex")
                                                                  if g["asker"] == "jordan")["used_at"]), True)

lw = W.Bubbles(_TMP / "live-world")
lw.sync("household-file", SNAPSHOT)
clinic = "household:maple-animal-clinic"
lw.state(W.Obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone", value="(555) 0142",
               observed_at="2019-04-01", kind="user", ref="", origin="user:alex", label="Alex", owner=None,
               audience="household", by="alex", bubble="household"), by="alex")
snap0 = lw.user_statements(clinic, "phone", "alex")
state0 = W.answer_state(next(f for f in lw.facts(clinic, "alex", "admin") if f.attr == "phone"))
c1 = S.confirm_all(lw, "alex", "admin", clinic)
state1 = W.answer_state(next(f for f in lw.facts(clinic, "alex", "admin") if f.attr == "phone"))
check("confirm all: the stale phone is restated as Alex's, no longer stale", (state0, c1["confirmed"], state1),
      ("stale", 1, "known"))
check("…a kid can't, nor on another bubble's entity", (refused(lambda: S.confirm_all(lw, "ava", "kid", clinic)),
      refused(lambda: S.confirm_all(lw, "alex", "admin", "alex:something"))), (403, 404))
S.undo("alex", c1["token"], w=lw)
check("undo: Alex's earlier statement is back exactly, and the fact is stale again",
      (lw.user_statements(clinic, "phone", "alex") == snap0,
       W.answer_state(next(f for f in lw.facts(clinic, "alex", "admin") if f.attr == "phone"))), (True, "stale"))
check("…and the household file's own evidence was never touched",
      len([e for f in lw.facts(clinic, "alex", "admin") if f.attr == "phone" for v in f.values for e in v.evidence]) >= 1,
      True)

print("── S2: the viewer matrix through the real routes ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

W._WORLD = None
W.get().sync("household-file", SNAPSHOT)
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


ROBIN, robin = pair("Robin Quarry", "adult")
AVA, ava = pair("Ava Quarry", "kid")
TV, _ = pair("Living room", "household", "tvos")
admin = hh.ADMIN_PERSON_ID
item_open.restore(admin, [{"asker": robin, "kind": "mail", "at": time.time() - 100 * 86400, "used_at": None}])
item_open.restore(robin, [{"asker": admin, "kind": "scan", "at": time.time() - 5 * 86400, "used_at": None}])
NEVER = {"admin": [f"gr-{'x'}"], "robin": [], "ava": ["Harbor Gas", "billing"]}
for nm, h in (("admin", M), ("robin", ROBIN), ("ava", AVA)):
    r1 = c.get("/v1/surfaces/household-facts", headers=h)
    r2 = c.get("/v1/surfaces/always-shared", headers=h)
    check(f"{nm}: both surfaces answer as jb-1", (r1.status_code, r2.status_code, r1.json().get("profileVersion"),
                                                  r2.json().get("profileVersion")), (200, 200, "jb-1", "jb-1"))
    leaks = [x for x in NEVER[nm] if x in r1.text + r2.text]
    check(f"{nm}: never-see list", leaks, [])
mine = lambda h: sorted((m["slots"]["subtitle"], m["slots"]["title"]) for g in c.get(  # noqa: E731
    "/v1/surfaces/always-shared", headers=h).json()["groups"] if g["role"] == "primary" for m in g["members"])
check("the admin sees only the grants the admin gave (to Robin), never Robin's to the admin",
      mine(M), [("Robin", "mail")])
check("Robin sees only Robin's own (to the admin)", mine(ROBIN), [(hh.ADMIN_PERSON_NAME, "scan")])
check("Ava has none, and no count of anyone's", (mine(AVA), c.get("/v1/surfaces/always-shared", headers=AVA).json()["sizeClass"]),
      ([], "few"))
check("the screen gets 403 on every surface route",
      [c.get(p_, headers=TV).status_code for p_ in ("/v1/surfaces/household-facts", "/v1/surfaces/always-shared")] +
      [c.post(p_, json=b_, headers=TV).status_code for p_, b_ in (("/v1/surfaces/household-facts/confirm", {"entity": clinic}),
                                                                ("/v1/sharing/grants/stop", {"asker": robin}),
                                                                ("/v1/surfaces/undo", {"token": "x"}))], [403] * 5)
check("a kid can't confirm household facts (the route table)",
      c.post("/v1/surfaces/household-facts/confirm", json={"entity": clinic}, headers=AVA).status_code, 403)
rc = c.post("/v1/surfaces/household-facts/confirm", json={"entity": clinic}, headers=ROBIN)
check("an adult confirms a whole entity, with a token", (rc.status_code, rc.json().get("confirmed"), bool(rc.json().get("token"))),
      (200, 1, True))
check("…the admin can't undo Robin's batch", c.post("/v1/surfaces/undo", json={"token": rc.json()["token"]}, headers=M).status_code,
      404)
check("…Robin can", c.post("/v1/surfaces/undo", json={"token": rc.json()["token"]}, headers=ROBIN).status_code, 200)
rs = c.post("/v1/sharing/grants/stop", json={"asker": robin}, headers=M)
check("stop all for Robin over the route, then undo", (rs.json().get("stopped"), c.post(
    "/v1/surfaces/undo", json={"token": rs.json()["token"]}, headers=M).json().get("restored"), mine(M)), (1, 1, [("Robin", "mail")]))
check("bad bodies → 400", [c.post("/v1/sharing/grants/stop", json={}, headers=M).status_code,
                           c.post("/v1/surfaces/household-facts/confirm", json={}, headers=ROBIN).status_code], [400, 400])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
