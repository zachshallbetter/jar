#!/usr/bin/env python3
# gate: hermetic
"""Home for a shared screen (titan-node#669): household events only, the next one, chores, the kids' pill only when
allowed, house tiles without locks/alarms/garage/cameras, what's playing, at most 4 household cards.

Run: python3 scripts/test_home_screen.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime

os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ["AGENT_TURN_LOG"] = os.path.join(tempfile.mkdtemp(), "turns.jsonl")
os.environ["HOUSEHOLD_DB"] = os.path.join(tempfile.mkdtemp(), "household.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import home_screen as H  # noqa: E402
from household_coordination import Event  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


PT = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 7, 17, 42, tzinfo=PT)
T = lambda h, m=0: datetime(2026, 10, 7, h, m, tzinfo=PT)   # noqa: E731
evs = [Event("Ava drop-off", T(8), T(8, 15), "Home", audience="household"),
       Event("Board meeting", T(16), T(17), "Zach", audience="private"),
       Event("Dinner: tacos", T(18, 30), T(19, 30), "Home", audience="household"),
       Event("Therapy", T(19), T(20), "Robin", audience="private"),
       Event("Garbage night", T(21), T(21, 15), "Home", audience="household")]
PEOPLE = [{"id": "zach", "name": "Zach", "role": "admin"},
          {"id": "leo", "name": "Leo", "role": "kid", "other_parent": "their dad"},
          {"id": "ava", "name": "Ava", "role": "kid", "other_parent": "their dad"}]
away = lambda kid, at: {"state": "away", "until": datetime(2026, 10, 8, 9, 0, tzinfo=PT), "then": "here"}  # noqa: E731
states = [
    {"entity_id": "climate.hall", "state": "heat", "attributes": {"friendly_name": "Hall", "current_temperature": 69,
                                                                  "temperature": 70, "hvac_action": "heating"}},
    {"entity_id": "light.lamp", "state": "on", "attributes": {"friendly_name": "Lamp"}},
    {"entity_id": "light.porch", "state": "on", "attributes": {"friendly_name": "Porch"}},
    {"entity_id": "light.den", "state": "off", "attributes": {"friendly_name": "Den"}},
    {"entity_id": "lock.front_door", "state": "unlocked", "attributes": {"friendly_name": "Front door"}},
    {"entity_id": "cover.garage", "state": "open", "attributes": {"friendly_name": "Garage door"}},
    {"entity_id": "alarm_control_panel.home", "state": "disarmed", "attributes": {"friendly_name": "Alarm"}},
    {"entity_id": "camera.porch", "state": "idle", "attributes": {"friendly_name": "Porch cam"}},
    {"entity_id": "media_player.kitchen", "state": "playing", "attributes": {"friendly_name": "Kitchen",
                                                                              "media_title": "Kind of Blue", "media_artist": "Miles Davis"}},
]
cards = [{"id": f"pc-{i}", "title": f"Card {i}", "kind": "info", "urgency": i % 3, "updated_at": i,
          "actions": [{"id": "a0", "label": "Done", "kind": "dismiss", "value": ""}]} for i in range(6)]
h = H.build(NOW, PEOPLE, evs, weather={"now": 58, "sky": "light rain"}, state_at=away, ha_states=states, cards=cards)
blob = json.dumps(h)

print("\n── the household's day, nobody's own ──")
check("today: household events only, past ones marked", [(e["title"], e["past"]) for e in h["today"]],
      [("Ava drop-off", True), ("Dinner: tacos", False), ("Garbage night", False)])
check("no one's own event, by any name", ("Board meeting" in blob, "Therapy" in blob), (False, False))
check("up next, with minutes to go", (h["up_next"]["title"], h["up_next"]["in_minutes"]), ("Dinner: tacos", 48))
check("chores from the household's events", [c["text"] for c in h["chores"]], ["Garbage night by 9 PM"])
check("the header", (h["header"]["date"], h["header"]["time"], h["header"]["weather"]["now"]), ("Wed Oct 7", "5:42 PM", 58))

print("\n── the kids' pill only when allowed ──")
check("allowed: one line", h["header"]["pill"], "Kids at their dad's until Thu 9 AM")
check("not allowed (a shared screen, switch off): none", H.build(NOW, PEOPLE, evs, state_at=None)["header"]["pill"], None)
check("home: none", H.pill(PEOPLE, NOW, lambda k, at: {"state": "here", "until": None}), None)

print("\n── the house ──")
check("thermostat and lights on, nothing hands-off", h["house"],
      [{"kind": "climate", "name": "Hall", "state": "heat", "inside": 69, "set_to": 70, "action": "heating"},
       {"kind": "lights", "on": 2}])
check("never a lock, garage door, alarm or camera", any(w in blob for w in ("Front door", "Garage door", "Alarm", "Porch cam")),
      False)
check("what's playing", h["playing"], [{"id": "media_player.kitchen", "where": "Kitchen", "state": "playing", "title": "Kind of Blue", "artist": "Miles Davis"}])

print("\n── the shelf ──")
check("at most 4, most urgent first", [c["card"] for c in h["shelf"]], ["pc-5", "pc-2", "pc-4", "pc-1"])
check("each with its own buttons", h["shelf"][0]["actions"], [{"id": "a0", "label": "Done", "kind": "dismiss", "value": ""}])

print("\n── empty sources: blocks left out, never an error ──")
e = H.build(NOW, PEOPLE, [], ha_states=None, cards=None)
check("an empty Home", (e["today"], e["up_next"], e["house"], e["shelf"], e["header"]["weather"]), ([], None, [], [], None))

print("\n── the week (jellybean#417) ──")
from datetime import date, timedelta  # noqa: E402
START = date(2026, 10, 5)   # Monday
by_day = {}
for i in range(-21, 7):
    d = START + timedelta(days=i)
    D = lambda h, m=0, d=d: datetime(d.year, d.month, d.day, h, m, tzinfo=PT)   # noqa: E731
    evs_d = []
    if d.weekday() in (0, 2):
        evs_d.append(Event("Ballet", D(17, 15), D(18, 30), "Home", people=["Ava"], audience="household"))
    if d == date(2026, 10, 7):
        evs_d += [Event("Plumber", D(15), D(16), "Home", audience="household"),
                  Event("Board meeting", D(16), D(17), "Zach", people=["Zach"], audience="private")]
    if d == date(2026, 10, 9):
        evs_d.append(Event("Soccer", D(15, 15), D(16, 15), "Home", people=["Leo"], audience="household"))
    by_day[d] = evs_d
chores_fn = lambda d: ([{"name": "Garbage", "at": datetime(d.year, d.month, d.day, 21, tzinfo=PT), "due": True, "why": "", "when": None},   # noqa: E731
                        {"name": "Recycling", "at": datetime(d.year, d.month, d.day, 21, tzinfo=PT), "due": True,
                         "why": "the kids are at their other parent's", "when": "kids away"}] if d.weekday() == 2 else [])
wk = H.week(START, by_day, datetime(2026, 10, 7, 12, tzinfo=PT), PEOPLE, chores=chores_fn, state_at=None,
            answers={"2026-10-09:leo:1515": {"zach": "take"}})
check("seven days from start, today marked", ([d["label"] for d in wk["days"]][:3], [d["today"] for d in wk["days"]][2]),
      (["Mon 5", "Tue 6", "Wed 7"], True))
check("a repeating event once, in series, with its rhythm and next date",
      wk["series"], [{"title": "Ballet", "rhythm": "Mondays and Wednesdays at 5:15 PM", "who": "Ava", "next": "Wed Oct 7, 5:15 PM"}])
check("its occurrences left out of the days", any(e["title"] == "Ballet" for d in wk["days"] for e in d["events"]), False)
wed = wk["days"][2]
check("a one-off household event, with when; nobody's own event", [(e["title"], e["at"]) for e in wed["events"]],
      [("Plumber", "3 PM")])
fri = wk["days"][4]
check("who, and who's driving (the family meeting's answer)", (fri["events"][0]["who"], fri["events"][0]["driver"]), ("Leo", "Zach"))
check("the day's chores, computed", [(c["text"], c["due"]) for c in wed["chores"]], [("Garbage", True), ("Recycling", True)])
check("custody off: no state lines", any("state" in d for d in wk["days"]), False)
wk2 = H.week(START, by_day, datetime(2026, 10, 7, 12, tzinfo=PT), PEOPLE, state_at=lambda k, at: {
    "state": "away", "until": datetime(2026, 10, 8, 9, tzinfo=PT), "then": "here"})
check("custody allowed: the day it changes", wk2["days"][3].get("state"), "Kids home from Thu 9 AM")
check("never a private title anywhere", "Board meeting" in json.dumps(wk2, default=str), False)

print("\n── the route is everyone's ──")
import household_identity as hh  # noqa: E402
check("a shared device, a kid", (hh.route_allowed("household", "GET", "/v1/home"), hh.route_allowed("kid", "GET", "/v1/home"),
                                 hh.route_allowed("household", "GET", "/v1/home/week")), (True, True, True))

check("play/pause/skip is everyone's too", [hh.route_allowed(r, "POST", "/v1/home/media") for r in ("household", "kid", "adult")],
      [True, True, True])
check("but not an engineering agent", hh.route_allowed(hh.ENGINEERING, "POST", "/v1/home/media"), False)

print("\n── POST /v1/home/media: only what Home shows playing, only play/pause/skip (jellybean#421) ──")
HOUSE = [
    {"entity_id": "media_player.living_room_tv", "state": "paused", "attributes": {"friendly_name": "Living Room TV", "media_title": "Bluey"}},
    {"entity_id": "media_player.bedroom", "state": "idle", "attributes": {"friendly_name": "Bedroom"}},
    {"entity_id": "media_player.den", "state": "playing", "attributes": {"friendly_name": "Den"}},   # no title: not on Home
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door", "media_title": "x"}},
    {"entity_id": "light.hall", "state": "on", "attributes": {"friendly_name": "Hall"}},
]
calls = []


def fake_ha(method, path, body):
    calls.append((method, path, body))
    if method == "GET":
        return HOUSE
    return []


def sent():
    return [(p, b) for m, p, b in calls if m == "POST"]


code, out = H.media_control("media_player.living_room_tv", "play", http=fake_ha)
check("a paused player Home shows: play", (code, out["ok"], sent()),
      (200, True, [("/api/services/media_player/media_play", {"entity_id": "media_player.living_room_tv"})]))
for action, svc in (("pause", "media_pause"), ("next", "media_next_track"), ("previous", "media_previous_track")):
    calls.clear()
    code, _ = H.media_control("media_player.living_room_tv", action.upper(), http=fake_ha)
    check(f"{action}", (code, sent()), (200, [(f"/api/services/media_player/{svc}", {"entity_id": "media_player.living_room_tv"})]))
for action in ("volume_up", "source", "off", "mute", "stop", ""):
    calls.clear()
    code, _ = H.media_control("media_player.living_room_tv", action, http=fake_ha)
    check(f"no {action or 'empty'} action", (code, sent()), (400, []))
for eid, why in (("media_player.bedroom", "idle"), ("media_player.den", "no title"), ("media_player.garage", "unknown")):
    calls.clear()
    code, _ = H.media_control(eid, "play", http=fake_ha)
    check(f"a player Home doesn't show ({why})", (code, sent()), (404, []))
for eid in ("lock.front_door", "light.hall", "cover.garage_door", "media_player", ""):
    calls.clear()
    code, _ = H.media_control(eid, "play", http=fake_ha)
    check(f"another domain refused ({eid or 'empty'})", (code, sent(), [c for c in calls if c[0] == "GET"]), (403, [], []))


def no_token(method, path, body):
    raise PermissionError("not connected")


check("Home Assistant not connected", H.media_control("media_player.living_room_tv", "play", http=no_token)[0], 503)

print("\n── GET /v1/home/house: rooms, lights, thermostats, scenes; never locks/alarm/garage/cameras (jellybean#420) ──")
import home_assistant as ha  # noqa: E402
ROOMS = [
    {"entity_id": "light.lamp", "state": "on", "attributes": {"friendly_name": "Lamp", "brightness": 153}},
    {"entity_id": "light.ceiling", "state": "off", "attributes": {"friendly_name": "Ceiling"}},
    {"entity_id": "light.porch", "state": "on", "attributes": {"friendly_name": "Porch"}},        # no area
    {"entity_id": "light.broken", "state": "unavailable", "attributes": {"friendly_name": "Broken"}},
    {"entity_id": "climate.main", "state": "heat", "attributes": {"friendly_name": "Thermostat", "current_temperature": 68,
                                                                "temperature": 70, "hvac_action": "heating"}},
    {"entity_id": "scene.movie_night", "state": "scening", "attributes": {"friendly_name": "Movie night"}},
    {"entity_id": "scene.bedtime", "state": "scening", "attributes": {"friendly_name": "Bedtime"}},
    {"entity_id": "switch.fan", "state": "on", "attributes": {"friendly_name": "Fan"}},
    {"entity_id": "media_player.living_room_tv", "state": "paused", "attributes": {"friendly_name": "Living Room TV"}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
    {"entity_id": "alarm_control_panel.home", "state": "armed_home", "attributes": {"friendly_name": "Alarm"}},
    {"entity_id": "cover.garage_door", "state": "closed", "attributes": {"friendly_name": "Garage Door"}},
    {"entity_id": "camera.driveway", "state": "idle", "attributes": {"friendly_name": "Driveway"}},
]
AREAS = {"light.lamp": "Living Room", "light.ceiling": "Kitchen", "climate.main": "Living Room",
         "lock.front_door": "Hall", "camera.driveway": "Garage", "cover.garage_door": "Garage"}
hcalls = []


def fake_house(method, path, body):
    hcalls.append((method, path, body))
    if method == "GET":
        return ROOMS
    if path == "/api/template":
        return "\n".join(f"{k}|{v}" for k, v in AREAS.items())
    return []


def hsent():
    return [(p, b) for m, p, b in hcalls if m == "POST" and p != "/api/template"]


ha._AREAS["at"] = 0.0
code, view = H.gather_house("household", http=fake_house)
check("200", code, 200)
check("rooms A to Z, Elsewhere last, empty rooms left out", [r["name"] for r in view["rooms"]], ["Kitchen", "Living Room", "Elsewhere"])
lr = view["rooms"][1]
check("a light on, with its brightness in percent", lr["lights"], [{"id": "light.lamp", "name": "Lamp", "on": True, "brightness": 60}])
check("a light off, no brightness", view["rooms"][0]["lights"], [{"id": "light.ceiling", "name": "Ceiling", "on": False}])
check("the thermostat", lr["climate"], [{"id": "climate.main", "name": "Thermostat", "state": "heat", "inside": 68, "set_to": 70,
                                         "action": "heating"}])
check("scenes, A to Z", view["scenes"], [{"id": "scene.bedtime", "name": "Bedtime"}, {"id": "scene.movie_night", "name": "Movie night"}])
blob = json.dumps(view)
for never in ("lock.", "alarm_control_panel.", "cover.", "camera.", "Front Door", "Garage", "Driveway", "switch.", "media_player.", "light.broken"):
    check(f"never {never}", never in blob, False)
check("nothing changed by looking", hsent(), [])
check("an empty house is a 200 with nothing in it", H.gather_house("household", http=lambda m, p, b: [] if m == "GET" else "")[1],
      {"rooms": [], "scenes": []})
check("Home Assistant not connected", H.gather_house("household", http=no_token)[0], 503)


def unreachable(method, path, body):
    raise OSError("down")


check("Home Assistant down", H.gather_house("household", http=unreachable)[0], 502)

print("\n── POST /v1/home/house: only what the view lists, only its actions, through home_control (jellybean#420) ──")
for eid, action, value, svc, data in (
        ("light.ceiling", "on", None, "light/turn_on", {}),
        ("light.lamp", "off", None, "light/turn_off", {}),
        ("light.lamp", "toggle", None, "light/toggle", {}),
        ("light.lamp", "brightness", 30, "light/turn_on", {"brightness_pct": 30}),
        ("climate.main", "set_temperature", 71, "climate/set_temperature", {"temperature": 71.0}),
        ("scene.movie_night", "activate", None, "scene/turn_on", {})):
    hcalls.clear()
    code, out = H.house_control(eid, action, value, "household", http=fake_house)
    check(f"{action} {eid}", (code, out["ok"], hsent()), (200, True, [(f"/api/services/{svc}", {"entity_id": eid, **data})]))
hcalls.clear()
code, out = H.house_control("climate.main", "set_temperature", 95, "household", http=fake_house)
check("outside the safe range: refused by home_control, nothing sent", (code, out["ok"], hsent()), (422, False, []))
for eid, action, value, want in (
        ("light.lamp", "set_temperature", 70, 400), ("climate.main", "off", None, 400), ("scene.movie_night", "off", None, 400),
        ("light.lamp", "brightness", None, 400), ("light.lamp", "brightness", "50", 400), ("light.lamp", "brightness", True, 400),
        ("light.lamp", "dance", None, 400)):
    hcalls.clear()
    code, _ = H.house_control(eid, action, value, "household", http=fake_house)
    check(f"no {action} on {eid} (value {value!r})", (code, hsent()), (want, []))
for eid in ("lock.front_door", "alarm_control_panel.home", "cover.garage_door", "camera.driveway", "switch.fan",
            "media_player.living_room_tv", "script.anything", "light", "", "light.lamp/../lock.front_door"):
    hcalls.clear()
    code, _ = H.house_control(eid, "on", None, "household", http=fake_house)
    check(f"refused before Home Assistant is asked ({eid or 'empty'})", (code, hcalls), (403, []))
for eid in ("light.broken", "light.garage", "scene.party"):
    hcalls.clear()
    code, _ = H.house_control(eid, "on" if eid.startswith("light") else "activate", None, "household", http=fake_house)
    check(f"one the view doesn't list ({eid})", (code, hsent()), (404, []))
hcalls.clear()
code, _ = H.house_control("light.lamp", "off", None, "kid", http=fake_house)
check("a kid: lights, thermostat and scenes are theirs (KID_HOME_DOMAINS)", (code, len(hsent())), (200, 1))
check("Home Assistant not connected", H.house_control("light.lamp", "off", None, "household", http=no_token)[0], 503)
check("Home Assistant down", H.house_control("light.lamp", "off", None, "household", http=unreachable)[0], 502)

orig = hh.KID_HOME_DOMAINS
hh.KID_HOME_DOMAINS = frozenset({"light"})   # a narrower kid list holds the view and the change
try:
    _, kid_view = H.gather_house("kid", http=fake_house)
    check("a narrower kid list: only those domains shown", (kid_view["scenes"], [r["climate"] for r in kid_view["rooms"]]),
          ([], [[], [], []]))
    hcalls.clear()
    check("and changed", (H.house_control("scene.movie_night", "activate", None, "kid", http=fake_house)[0], hcalls), (403, []))
finally:
    hh.KID_HOME_DOMAINS = orig

check("the house routes are everyone's", [hh.route_allowed(r, m, "/v1/home/house") for r in ("household", "kid", "adult")
                                          for m in ("GET", "POST")], [True] * 6)
check("but not an engineering agent's", [hh.route_allowed(hh.ENGINEERING, m, "/v1/home/house") for m in ("GET", "POST")],
      [False, False])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
