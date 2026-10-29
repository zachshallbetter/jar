#!/usr/bin/env python3
# gate: hermetic
"""Cards carry their item (titan-node#1235): a card about an item the viewer may see gets `item: {id, project?}`,
decided per viewer each time cards are served (GET /v1/proactive, GET /v1/cards/wait), from the viewer's own item
readers; never stored on the card, never on a safety card, `project` only when the viewer can see it, and any lookup
that fails leaves it off. Views × viewers: Alex and Robin (adults), Ava (Robin's kid), a living-room screen, and the
admin. Fictional people; temporary stores; the task and record runtimes are fakes that project by owner as the real
ones do; no model, no push.
Run: python3 scripts/test_card_item_link.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-carditem-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db",
             "SCHEDULED_DB": "scheduled.db", "REFERENCES_DB": "references.db", "WORLD_DIR": "world",
             "LINKS_DB": "links.db", "LINK_SALT_FILE": "link_salt", "FENCES_DB": "fences.db",
             "SITTER_DB": "sitter.db", "LOCK_UP_DB": "lock_up.db", "TV_UNLOCK_DB": "tv_unlock.db", "PUSH_DB": "push.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily",
             "MAIL_BILLS_DB": "mail_bills.db", "ITEMS_LATENCY_LOG": "items-latency.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
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
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


import card_items as CI  # noqa: E402

print("── the link, from fake readers ──")


def item(i, kind, projects=(), **kw):
    return {"id": i, "kind": kind, "projects": list(projects), **kw}


calls: dict[str, int] = {}


def reader(name, rows):
    def run():
        calls[name] = calls.get(name, 0) + 1
        return [dict(r, projects=list(r["projects"])) for r in rows]
    return run


R = {"scheduled": reader("scheduled", [item("scheduled:s1", "scheduled", card_key="sched-s1"),
                                       item("scheduled:s9", "scheduled", ["pr-a"], card_key="sched-s5")]),
     "tasks": reader("tasks", [item("task:t1", "task", ["pr-a", "pr-b"])]),
     "routines": reader("routines", [item("routine:r1", "routine")]),
     "references": reader("references", [item("ref:f1", "file", ["pr-hidden"])]),
     "bills": reader("bills", [item("bill:acct-1:2026-10-01", "bill", time={"due": "2026-10-09T17:00:00Z"}),
                               item("bill:acct-1:2026-09-01", "bill", time={"due": "2026-09-09T17:00:00Z"})]),
     "todos": reader("todos", [item("todo:1", "todo")])}
OK = {"pr-a", "pr-b"}


def card(src, key=None, **kw):
    return {"id": f"pc-{src}-{key}", "source": src, "key": key, **kw}


cards = [card("scheduled", "sched-s1"), card("scheduled", "sched-s5"), card("tasks", "task-t1"),
         card("routine", "place-r1"), card("file-share-changed", "file-changed-f1", data={"ref_id": "f1"}),
         card("bill-due", "bill-due:k1"), card("titan", "sched-s1"), card("scheduled", None),
         card("lock-up", "task-t1"), card("alarm", "sched-s1"), card("tasks", "task-nope"),
         card("tasks", "tasks-2026-10-07-alex"), card("file-share-changed", "file-changed-f1", data={"ref_id": "f2"}),
         card("agent", "x", item={"id": "todo:1"})]
calls.clear()
out = CI.attach(cards, R, lambda p: p in OK, bill_row=lambda k: ("acct-1", "2026-10-09") if k == "k1" else None)
links = [c.get("item") for c in out]
check("each card names its item: scheduled by its card key (a repeat's later row too), task, routine, file, bill",
      links[:6], [{"id": "scheduled:s1"}, {"id": "scheduled:s9", "project": "pr-a"}, {"id": "task:t1"},
                  {"id": "routine:r1"}, {"id": "ref:f1"}, {"id": "bill:acct-1:2026-10-01"}])
check("…only from its own source: another source's look-alike key, a card with no key: none", links[6:8], [None, None])
check("safety cards never carry it, whatever their key says (Lock Up, alarms)", links[8:10], [None, None])
check("an item not in this viewer's reader: none (never derived from the card alone)", links[10], None)
check("a digest's key (tasks-<day>-<owner>) isn't an item", links[11], None)
check("a card whose two names for its file disagree: none", links[12], None)
check("an `item` a card arrived with is dropped (it's decided here, never stored)", links[13], None)
check("two visible projects: project left off (never picked); a project not visible: left off",
      (links[2], links[4]), ({"id": "task:t1"}, {"id": "ref:f1"}))
check("one pass per request: each reader the cards need, once (not once per card); never one they don't need",
      calls, {"scheduled": 1, "tasks": 1, "routines": 1, "references": 1, "bills": 1})
calls.clear()
CI.attach([card("titan", "x"), card("lock-up", "lock-up:r1"), card("bill-due", "bill-due:k9")], R, lambda p: True,
          bill_row=lambda k: None)
check("a list with nothing linkable reads no store at all", calls, {})

two = dict(R, scheduled=reader("scheduled", [item("scheduled:a", "scheduled", card_key="sched-x"),
                                             item("scheduled:b", "scheduled", card_key="sched-x")]))
check("two items match: none (never guessed)", CI.attach([card("scheduled", "sched-x")], two)[0].get("item"), None)
import proactive as P  # noqa: E402
check("no source that names an item is a safety source", sorted(set(CI.SOURCES) & P.SAFETY_SOURCES), [])
real_safety = P.is_safety_source
P.is_safety_source = lambda src: src == "scheduled" or real_safety(src)   # the one safety test (#1289 review)
try:
    check("the safety check runs before any mapping (a safety source never links, even one that would)",
          CI.attach([card("scheduled", "sched-s1")], R)[0].get("item"), None)
finally:
    P.is_safety_source = real_safety
check("a safety source in any case or spacing never links ('Lock-Up')",
      CI.attach([card(" Lock-Up ", "sched-s1")], R)[0].get("item"), None)
marked = dict(R, scheduled=reader("scheduled", [item("scheduled:s1", "scheduled", card_key="sched-s1", safety=True)]))
check("an item its adapter marks safety: none", CI.attach([card("scheduled", "sched-s1")], marked)[0].get("item"), None)


def boom():
    raise OSError("store down")


down = dict(R, scheduled=boom)
got = CI.attach([card("scheduled", "sched-s1"), card("tasks", "task-t1")], down, lambda p: p in OK)
check("a reader failure leaves its cards without `item`; the others still carry theirs",
      [c.get("item") for c in got], [None, {"id": "task:t1"}])
got = CI.attach([card("scheduled", "sched-s5")], R, lambda p: (_ for _ in ()).throw(OSError("spaces down")))
check("a project lookup failure leaves `project` off (the item stays)", got[0].get("item"), {"id": "scheduled:s9"})
got = CI.attach([card("bill-due", "bill-due:k1")], R, bill_row=lambda k: (_ for _ in ()).throw(OSError("db locked")))
check("a bill-due lookup failure: none", got[0].get("item"), None)
check("no project_ok at all: never a project", CI.attach([card("scheduled", "sched-s5")], R)[0].get("item"),
      {"id": "scheduled:s9"})

print("── the route: views × viewers ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import bill_due  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive as P  # noqa: E402
import projects  # noqa: E402
import push  # noqa: E402
import references  # noqa: E402
import scheduled  # noqa: E402
import spaces  # noqa: E402

push.push_card_async = lambda card: None   # never a real push from a test
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ALEX, alex = pair("Alex Hale", "adult", "Alex's iPhone")
ROBIN, robin = pair("Robin Hale", "adult", "Robin's iPhone")
AVA, ava = pair("Ava Hale", "kid", "Ava's iPhone")
TV, _ = pair("Living room", "household", "Living room TV", platform="tvos")
c.patch(f"/v1/people/{ava}", json={"parents": [robin]}, headers=M)
admin = hh.ADMIN_PERSON_ID
VIEW = {"alex": ALEX, "robin": ROBIN, "ava": AVA, "tv": TV, "admin": M}

tomorrow = time.time() + 86400
# Alex's private reminder, filed under his private project; a household one in his private project, another in an
# adults project; a later row of a repeating series (it keeps the first row's card key)
mine = scheduled.add(alex, "Renew the passport", "notify", {"text": "x"}, tomorrow)
h_priv = scheduled.add(alex, "Water the plants", "notify", {"text": "x"}, tomorrow, audience="household")
h_adults = scheduled.add(alex, "Bins out", "notify", {"text": "x"}, tomorrow, audience="household")
series = scheduled.add(alex, "Feed the fish", "notify", {"text": "x"}, tomorrow, audience="household",
                       card_key="sched-first-of-series")
safe1 = scheduled.add(alex, "Porch light on", "notify", {"text": "x"}, tomorrow, audience="household")
safe2 = scheduled.add(alex, "Porch light off", "notify", {"text": "x"}, tomorrow, audience="household")
p_alex = spaces.create_project(alex, "adult", "Alex's paperwork")["id"]
p_adults = spaces.create_project(alex, "adult", "House jobs", audience="adults")["id"]
projects.file(alex, "adult", f"scheduled:{mine['id']}", p_alex)
projects.file(alex, "adult", f"scheduled:{h_priv['id']}", p_alex)
projects.file(alex, "adult", f"scheduled:{h_adults['id']}", p_adults)
# a task of Alex's (the task runtime is a fake; tasks_for projects through the real policy, lib.task_visible: own,
# shared, own kids'; the admin the same as any adult since #1239; none for a screen)
TASKS = [{"id": "t-alex", "goal": "Plan the trip", "status": "running", "owner": alex, "updated_at": "1",
          "steps": [{"title": "a", "state": "current"}]}]
lib.tasks_for = lambda viewer, role: {"tasks": [t for t in TASKS if lib.task_visible({"metadata": {"owner": t["owner"]}}, viewer, role)]}
# a file of Alex's (references from his own device)
ref = references.put(alex, "adult", "dev-alex", {"kind": "file", "native_id": "n-1", "title": "Lease.pdf"})
# a bill of Alex's (the record runtime is a fake; obligations and _node_visible are real), and its bill-due row
in3 = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
NODES = {"bill": [{"node_id": "b-alex", "node_type": "bill",
                   "data": {"doc_type": "bill", "vendor": "Harbor Water", "amount": 48.2, "due_date": in3,
                            "mail_date": datetime.now(timezone.utc).date().isoformat(), "provenance": {"owner": alex, "audience": "private"}}}], "receipt": []}
lib.record_nodes = lambda t, **k: (list(NODES.get(t, [])), True)
who_alex = hh.Identity({"id": alex, "name": "Alex Hale", "role": "adult"}, None, "adult", "test")
alex_bills = api._item_readers(who_alex, lib._local_now())["bills"]()
bill_id = alex_bills[0]["id"] if alex_bills else "bill:?:?"
account = bill_id[len("bill:"):].rsplit(":", 1)[0]
bcon = bill_due._db()
bcon.execute("INSERT INTO bill_due_cards VALUES (?,?,?,?,?,?)", ("k-alex", alex, account, in3, "sent", time.time()))
bcon.commit()
bcon.close()
check("(setup) Alex's bill is in his own bills reader", len(alex_bills), 1)

# The cards. Each about-an-item card goes to the whole household, so every viewer sees the card itself and only the
# item's own audience decides `item`.
HH = "household"
P.add_card("Passport reminder set", "", source="scheduled", to=HH, key=mine["card_key"])
P.add_card("Plants reminder set", "", source="scheduled", to=HH, key=h_priv["card_key"])
P.add_card("Bins reminder set", "", source="scheduled", to=HH, key=h_adults["card_key"])
P.add_card("Fish, next feed", "", source="scheduled", to=HH, key="sched-first-of-series")
P.add_card("Trip: step done", "", kind="update", source="tasks", to=HH, key="task-t-alex")
P.add_card("“Lease.pdf” changed", "", source="file-share-changed", to=HH, key=f"file-changed-{ref['id']}",
           data={"kind": "file_changed", "ref_id": ref["id"]})
P.add_card("Harbor Water is due", "", source="bill-due", to=HH, key="bill-due:k-alex",
           data={"kind": "bill_due", "key": "k-alex", "due": in3})
P.add_card("Lock up?", "", source="lock-up", to=HH, key=safe1["card_key"])   # a safety card, even with an item's key
P.add_card("Alarm is set", "", source="alarm", to=HH, key=safe2["card_key"])
P.add_card("Weekly verification", "", source="verification", to=HH, key="verify-weekly")


def served(view, path="/v1/proactive"):
    r = c.get(path, headers=VIEW[view], params={"since": 0} if "wait" in path else None)
    return {x["title"]: x.get("item") for x in r.json().get("cards", [])} if r.status_code == 200 else r.status_code


S = {v: served(v) for v in VIEW}
check("every viewer sees every card itself (the household audience)", {v: len(s) for v, s in S.items()},
      {v: 10 for v in VIEW})
check("Alex's private item: its card carries `item` (with his project) for him",
      S["alex"]["Passport reminder set"], {"id": f"scheduled:{mine['id']}", "project": p_alex})
check("…and not for another adult, a kid, a shared screen or the admin",
      [S[v]["Passport reminder set"] for v in ("robin", "ava", "tv", "admin")], [None, None, None, None])
check("a household item: `item` for everyone, the screen too",
      [S[v]["Plants reminder set"]["id"] if S[v]["Plants reminder set"] else None for v in VIEW],
      [f"scheduled:{h_priv['id']}"] * 5)
check("…its project (Alex's private one) only for Alex: left off for everyone who can't see it",
      {v: "project" in (S[v]["Plants reminder set"] or {}) for v in VIEW},
      {"alex": True, "robin": False, "ava": False, "tv": False, "admin": False})
check("an adults project: shown to the adults, left off for a kid and the screen",
      {v: (S[v]["Bins reminder set"] or {}).get("project") for v in VIEW},
      {"alex": p_adults, "robin": p_adults, "ava": None, "tv": None, "admin": p_adults})
check("a repeating series' card: the row that's pending now", S["robin"]["Fish, next feed"],
      {"id": f"scheduled:{series['id']}"})
check("Alex's task, file and bill: his",
      [S["alex"][t] for t in ("Trip: step done", "“Lease.pdf” changed", "Harbor Water is due")],
      [{"id": "task:t-alex"}, {"id": f"ref:{ref['id']}"}, {"id": bill_id}])
check("…the file and the bill for nobody else",
      {v: [S[v][t] for t in ("“Lease.pdf” changed", "Harbor Water is due")] for v in VIEW if v != "alex"},
      {v: [None, None] for v in VIEW if v != "alex"})
check("…the task only where Now shows it too: another adult's private task for nobody else, the admin included "
      "(#1239, Zach 10-08)", {v: S[v]["Trip: step done"] for v in VIEW if v != "alex"},
      {"robin": None, "ava": None, "tv": None, "admin": None})
check("safety cards never carry it, for anyone (Lock Up, alarm), whatever key they hold",
      {v: (S[v]["Lock up?"], S[v]["Alarm is set"]) for v in VIEW}, {v: (None, None) for v in VIEW})
check("a card about no item: none", {v: S[v]["Weekly verification"] for v in VIEW}, {v: None for v in VIEW})
check("absent, never null: no card says `item: null`",
      [x for x in c.get("/v1/proactive", headers=ROBIN).json()["cards"] if "item" in x and not x["item"]], [])
W = served("alex", "/v1/cards/wait")
check("the long-poll serves the same per-viewer link", (W["Passport reminder set"], served("robin", "/v1/cards/wait")
                                                       ["Passport reminder set"]),
      ({"id": f"scheduled:{mine['id']}", "project": p_alex}, None))

con = sqlite3.connect(os.environ["PROACTIVE_DB"])
cols = [r[1] for r in con.execute("PRAGMA table_info(cards)")]
blobs = [r[0] for r in con.execute("SELECT data FROM cards")]
con.close()
check("never stored on the card: no column, no `item` in any card's data",
      ("item" in cols, [b for b in blobs if b and "\"item\"" in b]), (False, []))

print("── failures and cost ──")
real_list_for = scheduled.list_for
scheduled.list_for = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
try:
    r = c.get("/v1/proactive", headers=ALEX)
    s_ = {x["title"]: x.get("item") for x in r.json()["cards"]}
    check("the scheduled store down: still 200, its cards without `item`, the task's still there",
          (r.status_code, s_["Passport reminder set"], s_["Plants reminder set"], s_["Trip: step done"]),
          (200, None, None, {"id": "task:t-alex"}))
finally:
    scheduled.list_for = real_list_for
real_readers = api._item_readers
api._item_readers = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("readers unavailable"))
try:
    r = c.get("/v1/proactive", headers=ALEX)
    check("no readers at all: still 200, every card, none with `item`",
          (r.status_code, len(r.json()["cards"]), [x["title"] for x in r.json()["cards"] if "item" in x]), (200, 10, []))
finally:
    api._item_readers = real_readers
real_spaces_get = spaces.get_project
spaces.get_project = lambda *a, **k: (_ for _ in ()).throw(OSError("spaces down"))
try:
    r = c.get("/v1/proactive", headers=ALEX)
    s_ = {x["title"]: x.get("item") for x in r.json()["cards"]}
    check("the project lookup down: `item` without `project`", s_["Passport reminder set"],
          {"id": f"scheduled:{mine['id']}"})
finally:
    spaces.get_project = real_spaces_get

counted: dict[str, int] = {}


def counting(ident, now_dt):
    rs = real_readers(ident, now_dt)

    def wrap(n, f):
        def run():
            counted[n] = counted.get(n, 0) + 1
            return f()
        return run
    return {n: wrap(n, f) for n, f in rs.items()}


api._item_readers = counting
try:
    c.get("/v1/proactive", headers=ALEX)
    check("one request, ten cards: each reader the cards need, once", counted,
          {"scheduled": 1, "tasks": 1, "references": 1, "bills": 1})
    counted.clear()
    c.get("/v1/proactive", headers=ALEX, params={"since": time.time() + 60})
    check("no cards: no reader runs", counted, {})
finally:
    api._item_readers = real_readers

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
