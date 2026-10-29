#!/usr/bin/env python3
# gate: hermetic
"""The brief per viewer (#1060; the apps' vectors, jellybean#1134): brief.{admin,adult,kid,screen}.json, one household
seen by Alex (admin), Robin (adult), Ava (Alex's and Robin's kid) and the living-room screen.

How each part is projected, per viewer:
  - schedule: schedule_view.view over fixed fictional calendars (audience, calendars, kids' calendars), then schedule@1;
  - cards: proactive.visible_to over fixed cards (the deck's own audience rule), each viewer's own mute/snooze counts,
    then cards@1;
  - items (Now): a per-viewer fixture, what each store hands that viewer (items.resolve trusts its readers; the live
    readers are exercised through the route below).
then brief@1 composes them. `--write` rewrites the four vectors (review the diff); without it, any drift fails.
Checks: each vector's members and operations come only from that viewer's own parts; no kid or screen vector holds
anything of an adult's private items, card controls included; excluded rows are the viewer's own. Through the real
route: an adult's private to-do and card never reach the kid's or the screen's brief.
Run: python3 scripts/test_surfaces_brief_viewers.py [--write]"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_TMP = Path(tempfile.mkdtemp(prefix="titan-brief-viewers-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl", "SURFACES_DB": "surfaces.db",
             "SITTER_DB": "sitter.db", "WORLD_DIR": "world", "KB_ACL_DB": "kb_acl.db", "LINKS_DB": "links.db",
             "LINK_SALT_FILE": "link_salt", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db", "TODOS_DB": "todos.db",
             "SCHEDULED_DB": "scheduled.db", "FENCES_DB": "fences.db", "REFERENCES_DB": "references.db",
             "ROUTINES_DB": "routines.db", "TASKS_DB": "tasks.db", "ITEM_REFS_DB": "item_refs.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily",
             "PUSH_DB": "push.db", "ITEMS_LATENCY_LOG": "items_latency.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TZ"] = "America/Los_Angeles"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL", "APNS_KEY_FILE"):
    os.environ.pop(k, None)
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
VECTORS = HERE / "evalsets" / "surfaces"
WRITE = "--write" in sys.argv
import items as I  # noqa: E402
import proactive  # noqa: E402
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
    check(f"S1 golden vector {name} (fails on drift)", got, json.loads(p.read_text()) if p.exists() else None)


def members(s, gid=None):
    return [m for g in s.get("groups") or [] if gid in (None, g["id"]) for m in g["members"]]


LA = ZoneInfo("America/Los_Angeles")
NOW = 1791331200.0                      # Oct 6, 17:00 in Los Angeles
TODAY = date(2026, 10, 6)
DAY = datetime(2026, 10, 6, tzinfo=LA).timestamp()
NOW_ITEMS = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc).timestamp()
PEOPLE = [{"id": "alex", "name": "Alex", "role": "admin", "calendars": []},
          {"id": "robin", "name": "Robin", "role": "adult", "calendars": ["Robin"]},
          {"id": "ava", "name": "Ava", "role": "kid", "parents": ["alex", "robin"], "calendars": ["Ava"]}]
VIEWERS = {"admin": ("alex", "admin"), "adult": ("robin", "adult"), "kid": ("ava", "kid"), "screen": (None, "household")}


def ev(nid, title, start, cal, owner="alex", end=None):
    return {"node_id": nid, "data": {"title": title, "start": start, "end": end, "calendar": cal,
                                     "provenance": {"owner": owner, "audience": "private"}}}


NODES = [ev("e-dentist", "Alex's dentist", "2026-10-06T09:00:00-07:00", "Work", end="2026-10-06T10:00:00-07:00"),
         ev("e-budget", "Alex's budget call", "2026-10-06T09:30:00-07:00", "Work", end="2026-10-06T10:15:00-07:00"),
         ev("e-ballet", "Ballet", "2026-10-06T16:30:00-07:00", "Ava"),
         ev("e-run", "Robin's run", "2026-10-07T07:00:00-07:00", "Robin", owner="robin"),
         ev("e-bins", "Bins out", "2026-10-07T20:00:00-07:00", "Family")]


def card(cid, title, to, *, source="routine", urgency=1, approval=False, can_mute=True, actions=None):
    """`to` as the store keeps it: "household", "admins", or a JSON list of person ids."""
    return {"id": cid, "title": title, "detail": None, "source": source, "kind": "info", "urgency": urgency,
            "created_at": NOW - 900, "updated_at": NOW - 600, "acked_at": None, "approval": approval, "can_mute": can_mute,
            "actions": actions or [], "action_prompt": None, "to": to, "expires_at": None, "data": None}


CARDS = [card("c-quote", "Approve Alex's plumber quote?", json.dumps(["alex"]), source="approve-cap", urgency=2, approval=True,
              actions=[{"id": "approve", "label": "Approve", "kind": "reply", "value": "approve"}]),
         card("c-parcel", "Robin's parcel arrived", json.dumps(["robin"])),
         card("c-bins", "Bins tonight", "household"),
         card("c-reading", "Ava's reading log", json.dumps(["ava"]))] + \
    [card(f"c-ok-{i}", f"Approve Alex's request {i}?", json.dumps(["alex"]), source="approve-cap", urgency=1, approval=True)
     for i in range(1, 6)]   # six decisions for the admin: the brief's five-member cap leaves one out
LEFT_OUT = {"admin": {"snoozed": 1}, "adult": {"muted": 1}, "kid": {}, "screen": {}}   # each viewer's own choices


def todo(i, owner, title):
    return I.from_todo({"id": i, "title": title, "for": owner, "created_at": 1.0, "done_at": None})


def sched_item(i, owner, title, at, aud="private"):
    return I.from_scheduled({"id": i, "owner": owner, "audience": aud, "what": title, "run_at": at, "state": "pending",
                             "updated_at": 1.0})


ITEMS = {   # what each store hands each viewer: their own, and the household's (today's appear in the brief)
    "admin": [todo("1", "alex", "Alex's tax forms"), sched_item("s1", "alex", "Call Alex's bank", NOW_ITEMS + 3600),
              sched_item("s9", "alex", "Pick up the groceries", NOW_ITEMS + 9000, aud="household")],
    "adult": [todo("2", "robin", "Robin's library books"), sched_item("s2", "robin", "Robin's call with Hale", NOW_ITEMS + 5400),
              sched_item("s9", "alex", "Pick up the groceries", NOW_ITEMS + 9000, aud="household")],
    "kid": [todo("3", "ava", "Pack Ava's swim bag"), sched_item("s3", "ava", "Ava's swim lesson", NOW_ITEMS + 7200),
            sched_item("s9", "alex", "Pick up the groceries", NOW_ITEMS + 9000, aud="household")],
    "screen": [sched_item("s9", "alex", "Pick up the groceries", NOW_ITEMS + 9000, aud="household")]}


def parts(v):
    pid, role = VIEWERS[v]
    sched = S.schedule(SV.view(NODES, TODAY, TODAY + timedelta(days=1), pid, role, PEOPLE, {"Family"}, zone=LA),
                       pid, role, NOW, TODAY, LA)
    served = [c for c in CARDS if proactive.visible_to(c["to"], pid, role)]
    deck = S.cards(served, pid, role, NOW, DAY, safety_sources=proactive.SAFETY_SOURCES, ineligible=LEFT_OUT[v])
    its = ITEMS[v]
    items_now = I.resolve("now", pid, role, {"todos": lambda: [x for x in its if x["kind"] == "todo"],
                                             "scheduled": lambda: [x for x in its if x["kind"] == "scheduled"],
                                             "tasks": lambda: [], "chats": lambda: []}, now=NOW_ITEMS)
    return sched, items_now, deck


PRIVATE_ADULT = {"titles": ["Approve Alex's request", "Alex's dentist", "Alex's budget call", "Approve Alex's plumber quote?", "Alex's tax forms",
                            "Call Alex's bank", "Robin's parcel arrived", "Robin's library books", "Robin's run",
                            "Robin's call with Hale"],
                 "ids": ["e-dentist", "e-budget", "c-quote", "c-ok-", "c-parcel", "todo:1", "todo:2", "scheduled:s1", "scheduled:s2",
                         "e-run"]}

print("── the four viewers' briefs (S1) ──")
B = {}
for v, (pid, role) in VIEWERS.items():
    sched, items_now, deck = parts(v)
    B[v] = b = S.brief(sched, items_now, deck, pid, role, NOW, TODAY, weather={"place": "Home", "sky": "clear"})
    vector(f"brief.{v}", b)
    mine = {m["subject"]["id"] for s in (sched, items_now, deck) for m in members(s)}
    check(f"{v}: every member comes from this viewer's own parts", sorted({m["subject"]["id"] for m in members(b)} - mine), [])
    own_ops = {o["id"] for s in (sched, items_now, deck) for o in s["operations"]}
    check(f"{v}: every operation comes from this viewer's own parts", sorted({o["id"] for o in b["operations"]} - own_ops), [])

print("── privacy: kid and screen hold nothing of an adult's private items ──")
for v in ("kid", "screen"):
    text = json.dumps(B[v])
    check(f"{v}: no adult's private title anywhere", [t for t in PRIVATE_ADULT["titles"] if t in text], [])
    check(f"{v}: no adult's private id anywhere, card controls included (op-act/op-mute/op-later/op-done)",
          [i for i in PRIVATE_ADULT["ids"] if i in text], [])
    check(f"{v}: no excluded row from anyone else's choices", B[v]["explanation"]["excluded"], [])
check("adult: never the admin's private items or controls", [i for i in ("e-dentist", "c-quote", "todo:1", "scheduled:s1",
                                                                       "Alex's") if i in json.dumps(B["adult"])], [])
check("admin: never Robin's private card or to-do (Robin's run is on Robin's calendar the admin was given)",
      [i for i in ("c-parcel", "todo:2") if i in json.dumps(B["admin"])], [])
check("the kid's brief: Ballet and their own and the household's actions today, the bins tomorrow",
      sorted(m["subject"]["id"] for m in members(B["kid"])), sorted(["e-ballet", "scheduled:s3", "scheduled:s9", "e-bins"]))
check("the screen's brief: the household's only", sorted(m["subject"]["id"] for m in members(B["screen"])),
      sorted(["scheduled:s9", "e-bins"]))
check("excluded rows are each viewer's own: the admin's cap overflow and snooze, Robin's mute, with from",
      {v: B[v]["explanation"]["excluded"] for v in ("admin", "adult")},
      {"admin": [{"rule": "attention-cap", "count": 1}, {"rule": "snoozed", "count": 1, "from": "cards"}],
       "adult": [{"rule": "muted", "count": 1, "from": "cards"}]})
check("the admin's needs-you holds five, most urgent first (the plumber quote, then the requests)",
      [m["subject"]["id"] for m in members(B["admin"], "g-needs-you")], ["c-quote", "c-ok-1", "c-ok-2", "c-ok-3", "c-ok-4"])

print("── through the real route: an adult's private to-do and card never reach the kid or the screen ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
import todos  # noqa: E402

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
AVA, ava = pair("Ava Quarry", "kid")
TV, _ = pair("Living room", "household", "tvos")
ADMIN = hh.ADMIN_PERSON_ID
SV.for_viewer = lambda s, e, v_, r, only=None: {"start": s.isoformat(), "end": e.isoformat(), "people": [],
                                                "household": {"events": []}, "partial": False}
import local_time  # noqa: E402
import scheduled  # noqa: E402

late_today = datetime.combine(local_time.now().date() + timedelta(days=1), datetime.min.time(),
                              tzinfo=local_time.zone()).timestamp() - 120   # still today, wherever the clock is
todos.add("Alex's private tax forms", ADMIN)
scheduled.add(ADMIN, "Alex's private call with the bank", "create_reminder", {}, late_today)
scheduled.add(robin, "Robin's private call with Hale", "create_reminder", {}, late_today)
proactive.add_card("Approve Alex's private plumber quote?", "Admin only", urgency=2, source="approve-cap", to=[ADMIN])
proactive.add_card("Robin's private parcel", "Robin only", urgency=1, source="routine", to=[robin])
got = {nm: c.get("/v1/brief", headers=h) for nm, h in (("admin", M), ("adult", ROBIN), ("kid", AVA), ("screen", TV))}
check("every viewer gets 200 and brief@1", {nm: (r.status_code, r.json()["provenance"]["resolver"]) for nm, r in got.items()},
      {nm: (200, "brief@1") for nm in got})
check("the admin's brief has the admin's own private action today and decision (through the live readers)",
      [t for t in ("Alex's private call with the bank", "Approve Alex's private plumber quote?") if t in got["admin"].text],
      ["Alex's private call with the bank", "Approve Alex's private plumber quote?"])
check("Robin's brief has Robin's own private action today", "Robin's private call with Hale" in got["adult"].text, True)
for nm in ("kid", "screen"):
    check(f"{nm}: no adult's private to-do or card through the live readers", [t for t in ("Alex's private", "Robin's private")
                                                                               if t in got[nm].text], [])
check("adult: never the admin's private to-do or card", "Alex's private" in got["adult"].text, False)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
