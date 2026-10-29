#!/usr/bin/env python3
# gate: hermetic
"""The daily brief (#1060 migration step 4: `temporal`, GET /v1/brief), resolved once on titan from this viewer's own
schedule, items (view=now) and cards surfaces. S1: composed from the real golden vectors (schedule.alex, cards.admin)
and a real items surface; golden vector scripts/evalsets/surfaces/brief.alex.json (`--write` rewrites it). Rules:
needs-you / today / coming-up, members and operations verbatim from their own surfaces, nothing they don't hold, and
the boundary: titan's sources only, never a reminder (the Mac's EventKit Reminders never leave it). S2 through the
real route: never-see lists per viewer, the route composes its own routes' answers, deterministic.
Run: python3 scripts/test_surfaces_brief.py [--write]"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-brief-surface-"))
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


def load(name):
    return json.loads((VECTORS / f"{name}.json").read_text())


def members(s, gid=None):
    return [m for g in s.get("groups") or [] if gid in (None, g["id"]) for m in g["members"]]


NOW = 1791331200.0
TODAY = date(2026, 10, 6)
NOW_ITEMS = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc).timestamp()
late = I.from_scheduled({"id": "s1", "owner": "alex", "audience": "private", "what": "Return the library books",
                         "run_at": NOW_ITEMS - 3600, "state": "pending", "updated_at": 1.0})
soon = I.from_scheduled({"id": "s2", "owner": "alex", "audience": "private", "what": "Call the plumber",
                         "run_at": NOW_ITEMS + 3600, "state": "pending", "updated_at": 1.0})
nextweek = I.from_scheduled({"id": "s3", "owner": "alex", "audience": "private", "what": "Renew the parking permit",
                             "run_at": NOW_ITEMS + 3 * 86400, "state": "pending", "updated_at": 1.0})
waiting = I.from_task({"id": "t2", "goal": "Wait on the quote", "status": "running", "owner": "alex", "updated_at": "1",
                       "steps": [{"title": "a", "state": "blocked"}]})
base = {"todos": lambda: [I.from_todo({"id": "1", "title": "Buy stamps", "for": "alex", "created_at": 1.0, "done_at": None})],
        "scheduled": lambda: [late, soon, nextweek], "tasks": lambda: [waiting], "chats": lambda: []}
items_now = I.resolve("now", "alex", "adult", base, now=NOW_ITEMS)
sched, deck = load("schedule.alex"), load("cards.admin")
WX = {"place": "Home", "now": 61, "sky": "clear", "high": 68, "low": 50, "rain": 0, "unit": "F"}

print("── S1: composed from the real surfaces ──")
b = S.brief(sched, items_now, deck, "alex", "admin", NOW, TODAY, weather=WX)
vector("brief.alex", b)
G = {g["id"]: g for g in b["groups"]}
ids = lambda gid: [m["subject"]["id"] for m in G[gid]["members"]]  # noqa: E731
check("profile temporal, purpose orient, jb-1", (b["profile"], b["purpose"], b["profileVersion"]), ("temporal", "orient", "jb-1"))
check("groups: needs you, today (the focus), coming up", [(g["id"], g["role"], g.get("focus")) for g in b["groups"]],
      [("g-needs-you", "attention", None), ("g-today", "primary", True), ("g-coming-up", "supporting", None)])
check("needs you: the cards needing a decision and what waits on you, by priority, at most 5",
      sorted(ids("g-needs-you")), sorted([m["subject"]["id"] for m in members(deck, "g-needs-a-decision")]
                                         + [m["subject"]["id"] for m in members(items_now, "g-waiting-on-you")])[:5])
check("today: today's events, then what Now holds for today",
      ids("g-today"), [m["subject"]["id"] for m in members(sched, "g-day-2026-10-06") + members(items_now, "g-today")])
check("coming up: tomorrow's events, then what's due soon",
      ids("g-coming-up"), [m["subject"]["id"] for m in members(sched, "g-day-2026-10-07") + members(items_now, "g-due-soon")])
src = {m["subject"]["id"]: m for s in (sched, items_now, deck) for m in members(s)}
check("every member is verbatim from its own surface (nothing re-derived)",
      [m["subject"]["id"] for m in members(b) if src.get(m["subject"]["id"]) != m and
       not any(m == x for s in (sched, items_now, deck) for x in members(s))], [])
srcops = {o["id"]: o for s in (sched, items_now, deck) for o in s["operations"]}
check("every operation is verbatim from its own surface", [o["id"] for o in b["operations"] if srcops.get(o["id"]) != o], [])
check("every member's actions resolve", sorted({a for m in members(b) for a in m["slots"].get("actions", [])}
                                                - {o["id"] for o in b["operations"]}), [])
check("the header: the household date and titan's weather line", b["header"], {"date": "Tue Oct 6", "weather": WX})
check("sources: which resolvers it composed", b["sources"], {"schedule": "schedule@1", "items": "items@1", "cards": "cards@1"})
check("BOUNDARY: no reminder member (the Mac's Reminders never leave it)",
      sorted({m["subject"]["kind"] for m in members(b)} & {"reminder", "reminders"}), [])
check("BOUNDARY: no reminder or EventKit input or source",
      [x for x in b["provenance"]["inputs"] + list(b["sources"].values()) + [b["accessProjection"]["rule"]]
       if any(w in str(x).lower() for w in ("reminder", "eventkit"))], [])
check("BOUNDARY: the resolver takes titan's surfaces only (no reminders parameter)",
      sorted(S.brief.__code__.co_varnames[:S.brief.__code__.co_argcount + S.brief.__code__.co_kwonlyargcount]),
      sorted(["sched", "items_now", "deck", "person", "role", "now", "today", "weather"]))
many = [{"id": f"c-ok-{i}", "title": f"Approve request {i}", "source": "approve-cap", "kind": "info", "urgency": 2,
         "created_at": NOW - 100, "updated_at": NOW - 100 + i, "acked_at": None, "approval": True, "can_mute": True,
         "actions": [], "action_prompt": None, "detail": None} for i in range(7)]
many.append({**many[0], "id": "c-done-yday", "approval": False, "acked_at": NOW - 3 * 86400})
deck7 = S.cards(many, "alex", "admin", NOW, NOW - 3600, ineligible={"muted": 2, "snoozed": 1})
b7 = S.brief({}, items_now, deck7, "alex", "admin", NOW, TODAY)
n_wait = len(members(items_now, "g-waiting-on-you"))
check("the cards' own trace: done before today, muted, snoozed", deck7["explanation"]["excluded"],
      [{"rule": "done-before-today", "count": 1}, {"rule": "muted", "count": 2}, {"rule": "snoozed", "count": 1}])
check("the brief's trace: what the attention cap left out, then the cards' muted and snoozed (never done-before-today)",
      b7["explanation"]["excluded"],
      [{"rule": "attention-cap", "count": 7 + n_wait - 5}, {"rule": "muted", "count": 2, "from": "cards"},
       {"rule": "snoozed", "count": 1, "from": "cards"}])
check("no rows at all when nothing is left out", S.brief({}, {}, {}, "ava", "kid", NOW, TODAY)["explanation"]["excluded"], [])
check("deterministic (S2-9)", S.brief(sched, items_now, deck, "alex", "admin", NOW, TODAY, weather=WX), b)
e = S.brief({}, {}, {}, "ava", "kid", NOW, TODAY)
check("nothing anywhere: three empty groups, no weather", ([len(g["members"]) for g in e["groups"]], e["header"]["weather"]),
      ([0, 0, 0], None))

print("── S2: through the real route ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
import local_time  # noqa: E402
import proactive  # noqa: E402
import schedule_view as SV  # noqa: E402
import weather  # noqa: E402

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
today = local_time.now().date()
d0, d1 = today.isoformat(), (today + timedelta(days=1)).isoformat()
PEOPLE = [{"id": ADMIN, "name": "Alex", "role": "admin", "calendars": []},
          {"id": robin, "name": "Robin", "role": "adult", "calendars": ["Robin"]},
          {"id": ava, "name": "Ava", "role": "kid", "parents": [ADMIN, robin], "calendars": ["Ava"]}]


def ev(nid, title, start, cal, owner):
    return {"node_id": nid, "data": {"title": title, "start": start, "end": None, "calendar": cal,
                                     "provenance": {"owner": owner, "audience": "private"}}}


NODES = [ev("e1", "Dentist", f"{d0}T23:30:00", "Work", ADMIN), ev("e2", "Ballet", f"{d0}T23:00:00", "Ava", ADMIN),
         ev("e3", "Bins out", f"{d1}T20:00:00", "Family", ADMIN), ev("e4", "Robin's run", f"{d1}T07:00:00", "Robin", ADMIN)]
SV.for_viewer = lambda s, e, v, r, only=None: {**SV.view(NODES, s, e, v, r, PEOPLE, {"Family"}, only=only,
                                                         zone=local_time.zone()), "partial": False}
weather.today_summary = lambda **k: {"place": "Home", "sky": "clear"}
proactive.add_card("Approve the plumber quote?", "For the admin", urgency=2, source="approve-cap", to=[ADMIN])
proactive.add_card("Robin's parcel arrived", "Robin only", urgency=1, source="routine", to=[robin])
proactive.add_card("Bins tonight", "Everyone", urgency=1, source="routine", to="household")


def get(h):
    r = c.get("/v1/brief", headers=h)
    return r.status_code, r.json(), r.text


NEVER = {"admin": ["Robin's parcel"], "robin": ["Approve the plumber", "Dentist"],
         "ava": ["Approve the plumber", "Dentist", "Robin's parcel", "Robin's run"],
         "screen": ["Approve the plumber", "Dentist", "Robin's parcel", "Robin's run", "Ballet"]}
for nm, h in (("admin", M), ("robin", ROBIN), ("ava", AVA), ("screen", TV)):
    code, s, text = get(h)
    check(f"{nm}: 200, jb-1 brief", (code, s.get("profileVersion"), (s.get("provenance") or {}).get("resolver")),
          (200, "jb-1", "brief@1"))
    check(f"{nm}: never-see list (whole JSON)", [t for t in NEVER[nm] if t in text], [])
    check(f"{nm}: no reminder member", [m["subject"]["kind"] for m in members(s) if "reminder" in m["subject"]["kind"]], [])
code, a, _ = get(M)
check("the admin: their own events and Ava's Ballet today; tomorrow Robin's calendar on their Mac, then the bins",
      ([m["subject"]["id"] for m in members(a, "g-today")], [m["subject"]["id"] for m in members(a, "g-coming-up")]),
      (["e2", "e1"], ["e4", "e3"]))
check("Robin: their own calendar's run tomorrow, never the admin's dentist",
      [m["subject"]["id"] for m in members(get(ROBIN)[1])], ["e2", "e4", "e3"])
check("…the route composes exactly its own routes' answers for this caller",
      [m["subject"]["id"] for m in members(a)],
      [m["subject"]["id"] for m in members(S.brief(c.get(f"/v1/schedule?as=surface&start={d0}&end={d1}", headers=M).json(),
                                                   c.get("/v1/items?view=now", headers=M).json(),
                                                   c.get("/v1/proactive?as=surface", headers=M).json(),
                                                   ADMIN, "admin", 0, today))])
check("the household card is on everyone's deck, not a decision", any("Bins tonight" in m["slots"]["title"]
                                                                     for m in members(a, "g-needs-you")), False)
check("same state, same fingerprint (S2-9)", get(AVA)[1]["fingerprint"], get(AVA)[1]["fingerprint"])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
