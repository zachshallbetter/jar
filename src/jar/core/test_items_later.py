#!/usr/bin/env python3
# gate: hermetic
"""Later on Now's items (titan-node#1233): view=now members of kind todo, scheduled and task carry a per-viewer
`later` op ({until}); POST /v1/items/{id}/later hides that item from the caller's own Now until then, the way a card's
Later does (#1039). Checked here: views × viewers (one adult's Later never hides the item for another adult, a kid or
the shared screen), expiry on a pinned clock (back if still current; a done to-do stays gone), the op only on those
kinds, only in Now, never on a screen or a safety item, and a bad `until` refused with card snooze's own bounds. The
iPhone's exact body ({"until": ISO 8601}, with and without an offset) is posted to the op's own route.
Fictional people (Alex and Robin, adults; Ava, a kid; a living-room screen); throwaway stores; no model.
Run: python3 scripts/test_items_later.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-items-later-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db",
             "SCHEDULED_DB": "scheduled.db", "REFERENCES_DB": "references.db", "WORLD_DIR": "world",
             "LINKS_DB": "links.db", "LINK_SALT_FILE": "link_salt", "FENCES_DB": "fences.db",
             "SITTER_DB": "sitter.db", "LOCK_UP_DB": "lock_up.db", "TV_UNLOCK_DB": "tv_unlock.db", "PUSH_DB": "push.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily",
             "ITEMS_LATENCY_LOG": "items-latency.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL", "APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


import items as I  # noqa: E402

print("── the resolver ──")
NOW = datetime(2026, 10, 8, 17, 0, tzinfo=timezone.utc).timestamp()


def todo(i, owner, title, done=False):
    return I.from_todo({"id": i, "title": title, "for": owner, "created_at": 1.0, "done_at": 2.0 if done else None})


def sched(i, owner, title, at, aud="private", tool="notify", args=None):
    return I.from_scheduled({"id": i, "owner": owner, "audience": aud, "what": title, "run_at": at, "state": "pending",
                             "updated_at": 1.0, "tool": tool, "args": args or {"text": "x"}})


def base():
    return {
        "todos": lambda: [todo("1", "alex", "Buy stamps"), todo("2", "household", "Bins out")],
        "scheduled": lambda: [sched("s1", "alex", "Call the plumber", NOW + 3600, "household"),
                              sched("s2", "alex", "Lock the back door", NOW + 7200, "household", "home_control",
                                    {"entity_id": "lock.back_door", "service": "lock"})],
        "tasks": lambda: [I.from_task({"id": "t1", "goal": "Plan the trip", "status": "running", "owner": "alex",
                                       "updated_at": "1", "steps": [{"title": "a", "state": "current"}]})],
        "chats": lambda: [I.from_chat({"id": "c1", "title": "Garden ideas", "last_at": "2026-10-08T16:00:00Z"},
                                      "alex", None)],
        "routines": lambda: [I.from_routine({"id": "r1", "name": "Bedtime", "owner": "alex"},
                                            "2026-10-08T19:00:00Z")],
        "events": lambda: [I.from_event({"id": "e1", "title": "Soccer at Harbor Park", "start": "2026-10-08T19:30:00Z"})],
        "references": lambda: [I.from_reference({"id": "f1", "title": "Lease.pdf", "owner": "alex",
                                                 "dates": {"modified": NOW - 60}})],
        "notes": lambda: [I.from_note({"source_id": "n1", "title": "Wifi", "metadata": {"saved": "2026-10-08T15:00:00Z"}},
                                      "alex", "private")],
    }


def later_subjects(surface):
    return sorted(o["subject"]["id"] for o in surface["operations"] if o["verb"] == "later")


def member_ids(surface):
    return sorted(m["subject"]["id"] for g in surface["groups"] for m in g["members"])


now_a = I.resolve("now", "alex", "adult", base(), now=NOW)
check("Now: Later on the to-dos, the scheduled action and the task, nothing else (no chat, routine, event, file, "
      "note) and never the lock (a safety item)", later_subjects(now_a),
      ["scheduled:s1", "task:t1", "todo:1", "todo:2"])
check("…the safety row is in Now all the same, marked safety", ("scheduled:s2" in member_ids(now_a),
                                                                 I.is_safety(sched("x", "a", "b", NOW, tool="home_control",
                                                                                   args={"entity_id": "alarm_control_panel.home"}))),
      (True, True))
op = next(o for o in now_a["operations"] if o["id"] == "op-later-todo:2")
check("the op: route, contract, reversible", (op["route"], op["contract"], op["reversibility"], op["verb"]),
      ({"method": "POST", "path": "/v1/items/todo:2/later"}, "jellybean.todo.later@1", "reversible", "later"))
check("…and it's in its member's actions", any("op-later-todo:2" in m["slots"]["actions"]
                                               for g in now_a["groups"] for m in g["members"]
                                               if m["subject"]["id"] == "todo:2"), True)
check("a shared screen: no Later anywhere", later_subjects(I.resolve("now", None, "household", base(), now=NOW)), [])
check("a screen even when a viewer id is passed (role household reads as nobody)",
      later_subjects(I.resolve("now", "alex", "household", base(), now=NOW)), [])
kid_readers = {**base(), "todos": lambda: [todo("2", "household", "Bins out"), todo("4", "ava", "Spelling list")],
               "tasks": lambda: []}   # a kid's own readers: Alex's private to-do and task aren't theirs to see
check("a kid has Later on what they see", later_subjects(I.resolve("now", "ava", "kid", kid_readers, now=NOW)),
      ["scheduled:s1", "todo:2", "todo:4"])
for v in ("time", "unsorted", "person:alex"):
    kw = {"start": "2026-10-08", "end": "2026-10-09"} if v == "time" else {}
    check(f"view={v}: no Later", later_subjects(I.resolve(v, "alex", "adult", base(), now=NOW,
                                                          people={"alex"}, **kw)), [])
check("a done to-do: no Later", I.may_later(todo("3", "alex", "Old", True), "alex", "adult"), False)
check("may_later: no viewer, a screen, other kinds", (I.may_later(todo("1", "alex", "x"), None, "adult"),
                                                      I.may_later(todo("1", "alex", "x"), "alex", "household"),
                                                      I.may_later({"kind": "bill", "state": {"is": "open"}}, "alex", "adult"),
                                                      I.may_later({"kind": "mail", "state": {"is": "open"}}, "alex", "adult")),
      (False, False, False, False))

hidden = I.resolve("now", "alex", "adult", base(), now=NOW, snoozed={"todo:2", "task:t1"})
check("snoozed: gone from this viewer's Now (members, moving list, sentence count)",
      ("todo:2" in member_ids(hidden), "task:t1" in member_ids(hidden), hidden["sentence"]["facts"]["moving"],
       hidden["explanation"]["excluded"]), (False, False, 0, [{"rule": "later", "count": 2}]))
check("…and the rest of Now is unchanged", sorted(set(member_ids(now_a)) - {"todo:2", "task:t1"}), member_ids(hidden))
check("a snoozed set never hides anything in another view (person page, Time)", (
      "todo:1" in member_ids(I.resolve("person:alex", "alex", "adult", base(), now=NOW, people={"alex"},
                                       snoozed={"todo:1"})),
      "scheduled:s1" in member_ids(I.resolve("time", "alex", "adult", base(), now=NOW, start="2026-10-08",
                                             end="2026-10-09", snoozed={"scheduled:s1"}))), (True, True))
check("…nor on a screen (it has no snoozes of its own)", "todo:2" in member_ids(
      I.resolve("now", None, "household", base(), now=NOW, snoozed={"todo:2"})), True)
check("the fingerprint moves when an item is put off", hidden["fingerprint"] != now_a["fingerprint"], True)

print("── the route: views × viewers ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive as P  # noqa: E402
import push  # noqa: E402
import scheduled  # noqa: E402
import todos  # noqa: E402
import world as _world  # noqa: E402

push.push_card_async = lambda card: None   # never a real push from a test
api.ITEMS_LATENCY_LOG = _TMP / "items-latency.jsonl"
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}

# a pinned clock: the view and the route both read the household's now through _local_now
TZ = timezone(timedelta(hours=-7))
CLOCK = {"now": datetime(2026, 10, 8, 10, 0, tzinfo=TZ)}
lib._local_now = lambda: CLOCK["now"]
T0 = CLOCK["now"].timestamp()

# the runtimes are fakes: no KB, no records, no calendar; the task runtime projects by owner
lib.kb_sources = lambda *a, **k: []
lib._runtime_request = lambda *a, **k: {"chunks": []}
lib.record_nodes = lambda t, **k: ([], True)
_world.household_calendars = lambda *a, **k: {"family"}


def pair(name, role, device, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ALEX, alex = pair("Alex Hale", "adult", "Alex's iPhone")
ROBIN, robin = pair("Robin Hale", "adult", "Robin's iPhone")
AVA, ava = pair("Ava Hale", "kid", "Ava's iPhone")
TV, _ = pair("Living room", "household", "Living room TV", platform="tvos")
TASKS = [{"id": "trip", "goal": "Plan the trip", "status": "running", "owner": alex, "updated_at": "1",
          "steps": [{"title": "a", "state": "current"}]}]
lib.tasks_for = lambda viewer, role: {"tasks": [t for t in TASKS if viewer and t["owner"] == viewer]}

bins = "todo:" + todos.add("Bins out tonight", "household", created_by="alex")["id"]
vet = "todo:" + todos.add("Call the vet", alex, created_by="alex")["id"]
robins = "todo:" + todos.add("Robin's own errand", robin, created_by="robin")["id"]
plants = "scheduled:" + scheduled.add(alex, "Water the plants", "notify", {"text": "x"}, T0 + 3600,
                                      audience="household")["id"]
# a row older than scheduled.refusal's hands-off rule: shown, never with Later (jellybean#927's safety rule)
lockrow = "scheduled:" + scheduled.add(alex, "Lock the front door", "home_control",
                                       {"entity_id": "lock.front_door", "service": "lock"}, T0 + 7200,
                                       audience="household")["id"]
task = "task:trip"


def now_view(h):
    return c.get("/v1/items?view=now", headers=h).json()


def shown(h):
    return set(member_ids(now_view(h)))


def later_ops(h):
    return {o["subject"]["id"]: o for o in now_view(h)["operations"] if o["verb"] == "later"}


a_ops = later_ops(ALEX)
check("Alex's Now: Later on the household to-do, his own to-do, the scheduled action and his task; not the lock",
      sorted(a_ops), sorted([bins, vet, plants, task]))
check("…the lock row is in his Now, without Later", lockrow in shown(ALEX), True)
check("…every later op is POST /v1/items/<id>/later, reversible",
      all(o["route"] == {"method": "POST", "path": f"/v1/items/{i}/later"} and o["reversibility"] == "reversible"
          for i, o in a_ops.items()), True)
check("the screen's Now: the household's items, no Later at all", (bins in shown(TV), plants in shown(TV),
                                                                    later_ops(TV)), (True, True, {}))
check("Ava (a kid) has Later on what she sees", sorted(later_ops(AVA)), sorted([bins, plants]))
check("Robin's Now: Later on the household's and his own", sorted(later_ops(ROBIN)), sorted([bins, plants, robins]))

before = {n: shown(h) for n, h in (("robin", ROBIN), ("ava", AVA), ("tv", TV))}
# the iPhone's exact body: {"until": "<ISO8601>"} with an offset, posted to the op's own route
until_iso = (CLOCK["now"] + timedelta(hours=3)).isoformat()
r = c.post(a_ops[bins]["route"]["path"], json={"until": until_iso}, headers=ALEX)
check("Alex: Later on the household to-do with {until: ISO 8601 with an offset} → 200, snoozed_until",
      (r.status_code, r.json()), (200, {"id": bins, "snoozed_until": T0 + 3 * 3600}))
naive = (CLOCK["now"] + timedelta(hours=2)).astimezone(timezone.utc).replace(tzinfo=None).isoformat()
r = c.post(a_ops[task]["route"]["path"], json={"until": naive}, headers=ALEX)
check("…{until: ISO 8601 without an offset} (read as UTC, as card snooze reads it) → 200",
      (r.status_code, r.json().get("snoozed_until")), (200, T0 + 2 * 3600))
r = c.post(a_ops[vet]["route"]["path"], json={"until": T0 + 3600}, headers=ALEX)
check("…epoch seconds, as card snooze takes them → 200", r.status_code, 200)
a_now = shown(ALEX)
check("Alex's Now: the three he put off are gone; the scheduled action and the lock remain",
      (bins in a_now, task in a_now, vet in a_now, plants in a_now, lockrow in a_now), (False, False, False, True, True))
check("…his sentence no longer counts the task as moving", now_view(ALEX)["sentence"]["facts"]["moving"], 0)
check("Robin's, Ava's and the screen's Now are unchanged",
      {n: shown(h) for n, h in (("robin", ROBIN), ("ava", AVA), ("tv", TV))}, before)
check("…the household to-do still carries Later for Robin", bins in later_ops(ROBIN), True)
check("Alex's other views are unchanged: the task is still on his person page",
      task in member_ids(c.get(f"/v1/items?view=person:{alex}", headers=ALEX).json()), True)

r = c.post(f"/v1/items/{plants}/later", json={"until": (CLOCK["now"] + timedelta(hours=1)).isoformat()}, headers=AVA)
check("Ava (a kid) puts the scheduled action off → 200", r.status_code, 200)
check("…gone from her Now only", (plants in shown(AVA), plants in shown(ALEX), plants in shown(ROBIN), plants in shown(TV)),
      (False, True, True, True))

print("── refused ──")
r = c.post(f"/v1/items/{bins}/later", json={"until": until_iso}, headers=TV)
check("the shared screen → 403", r.status_code, 403)
check("…and nothing was hidden for anyone by it", bins in shown(ROBIN), True)
check("someone else's private to-do (Robin's, for Alex) → 404",
      c.post(f"/v1/items/{robins}/later", json={"until": until_iso}, headers=ALEX).status_code, 404)
check("an id that doesn't exist → 404",
      c.post("/v1/items/todo:nope/later", json={"until": until_iso}, headers=ALEX).status_code, 404)
r = c.post(f"/v1/items/{lockrow}/later", json={"until": until_iso}, headers=ALEX)
check("a safety item (the lock row) → 403 cannot_snooze", (r.status_code, r.json().get("error")), (403, "cannot_snooze"))
for kind_id in ("chat:c1", "bill:acct:2026-10-01", "event:e1", "mail:m1", "routine:r1", "note:n1", "ref:f1"):
    r = c.post(f"/v1/items/{kind_id}/later", json={"until": until_iso}, headers=ALEX)
    check(f"{kind_id.split(':')[0]}: no Later → 400 not_snoozable", (r.status_code, r.json().get("error")),
          (400, "not_snoozable"))

card = P.add_card("Pick up the dry cleaning", kind="reminder", source="todos", to="adults")
BAD = [("missing", {}), ("empty", {"until": ""}), ("words", {"until": "tomorrow"}), ("a bool", {"until": True}),
       ("past", {"until": (CLOCK["now"] - timedelta(minutes=1)).isoformat()}), ("now", {"until": T0}),
       ("31 days out", {"until": (CLOCK["now"] + timedelta(days=31)).isoformat()}), ("a list", {"until": [1]})]
for label, body in BAD:
    r = c.post(f"/v1/items/{plants}/later", json=body, headers=ROBIN)
    check(f"until {label} → 400 bad_until", (r.status_code, r.json().get("error")), (400, "bad_until"))
r = c.post(f"/v1/items/{plants}/later", content=b'{"until": NaN}', headers={**ROBIN, "content-type": "application/json"})
check("until NaN → 400 bad_until", (r.status_code, r.json().get("error")), (400, "bad_until"))
r = c.post(f"/v1/items/{plants}/later", json={"until": (CLOCK["now"] + timedelta(days=30)).isoformat()}, headers=ROBIN)
check("exactly 30 days out → 200 (card snooze's own bound)", r.status_code, 200)
check("card snooze refuses the same bad until with the same error", c.post(
      f"/v1/proactive/{card['id']}/snooze", json={"until": "tomorrow"}, headers=ROBIN).json().get("error"), "bad_until")
check("the bound is card snooze's", P.SNOOZE_MAX, 30 * 86400.0)
check("route allowlist: people yes, a screen no",
      [hh.route_allowed(r_, "POST", f"/v1/items/{bins}/later") for r_ in ("adult", "kid", "household")],
      [True, True, False])

print("── expiry (the pinned clock) ──")
check("the store: Alex's snoozes now", P.snoozed_items(alex, T0), {bins, task, vet})
check("…another person's are their own", (P.snoozed_items(robin, T0), P.snoozed_items(ava, T0)), ({plants}, {plants}))
todos.complete(vet.split(":", 1)[1], "Alex")   # done while put off: it never comes back
CLOCK["now"] += timedelta(hours=1, seconds=1)
check("+1 h: the vet to-do's snooze ended, but it's done: not back", vet in shown(ALEX), False)
check("…Ava's snooze on the scheduled action has ended too", P.snoozed_items(ava, CLOCK["now"].timestamp()), set())
check("…Alex's task and household to-do still hidden", (task in shown(ALEX), bins in shown(ALEX)), (False, False))
CLOCK["now"] += timedelta(hours=1)
check("+2 h: the task is back on Alex's Now, Later on it again", (task in shown(ALEX), task in later_ops(ALEX)),
      (True, True))
check("…the household to-do still hidden", bins in shown(ALEX), False)
CLOCK["now"] = datetime.fromtimestamp(T0 + 3 * 3600, TZ)
check("at exactly until: back (until is the moment it returns)", bins in shown(ALEX), True)
check("…nobody else's Now changed at any step", (bins in shown(ROBIN), bins in shown(AVA), bins in shown(TV)),
      (True, True, True))
r = c.post(f"/v1/items/{bins}/later", json={"until": (CLOCK["now"] + timedelta(hours=1)).isoformat()}, headers=ALEX)
check("Later again replaces the old snooze", (r.status_code, bins in shown(ALEX)), (200, False))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
