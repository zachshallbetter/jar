#!/usr/bin/env python3
# gate: hermetic
""""Turn everything off" (titan-node#1343): lights, switches, fans, and TVs and speakers (paused when playing, never a
volume change); never climate, covers (blinds too), locks, alarms, cameras or a security-shaped device, for any role.
It never acts on the first call: it lists what's on and holds exactly those for the person's Do it (held_actions plus a
card); the model can't confirm it. On Do it it re-checks the live states and acts only on the held devices still on and
still allowed, so a device that changed, or a list someone widened, can't widen the act. Views × roles. Fictional house.
Run: python3 scripts/test_everything_off.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-alloff-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "PROACTIVE_DB": "proactive.db", "HELD_ACTIONS_DB": "held.db", "PUSH_DB": "push.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402
import held_actions  # noqa: E402
import home_assistant as ha  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


def ent(eid, state, name, **a):
    return {"entity_id": eid, "state": state, "attributes": {"friendly_name": name, **a}}


HOUSE = [ent("light.kitchen", "on", "Kitchen Light"), ent("light.hall", "off", "Hall Light"),
         ent("switch.coffee_maker", "on", "Coffee Maker"), ent("switch.garage_door_opener", "on", "Garage Door Opener"),
         ent("fan.bedroom", "on", "Bedroom Fan"), ent("media_player.living_room_tv", "playing", "Living Room TV"),
         ent("media_player.kitchen_speaker", "idle", "Kitchen Speaker"), ent("climate.living_room", "heat", "Thermostat"),
         ent("cover.blinds", "open", "Living Room Blinds", device_class="blind"), ent("lock.front_door", "unlocked", "Front Door"),
         ent("alarm_control_panel.home", "disarmed", "Home Alarm"), ent("camera.porch", "idle", "Porch Camera")]
state = {e["entity_id"]: dict(e) for e in HOUSE}
posts: list[tuple[str, str]] = []


def fake_http(method, path, body=None):
    if method == "GET" and path.startswith("/api/states"):
        return list(state.values())
    if method == "POST":
        posts.append((path, (body or {}).get("entity_id")))
        return []
    return ""


ha._http = fake_http
hh.reset_store(hh.HouseholdStore(_TMP / "household.db"))
lib._TURN_IO.person = {"id": hh.ADMIN_PERSON_ID}
_role, _src = lib.turn_role, lib.turn_source
lib.turn_source = lambda: "user"

print("── the plan: what's on, in the counted kinds only ──")
plan = ha.everything_off_plan(list(state.values()))
check("lights, switches, fans, TVs and speakers that are on; never climate, blinds, lock, alarm, camera, or the "
      "security-shaped garage door opener", sorted(d["entity_id"] for d in plan),
      ["fan.bedroom", "light.kitchen", "media_player.kitchen_speaker", "media_player.living_room_tv", "switch.coffee_maker"])
check("names are HA's own", sorted(d["name"] for d in plan), ["Bedroom Fan", "Coffee Maker", "Kitchen Light", "Kitchen Speaker",
                                                             "Living Room TV"])
check("a kid's plan: only the domains a kid may change", sorted(d["domain"] for d in ha.everything_off_plan(
      list(state.values()), hh.home_domains_for("kid"))) == sorted(d["domain"] for d in plan if d["domain"] in hh.KID_HOME_DOMAINS), True)

print("── the first call never acts: it holds the listed devices for the person's OK ──")
for phrase in ("everything", "Everything", "all", "the whole house", "all the lights and switches"):
    check(f"device {phrase!r} is 'everything'", bool(ha.EVERYTHING_RX.match(phrase)), True)
check("…but a device named 'All Lights Group' isn't", bool(ha.EVERYTHING_RX.match("All Lights Group")), False)
lib.turn_role = lambda: "admin"
posts.clear()
out = lib.execute_tool("home_control", {"device": "everything", "action": "off"})
check("0 POSTs; the reply lists what would go off and asks for Do it", (posts, "Kitchen Light" in out, "Do it" in out), ([], True, True))
cards = proactive.cards_for(hh.ADMIN_PERSON_ID, "admin") if hasattr(proactive, "cards_for") else None
import sqlite3  # noqa: E402
con = sqlite3.connect(os.environ["HELD_ACTIONS_DB"])
held_rows = con.execute("SELECT id, tool, args FROM held").fetchall()
check("one held call, home_control with exactly the planned devices", (len(held_rows), held_rows[0][1],
      sorted(__import__("json").loads(held_rows[0][2])["entities"])), (1, "home_control", sorted(d["entity_id"] for d in plan)))
hid = held_rows[0][0]
posts.clear()
out = lib.execute_tool("home_control", {"device": "everything", "action": "off", "entities": ["lock.front_door", "light.kitchen"]})
check("the model passing its own list on a fresh call: ignored, planned and held again, 0 POSTs", (posts, "Do it" in out), ([], True))

print("── Do it: re-checked, then only what's still on and still allowed ──")
state["light.kitchen"]["state"] = "off"     # turned off by hand meanwhile
posts.clear()
out = lib._run_held_action(hid)
check("POSTs: the fan, coffee maker, speaker off; the TV paused (not a volume change); the light skipped (already off)",
      sorted(posts), sorted([("/api/services/fan/turn_off", "fan.bedroom"), ("/api/services/switch/turn_off", "switch.coffee_maker"),
                             ("/api/services/media_player/turn_off", "media_player.kitchen_speaker"),
                             ("/api/services/media_player/media_pause", "media_player.living_room_tv")]))
check("never a lock, alarm, cover, camera or climate service", [p for p, _ in posts if p.split("/")[3] in
      ("lock", "alarm_control_panel", "cover", "camera", "climate")], [])
check("the reply says what was turned off", ("Bedroom Fan" in out, "(paused)" in out), (True, True))
check("Do it runs once", "Turned off" in lib._run_held_action(hid), False)
state["light.kitchen"]["state"] = "on"

print("── a held list widened by hand can't widen the act ──")
hid2 = held_actions.put(hh.ADMIN_PERSON_ID, "home_control", {"device": "everything", "action": "off",
                                                            "entities": ["light.kitchen", "lock.front_door", "cover.blinds",
                                                                         "switch.garage_door_opener", "climate.living_room"]})
posts.clear()
lib._run_held_action(hid2)
check("only the kitchen light: the lock, blinds, garage opener and thermostat in the list are dropped",
      posts, [("/api/services/light/turn_off", "light.kitchen")])

print("── roles ──")
for role in ("kid", "household"):
    lib.turn_role = lambda r=role: r
    posts.clear()
    out = lib.execute_tool("home_control", {"device": "everything", "action": "off"})
    check(f"{role}: holds, 0 POSTs, and never lists the garage door opener", (posts, "Garage Door Opener" in out), ([], False))
lib.turn_role = lambda: "admin"
lib.turn_source = lambda: "eval"
posts.clear()
before = con.execute("SELECT COUNT(*) FROM held").fetchone()[0]
lib.execute_tool("home_control", {"device": "everything", "action": "off"})
check("an eval turn (home_control is simulated): nothing sent, nothing held",
      (posts, con.execute("SELECT COUNT(*) FROM held").fetchone()[0] - before), ([], 0))
for k in list(state):
    state[k]["state"] = "off" if k.split(".")[0] in ("light", "switch", "fan", "media_player") else state[k]["state"]
lib.turn_source = lambda: "user"
check("nothing on: says so, nothing held", "Nothing to turn off" in lib.execute_tool("home_control", {"device": "all", "action": "off"}), True)
lib.turn_role, lib.turn_source = _role, _src
lib._TURN_IO.person = None

print("── the Do it is the asker's alone: a kid's hold can't be confirmed from an adult's or the screen's deck, nor the reverse ──")
lib.turn_source = lambda: "user"   # a person's own turn (the gate sets AGENT_TURN_SOURCE=test: never rely on the default)
for k in state:   # something on again
    if k in ("light.kitchen", "media_player.living_room_tv"):
        state[k]["state"] = "on" if k.startswith("light") else "playing"
store = hh.get_store()
people = {r: store.add_person(n, r)["id"] for r, n in (("adult", "Robin Hale"), ("kid", "Ava Hale"), ("household", "Den Screen"))}
store.update_person(people["kid"], parents=[people["adult"]])


def hold_as(role):
    lib.turn_role = lambda r=role: r
    lib._TURN_IO.person = {"id": people[role]}
    n0 = con.execute("SELECT COUNT(*) FROM held").fetchone()[0]
    lib.execute_tool("home_control", {"device": "everything", "action": "off"})
    row = con.execute("SELECT id FROM held ORDER BY rowid DESC LIMIT 1").fetchone()
    return row[0] if con.execute("SELECT COUNT(*) FROM held").fetchone()[0] > n0 else None


def run_as(role, hid):
    lib.turn_role = lambda r=role: r
    lib._TURN_IO.person = {"id": people[role]}
    posts.clear()
    out = lib._run_held_action(hid)
    return out, list(posts)


kid_hid = hold_as("kid")
check("the kid's hold exists", bool(kid_hid), True)
pcon = sqlite3.connect(os.environ["PROACTIVE_DB"])
pcon.row_factory = sqlite3.Row
card = pcon.execute("SELECT * FROM cards WHERE card_key=?", (f"held-{kid_hid}",)).fetchone()
raw_aud = card["audience"] if card else None
check("its card is for the kid alone (and the kid can see it)", (people["kid"] in str(raw_aud),
      proactive.visible_to(raw_aud, people["kid"], "kid")), (True, True))
for who in ("adult", "household"):
    check(f"{who} can't see the kid's card", proactive.visible_to(raw_aud, people[who], who), False)
    out, sent = run_as(who, kid_hid)
    check(f"{who} pressing the kid's Do it: refused, 0 POSTs", (sent, "Turned off" in out), ([], False))
out, sent = run_as("kid", kid_hid)
check("…the kid's own Do it still works after those refusals (not spent by them)", (bool(sent), "Turned off" in out), (True, True))
for k in ("light.kitchen", "media_player.living_room_tv"):
    state[k]["state"] = "on" if k.startswith("light") else "playing"
adult_hid = hold_as("adult")
out, sent = run_as("kid", adult_hid)
check("the reverse: the kid pressing an adult's Do it: refused, 0 POSTs", (sent, "Turned off" in out), ([], False))
out, sent = run_as("household", adult_hid)
check("…and the screen: refused, 0 POSTs", (sent, "Turned off" in out), ([], False))
lib.turn_role, lib.turn_source = _role, _src
lib._TURN_IO.person = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
