#!/usr/bin/env python3
"""Cards to people and devices (proactive.py, agent_api_server's /v1/proactive, /v1/cards,
/v1/cards/wait, household_identity.card_send_refusal, notify_user's recipients).

Hermetic: temp stores, no model, nothing leaves the process. Run: python3 scripts/test_cards.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-cards-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha-token")
os.environ.pop("TITAN_DISCOVERY", None)
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("TITAN_ALERT_NTFY_URL", None)
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")
    return good


DB = _TMP / "cards-unit.db"
print("── audiences ──")
check("default is the admins (every card before audiences)", proactive.audience(None), "admins")
check("named groups", [proactive.audience(x) for x in ("household", "Adults", "admins")], ["household", "adults", "admins"])
check("people, sorted and lowercased", proactive.audience(["Robin", "zach"]), '["robin", "zach"]')
check("a comma list", proactive.audience("robin, zach"), '["robin", "zach"]')
check("a JSON list", proactive.audience('["kid1"]'), '["kid1"]')
vis = [(a, p, r) for a in ("admins", "adults", "household", '["robin"]')
       for p, r in (("zach", "admin"), ("robin", "adult"), ("kid1", "kid"), ("household", "household"))
       if proactive.visible_to(a, p, r)]
check("who sees what", vis, [("admins", "zach", "admin"),
                             ("adults", "zach", "admin"), ("adults", "robin", "adult"),
                             ("household", "zach", "admin"), ("household", "robin", "adult"),
                             ("household", "kid1", "kid"), ("household", "household", "household"),
                             ('["robin"]', "robin", "adult")])

print("\n── add, list, update in place, dismiss per person, expire ──")
a = proactive.add_card("Build failed", "jellybean-build exit 1", db=DB)
b = proactive.add_card("Dinner at 6", "", to="household", db=DB, sender="Robin")
m = proactive.add_card("Plumber at 3", "Rivera Plumbing", to=["robin"], db=DB, sender="Zach")
check("a system card goes to the admin only", [c["id"] for c in proactive.list_cards(db=DB, person="robin", role="adult")],
      [b["id"], m["id"]])
check("the admin sees his and the household's", [c["id"] for c in proactive.list_cards(db=DB, person="zach", role="admin")],
      [a["id"], b["id"]])
check("a kid sees only the household card", [c["title"] for c in proactive.list_cards(db=DB, person="kid1", role="kid")],
      ["Dinner at 6"])
check("an internal caller sees them all", len(proactive.list_cards(db=DB)), 3)
check("cards carry to/from/key", {k: b[k] for k in ("to", "from", "key")}, {"to": "household", "from": "Robin", "key": None})
check("dismissing is per person", proactive.ack_card(b["id"], db=DB, person="zach"), True)
check("…gone for Zach", [c["title"] for c in proactive.list_cards(db=DB, person="zach", role="admin")], ["Build failed"])
check("…still there for the kid", [c["title"] for c in proactive.list_cards(db=DB, person="kid1", role="kid")], ["Dinner at 6"])
check("include_acked shows it with when", proactive.list_cards(db=DB, person="zach", role="admin", include_acked=True)[1]["acked_at"] is not None, True)

w1 = proactive.add_card("Washer: 40 min left", "", kind="update", to="household", key="washer", db=DB)
proactive.ack_card(w1["id"], db=DB, person="kid1")
t_before = time.time()
time.sleep(0.01)
w2 = proactive.add_card("Washer: done", "Move it to the dryer", kind="update", to="household", key="washer", db=DB, urgency=2)
check("the same key updates the same card", (w2["id"] == w1["id"], w2.get("updated")), (True, True))
kid = [c for c in proactive.list_cards(db=DB, person="kid1", role="kid") if c["id"] == w1["id"]]
check("…with the new content, shown again to someone who'd dismissed it", [(c["title"], c["urgency"]) for c in kid],
      [("Washer: done", 2)])
check("…and it counts as new for a device polling since before the update",
      [c["title"] for c in proactive.list_cards(since=t_before, db=DB, person="zach", role="admin")], ["Washer: done"])
other = proactive.add_card("Washer: done", "", to="adults", key="washer", db=DB)
check("the same key for other people is another card", other["id"] != w1["id"], True)
gone = proactive.add_card("Door open", "", to="household", expires_in=0.05, db=DB)
time.sleep(0.1)
check("an expired card is gone", gone["id"] in [c["id"] for c in proactive.list_cards(db=DB, include_acked=True)], False)
check("last_change moves with updates", proactive.last_change(db=DB) >= w2["updated_at"], True)
dc = proactive.add_card("Who's getting Leo?", "", to="adults", key="coord", db=DB,
                        data={"need": "driver", "events": [{"title": "Pickup Leo", "start": "2026-10-01T14:50"}]})
check("data round-trips", [c["data"] for c in proactive.list_cards(db=DB) if c["id"] == dc["id"]],
      [{"need": "driver", "events": [{"title": "Pickup Leo", "start": "2026-10-01T14:50"}]}])
check("data over 4 KB or not an object is dropped",
      (proactive.add_card("x", db=DB, data={"b": "y" * 5000})["data"], proactive.add_card("y", db=DB, data=[1])["data"]), (None, None))
check("an update replaces data", proactive.add_card("Who's getting Leo?", "", to="adults", key="coord", db=DB, data={"need": "overlap"})["data"],
      {"need": "overlap"})

print("\n── a card made before audiences existed ──")
con = sqlite3.connect(str(_TMP / "legacy.db"))
con.execute("CREATE TABLE cards (id TEXT PRIMARY KEY, source TEXT, kind TEXT, title TEXT, detail TEXT, urgency INTEGER, "
            "action_prompt TEXT, created_at REAL, acked_at REAL, actions TEXT)")
con.execute("INSERT INTO cards VALUES ('pc-old','titan','info','Old card','',1,NULL,?,NULL,'[]')", (time.time() - 60,))
con.commit(); con.close()
check("…is the admin's, as before", ([c["title"] for c in proactive.list_cards(db=_TMP / "legacy.db", person="zach", role="admin")],
                                     proactive.list_cards(db=_TMP / "legacy.db", person="robin", role="adult")),
      (["Old card"], []))

print("\n── who may send to whom ──")


class People:
    def people(self):
        return [{"id": "zach", "name": "Zach", "role": "admin"}, {"id": "robin", "name": "Robin", "role": "adult"},
                {"id": "leo", "name": "Leo", "role": "kid"}, {"id": "hale", "name": "Hale", "role": "kid"}]


P = People()
check("adults and the admin send anywhere", [hh.card_send_refusal(r, "x", "household", P) for r in ("admin", "adult")], [None, None])
check("a kid may send to the grown-ups and themself",
      [hh.card_send_refusal("kid", "leo", a, P) for a in ("adults", '["robin"]', '["leo"]', '["leo", "zach"]')],
      [None, None, None, None])
check("…not to a sibling or everyone", [bool(hh.card_send_refusal("kid", "leo", a, P)) for a in ('["hale"]', "household")],
      [True, True])
check("a household device may", hh.card_send_refusal("household", "household", "household", P), None)

print("\n── notify_user's recipients ──")
store = hh.HouseholdStore(_TMP / "people.db")
hh.reset_store(store)
store.add_person("Robin", "adult")
store.add_person("Leo", "kid")
ids = {p["name"]: p["id"] for p in store.people()}
lib._TURN_IO.role, lib._TURN_IO.person = "admin", {"id": "zach", "name": "Zach"}
check("no `to` → the person asking", lib._card_recipients(None), (["zach"], "you", None))
check("a name → that person", lib._card_recipients("Robin"), ([ids["Robin"]], "Robin", None))
check("two names", lib._card_recipients("Robin and Leo")[:2], ([ids["Robin"], ids["Leo"]], "Robin and Leo"))
check("everyone", lib._card_recipients("household")[:2], ("household", "everyone in the household"))
r = lib._card_recipients("Jordan")
check("an unknown name is refused with the list", (r[0], r[2].startswith("Not sent: no one in the household called Jordan"),
                                                 "Robin" in r[2]), (None, True, True))
lib._TURN_IO.role, lib._TURN_IO.person = "kid", {"id": ids["Leo"], "name": "Leo"}
check("a kid can't send to everyone", lib._card_recipients("household")[2], "Not sent: A card to the whole household comes from a grown-up.")
check("a kid can send to Robin", lib._card_recipients("Robin")[2], None)
os.environ["AGENT_TURN_SOURCE"] = "user"
r = lib.execute_tool("notify_user", {"title": "Can I have a snack?", "to": "Robin"})
check("the tool sends it, and says Robin has no device yet", (r.startswith("Card sent to Robin: Can I have a snack?"),
      "no paired Jellybean yet" in r, "send_message" in r), (True, True, True))
got = [c for c in proactive.list_cards(person=ids["Robin"], role="adult") if c["title"] == "Can I have a snack?"]
check("…and Robin has it, from Leo", [(c["from"], c["to"]) for c in got], [("Leo", json.dumps([ids["Robin"]]))])
check("…as a note (#1286: the phone can tell it from titan's own cards)", [c["source"] for c in got], ["note"])
lib.execute_tool("notify_user", {"title": "Ask about the bus", "to": "me"})
mine_c = [c for c in proactive.list_cards(person=ids["Leo"], role="kid") if c["title"] == "Ask about the bus"]
check("a card to the asker themself stays `agent`", [c["source"] for c in mine_c], ["agent"])
lib._TURN_IO.role, lib._TURN_IO.person = "admin", {"id": "zach", "name": "Zach"}
lib.execute_tool("notify_user", {"title": "Dinner at 6", "to": "household"})
lib.execute_tool("notify_user", {"title": "Plumber at 3", "to": "Robin and Leo"})
made = [c for c in proactive.list_cards() if c["title"] in ("Dinner at 6", "Plumber at 3")]
check("a note to everyone, or to Robin and Leo: `note`", sorted(c["source"] for c in made), ["note", "note"])
for c_ in made + mine_c:   # out of the way of the API checks below
    proactive.ack_card(c_["id"])
check("_notify_source: no named person (a background turn) is `agent` whoever it's to",
      [lib._notify_source(to, None) for to in ([ids["Robin"]], "household", "adults", ["zach"])], ["agent"] * 4)
check("…with one: `note` for others, `agent` for the asker alone",
      [lib._notify_source(to, "Zach") for to in ([ids["Robin"]], "household", "adults", ["zach"], ["zach", ids["Robin"]])],
      ["note", "note", "note", "agent", "note"])
lib._TURN_IO.role = lib._TURN_IO.person = None

print("\n── mutes keep their scope: agent cards and notes are separate sources ──")
MDB = _TMP / "mute-scope.db"
a1 = proactive.add_card("Filter due", kind="suggestion", source="agent", to=["p_alex"], db=MDB)
n1 = proactive.add_card("Plumber at 3", kind="suggestion", source="note", to=["p_alex"], sender="Robin", db=MDB)
proactive.mute(a1["id"], person="p_alex", role="adult", db=MDB)
a2 = proactive.add_card("Change the water filter", kind="suggestion", source="agent", to=["p_alex"], db=MDB)
n2 = proactive.add_card("Pick up Ava", kind="suggestion", source="note", to=["p_alex"], sender="Robin", db=MDB)
left = {c["id"] for c in proactive.list_cards(db=MDB, person="p_alex", role="adult")}
check("a mute of titan's agent cards hides the next agent card, never a note", (a2["id"] in left, n1["id"] in left, n2["id"] in left),
      (False, True, True))
proactive.mute(n2["id"], person="p_alex", role="adult", db=MDB)
a3 = proactive.add_card("Rotate the mattress", kind="suggestion", source="agent", to=["p_alex"], db=MDB)
n3 = proactive.add_card("Call me back", kind="suggestion", source="note", to=["p_alex"], sender="Robin", db=MDB)
left = {c["id"] for c in proactive.list_cards(db=MDB, person="p_alex", role="adult")}
check("…and a mute of notes hides notes, not titan's other agent cards (beyond the earlier mute)",
      (n3["id"] in left, a3["id"] in left), (False, False))
check("…the two mutes are two scopes", sorted((m["source"], m["kind"]) for m in proactive.mutes("p_alex", db=MDB)),
      [("agent", "suggestion"), ("note", "suggestion")])
proactive.unmute("p_alex", next(m["id"] for m in proactive.mutes("p_alex", db=MDB) if m["source"] == "agent"), db=MDB)
a4 = proactive.add_card("Descale the kettle", kind="suggestion", source="agent", to=["p_alex"], db=MDB)
left = {c["id"] for c in proactive.list_cards(db=MDB, person="p_alex", role="adult")}
check("…unmuting agent cards brings them back; notes stay muted", (a4["id"] in left, n3["id"] in left), (True, False))

print("\n── over the API ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
store = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(store)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    t = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": f"{name}'s iPhone", "platform": "ios"}).json()
    return {"X-Agent-Token": t["token"]}, s["person"]["id"]


MIR, mir_id = pair("Robin", "adult")
KID, kid_id = pair("Leo", "kid")
r = c.post("/v1/cards", json={"to": "Robin", "title": "Plumber at 3", "detail": "Rivera, ask for Luis"}, headers=M)
check("POST /v1/cards takes a name", (r.status_code, r.json()["card"]["to"]), (200, json.dumps([mir_id])))
check("…and refuses a name that's nobody", c.post("/v1/cards", json={"to": "Jordan", "title": "x"}, headers=M).status_code, 400)
r = c.post("/v1/cards", json={"to": [mir_id], "title": "Pick up milk"}, headers=M).json()["card"]
check("the admin sends Robin a card", (r["to"], r["from"]), (json.dumps([mir_id]), "Zach"))
check("the master token (an automation, a script) with no source: still `titan`, never a note \"From Zach\"", r["source"], "titan")
wd = c.post("/v1/cards", json={"to": [mir_id], "title": "Washer: done", "source": "home"}, headers=M).json()["card"]
check("a source given is kept", wd["source"], "home")
proactive.ack_card(wd["id"])
mine = c.get("/v1/proactive", headers=MIR).json()
check("Robin's device gets it, and a `now` to poll from", ([x["title"] for x in mine["cards"]] [-1], "now" in mine),
      ("Pick up milk", True))
check("the kid's device doesn't", [x["title"] for x in c.get("/v1/proactive", headers=KID).json()["cards"]], [])
check("a kid may read cards now (their own)", c.get("/v1/proactive", headers=KID).status_code, 200)
check("a kid may not send to everyone", c.post("/v1/cards", json={"to": "household", "title": "hi"}, headers=KID).status_code, 403)
check("a kid may send to the adults", c.post("/v1/cards", json={"to": "adults", "title": "Home from school"}, headers=KID).status_code, 200)
check("no title → 400", c.post("/v1/cards", json={"to": "adults"}, headers=M).status_code, 400)
card_id = r["id"]
rl = c.post("/v1/cards", json={"to": "adults", "title": "Running late"}, headers=MIR).json()["card"]
check("a member's own card with no source is still `person` (a note source on the phone already)", rl["source"], "person")
proactive.ack_card(rl["id"])
zs = c.post("/v1/pair/start", json={"person_id": hh.ADMIN_PERSON_ID}, headers=M).json()
ZPH = {"X-Agent-Token": c.post("/v1/pair/claim", json={"code": zs["code"], "device_name": "Zach's iPhone",
                                                       "platform": "ios"}).json()["token"]}
zn = c.post("/v1/cards", json={"to": [mir_id], "title": "Plumber at 3"}, headers=ZPH).json()["card"]
check("the admin from a paired device with no source: a note (#1286)", (zn["source"], zn["from"]), ("note", "Zach"))
proactive.ack_card(zn["id"])
check("…and with a source given, it's kept", c.post("/v1/cards", json={"to": [mir_id], "title": "Washer: done", "source": "home"},
                                                   headers=ZPH).json()["card"]["source"], "home")
proactive.ack_card(next(x["id"] for x in proactive.list_cards() if x["title"] == "Washer: done"))
check("the kid can't dismiss Robin's card", c.post(f"/v1/proactive/{card_id}/ack", headers=KID).json(), {"acked": False})
check("Robin dismisses it", c.post(f"/v1/proactive/{card_id}/ack", headers=MIR).json(), {"acked": True})
check("…and it's gone for her", card_id in [x["id"] for x in c.get("/v1/proactive", headers=MIR).json()["cards"]], False)

since = time.time()
t0 = time.monotonic()
r = c.get(f"/v1/cards/wait?since={since}&timeout=1", headers=MIR).json()
check("long-poll with nothing new returns empty after the timeout", (r["cards"], time.monotonic() - t0 >= 0.9), ([], True))
proactive.add_card("Dryer done", "", to="household", key="dryer")
r = c.get(f"/v1/cards/wait?since={since}&timeout=5", headers=MIR).json()
check("long-poll returns the new card right away", [x["title"] for x in r["cards"]], ["Dryer done"])
r2 = c.get(f"/v1/cards/wait?since={r['now']}&timeout=1", headers=MIR).json()
check("…and polling from its `now` doesn't repeat it", r2["cards"], [])
proactive.add_card("Dryer: folded", "", to="household", key="dryer")
r3 = c.get(f"/v1/cards/wait?since={r['now']}&timeout=5", headers=KID).json()
check("an update wakes the long-poll too", [x["title"] for x in r3["cards"]], ["Dryer: folded"])

import cross_device as _xdc
_xdc.SHUTTING_DOWN.set()
r = c.get(f"/v1/cards/wait?since={time.time()}&timeout=5", headers=MIR)
check("a restart ends the card long-poll with a clean 503", (r.status_code, r.headers.get("Retry-After"), r.json().get("retry")), (503, "5", True))
_xdc.SHUTTING_DOWN.clear()

print("── titles are clipped at a word (notifications audit) ──")
long_t = "Move “Quarterly planning with the Riverside Water & Power team” to Thursday October 15 at 10:00 AM, " \
         "then the follow-up review — and the retro"
got = proactive.clip_title(long_t)
check("a long title: at most 120 characters, cut at a space, no dangling comma or dash",
      (len(got) <= 120, long_t.startswith(got), got[-1] not in ",;:-–—… ", long_t[len(got)] == " "), (True, True, True, True))
check("…ending on the last whole word", got, "Move “Quarterly planning with the Riverside Water & Power team” to Thursday "
                                              "October 15 at 10:00 AM, then the follow-up")
check("a short title is only stripped", proactive.clip_title("  Washer: done  "), "Washer: done")
check("one long word: a hard cut, never empty", proactive.clip_title("x" * 200), "x" * 120)
check("a cut that lands after a dash drops it", proactive.clip_title("a" * 60 + " " + "b" * 50 + " — " + "c" * 30), "a" * 60 + " " + "b" * 50)
c = proactive.add_card(long_t, to="admins", key="clip-test", expires_in=60)
check("add_card stores the clipped title", c["title"], got)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
