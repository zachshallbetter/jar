#!/usr/bin/env python3
# gate: hermetic
"""Per-source modes and per-device delivery for cards (titan-node#1039, 10-06), all per person:
  - GET/PUT /v1/proactive/sources: notify | quiet | stop for each source a person has had cards from, with their own
    counts this week; stop is a source-wide mute; quiet keeps the deck and drops every interruption; safety sources
    can be quiet, never stopped; a shared screen can't stop;
  - GET/PUT /v1/proactive/delivery: all | important | off for each of a person's own devices, household | off for a
    household TV (itself only);
  - every card carries `alert` for the asking device, the long-poll still delivers (alert false) so decks stay whole,
    and push.targets follows the same rule;
  - quiet hours on the same endpoint: a person's own window (weekdays / weekend, a window belongs to the day it starts
    on, Friday and Saturday nights are the weekend), a device override or {"off": true}; inside it only important
    cards alert (none, if except_important is false), safety sources always do; a TV has only its own.
One person's settings never change another's. Fictional people; throwaway stores; no push leaves.
Run: python3 scripts/test_card_delivery.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-carddelivery-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "SPACES_DB": "spaces.db", "LOCK_UP_DB": "lock_up.db", "TV_UNLOCK_DB": "tv_unlock.db",
             "SITTER_DB": "sitter.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import titan_alert  # noqa: E402
titan_alert.alert = lambda *a, **k: None

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive as P  # noqa: E402
import push  # noqa: E402

push.push_card_async = lambda card: None
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios", person_id=None):
    body = {"person_id": person_id} if person_id else {"new_person": {"name": name, "role": role}}
    s = c.post("/v1/pair/start", json=body, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ALEX, alex = pair("Alex Slate", "adult", "Alex's iPhone")
ALEX_MAC, _ = pair(None, None, "Alex's Mac", platform="mac", person_id=alex)
ROBIN, robin = pair("Robin Slate", "adult", "Robin's iPhone")
AVA, ava = pair("Ava Slate", "kid", "Ava's iPhone")
TV, _tv = pair("Living room", "household", "Living room TV", platform="tvos")


def devs(h):
    return {d["name"]: d for d in c.get("/v1/proactive/delivery", headers=h).json()["devices"]}


def card(h, cid):
    return next((x for x in c.get("/v1/proactive", headers=h).json()["cards"] if x["id"] == cid), None)


A_PHONE, A_MAC = devs(ALEX)["Alex's iPhone"]["id"], devs(ALEX)["Alex's Mac"]["id"]

print("── sources: per person, with this week's own counts ──")
p1 = P.add_card("A café nearby", kind="suggestion", source="places", to="adults")
P.add_card("Another café", kind="suggestion", source="places", to="adults")
lk = P.add_card("Lock up?", source="lock-up", to="adults", urgency=2)
srcs = {s["source"]: s for s in c.get("/v1/proactive/sources", headers=ALEX).json()["sources"]}
check("Alex's sources: places and lock-up, each notify", {k: v["mode"] for k, v in srcs.items()}, {"places": "notify", "lock-up": "notify"})
check("…with this week's shown counts, busiest first", ([s for s in srcs], srcs["places"]["this_week"]["shown"]), (["places", "lock-up"], 2))
check("…a safety source can't be stopped", (srcs["lock-up"]["can_stop"], srcs["places"]["can_stop"]), (False, True))
check("a kid never sees the adults' sources", c.get("/v1/proactive/sources", headers=AVA).json()["sources"], [])

print("\n── quiet: in the deck, never interrupting, for Alex only ──")
r = c.put("/v1/proactive/sources", json={"source": "places", "mode": "quiet"}, headers=ALEX)
check("Alex sets places to quiet", (r.status_code, r.json()), (200, {"source": "places", "mode": "quiet"}))
ac = card(ALEX, p1["id"])
check("…the card is still in Alex's deck, alert false", (ac is not None, ac and ac["alert"]), (True, False))
check("…Robin's is untouched: alert true", card(ROBIN, p1["id"])["alert"], True)
devices = push._household_devices()
push._tokens = lambda db=None: [{"device_id": d["id"], "live": False} for d in devices]
check("…no push to Alex's devices, Robin's still gets one", sorted(t["device_id"] for t in push.targets(p1, devices)),
      sorted(d["id"] for d in devices if d["person_id"] == robin))
check("a safety source may be quiet", c.put("/v1/proactive/sources", json={"source": "lock-up", "mode": "quiet"},
                                            headers=ALEX).status_code, 200)
check("…but never stopped: 403 cannot_stop", (lambda r_: (r_.status_code, r_.json()))(
    c.put("/v1/proactive/sources", json={"source": "lock-up", "mode": "stop"}, headers=ALEX)), (403, {"error": "cannot_stop"}))
check("…and the lock-up card stays in the deck", card(ALEX, lk["id"]) is not None, True)

print("\n── stop: the source-wide mute ──")
r = c.put("/v1/proactive/sources", json={"source": "places", "mode": "stop"}, headers=ALEX)
check("Alex stops places", r.json(), {"source": "places", "mode": "stop"})
check("…its cards are gone from Alex's deck, not Robin's", (card(ALEX, p1["id"]), card(ROBIN, p1["id"]) is not None), (None, True))
new = P.add_card("A park nearby", kind="info", source="places", to="adults")
check("…a new places card of another kind: gone for Alex too", card(ALEX, new["id"]), None)
mutes = c.get("/v1/proactive/mutes", headers=ALEX).json()["mutes"]
check("…listed with the mutes as source-wide (kind *)", [(m["source"], m["kind"]) for m in mutes], [("places", "*")])
c.delete(f"/v1/proactive/mutes?id={mutes[0]['id']}", headers=ALEX)
check("deleting that mute brings places back, mode notify", (card(ALEX, new["id"]) is not None,
      {s["source"]: s["mode"] for s in c.get("/v1/proactive/sources", headers=ALEX).json()["sources"]}.get("places")), (True, "notify"))
c.put("/v1/proactive/sources", json={"source": "places", "mode": "quiet"}, headers=ALEX)
c.put("/v1/proactive/sources", json={"source": "places", "mode": "notify"}, headers=ALEX)
check("notify clears quiet", card(ALEX, new["id"])["alert"], True)
for bad in ({"source": "places", "mode": "loud"}, {"source": "", "mode": "quiet"}, {}):
    check(f"bad body {bad}: 400", c.put("/v1/proactive/sources", json=bad, headers=ALEX).status_code, 400)
check("a household TV can't stop anything", c.put("/v1/proactive/sources", json={"source": "routine", "mode": "stop"},
                                                  headers=TV).status_code, 403)

print("\n── delivery: per device, own devices only ──")
d = devs(ALEX)
check("Alex sees his iPhone and Mac, all by default", sorted((k, v["level"], tuple(v["levels"])) for k, v in d.items()),
      [("Alex's Mac", "all", ("all", "important", "off")), ("Alex's iPhone", "all", ("all", "important", "off"))])
check("…never Robin's devices", any("Robin" in k for k in d), False)
rphone = devs(ROBIN)["Robin's iPhone"]["id"]
check("Alex can't set Robin's iPhone: 404", c.put("/v1/proactive/delivery", json={"device_id": rphone, "level": "off"},
                                                   headers=ALEX).status_code, 404)
check("…nor a household level: 400", c.put("/v1/proactive/delivery", json={"device_id": A_PHONE, "level": "household"},
                                            headers=ALEX).status_code, 400)
c.put("/v1/proactive/delivery", json={"device_id": A_PHONE, "level": "important"}, headers=ALEX)
calm = P.add_card("Bins tonight", source="routine", to="adults", urgency=1)
loud = P.add_card("Water leak in the garage", source="routine", to="adults", urgency=2)
phone_cards = {x["id"]: x["alert"] for x in c.get("/v1/proactive", headers=ALEX).json()["cards"]}
mac_cards = {x["id"]: x["alert"] for x in c.get("/v1/proactive", headers=ALEX_MAC).json()["cards"]}
check("iPhone at important: the calm card doesn't alert, the urgent one does", (phone_cards[calm["id"]], phone_cards[loud["id"]]),
      (False, True))
check("…the Mac (all) alerts for both", (mac_cards[calm["id"]], mac_cards[loud["id"]]), (True, True))
check("…an approval card counts as important", P.alert_for({"source": "approve-cap", "urgency": 1}, alex, "adult", A_PHONE), True)
check("…and push skips the iPhone for the calm card, keeps the Mac and Robin", sorted(t["device_id"] for t in push.targets(calm, devices)),
      sorted([A_MAC] + [x["id"] for x in devices if x["person_id"] == robin]))
c.put("/v1/proactive/delivery", json={"device_id": A_PHONE, "level": "off"}, headers=ALEX)
w = c.get("/v1/cards/wait?since=0&timeout=1", headers=ALEX).json()["cards"]
check("off: the long-poll still brings the cards (the deck stays whole), none alerting",
      (len(w) > 0, any(x["alert"] for x in w)), (True, False))
check("…Robin's iPhone is unchanged (all)", devs(ROBIN)["Robin's iPhone"]["level"], "all")

print("\n── a household TV: household | off, itself only ──")
t = devs(TV)
check("the TV sees only itself, household by default", [(v["level"], tuple(v["levels"])) for v in t.values()],
      [("household", ("household", "off"))])
hc = P.add_card("Movie night at 7", source="routine", to="household")
check("…a household card alerts on it", card(TV, hc["id"])["alert"], True)
tid = next(iter(t.values()))["id"]
check("…a person's level is refused: 400", c.put("/v1/proactive/delivery", json={"device_id": tid, "level": "all"},
                                                 headers=TV).status_code, 400)
c.put("/v1/proactive/delivery", json={"device_id": tid, "level": "off"}, headers=TV)
check("…off: listed, not alerting", card(TV, hc["id"])["alert"], False)
check("…and the TV can't set Alex's devices", c.put("/v1/proactive/delivery", json={"device_id": A_MAC, "level": "off"},
                                                     headers=TV).status_code, 404)

print("\n── quiet hours: the window clock ──")
from datetime import datetime, timedelta  # noqa: E402
import local_time  # noqa: E402
Z = local_time.zone()
Q = {"weekdays": {"start": "22:00", "end": "07:00"}, "weekend": {"start": "23:00", "end": "08:00"}, "except_important": True}


def at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, day, h, m, tzinfo=Z).timestamp()


for label, day, hhmm, want in (("Tue 22:30, the weekday window", 6, "22:30", True), ("Tue 21:59, before it", 6, "21:59", False),
                               ("Wed 06:59, Tuesday's window still", 7, "06:59", True), ("Wed 07:00, over", 7, "07:00", False),
                               ("Fri 22:30: Friday night is the weekend (23:00)", 9, "22:30", False),
                               ("Fri 23:00", 9, "23:00", True), ("Sat 07:30, Friday's weekend window", 10, "07:30", True),
                               ("Sun 22:30: Sunday night is a weekday", 11, "22:30", True),
                               ("Mon 07:30: Sunday's window ended at 07:00", 12, "07:30", False)):
    check(f"in_quiet {label}", P.in_quiet(Q, at(day, hhmm)), want)
check("a null weekend: no quiet on Saturday morning", P.in_quiet({**Q, "weekend": None}, at(10, "07:30")), False)
check("a same-day window: 13:00–15:00 holds 14:00, not 15:00",
      [P.in_quiet({"weekdays": {"start": "13:00", "end": "15:00"}, "weekend": None}, at(6, t)) for t in ("14:00", "15:00")],
      [True, False])
check("off / none: never quiet", (P.in_quiet(None, at(6, "23:00")), P.in_quiet({"off": True}, at(6, "23:00"))), (False, False))


def window(lo, hi):
    """A window lo..hi minutes from now, on every day (so the test doesn't depend on today's weekday)."""
    n = datetime.now(Z)
    f = lambda k: (n + timedelta(minutes=k)).strftime("%H:%M")  # noqa: E731
    return {"weekdays": {"start": f(lo), "end": f(hi)}, "weekend": {"start": f(lo), "end": f(hi)}}


NOW_Q, LATER_Q = window(-60, 60), window(120, 180)

print("\n── quiet hours: per person, through the API ──")
g = c.get("/v1/proactive/delivery", headers=ROBIN).json()
check("off by default: quiet null, each device null (follows)", (g["quiet"], [d_["quiet"] for d_ in g["devices"]]), (None, [None]))
r = c.put("/v1/proactive/delivery", json={"quiet": NOW_Q}, headers=ROBIN)
check("Robin sets quiet hours: the full body back, except_important filled in",
      (r.status_code, r.json()["quiet"], len(r.json()["devices"])), (200, {**NOW_Q, "except_important": True}, 1))
rc = {x["id"]: x["alert"] for x in c.get("/v1/proactive", headers=ROBIN).json()["cards"]}
check("inside it: the calm card doesn't alert, the urgent one does, lock-up does", (rc[calm["id"]], rc[loud["id"]], rc[lk["id"]]),
      (False, True, True))
check("…the deck still lists the calm card", calm["id"] in rc, True)
check("…Alex's Mac is untouched (alerts for calm)", {x["id"]: x["alert"] for x in c.get("/v1/proactive", headers=ALEX_MAC).json()["cards"]}[calm["id"]], True)
check("…push skips Robin's iPhone for the calm card", rphone in {t_["device_id"] for t_ in push.targets(calm, devices)}, False)
c.put("/v1/proactive/delivery", json={"quiet": {**NOW_Q, "except_important": False}}, headers=ROBIN)
rc = {x["id"]: x["alert"] for x in c.get("/v1/proactive", headers=ROBIN).json()["cards"]}
check("except_important false: the urgent card is quiet too, lock-up (safety) still alerts", (rc[loud["id"]], rc[lk["id"]]), (False, True))
c.put("/v1/proactive/delivery", json={"quiet": LATER_Q}, headers=ROBIN)
check("outside the window: the calm card alerts", card(ROBIN, calm["id"])["alert"], True)

print("\n── quiet hours: a device's override ──")
c.put("/v1/proactive/delivery", json={"quiet": NOW_Q}, headers=ROBIN)
r = c.put("/v1/proactive/delivery", json={"device_id": rphone, "quiet": {"off": True}}, headers=ROBIN)
check("the iPhone set never-quiet: alerts in Robin's hours, PUT echoes id + level",
      (card(ROBIN, calm["id"])["alert"], r.json()["id"], r.json()["level"], r.json()["devices"][0]["quiet"]),
      (True, rphone, "all", {"off": True}))
c.put("/v1/proactive/delivery", json={"device_id": rphone, "quiet": LATER_Q}, headers=ROBIN)
check("an override window replaces the person's (outside it: alerts)", card(ROBIN, calm["id"])["alert"], True)
c.put("/v1/proactive/delivery", json={"device_id": rphone, "quiet": None}, headers=ROBIN)
check("null: the device follows Robin again (quiet)", card(ROBIN, calm["id"])["alert"], False)
check("Alex can't set Robin's iPhone's quiet: 404",
      c.put("/v1/proactive/delivery", json={"device_id": rphone, "quiet": None}, headers=ALEX).status_code, 404)
for label, bad in (("an hour of 25", {"quiet": {"weekdays": {"start": "25:00", "end": "07:00"}}}),
                   ("an unknown key", {"quiet": {**NOW_Q, "holidays": None}}),
                   ("start = end", {"quiet": {"weekdays": {"start": "22:00", "end": "22:00"}}}),
                   ("{off} for a person", {"quiet": {"off": True}}),
                   ("a level + a bad quiet (nothing saved)", {"device_id": rphone, "level": "off", "quiet": {"weekend": 1}}),
                   ("an empty body", {})):
    check(f"bad: {label} → 400", c.put("/v1/proactive/delivery", json=bad, headers=ROBIN).status_code, 400)
check("…the level stayed all", devs(ROBIN)["Robin's iPhone"]["level"], "all")
c.put("/v1/proactive/delivery", json={"quiet": None}, headers=ROBIN)
check("quiet null turns it off", (c.get("/v1/proactive/delivery", headers=ROBIN).json()["quiet"], card(ROBIN, calm["id"])["alert"]),
      (None, True))

print("\n── quiet hours: a household TV has only its own ──")
check("the TV has no person quiet", (c.get("/v1/proactive/delivery", headers=TV).json()["quiet"],
                                     c.put("/v1/proactive/delivery", json={"quiet": NOW_Q}, headers=TV).status_code), (None, 400))
r = c.put("/v1/proactive/delivery", json={"device_id": tid, "level": "household", "quiet": NOW_Q}, headers=TV)
check("…level and its own quiet in one body", (r.status_code, r.json()["level"], r.json()["devices"][0]["quiet"]["weekdays"]),
      (200, "household", NOW_Q["weekdays"]))
check("…inside it, the household card doesn't alert", card(TV, hc["id"])["alert"], False)

print("\n── eval turns ──")
import agent_runtime_lib as lib  # noqa: E402
src = Path(lib.__file__).read_text()
check("no agent tool sets modes or delivery (app endpoints only; a future one must be eval-simulated)",
      tuple(f in src for f in ("set_source_mode(", "set_device_level(", "set_person_quiet(", "set_device_quiet(")),
      (False, False, False, False))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
