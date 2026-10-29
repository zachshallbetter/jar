#!/usr/bin/env python3
# gate: hermetic
"""The schedule as a ResolvedSurface (#1060 migration step 4: `temporal`, GET /v1/schedule?as=surface). Built from
schedule_view.view's real payload over fictional calendars (Alex the admin, Robin and Jordan adults, Ava a kid), never
a hand-made shape. S1: golden vectors scripts/evalsets/surfaces/schedule.{alex,ava,screen}.json (`--write` rewrites
them; review the diff). Rules: one group per household day (today primary), all-day first, overlaps of the viewer's own
timed events, the plain event verbatim, open only. S2 through the real route: each viewer's never-see list, a fence,
deterministic, the plain shape unchanged.
Run: python3 scripts/test_surfaces_schedule.py [--write]"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_TMP = Path(tempfile.mkdtemp(prefix="titan-schedule-surface-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl", "SURFACES_DB": "surfaces.db",
             "SITTER_DB": "sitter.db", "WORLD_DIR": "world", "KB_ACL_DB": "kb_acl.db", "LINKS_DB": "links.db",
             "LINK_SALT_FILE": "link_salt", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db", "TODOS_DB": "todos.db",
             "SCHEDULED_DB": "scheduled.db", "FENCES_DB": "fences.db", "REFERENCES_DB": "references.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TZ"] = "America/Los_Angeles"
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
VECTORS = HERE / "evalsets" / "surfaces"
WRITE = "--write" in sys.argv
import schedule_view as SV  # noqa: E402
import surfaces as S  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def vector(name, got):
    p = VECTORS / f"{name}.json"
    if WRITE:
        p.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    check(f"S1 golden vector {name}", got, json.loads(p.read_text()) if p.exists() else None)


LA = ZoneInfo("America/Los_Angeles")
NOW = 1791331200.0                 # 2026-10-07T00:00:00Z = Oct 6, 17:00 in Los Angeles
TODAY = date(2026, 10, 6)


def people(alex="alex", robin="robin", jordan="jordan", ava="ava"):
    return [{"id": alex, "name": "Alex", "role": "admin", "calendars": []},
            {"id": robin, "name": "Robin", "role": "adult", "calendars": ["Robin"]},
            {"id": jordan, "name": "Jordan", "role": "adult", "calendars": []},
            {"id": ava, "name": "Ava", "role": "kid", "parents": [alex, robin], "calendars": ["Ava"]}]


def ev(nid, title, start, cal, owner="alex", aud="private", end=None, all_day=False, loc=None):
    return {"node_id": nid, "data": {"title": title, "start": start, "end": end, "calendar": cal, "all_day": all_day,
                                     "location": loc, "provenance": {"owner": owner, "audience": aud}}}


def nodes(day: date, alex="alex", jordan="jordan"):
    d0, d1 = day.isoformat(), (day + timedelta(days=1)).isoformat()
    return [ev("e1", "Ballet", f"{d0}T16:30:00-07:00", "Ava", owner=alex, end=f"{d0}T17:30:00-07:00"),
            ev("e2", "Take out the bins", f"{d1}T20:00:00-07:00", "Family", owner=alex),
            ev("e3", "Dentist", f"{d0}T09:00:00-07:00", "Work", owner=alex, end=f"{d0}T10:00:00-07:00", loc="Harbor Dental"),
            ev("e4", "Budget call", f"{d0}T09:30:00-07:00", "Work", owner=alex, end=f"{d0}T10:15:00-07:00"),
            ev("e5", "Shift", f"{d0}T12:00:00-07:00", "Home", owner=jordan, end=f"{d0}T20:00:00-07:00"),
            ev("e6", "School holiday", d1, "Family", owner=alex, all_day=True),
            ev("e7", "Coffee with Hale", f"{d1}T10:00:00-07:00", "Robin", owner=alex)]


HH = {"Family"}
W = (TODAY, TODAY + timedelta(days=6))


def view(viewer, role, **kw):
    return SV.view(nodes(TODAY), *W, viewer, role, people(), HH, zone=LA, **kw)


print("── S1: the real schedule view, resolved ──")
alex = S.schedule(view("alex", "admin"), "alex", "admin", NOW, TODAY, LA)
vector("schedule.alex", alex)
vector("schedule.ava", S.schedule(view("ava", "kid"), "ava", "kid", NOW, TODAY, LA))
screen = S.schedule(view(None, "household"), None, "household", NOW, TODAY, LA)
vector("schedule.screen", screen)
G = {g["id"]: g for g in alex["groups"]}
mids = lambda s, gid: [m["subject"]["id"] for g in s["groups"] if g["id"] == gid for m in g["members"]]  # noqa: E731
check("profile temporal, purpose monitor, jb-1", (alex["profile"], alex["purpose"], alex["profileVersion"]),
      ("temporal", "monitor", "jb-1"))
check("overlaps first, then one group per household day; today primary",
      [(g["id"], g["role"]) for g in alex["groups"]],
      [("g-overlaps", "attention"), ("g-day-2026-10-06", "primary"), ("g-day-2026-10-07", "supporting")])
check("a day's heading: Today, else the app's own day words, with the date as args",
      [g["label"] for g in alex["groups"][1:]],
      [{"key": "schedule.day", "text": "Today", "args": {"date": "2026-10-06"}},
       {"key": "schedule.day", "text": "Wed Oct 7", "args": {"date": "2026-10-07"}}])
check("today in start order", mids(alex, "g-day-2026-10-06"), ["e3", "e4", "e1"])
check("all-day first, then by start", mids(alex, "g-day-2026-10-07"), ["e6", "e7", "e2"])
check("overlaps: the viewer's own timed events that overlap (Dentist and the budget call)", mids(alex, "g-overlaps"), ["e3", "e4"])
check("…marked as such", {m["slots"]["status"]["text"] for m in G["g-overlaps"]["members"]}, {"schedule.overlaps"})
check("a kid's Ballet (Alex's child) is on Alex's schedule but never an overlap of Alex's own", "e1" in mids(alex, "g-overlaps"), False)
check("whose: someone else's event says whose (audience slot), your own and the household's don't",
      {m["subject"]["id"]: m["slots"].get("audience") for g in alex["groups"][1:] for m in g["members"]},
      {"e3": None, "e4": None, "e1": "Ava", "e6": None, "e7": "Robin", "e2": None})
check("never Jordan's private shift on Alex's schedule",
      ("e5" in [m["subject"]["id"] for g in alex["groups"] for m in g["members"]], '"Shift"' in json.dumps(alex)), (False, False))
check("each member keeps its plain event verbatim, plus whose",
      {m["subject"]["id"]: (m["event"], m["whose"]["id"]) for g in alex["groups"][1:] for m in g["members"]},
      {e["id"]: (e, p) for p, evs in [(r["id"], r["events"]) for r in view("alex", "admin")["people"]]
       + [("household", view("alex", "admin")["household"]["events"])] for e in evs})
AOPS = {o["id"]: o for o in alex["operations"]}
check("open and ask; ask sends fixed words and a reference, never the calendar's title (#1322, #473)",
      (sorted({o["verb"] for o in alex["operations"]}), AOPS["op-ask-e3"]["params"],
       any("Dentist" in json.dumps(o) for o in alex["operations"])),
      (["ask", "open"], {"prompt": "Tell me about this.", "about": {"kind": "event", "id": "e3"}}, False))
check("Ava: her calendar and the household, no overlaps of anyone else's",
      [m["subject"]["id"] for g in S.schedule(view("ava", "kid"), "ava", "kid", NOW, TODAY, LA)["groups"]
       for m in g["members"]], ["e1", "e6", "e2"])
check("a shared screen: the household calendars only, never an overlaps group",
      ([g["id"] for g in screen["groups"]], [m["subject"]["id"] for g in screen["groups"] for m in g["members"]]),
      (["g-day-2026-10-06", "g-day-2026-10-07"], ["e6", "e2"]))
check("an empty schedule is today, empty", [(g["id"], len(g["members"])) for g in
                                            S.schedule({}, "ava", "kid", NOW, TODAY, LA)["groups"]], [("g-day-2026-10-06", 0)])
check("deterministic (S2-9)", S.schedule(view("alex", "admin"), "alex", "admin", NOW, TODAY, LA), alex)
check("partial rides along", S.schedule({"partial": True}, "ava", "kid", NOW, TODAY, LA)["partial"], True)

print("── S2: through the real route ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
import local_time  # noqa: E402

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
    return {"X-Agent-Token": tok}, next((p["id"] for p in st.people() if p["name"] == name), None)


ROBIN, robin = pair("Robin Quarry", "adult")
JORDAN, jordan = pair("Jordan Quarry", "adult")
AVA, ava = pair("Ava Quarry", "kid")
TV, _ = pair("Living room", "household", "tvos")
ADMIN = hh.ADMIN_PERSON_ID
today = local_time.now().date()
PEOPLE = people(ADMIN, robin, jordan, ava)
NODES = nodes(today, ADMIN, jordan)
fence_off = set()
SV.for_viewer = lambda s, e, v, r, only=None: {**SV.view(   # the route's call, over this test's calendars
    NODES, s, e, v, r, PEOPLE, HH, only=only, zone=local_time.zone(),
    fence=lambda v_, r_, o, *, text, item: not (item in fence_off and v_ != ADMIN)), "partial": False}


def surface(h, q=""):
    r = c.get(f"/v1/schedule?as=surface{q}", headers=h)
    return r.status_code, r.json(), r.text


NEVER = {"admin": ["Shift"], "robin": ["Dentist", "Budget call", "Shift"], "jordan": ["Dentist", "Ballet", "Coffee with Hale"],
         "ava": ["Dentist", "Shift", "Coffee with Hale"], "screen": ["Dentist", "Shift", "Ballet", "Coffee with Hale"]}
for nm, h in (("admin", M), ("robin", ROBIN), ("jordan", JORDAN), ("ava", AVA), ("screen", TV)):
    code, s, text = surface(h)
    check(f"{nm}: 200, jb-1 temporal", (code, s.get("profileVersion"), s.get("profile")), (200, "jb-1", "temporal"))
    check(f"{nm}: never-see list (whole JSON)", [t for t in NEVER[nm] if f'"{t}"' in text], [])
check("the admin's overlaps through the route", [m["subject"]["id"] for g in surface(M)[1]["groups"]
                                                 if g["id"] == "g-overlaps" for m in g["members"]], ["e3", "e4"])
check("Jordan's own shift is Jordan's", "e5" in [m["subject"]["id"] for g in surface(JORDAN)[1]["groups"] for m in g["members"]], True)
fence_off.add("e7")
check("a fence keeps Robin's coffee from Robin once fenced (only the admin admitted)",
      "Coffee with Hale" in surface(ROBIN)[2], False)
fence_off.clear()
check("same state, same fingerprint (S2-9)", surface(AVA)[1]["fingerprint"], surface(AVA)[1]["fingerprint"])
check("the people filter passes through", sorted({m["whose"]["id"] for g in surface(M, f"&people={ava}")[1]["groups"]
                                                 for m in g["members"]}), [ava])
check("a bad window is still 400", c.get("/v1/schedule?as=surface&start=2026-10-10&end=2026-10-01", headers=M).status_code, 400)
old = c.get("/v1/schedule", headers=M).json()
check("old clients keep the plain view", sorted(old), ["end", "household", "partial", "people", "start"])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
