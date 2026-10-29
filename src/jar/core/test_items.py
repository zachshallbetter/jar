#!/usr/bin/env python3
# gate: hermetic
"""The items surface (#1084): GET /v1/items for every view × {admin, another adult, a kid, a shared screen}, through
the real route and the real stores (todos, scheduled actions, references, chats, projects, KB notes), plus the
resolver's own rules. The privacy rule: every member, count, group and string is built only from what the viewer can
see, so each viewer has a never-see list (other people's private titles and ids) checked against the whole JSON of
every view. Fictional people (Alex and Robin, adults; Ava, Robin's kid; a living-room screen); temporary stores; the
KB runtime and the task runtime are fakes that project by owner as the real ones do; no model.
Run: python3 scripts/test_items.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-items-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db",
             "SCHEDULED_DB": "scheduled.db", "REFERENCES_DB": "references.db", "WORLD_DIR": "world",
             "LINKS_DB": "links.db", "LINK_SALT_FILE": "link_salt", "FENCES_DB": "fences.db",
             "SITTER_DB": "sitter.db", "LOCK_UP_DB": "lock_up.db", "TV_UNLOCK_DB": "tv_unlock.db", "PUSH_DB": "push.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
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
NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc).timestamp()


def todo(i, owner, title, done=False):
    return I.from_todo({"id": i, "title": title, "for": owner, "created_at": 1.0, "done_at": 2.0 if done else None})


def sched(i, owner, title, at, aud="private"):
    return I.from_scheduled({"id": i, "owner": owner, "audience": aud, "what": title, "run_at": at, "state": "pending",
                             "updated_at": 1.0})


late = sched("s1", "alex", "Return the library books", NOW - 3600)
soon = sched("s2", "alex", "Call the plumber", NOW + 3600)
next_week = sched("s3", "alex", "Dentist reminder", NOW + 3 * 86400)
moving = I.from_task({"id": "t1", "goal": "Plan the trip", "status": "running", "owner": "alex", "updated_at": "1",
                      "steps": [{"title": "a", "state": "done"}, {"title": "b", "state": "current"}, {"title": "c", "state": "pending"}]})
waiting = I.from_task({"id": "t2", "goal": "Wait on the quote", "status": "running", "owner": "alex", "updated_at": "1",
                       "steps": [{"title": "a", "state": "blocked"}]})
theirs = I.from_task({"id": "t3", "goal": "Fence quote", "status": "running", "owner": "robin", "updated_at": "1",
                      "steps": [{"title": "a", "state": "blocked"}]})
chat = I.from_chat({"id": "c1", "title": "Garden ideas", "last_at": "2026-10-06T14:00:00Z"}, "alex", None)
filed = I.from_chat({"id": "c2", "title": "Kitchen paint", "last_at": "2026-10-05T14:00:00Z"}, "alex", {"project": "pr-1"})
base = {"todos": lambda: [todo("1", "alex", "Buy stamps"), todo("2", "household", "Bins out"), todo("3", "alex", "Old", True)],
        "scheduled": lambda: [late, soon, next_week], "tasks": lambda: [moving, waiting, theirs], "chats": lambda: [chat, filed]}
now = I.resolve("now", "alex", "adult", base, now=NOW)
ids = {g["id"]: [m["subject"]["id"] for m in g["members"]] for g in now["groups"]}
check("now: waiting on you (waiting first, then late), today's, open to-dos, moving, waiting on others; done never; "
      "a chat with no suggestion is NOT 'needs a home' (Zach 10-10: not everything is a project)", ids,
      {"g-waiting-on-you": ["task:t2", "scheduled:s1"], "g-today": ["scheduled:s2"], "g-open": ["todo:1", "todo:2"],
       "g-moving": ["task:t1"], "g-waiting-on-others": ["task:t3"], "g-needs-a-home": []})   # kept empty: shipped apps read it
check("…roles", [g["role"] for g in now["groups"]], ["attention", "primary", "supporting", "supporting", "supporting",
                                                     "supporting"])
check("…and the same lists at the top level", ([x["id"] for x in now["waiting_on_you"]],
                                              [x["id"] for x in now["waiting_on_others"]], [x["id"] for x in now["moving"]]),
      (["task:t2", "scheduled:s1"], ["task:t3"], ["task:t1"]))
check("a moving task says step k of n", [m["slots"].get("subtitle") for g in now["groups"] for m in g["members"]
                                         if m["subject"]["id"] == "task:t1"], ["2/3"])
check("every member has a basis; every action names an operation that exists",
      all(m["basis"] and set(m["slots"]["actions"]) <= {o["id"] for o in now["operations"]}
          for g in now["groups"] for m in g["members"]), True)
check("operations name existing routes only", sorted({(o["verb"], o.get("route", {}).get("method")) for o in now["operations"]}),
      [("done", "POST"), ("file", "POST"), ("later", "POST"), ("open", None), ("remove", "DELETE")])
check("sizeClass after projection", now["sizeClass"], "dozens" if sum(len(v) for v in ids.values()) > 5 else "few")
t = I.resolve("time", "alex", "adult", base, now=NOW, start="2026-10-06", end="2026-10-12")
check("time: one group per day in range, today primary",
      [(g["id"], g["role"]) for g in t["groups"]],
      [("g-day-2026-10-06", "primary"), ("g-day-2026-10-09", "supporting")])
check("time: only what's in range (yesterday's chat and anything undated are out)",
      sorted(m["subject"]["id"] for g in t["groups"] for m in g["members"]),
      ["chat:c1", "scheduled:s1", "scheduled:s2", "scheduled:s3"])
try:
    I.resolve("time", "alex", "adult", base, now=NOW, start="2026-10-06", end="2027-01-01")
    check("time: over 62 days → 400", "resolved", 400)
except I.Refused as e:
    check("time: over 62 days → 400", e.status, 400)
u = I.resolve("unsorted", "alex", "adult", base, now=NOW)
check("unsorted: a plain question with no suggestion isn't a filing backlog (it lives in History)",
      [m["subject"]["id"] for g in u["groups"] for m in g["members"]], [])
p = I.resolve("project:pr-1", "alex", "adult", base, now=NOW, project_ok=lambda x: x == "pr-1")
check("project: what's filed under it", [m["subject"]["id"] for g in p["groups"] for m in g["members"]], ["chat:c2"])
try:
    I.resolve("project:pr-9", "alex", "adult", base, now=NOW, project_ok=lambda x: x == "pr-1")
    check("a project they can't see: 404", "resolved", 404)
except I.Refused as e:
    check("a project they can't see: 404", e.status, 404)
check("the same inputs: the same surface (no hidden clock or randomness)",
      I.resolve("now", "alex", "adult", base, now=NOW + 60) == I.resolve("now", "alex", "adult", base, now=NOW + 60), True)
down = dict(base, notes=lambda: (_ for _ in ()).throw(OSError("KB down")))
d = I.resolve("now", "alex", "adult", down, now=NOW)
check("a source down is unknown, never 'nothing there'", (d["states"]["unknown"], [g["sources"] for g in d["groups"]
                                                                                    if g["role"] == "unknown"]), (1, [["notes"]]))

print("── the next step and the live sentence (#1086) ──")
n2 = I.resolve("now", "alex", "adult", base, now=NOW, call_me="Al")
check("Now's sentence: facts counted from the viewer's lists, the template's words",
      (n2["sentence"]["facts"], n2["sentence"]["text"], n2["sentence"]["source"]),
      ({"part_of_day": "afternoon", "name": "Al", "waiting_on_you": 2, "moving": 1},
       "Good afternoon, Al. 2 things wait on you; titan is moving 1 along on its own.", "template"))
check("Now's next steps: waiting on you first, then yours by time", [x["id"] for x in n2["next"]],
      ["task:t2", "scheduled:s1", "scheduled:s2", "scheduled:s3", "todo:1"])
check("nothing waiting: said plainly", I.resolve("now", "alex", "adult", {"todos": lambda: []}, now=NOW, call_me="Al")
      ["sentence"]["text"], "Good afternoon, Al. Nothing waits on you.")
check("a shared screen's sentence names nobody", I.resolve("now", None, "household", {"todos": lambda: []}, now=NOW,
                                                           call_me="Al")["sentence"]["facts"]["name"], None)
proj_items = [I.from_chat({"id": "c5", "title": "Snack rota", "last_at": "2026-10-06T14:00:00Z"}, "alex", {"project": "pr-1"}),
              dict(sched("s9", "alex", "Game at Harbor Park", NOW + 2 * 86400, "household"), projects=["pr-1"]),
              dict(I.from_task({"id": "t9", "goal": "Medical form", "status": "running", "owner": "robin", "updated_at": "1",
                                "steps": [{"title": "a", "state": "blocked"}]}), projects=["pr-1"])]
kw = dict(now=NOW, project_ok=lambda x: x == "pr-1", names={"robin": "Robin", "alex": "Alex"},
          project_name=lambda x: "Soccer season")
shared = I.resolve("project:pr-1", "alex", "adult", {"x": lambda: proj_items}, **kw)
check("a project's next step and sentence: the next thing coming, and who else it waits on when it's shared with you",
      (shared["next"]["id"], shared["sentence"]["text"]),
      ("scheduled:s9", "Soccer season: 3 open, next Game at Harbor Park Thursday 15:00. Waiting on Robin."))
check("…and the ISO time stays in the facts for the apps", shared["sentence"]["facts"]["next"]["at"], I._iso(NOW + 2 * 86400))
PT = timezone(timedelta(hours=-7))
check("spoken times in the household's zone", [I.spoken_when(I._iso(NOW + h * 3600), NOW, PT) for h in (2, 20, 50, 400)]
      + [I.spoken_when(I._iso(NOW + 26 * 3600), NOW, PT, all_day=True)],
      ["today 10:00", "tomorrow 4:00", "Thursday 10:00", "Oct 23", "tomorrow"])
unshared = I.resolve("project:pr-1", "alex", "adult", {"x": lambda: proj_items[:2]}, **kw)
check("…and when Robin's item isn't shared with you, no Robin and no count of it",
      ("Robin" in json.dumps(unshared), unshared["sentence"]["facts"]["open"]), (False, 2))

print("── File it suggestions (#1087) on the resolver ──")
SG = {"chat:c1": {"project": "pr-1", "reason": "Same record touched as a chat in Kitchen", "state": "accepted",
                  "auto": False, "via": ["chat:c2"]},
      "chat:c2": {"project": "pr-9", "reason": "filed already", "state": "accepted", "auto": False, "via": []}}
for v, gid in (("now", "g-needs-a-home"), ("unsorted", "g-unsorted")):
    r = I.resolve(v, "alex", "adult", base, now=NOW, suggest=lambda its: SG)
    m = next(m for g in r["groups"] if g["id"] == gid for m in g["members"] if m["subject"]["id"] == "chat:c1")
    op = next(o for o in r["operations"] if o["id"] == "op-file-chat:c1")
    check(f"{v}: the reason is the subtitle, the file operation carries the project",
          (m["slots"].get("subtitle"), m["suggestion"], op.get("params"), m["basis"][-1]["rule"]),
          ("Same record touched as a chat in Kitchen", {"project": "pr-1", "state": "accepted", "auto": False},
           {"project": "pr-1"}, "suggested-project"))
    check(f"{v}: a filed item never takes one (only Unsorted members)", "op-file-chat:c2" in {o["id"] for o in r["operations"]
                                                                          if o.get("params")}, False)
plain = I.resolve("now", "alex", "adult", base, now=NOW)
check("no suggester: no subtitle, a different fingerprint", (any("suggestion" in m for g in plain["groups"] for m in g["members"]),
      plain["fingerprint"] != I.resolve("now", "alex", "adult", base, now=NOW, suggest=lambda its: SG)["fingerprint"]), (False, True))


def broken(its):
    raise RuntimeError("links down")


check("a suggester that fails: nothing is offered for filing (no suggestion, no backlog), and nothing errors",
      [m["subject"]["id"] for g in I.resolve("unsorted", "alex", "adult", base, now=NOW, suggest=broken)["groups"]
       for m in g["members"]], [])
check("with a suggestion, the chat is offered in Unsorted and in Now's needs-a-home",
      ([m["subject"]["id"] for g in I.resolve("unsorted", "alex", "adult", base, now=NOW, suggest=lambda its: SG)["groups"]
        for m in g["members"]],
       [m["subject"]["id"] for g in I.resolve("now", "alex", "adult", base, now=NOW, suggest=lambda its: SG)["groups"]
        if g["id"] == "g-needs-a-home" for m in g["members"]]), (["chat:c1"], ["chat:c1"]))

print("── the route, every view × every viewer ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import kb_privacy  # noqa: E402
import references  # noqa: E402
import scheduled  # noqa: E402
import spaces  # noqa: E402
import todos  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ALEX, alex = pair("Alex Quarry", "adult", "Alex's iPhone")
ROBIN, robin = pair("Robin Quarry", "adult", "Robin's iPhone")
AVA, ava = pair("Ava Quarry", "kid", "Ava's iPhone")
TV, _ = pair("Living room", "household", "Living room TV", platform="tvos")
c.patch(f"/v1/people/{ava}", json={"parents": [robin]}, headers=M)
admin = hh.ADMIN_PERSON_ID
WHO = {"admin": (M, admin, "admin"), "alex": (ALEX, alex, "adult"), "ava": (AVA, ava, "kid"), "tv": (TV, None, "household")}

today = datetime.now(timezone.utc).date()
# Each person's private things carry a marker only they may ever see; the household's are for everyone.
SECRET = {}
tomorrow = time.time() + 86400
for name, (_, pid, role) in WHO.items():
    if pid is None:
        continue
    tag = f"zz{name}secret"
    SECRET[name] = tag
    todos.add(f"{tag} todo", pid, created_by=name)
    scheduled.add(pid, f"{tag} reminder", "notify", {"text": "x"}, tomorrow)
    references.put(pid, role, f"dev-{name}", {"kind": "file", "native_id": f"n-{name}", "title": f"{tag} file"})
    api._get_memory().add_message(hh.conv_id_for(pid), "user", f"{tag} chat about the weekend")
    pr = spaces.create_project(pid, role, f"{tag} project")
    SECRET[name + ":project"] = pr["id"]
todos.add("Bins out tonight", "household", created_by="alex")
scheduled.add(alex, "Water the plants", "notify", {"text": "x"}, tomorrow, audience="household")
for name, (_, pid, _) in WHO.items():
    if pid:
        cid = next(iter(c["id"] for c in __import__("conversations").list_for(api._get_memory()._db, hh.conv_id_for(pid), 5)))
        spaces.tag(pid, cid, SECRET[name + ":project"], None)

# the KB: a private note per person and one for the household (the runtime is a fake; kb_privacy is real)
KB = {}
for name, (_, pid, _) in WHO.items():
    if pid:
        KB[f"note-{name}"] = {"source_id": f"note-{name}", "title": f"{SECRET[name]} note", "source_type": "note",
                              "metadata": {"owner": pid, "audience": "private"}}
        kb_privacy.set_acl(f"note-{name}", pid, "private", by=pid)
KB["note-house"] = {"source_id": "note-house", "title": "Wifi for guests", "source_type": "note",
                    "metadata": {"owner": alex, "audience": "household"}}
kb_privacy.set_acl("note-house", alex, "household", by=alex)
# #1108: triaged mail in the KB: Alex's own, and the admin's (even marked household, it's the admin's alone)
day_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
KB["cx-mail-alex"] = {"source_id": "cx-mail-alex", "title": f"{SECRET['alex']} mail subject", "source_type": "connector:mail",
                      "metadata": {"owner": alex, "audience": "private", "observed_at": day_iso}}
kb_privacy.set_acl("cx-mail-alex", alex, "private", by=alex)
KB["cx-mail-admin"] = {"source_id": "cx-mail-admin", "title": f"{SECRET['admin']} mail marked household",
                       "source_type": "connector:mail", "metadata": {"owner": admin, "audience": "household",
                                                                    "observed_at": day_iso}}
kb_privacy.set_acl("cx-mail-admin", admin, "household", by=admin)
# #1108: bills and appointments in the record runtime (a fake; obligations, _node_visible and schedule_view are real)
in3 = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
NODES = {"bill": [{"node_id": f"b-{n}", "node_type": "bill",
                   "data": {"doc_type": "bill", "vendor": f"{SECRET[n]} Water", "amount": 48.2, "due_date": in3,
                            "mail_date": str(today), "provenance": {"owner": pid, "audience": "private"}}}
                  for n, (_, pid, _) in WHO.items() if pid and n != "ava"]
         + [{"node_id": "b-house", "node_type": "bill",   # a household bill: adults see it, a kid never does
             "data": {"doc_type": "bill", "vendor": "Harbor Gas", "amount": 61.2, "due_date": in3, "mail_date": str(today),
                      "provenance": {"owner": alex, "audience": "household"}}}],
         "receipt": [],
         "appointment": [{"node_id": "ap-house", "data": {"title": "Soccer at Harbor Park", "start": f"{in3}T16:00:00",
                                                          "calendar": "Family", "provenance": {"owner": alex,
                                                                                               "audience": "household"}}},
                         {"node_id": "ap-alex", "data": {"title": f"{SECRET['alex']} dentist", "start": f"{in3}T09:00:00",
                                                         "calendar": "Alex work",
                                                         "provenance": {"owner": alex, "audience": "private"}}}]}
lib.record_nodes = lambda t, **k: (list(NODES.get(t, [])), True)
import world as _world  # noqa: E402
_world.household_calendars = lambda *a, **k: {"family"}
lib.kb_sources = lambda *a, **k: [dict(v) for v in KB.values()]
lib._runtime_request = lambda svc, path, method="GET", payload=None, timeout=30: {
    "chunks": [{"chunk_index": 0, "chunk_text": "x", "metadata": KB[path.split("/sources/")[1].split("/")[0]]["metadata"]}]}
# the task runtime: tasks_for projects by owner; a shared screen sees none of these private tasks
TASKS = [{"id": f"t-{n}", "goal": f"{SECRET[n]} task", "status": "running", "owner": pid, "updated_at": "1",
          "moved_at": "2026-10-06T10:00:00Z", "steps": [{"title": "a", "state": "current"}]}
         for n, (_, pid, _) in WHO.items() if pid]
lib.tasks_for = lambda viewer, role: {"tasks": [t for t in TASKS if viewer and t["owner"] == viewer]}

# #1087: a second, unfiled chat of Alex's linked (chat_links) to his filed one, and a forged row tying it to Ava's filed
# chat: Ava's chat isn't in Alex's projected items, so it can never carry a suggestion or show in its reason
import chat_links  # noqa: E402
import conversations  # noqa: E402
import hooks  # noqa: E402
api._get_memory().add_message(hh.conv_id_for(alex), "user", "the paint colours again", thread="th-alex-2")
a_convs = conversations.list_for(api._get_memory()._db, hh.conv_id_for(alex), 5)
a_filed = next(x["id"] for x in a_convs if SECRET["alex"] in x["title"])
a_new = next(x["id"] for x in a_convs if x["id"] != a_filed)
v_chat = conversations.list_for(api._get_memory()._db, hh.conv_id_for(ava), 5)[0]["id"]
api._get_memory().add_message(hh.conv_id_for(alex), "user", "trim colour for the kitchen", thread="th-alex-3")
a3 = next(x["id"] for x in conversations.list_for(api._get_memory()._db, hh.conv_id_for(alex), 5)
          if x["id"] not in (a_filed, a_new))
api._get_memory().add_message(hh.conv_id_for(ava), "user", "spelling list again", thread="th-ava-2")
v_new = next(x["id"] for x in conversations.list_for(api._get_memory()._db, hh.conv_id_for(ava), 5) if x["id"] != v_chat)
lcon = hooks.connect()
chat_links.save(lcon, [chat_links.Edge(*sorted((chat_links.item_id(a_new), chat_links.item_id(a_filed))), "record",
                                       "accepted", alex, ["same record touched"], {"hours_apart": 1.0}, "x"),
                       chat_links.Edge(*sorted((chat_links.item_id(a_new), chat_links.item_id(v_chat))), "lineage",
                                       "verified", alex, ["branched from it"], {}, "x"),
                       chat_links.Edge(*sorted((chat_links.item_id(v_new), chat_links.item_id(v_chat))), "lineage",
                                       "verified", ava, ["branched from it"], {}, "x"),
                       chat_links.Edge(*sorted((chat_links.item_id(a3), chat_links.item_id(a_filed))), "lineage",
                                       "verified", alex, ["branched from it"], {}, "x")])
import file_suggest  # noqa: E402
FILED = file_suggest.run_all(apply=True, con=lcon)
lcon.close()

VIEWS = ["now", "unsorted", f"time&from={today}&to={today + timedelta(days=6)}"] + \
        [f"project:{SECRET[n + ':project']}" for n in ("admin", "alex", "ava")] + \
        [f"person:{pid}" for _, pid, _ in WHO.values() if pid]
NEVER = {"admin": [SECRET["alex"], SECRET["ava"]], "alex": [SECRET["admin"], SECRET["ava"]],
         "ava": [SECRET["admin"], SECRET["alex"]], "tv": [SECRET["admin"], SECRET["alex"], SECRET["ava"]]}
for name, (h, pid, role) in WHO.items():
    leaks, codes = [], {}
    for v in VIEWS:
        r = c.get(f"/v1/items?view={v}", headers=h)
        body = r.text
        codes[v.split("&")[0].split(":")[0]] = codes.get(v.split("&")[0].split(":")[0], set()) | {r.status_code}
        leaks += [(v, s) for s in NEVER[name] if s in body]
    check(f"{name}: never sees another person's private items in any view (members, counts, ids, strings)", leaks, [])
    check(f"{name}: every view answers", sorted(codes), ["now", "person", "project", "time", "unsorted"])

print("── what each one does see ──")


def members(h, v):
    r = c.get(f"/v1/items?view={v}", headers=h)
    return [m["slots"]["title"] for g in r.json().get("groups", []) for m in g["members"]] if r.status_code == 200 else r.status_code


check("Alex's Now: their to-do and the household's", sorted(t for t in members(ALEX, "now") if "todo" in t or "Bins" in t),
      ["Bins out tonight", f"{SECRET['alex']} todo"])
un = c.get("/v1/items?view=unsorted", headers=ALEX).json()
um = [m for g in un["groups"] for m in g["members"]]
check("Alex's Unsorted: his new chat, suggested into his project by the record link (not Ava's verified one)",
      [(m["slots"]["title"], m["slots"].get("subtitle"), (m.get("suggestion") or {}).get("project")) for m in um],
      [("the paint colours again", f"Same record touched as a chat in {SECRET['alex']} project", SECRET["alex:project"])])
check("…its file operation carries his project", next(o for o in un["operations"] if o["verb"] == "file").get("params"),
      {"project": SECRET["alex:project"]})
check("Ava's Unsorted: no suggestions for a kid, even on her own verified link, so nothing to file (her chat is in History)",
      [(m["slots"]["title"], m.get("suggestion"), m["slots"].get("subtitle")) for g in c.get(
          "/v1/items?view=unsorted", headers=AVA).json()["groups"] for m in g["members"]],
      [])
print("── #1087: titan files a verified link on its own; Filed in … · Undo ──")
check("run_all: only Alex's verified link to his own project was filed (not Ava's: a kid; not the forged one)", FILED,
      {"people": 3, "filed": 1})
nowv = c.get("/v1/items?view=now", headers=ALEX).json()
fg = next((g for g in nowv["groups"] if g["id"] == "g-filed-for-you"), None)
check("Alex's Now: 'Filed in …' for the branched chat, with an undo action",
      [(m["slots"]["title"], m["slots"].get("subtitle"), [a for a in m["slots"]["actions"] if a.startswith("op-undo")])
       for m in (fg or {}).get("members", [])],
      [("trim colour for the kitchen", f"Filed in {SECRET['alex']} project", [f"op-undo-chat:{a3}"])])
uop = next(o for o in nowv["operations"] if o["id"] == f"op-undo-chat:{a3}")
check("…the undo operation is the project undo route with its token", (uop["route"], sorted(uop["params"])),
      ({"method": "POST", "path": "/v1/projects/undo"}, ["token"]))
check("…and the chat is on his project page", f"chat:{a3}" in [m["subject"]["id"] for g in c.get(
      f"/v1/items?view=project:{SECRET['alex:project']}", headers=ALEX).json()["groups"] for m in g["members"]], True)
check("the admin's Now never shows Alex's filing", "g-filed-for-you" in [g["id"] for g in c.get(
      "/v1/items?view=now", headers=M).json()["groups"]], False)
r = c.post("/v1/projects/undo", json={"token": uop["params"]["token"]}, headers=ALEX)
check("Undo puts it back: 200, gone from Now, back in Unsorted", (r.status_code, "g-filed-for-you" in [g["id"] for g in c.get(
      "/v1/items?view=now", headers=ALEX).json()["groups"]], f"chat:{a3}" in [m["subject"]["id"] for g in c.get(
      "/v1/items?view=unsorted", headers=ALEX).json()["groups"] for m in g["members"]]), (200, False, True))
lcon = hooks.connect()
check("…and titan never files that pair again", file_suggest.run_all(apply=True, con=lcon), {"people": 3, "filed": 0})
lcon.close()
check("Alex's project page: their filed chat",
      [t for t in members(ALEX, f"project:{SECRET['alex:project']}") if "chat" in t], [f"{SECRET['alex']} chat about the weekend"])
check("someone else's private project: 404", members(ALEX, f"project:{SECRET['admin:project']}"), 404)
check("Ava's own to-do is in her Now", [t for t in members(AVA, "now") if "todo" in t], [f"{SECRET['ava']} todo"])
check("the screen's Now: household things only", sorted(members(TV, "now")), ["Bins out tonight"])
tv_time = members(TV, f"time&from={today}&to={today + timedelta(days=6)}")
check("the screen's Time: the household's scheduled action and event, nobody's private one", sorted(tv_time),
      ["Soccer at Harbor Park", "Water the plants"])
check("a person page shows only what this viewer can see of theirs: Ava looking at Alex sees no private item",
      [t for t in members(AVA, f"person:{alex}") if "secret" in t], [])
check("Ava's Now sentence: her name, her counts", (lambda b: (b["sentence"]["facts"]["name"],
      b["sentence"]["facts"]["waiting_on_you"]))(c.get("/v1/items?view=now", headers=AVA).json()), ("Ava", 0))
print("── #1085: a household project shows each filed item only to who may see it ──")
import hooks  # noqa: E402
import projects  # noqa: E402
hcon = hooks.connect()
hooks.index(hcon, "mail-alex-1", "mail", hooks.extract({"title": "zzalexsecret mail", "text": "From: a <a@b.example>\n\nhi"},
                                                        lambda t: []), alex, "private")
hcon.commit()
hp = spaces.create_project(alex, "adult", "Soccer season", audience="household")
projects.file(alex, "adult", "mail-alex-1", hp["id"])
projects.file(alex, "adult", f"todo:{todos.add('Snack rota', 'household', created_by='alex')['id']}", hp["id"])
def body_of(h, v):
    return c.get(f"/v1/items?view={v}", headers=h).text


check("Alex sees his own mail on the household project's page", "mail-alex-1" in body_of(ALEX, f"project:{hp['id']}"), True)
for nm, h in (("the admin", M), ("Ava", AVA)):
    b = body_of(h, f"project:{hp['id']}")
    check(f"{nm} sees the household project with the shared to-do, never Alex's mail (not its id, not a count)",
          ("Snack rota" in b, "mail-alex-1" in b, "zzalexsecret" in b), (True, False, False))
print("── #1142: project people, member audience, name_source, plan steps ──")
import world as W  # noqa: E402
W.get().sync("items-test-people", [
    W.Obs(entity="coach-hale", etype="person", name="Coach Hale", attr="relation", value="coach",
          observed_at="2026-09-01", kind="household", ref="hh#coach", origin="household.yaml", label="the household file",
          owner=None, audience="household", bubble="household"),
    W.Obs(entity="jordan-reyes", etype="person", name="Jordan Reyes", attr="relation", value="carpool",
          observed_at="2026-10-01", kind="contact", ref="alex#jordan", origin=f"contacts:{alex}", label="contacts",
          owner=alex, audience="private", bubble=alex)])
hcon = hooks.connect()
for iid, src, title, owner_, aud_ in (("msg-alex-jordan", "message", "Jordan Reyes: I can drive Saturday", alex, "private"),
                                      ("mail-hh-coach", "mail", "Coach Hale: practice moved to Thursday", alex, "household"),
                                      ("mail-adults-fee", "mail", "Registration fee receipt", alex, "adults")):
    hooks.index(hcon, iid, src, hooks.extract({"title": title, "text": f"From: {title.split(':')[0]} <x@example.org>\n\nhi"},
                                              hooks.entity_finder(owner_, "adult")), owner_, aud_)
    projects.file(alex, "adult", iid, hp["id"])
hcon.commit()
hcon.close()


def page(h, pid_):
    return c.get(f"/v1/items?view=project:{pid_}", headers=h).json()


everyone = sorted([admin, alex, robin, ava])
for nm, h, outside in (("Alex", ALEX, [("Coach Hale", "coach"), ("Jordan Reyes", "carpool")]),
                       ("Robin", ROBIN, [("Coach Hale", "coach")]), ("Ava", AVA, [("Coach Hale", "coach")]),
                       ("the admin", M, [("Coach Hale", "coach")])):
    pg = page(h, hp["id"])
    check(f"{nm}: the household project's people are the household who may see it (never the screen)",
          sorted(x["id"] for x in pg["people"] if not x.get("outside")), everyone)
    check(f"{nm}: outside people only from items they see (the Jordan rule)",
          [(x["name"], x["role"]) for x in pg["people"] if x.get("outside")], outside)
    if nm != "Alex":
        check(f"{nm}: never Jordan, not a name, an id or a relation", [s_ for s_ in ("Jordan", "jordan-reyes", "carpool")
                                                                      if s_ in json.dumps(pg)], [])
check("roles are household roles", sorted({x["role"] for x in page(ALEX, hp["id"])["people"] if not x.get("outside")}),
      ["admin", "adult", "kid"])
check("a private project's people: its owner alone", [x["id"] for x in page(ALEX, SECRET["alex:project"])["people"]], [alex])
aud_of = lambda h: {m["subject"]["id"]: m["slots"].get("audience") for g in page(h, hp["id"])["groups"] for m in g["members"]}
a_aud = aud_of(ALEX)
check("Alex: each member says its own audience (private / household / adults)",
      (a_aud.get("msg-alex-jordan"), a_aud.get("mail-hh-coach"), a_aud.get("mail-adults-fee")),
      ("private", "household", "adults"))
check("…every member carries one", all(v in ("private", "adults", "household", "named") for v in a_aud.values()), True)
check("Ava: the adults' item isn't there at all, so neither is its audience",
      ("mail-adults-fee" in aud_of(AVA), "adults" in json.dumps(page(AVA, hp["id"]))), (False, False))
check("name_source: a project a person named", (page(ALEX, hp["id"])["name_source"],
      next(p_["name_source"] for p_ in c.get("/v1/projects", headers=ALEX).json()["projects"] if p_["id"] == hp["id"])),
      ("person", "person"))
_pc = spaces._db()
_pc.execute("UPDATE projects SET name_source='earned' WHERE id=?", (hp["id"],))
_pc.commit()
_pc.close()
check("…and one titan named (Named by titan · Rename)", (page(ROBIN, hp["id"])["name_source"],
      next(p_["name_source"] for p_ in c.get("/v1/projects", headers=ROBIN).json()["projects"] if p_["id"] == hp["id"])),
      ("earned", "earned"))
# the resolver: plan steps inline, and the audience rule's cases
plan = I.from_task({"id": "t9", "goal": "End-of-season party", "status": "running", "owner": "alex", "updated_at": "1",
                    "steps": [{"title": "Pick the date", "state": "done"}, {"title": "Book the pavilion", "state": "current"},
                              {"title": "Collect the money", "state": "pending"}],
                    "autonomy": {"on": True, "next_at": "2026-10-08T09:00:00-07:00", "halted": False}})
plan["projects"] = ["pr-1"]
stuck = I.from_task({"id": "t10", "goal": "Renew the permit", "status": "running", "owner": "alex", "updated_at": "1",
                     "steps": [{"title": "Fill the form", "state": "blocked"}], "autonomy": {"on": False}})
stuck["projects"] = ["pr-1"]
rp = I.resolve("project:pr-1", "alex", "adult", {"x": lambda: [plan, stuck]}, now=NOW, project_ok=lambda x: True)
steps_of = {m["subject"]["id"]: m.get("steps") for g in rp["groups"] for m in g["members"]}
check("plan steps inline: titan on the steps it advances, next_at on the current one",
      steps_of["task:t9"], [{"title": "Pick the date", "state": "done", "who": None, "next_at": None},
                            {"title": "Book the pavilion", "state": "current", "who": "titan",
                             "next_at": "2026-10-08T09:00:00-07:00"},
                            {"title": "Collect the money", "state": "pending", "who": "titan", "next_at": None}])
check("…a blocked step waits on its owner", steps_of["task:t10"],
      [{"title": "Fill the form", "state": "blocked", "who": "alex", "next_at": None}])
for label, it_, viewer_, role_, want in (
        ("a list of just you", {"audience": '["alex"]', "owner": "alex"}, "alex", "adult", "private"),
        ("a list with others: named, never who", {"audience": '["alex", "robin"]', "owner": "alex"}, "alex", "adult", "named"),
        ("shared with you by someone", {"audience": '["robin"]', "owner": "alex"}, "robin", "adult", "named"),
        ("the admin's own admins item", {"audience": "admins", "owner": ""}, "zach", "admin", "private"),
        ("an adult's item for the admins", {"audience": "admins", "owner": "alex"}, "alex", "adult", "named"),
        ("household on a shared screen", {"audience": "household", "owner": "household"}, None, "household", "household"),
        ("T7: an adults' item on a shared screen says adults, not household",
         {"audience": "adults", "owner": "household"}, None, "household", "adults"),
        ("T7: a named item on a shared screen is named, never household or 'only you'",
         {"audience": '["alex", "robin"]', "owner": "household"}, None, "household", "named"),
        ("T7: a private one on a shared screen: named (no one is 'you' there)",
         {"audience": "private", "owner": "household"}, None, "household", "named"),
        ("adults", {"audience": "adults", "owner": "alex"}, "robin", "adult", "adults"),
        ("an unreadable audience is the narrowest", {"audience": "[oops", "owner": "alex"}, "alex", "adult", "private")):
    check(f"audience slot: {label}", I.audience_slot(it_, viewer_, role_), want)
print("── #1149: slots.projects, only projects the viewer may see ──")
ap_ = spaces.create_project(alex, "adult", "Gutter work", audience="adults")
t_priv = todos.add("Recycling run", "household", created_by="alex")["id"]
t_adul = todos.add("Gutter quote", "household", created_by="alex")["id"]
projects.file(alex, "adult", f"todo:{t_priv}", SECRET["alex:project"])
projects.file(alex, "adult", f"todo:{t_adul}", ap_["id"])


def proj_slots(h):
    b = c.get("/v1/items?view=now", headers=h).json()
    return {m["slots"]["title"]: m["slots"].get("projects") for g in b["groups"] for m in g["members"]}, json.dumps(b)


for nm, h, want in (("Alex", ALEX, ([SECRET["alex:project"]], [ap_["id"]])), ("Robin", ROBIN, ([], [ap_["id"]])),
                    ("the admin", M, ([], [ap_["id"]])), ("Ava", AVA, ([], [])), ("the screen", TV, ([], []))):
    sl, body = proj_slots(h)
    check(f"{nm}: a shared to-do carries only the projects they may see", (sl.get("Recycling run"), sl.get("Gutter quote")),
          want)
    check(f"{nm}: every member has slots.projects (unfiled: empty)", all(isinstance(v, list) for v in sl.values()), True)
    if nm != "Alex":
        check(f"{nm}: Alex's private project's id is nowhere in the view", SECRET["alex:project"] in body, False)
    if nm in ("Ava", "the screen"):
        check(f"{nm}: the adults' project id is nowhere in the view", ap_["id"] in body, False)
check("an unfiled member's projects are empty", proj_slots(ALEX)[0].get("Bins out tonight"), [])
print("── #1169: file every kind, where the viewer may ──")


def view_of(h, v):
    return c.get(f"/v1/items?view={v}", headers=h).json()


def file_ops(b):
    """member title → (has file op, has unfile op, file route method)"""
    ops = {o["id"]: o for o in b.get("operations", [])}
    out = {}
    for g in b.get("groups", []):
        for m in g["members"]:
            acts = m["slots"].get("actions", [])
            f = next((ops[a] for a in acts if a.startswith("op-file-")), None)
            out[m["slots"]["title"]] = (f is not None, any(a.startswith("op-unfile-") for a in acts),
                                        (f or {}).get("route", {}).get("method"))
    return out


a_now = file_ops(view_of(ALEX, "now"))
check("Alex: their own to-do and the household's carry File (POST /v1/items/…/file)",
      (a_now.get(f"{SECRET['alex']} todo"), a_now.get("Bins out tonight")), ((True, False, "POST"), (True, False, "POST")))
v_now = file_ops(view_of(AVA, "now"))
check("Ava (a kid): her own to-do files, the household's doesn't", (v_now.get(f"{SECRET['ava']} todo", (None,))[0],
                                                                    v_now.get("Bins out tonight", (None,))[0]), (True, False))
check("the screen: nothing files", any(x[0] for x in file_ops(view_of(TV, "now")).values()), False)
check("the admin may file the household's to-do", file_ops(view_of(M, "now")).get("Bins out tonight", (None,))[0], True)
ids_now = {m["slots"]["title"]: m["subject"]["id"] for g in view_of(ALEX, "now")["groups"] for m in g["members"]}
alex_todo, bins = ids_now[f"{SECRET['alex']} todo"], ids_now["Bins out tonight"]
ava_todo = next(m["subject"]["id"] for g in view_of(AVA, "now")["groups"] for m in g["members"]
                if m["slots"]["title"] == f"{SECRET['ava']} todo")
fp = lambda h, i, pr: c.post(f"/v1/items/{i}/file", json={"project": pr}, headers=h)  # noqa: E731
check("Robin can't file Alex's private to-do (404: it doesn't exist for Robin)", fp(ROBIN, alex_todo, hp["id"]).status_code, 404)
check("…nor unfile it", c.post(f"/v1/items/{alex_todo}/unfile", headers=ROBIN).status_code, 404)
check("Ava can't file the household's to-do", fp(AVA, bins, SECRET["ava:project"]).status_code, 404)
check("…but files her own into her project", fp(AVA, ava_todo, SECRET["ava:project"]).status_code, 200)
check("the screen can't file at all", fp(TV, bins, hp["id"]).status_code, 403)
check("an id nobody has → 404", fp(ALEX, "todo:999999", hp["id"]).status_code, 404)
r = fp(ALEX, alex_todo, hp["id"])
check("Alex files their private to-do into the household project, with an undo token", (r.status_code, bool(r.json().get("undo"))),
      (200, True))
pg_a = view_of(ALEX, f"project:{hp['id']}")
check("…it's on Alex's project page, with Unfile", file_ops(pg_a).get(f"{SECRET['alex']} todo"), (True, True, "POST"))
for nm, h in (("Robin", ROBIN), ("Ava", AVA), ("the admin", M)):
    b = view_of(h, f"project:{hp['id']}")
    check(f"{nm}: Alex's private to-do filed in a shared project is not there, not counted, not in any string",
          (SECRET["alex"] in json.dumps(b), alex_todo in json.dumps(b),
           b["sentence"]["facts"]["open"] == len([m for g in b["groups"] for m in g["members"]
                                                  if m["subject"]["id"] != alex_todo])), (False, False, True))
check("…and filing never changed who sees it: Robin's Now still lacks it", SECRET["alex"] in json.dumps(view_of(ROBIN, "now")), False)
r2 = c.post("/v1/projects/undo", json={"token": r.json()["undo"]}, headers=ALEX)
check("undo takes it back out", (r2.status_code, f"{SECRET['alex']} todo" in file_ops(view_of(ALEX, f"project:{hp['id']}"))),
      (200, False))
r3 = fp(ALEX, bins, hp["id"])
check("Alex files the household's to-do; Ava sees it on the household project, with no File or Unfile of her own",
      (r3.status_code, file_ops(view_of(AVA, f"project:{hp['id']}")).get("Bins out tonight")), (200, (False, False, None)))
check("…Robin (an adult) may unfile it", file_ops(view_of(ROBIN, f"project:{hp['id']}")).get("Bins out tonight"),
      (True, True, "POST"))
check("…and does", c.post(f"/v1/items/{bins}/unfile", headers=ROBIN).status_code, 200)
print("── #1173: make things inside a project ──")
pg = c.get(f"/v1/items?view=project:{hp['id']}", headers=ALEX).json()
pops = {o["verb"]: o for o in pg["operations"] if o["subject"] == {"kind": "project", "id": hp["id"]}
        and o["verb"] in ("add", "ask")}
check("a project page offers Add (a to-do) and Ask, each carrying the project",
      sorted((v, o["route"]["method"], o["route"]["path"], o["params"]) for v, o in pops.items()),
      [("add", "POST", "/v1/todos", {"project": hp["id"]}), ("ask", "POST", "/v1/prompt/stream", {"project": hp["id"]})])
check("…named on the project group", set(o["id"] for o in pops.values()) <= set(
      next(g for g in pg["groups"] if g["id"] == "g-project").get("operations", [])), True)
r = c.post("/v1/todos", json={"title": "Order the trophies", "project": hp["id"]}, headers=ALEX)
check("Add: the to-do is made in the project, with an undo for the filing",
      (r.status_code, r.json().get("project"), bool(r.json().get("undo"))), (200, hp["id"], True))
check("…it's on the project page", "Order the trophies" in json.dumps(c.get(f"/v1/items?view=project:{hp['id']}",
                                                                            headers=ALEX).json()), True)
check("…undo leaves the to-do, out of the project", (c.post("/v1/projects/undo", json={"token": r.json()["undo"]},
                                                            headers=ALEX).status_code,
      "Order the trophies" in json.dumps(c.get(f"/v1/items?view=project:{hp['id']}", headers=ALEX).json()),
      "Order the trophies" in json.dumps(c.get("/v1/items?view=now", headers=ALEX).json())), (200, False, True))
check("Add into a project you can't see: 404, nothing made", (c.post("/v1/todos", json={"title": "zzsneaky",
      "project": SECRET["alex:project"]}, headers=ROBIN).status_code, "zzsneaky" in json.dumps(
      c.get("/v1/items?view=now", headers=ROBIN).json())), (404, False))
import agent_runtime_lib as lib  # noqa: E402
lib._TURN_INFO.info = {}
t_made = todos.add("Book the pavilion", alex, created_by="alex")
lib._note_created(f"todo:{t_made['id']}")
lib._note_created(f"todo:{t_made['id']}")       # noted once
turn = {"file_under": {"project": hp["id"]}, "created": list(lib._TURN_INFO.info["created"])}
lib._TURN_INFO.info = None
who_alex = hh.Identity({"id": alex, "name": "Alex Quarry", "role": "adult"}, None, "adult", "device")
check("Ask kept with this project: what the turn made is filed there", (turn["created"], api._file_turn_made(turn, who_alex),
      "Book the pavilion" in json.dumps(c.get(f"/v1/items?view=project:{hp['id']}", headers=ALEX).json())),
      ([f"todo:{t_made['id']}"], 1, True))
check("…a turn without a project files nothing", api._file_turn_made({"created": ["todo:1"]}, who_alex), 0)
check("…a shared screen's turn files nothing",
      api._file_turn_made(turn, hh.Identity({"id": "tv", "name": "Living room", "role": "household"}, None, "household",
                                            "device")), 0)
check("…into a project the maker can't see, nothing (and nothing breaks)",
      api._file_turn_made({"file_under": {"project": SECRET["admin:project"]}, "created": [f"todo:{t_made['id']}"]},
                          who_alex), 0)
print("── #1175: Unsorted lists your own items that already carry a File-it suggestion ──")
import linker as _L  # noqa: E402
_L.SCORE_SYNC = True                   # a write is scored at once (the background re-score is for live)
hcon = hooks.connect()
for who_, pid_, tag_, order in (("alex", alex, "alex", "PS-4401"), ("admin", admin, "admin", "PS-5502"),
                                ("ava", ava, "ava", "PS-6603")):
    for n_ in (1, 2):
        hooks.index(hcon, f"scan-{tag_}-{n_}", "scan", hooks.extract(
            {"title": f"Receipt {n_}", "text": f"From: Pine Shop <orders@pineshop.example>\n\nOrder #{order} receipt {n_}."},
            lambda t: []), pid_, "private")
hooks.index(hcon, "scan-alex-9", "scan", hooks.extract({"title": "Warranty card", "text": "Unrelated warranty card."},
                                                       lambda t: []), alex, "private")
hcon.commit()
hcon.close()
for who_, pid_, role_ in (("alex", alex, "adult"), ("admin", admin, "admin"), ("ava", ava, "kid")):
    projects.file(pid_, role_, f"scan-{who_}-1", SECRET[f"{who_}:project"])


def unsorted_of(h):
    b = c.get("/v1/items?view=unsorted", headers=h).json()
    return {m["subject"]["id"]: m for g in b["groups"] for m in g["members"]}, b


ua, ub = unsorted_of(ALEX)
m2 = ua.get("scan-alex-2")
check("Alex's unfiled receipt that shares an order number with one in their project is in Unsorted, with the suggestion",
      (bool(m2), (m2 or {}).get("suggestion", {}).get("project"), str((m2 or {})["slots"].get("subtitle")).startswith("Same ") and
       str((m2 or {})["slots"].get("subtitle")).endswith(f"in {SECRET['alex']} project")),
      (True, SECRET["alex:project"], True))
fop = next((o for o in ub["operations"] if o["id"] == "op-file-scan-alex-2"), {})
check("…its File carries the project", (fop.get("route", {}).get("path"), fop.get("params")),
      ("/v1/items/scan-alex-2/file", {"project": SECRET["alex:project"]}))
check("an unlinked item of Alex's isn't there (only items with a suggestion)", "scan-alex-9" in ua, False)
check("Alex never gets the admin's suggested receipt, nor any word of it", ("scan-admin-2" in json.dumps(ub), "PS-5502" in json.dumps(ub)),
      (False, False))
check("the admin gets their own", "scan-admin-2" in unsorted_of(M)[0], True)
check("a kid gets none (no suggestions for kids)", [i for i in unsorted_of(AVA)[0] if i.startswith("scan-")], [])
check("the screen gets none", [i for i in unsorted_of(TV)[0] if i.startswith("scan-")], [])
check("…and never in Now or Time (Unsorted only)", "scan-alex-2" in json.dumps(c.get("/v1/items?view=now", headers=ALEX).json()),
      False)
_cap = api.UNSORTED_SUGGESTED_CAP
api.UNSORTED_SUGGESTED_CAP = 0
check("the cap holds", [i for i in unsorted_of(ALEX)[0] if i.startswith("scan-")], [])
api.UNSORTED_SUGGESTED_CAP = _cap
rf = c.post("/v1/items/scan-alex-2/file", json={"project": SECRET["alex:project"]}, headers=ALEX)
check("filing it from Unsorted works (the route finds the suggested item) and it leaves Unsorted",
      (rf.status_code, "scan-alex-2" in unsorted_of(ALEX)[0]), (200, False))
check("…Robin can't file Alex's receipt", c.post("/v1/items/scan-alex-1/unfile", headers=ROBIN).status_code, 404)
print("── #1181: the items readers are warmed at start ──")
_calls = []
_real_readers = api._item_readers


def _counting_readers(ident, now_dt):
    rs = _real_readers(ident, now_dt)

    def wrap(name, fn):
        def run():
            _calls.append((ident.role, name))
            if name == "tasks":
                raise RuntimeError("a store down at start")     # never stops the warm-up
            return fn()
        return run
    return {n: wrap(n, f) for n, f in rs.items()}


api._item_readers = _counting_readers
try:
    w_ = api._warm_items()
finally:
    api._item_readers = _real_readers
roles_ = sorted({r for r, _ in _calls})
n_people = len([p for p in hh.get_store().people() if p.get("role") in ("admin", "adult", "kid")])
n_readers = len(_real_readers(hh.Identity({"id": alex, "name": "Alex", "role": "adult"}, None, "adult", "x"), datetime.now()))
check("warm at start: every reader, once per live person and once as a shared screen, past a failing one",
      (w_["people"], roles_, len(_calls)), (n_people + 1, ["admin", "adult", "household", "kid"], (n_people + 1) * n_readers))
print("── #1181: tasks read in-process ──")
check("GET /v1/tasks (the admin's route): served in-process, no subprocess; a screen still 403",
      ([t["goal"] for t in c.get("/v1/tasks", headers=M).json()["tasks"]], c.get("/v1/tasks", headers=TV).status_code),
      ([f"{SECRET['admin']} task"], 403))
check("the items tasks reader: Alex's own, the screen none",
      ([m["subject"]["id"] for g in c.get("/v1/items?view=person:" + alex, headers=ALEX).json()["groups"] for m in g["members"]
        if m["subject"]["kind"] == "task"], [m for g in c.get("/v1/items?view=now", headers=TV).json()["groups"]
                                              for m in g["members"] if m["subject"]["kind"] == "task"]),
      ([f"task:t-alex"], []))
print("── #1177: Rename / Share on a kept project, only for who may ──")


def mops(h, pid_):
    b = c.get(f"/v1/items?view=project:{pid_}", headers=h).json()
    return sorted((o["verb"], tuple(o.get("params", {}).get("audience") or ())) for o in b.get("operations", [])
                  if o["subject"] == {"kind": "project", "id": pid_} and o["verb"] in ("edit", "share"))


check("Alex's private project: Rename, and Share offering the household's other adults",
      mops(ALEX, SECRET["alex:project"]), [("edit", ()), ("share", tuple(sorted([admin, robin])))])
check("Alex's household project: Rename, no Share (everyone already sees it)", mops(ALEX, hp["id"]), [("edit", ())])
check("Robin on Alex's household project: neither (not the owner)", mops(ROBIN, hp["id"]), [])
check("Ava (a kid): neither", mops(AVA, hp["id"]), [])
check("the admin: Rename on Alex's household project", mops(M, hp["id"]), [("edit", ())])
check("a kid is never offered as someone to share with", ava in json.dumps(mops(ALEX, SECRET["alex:project"])), False)
check("the proposals route carries Keep/Not a project per proposal (none for a kid or the screen)",
      [c.get("/v1/projects/proposals", headers=h).status_code for h in (ALEX, AVA, TV)] +
      [len(c.get("/v1/projects/proposals", headers=AVA).json().get("proposals", []))], [200, 200, 403, 0])
print("── #1181: notes and mail share one KB read per request ──")
import kb_privacy as _kbp  # noqa: E402
_vs_calls = []
_real_vs = _kbp.visible_sources
_kbp.visible_sources = lambda *a, **k: (_vs_calls.append(1), _real_vs(*a, **k))[1]
try:
    c.get("/v1/items?view=now", headers=ALEX)
    one = len(_vs_calls)
    c.get("/v1/items?view=now", headers=ALEX)
    two = len(_vs_calls)
finally:
    _kbp.visible_sources = _real_vs
check("one KB visibility read per request (notes and mail share it), never carried to the next request", (one, two), (1, 2))
print("── the household screen's Time view (tv-948-household-time): household items only ──")
_end3 = (datetime.fromisoformat(in3) + timedelta(days=3)).date().isoformat()
_extra = [{"node_id": "ap-trip", "data": {"title": "Harbor camping trip", "start": in3, "end": _end3, "all_day": True,
                                          "calendar": "Family", "provenance": {"owner": alex, "audience": "household"}}},
          {"node_id": "ap-alex-trip", "data": {"title": f"{SECRET['alex']} work trip", "start": in3, "end": _end3,
                                               "all_day": True, "calendar": "Alex work",
                                               "provenance": {"owner": alex, "audience": "private"}}}]
NODES["appointment"] += _extra
_rng = f"time&from={today}&to={today + timedelta(days=13)}"
tvb = c.get(f"/v1/items?view={_rng}", headers=TV).json()
tv_m = [m for g in tvb["groups"] for m in g["members"]]
check("screen Time: every member is the household's", sorted({m["slots"].get("audience") for m in tv_m}), ["household"])
check("…no bill (money is adults-only, #917), no mail, no task, no chat", sorted({m["subject"]["kind"] for m in tv_m} &
                                                                                 {"bill", "mail", "task", "chat"}), [])
check("…nobody's own or private item, and not the household's bill, in any string",
      [x for x in (SECRET["admin"], SECRET["alex"], SECRET["ava"], "Harbor Gas", "b-house") if x in json.dumps(tvb)], [])
check("…no sentence, next or waiting counts built from anyone's items", sorted(set(tvb) & {"sentence", "next",
      "waiting_on_you", "waiting_on_others", "moving"}), [])
trip = [(g["id"][6:], m["slots"].get("span")) for g in tvb["groups"] for m in g["members"]
        if m["slots"]["title"] == "Harbor camping trip"]
check("…a household trip spans its three days (#1161)", trip,
      [((datetime.fromisoformat(in3) + timedelta(days=i)).date().isoformat(), {"day": i + 1, "of": 3}) for i in range(3)])
check("…Alex's private trip is on no day", "work trip" in json.dumps(tvb), False)
check("…sizeClass counts household members only", tvb["sizeClass"],
      "few" if len({m["subject"]["id"] for m in tv_m}) <= 5 else "dozens")
check("Alex's Time has the household trip too, and not his private one: a person's own appointments stay on their "
      "devices (#1108)", sorted({m["slots"]["title"] for g in c.get(f"/v1/items?view={_rng}", headers=ALEX).json()
                                 ["groups"] for m in g["members"] if "trip" in m["slots"]["title"]}), ["Harbor camping trip"])
for _x in _extra:
    NODES["appointment"].remove(_x)
print("── #1180: a project page's items linked to it (suggested, not filed), behind PROJECT_LINKED ──")
hcon = hooks.connect()
hooks.index(hcon, "scan-alex-3", "scan", hooks.extract(
    {"title": "Receipt 3", "text": "From: Pine Shop <orders@pineshop.example>\n\nOrder #PS-4401 receipt 3."}, lambda t: []),
    alex, "household")
hooks.index(hcon, "scan-robin-4", "scan", hooks.extract(
    {"title": "Receipt 4", "text": "From: Pine Shop <orders@pineshop.example>\n\nOrder #PS-4401 receipt 4."}, lambda t: []),
    robin, "private")
hcon.commit()
hcon.close()
os.environ.pop("PROJECT_LINKED", None)
pv = c.get(f"/v1/items?view=project:{SECRET['alex:project']}", headers=ALEX).json()
check("flag off: no linked group", [g["id"] for g in pv["groups"] if g["id"] == "g-linked"], [])
os.environ["PROJECT_LINKED"] = "1"
pv = c.get(f"/v1/items?view=project:{SECRET['alex:project']}", headers=ALEX).json()
lg = next((g for g in pv["groups"] if g["id"] == "g-linked"), {"members": []})
lids = [m["subject"]["id"] for m in lg["members"]]
check("flag on: an unfiled receipt linked to the project's receipts is suggested here, with its reason",
      ("scan-alex-3" in lids, lg.get("role"), all(str(m["slots"].get("subtitle", "")).startswith(("Same ", "Possibly"))
                                                  for m in lg["members"])), (True, "relationship", True))
fo = next((o for o in pv["operations"] if o["id"] == "op-file-scan-alex-3"), {})
check("…its File carries this project", fo.get("params"), {"project": SECRET["alex:project"]})
check("…showing it filed nothing", "scan-alex-3" in projects.filings(), False)
check("Robin's private receipt (linked the same way) is never on Alex's page", "scan-robin-4" in json.dumps(pv), False)
check("…already-filed items aren't repeated as linked", any(i in lids for i in ("scan-alex-1", "scan-alex-2")), False)
check("a kid's view of a project gets no linked group", [g["id"] for g in c.get(
    f"/v1/items?view=project:{SECRET['ava:project']}", headers=AVA).json()["groups"] if g["id"] == "g-linked"], [])
os.environ.pop("PROJECT_LINKED", None)
import project_linked_eval as PLE  # noqa: E402
import linker as _L2  # noqa: E402
_hc = hooks.connect()
_items, _edges = _L2.scored(_hc)
_hc.close()
_ev = PLE.evaluate(alex, "adult", _items, _edges, {i: p_ for i, p_ in projects.filings().items() if i in _items},
                   __import__("file_suggest").visible_projects(alex, "adult"), __import__("threadview")._visible_fn(alex, "adult"))
check("the leave-one-out measure: each of Alex's filed receipts, held out, is suggested back to its project",
      (_ev["filed"] >= 2, _ev["right"] >= 2, _ev["precision"]), (True, True, 1.0))
print("── #1181: Unsorted's suggestions kept per viewer under a content key ──")
import file_suggest as _fs  # noqa: E402
_sg_calls = []
_real_sg = _fs.suggest
_fs.suggest = lambda *a, **k: (_sg_calls.append(1), _real_sg(*a, **k))[1]
try:
    api._SUGGESTED_MEMO.clear()
    _keep = lambda _i, _g, x: True  # noqa: E731
    a1 = api._linked_suggestions(alex, "adult", _keep, 20, which="t")
    a2 = api._linked_suggestions(alex, "adult", _keep, 20, which="t")
    n_same = len(_sg_calls)
    api._linked_suggestions(admin, "admin", _keep, 20, which="t")
    n_other = len(_sg_calls)
    _hc = hooks.connect()
    hooks.index(_hc, "scan-alex-7", "scan", hooks.extract({"title": "Receipt 7", "text": "Order #PS-4401 receipt 7."},
                                                          lambda t: []), alex, "private")
    _hc.commit()
    _hc.close()
    projects.file(alex, "adult", "scan-alex-7", SECRET["alex:project"])
    api._linked_suggestions(alex, "adult", _keep, 20, which="t")
    n_filed = len(_sg_calls)
    _old = api.SUGGESTED_MEMO_MAX_S
    api.SUGGESTED_MEMO_MAX_S = 0.0
    api._linked_suggestions(alex, "adult", _keep, 20, which="t")
    n_aged = len(_sg_calls)
    api.SUGGESTED_MEMO_MAX_S = _old
finally:
    _fs.suggest = _real_sg
check("the same viewer twice: suggested once, the same items", (n_same, [x["id"] for x in a1] == [x["id"] for x in a2]), (1, True))
check("another viewer is their own entry", n_other, 2)
check("a new filing (or graph change) recomputes", n_filed, 3)
check("…and nothing is older than the ceiling", n_aged, 4)
print("── #1181: every items request's time is logged by view kind and role, nothing else ──")
api.ITEMS_LATENCY_LOG = _TMP / "items-latency.jsonl"
c.get(f"/v1/items?view=project:{SECRET['alex:project']}", headers=ALEX)
c.get("/v1/items?view=now", headers=TV)
_lat = [json.loads(l) for l in api.ITEMS_LATENCY_LOG.read_text().splitlines()]
_k = ["cold", "ms", "parts", "role", "status", "ts", "view"]   # #1552: per-reader ms and the cold-cache flag too
check("one row per request: view kind (no id), role, ms, status, parts, cold",
      [(r["view"], r["role"], sorted(r)) for r in _lat[-2:]], [("project", "adult", _k), ("now", "household", _k)])
check("…never an id or a name", any(x in api.ITEMS_LATENCY_LOG.read_text() for x in (SECRET["alex:project"], alex, "Alex")), False)
print("── #1108: bills, triaged mail, household events, plan steps ──")
for nm, h in (("admin", M), ("alex", ALEX), ("ava", AVA), ("tv", TV)):
    check(f"{nm}: every source answered (nothing unknown: the new readers really ran)",
          c.get("/v1/items?view=now", headers=h).json()["states"]["unknown"], 0)
alex_now = c.get("/v1/items?view=now", headers=ALEX).json()
bill_m = [m for g in alex_now["groups"] for m in g["members"] if m["subject"]["kind"] == "bill"]
check("Alex's Now: his own bill and the household's, amounts as trailing values, no one else's",
      sorted((m["slots"]["title"], m["slots"].get("trailing")) for m in bill_m),
      [("Harbor Gas", {"value": "$61.20"}), (f"{SECRET['alex']} Water", {"value": "$48.20"})])
check("#1155: a bill is a day: slots.all_day and meta the due date (never an hour made from 17:00Z)",
      sorted((m["slots"].get("all_day"), len(m["slots"].get("meta") or "")) for m in bill_m), [(True, 10), (True, 10)])
_evs = [I.from_event({"id": "e1", "title": "Field day", "start": "2026-10-09T00:00:00Z", "all_day": True}),
        I.from_event({"id": "e2", "title": "Book fair", "start": "2026-10-12"}),
        I.from_event({"id": "e3", "title": "Practice", "start": "2026-10-09T17:00:00-07:00", "end": "2026-10-09T18:00:00-07:00"})]
_r = I.resolve(f"time&from=2026-10-08&to=2026-10-14".split("&")[0], None, "household", {"x": lambda: _evs}, now=NOW,
               start="2026-10-08", end="2026-10-14")
_sl = {m["subject"]["id"]: m["slots"] for g in _r["groups"] for m in g["members"]}
check("#1155: an all-day event and a date-only one carry all_day and the date; a timed one keeps its time",
      [(_sl[i].get("all_day"), _sl[i].get("meta")) for i in ("event:e1", "event:e2", "event:e3")],
      [(True, "2026-10-09"), (True, "2026-10-12"), (None, "2026-10-09T17:00:00-07:00")])
print("── #1161: a multi-day item on every day it overlaps (TZ America/Los_Angeles) ──")
from zoneinfo import ZoneInfo  # noqa: E402
LA = ZoneInfo("America/Los_Angeles")
_trip = I.from_event({"id": "trip", "title": "Harbor camping trip", "start": "2026-10-09", "end": "2026-10-12", "all_day": True})
_late = I.from_event({"id": "late", "title": "Late shift", "start": "2026-10-09T22:00:00-07:00", "end": "2026-10-10T02:00:00-07:00"})
_mid = I.from_event({"id": "mid", "title": "Film night", "start": "2026-10-09T20:00:00-07:00", "end": "2026-10-10T00:00:00-07:00"})
_one = I.from_event({"id": "one", "title": "Book fair", "start": "2026-10-12"})
_priv = {**I.from_event({"id": "priv", "title": "zzprivatetrip", "start": "2026-10-09", "end": "2026-10-11", "all_day": True}),
         "owner": "robin", "audience": "private"}


def _days(its, frm, to, viewer="alex", role="adult"):
    r = I.resolve("time", viewer, role, {"x": lambda: its}, now=NOW, tz=LA, start=frm, end=to)
    return r, {g["id"][6:]: [(m["subject"]["id"], m["slots"].get("span"), m["slots"].get("all_day"), m["slots"].get("meta"),
                              m["slots"].get("end")) for m in g["members"]] for g in r["groups"]}


_, dd = _days([_trip, _late, _mid, _one], "2026-10-08", "2026-10-14")
check("an all-day Fri–Sun trip is on Fri, Sat and Sun (exclusive end Mon), each day all-day with its span",
      [(d, x) for d in sorted(dd) for x in dd[d] if x[0] == "event:trip"],
      [("2026-10-09", ("event:trip", {"day": 1, "of": 3}, True, "2026-10-09", "2026-10-12")),
       ("2026-10-10", ("event:trip", {"day": 2, "of": 3}, True, "2026-10-10", "2026-10-12")),
       ("2026-10-11", ("event:trip", {"day": 3, "of": 3}, True, "2026-10-11", "2026-10-12"))])
check("a timed overnight 22:00–02:00: its start time on the first day, all-day on the second",
      [(d, x) for d in sorted(dd) for x in dd[d] if x[0] == "event:late"],
      [("2026-10-09", ("event:late", {"day": 1, "of": 2}, None, "2026-10-09T22:00:00-07:00", "2026-10-10T02:00:00-07:00")),
       ("2026-10-10", ("event:late", {"day": 2, "of": 2}, True, "2026-10-10", "2026-10-10T02:00:00-07:00"))])
check("ending exactly at midnight (exclusive): that one day, unchanged (no span, no end)",
      [(d, x) for d in sorted(dd) for x in dd[d] if x[0] == "event:mid"],
      [("2026-10-09", ("event:mid", None, None, "2026-10-09T20:00:00-07:00", None))])
check("a date-only single day stays on its own day in LA (never the day before through UTC), unchanged",
      [(d, x) for d in sorted(dd) for x in dd[d] if x[0] == "event:one"], [("2026-10-12", ("event:one", None, True, "2026-10-12", None))])
check("on a later day, the continuing span comes first", [x[0] for x in dd["2026-10-10"]], ["event:trip", "event:late"])
_, d2 = _days([_trip], "2026-10-10", "2026-10-14")
check("a span that began before `from` is there from `from` on, numbered from its own start",
      [(d, x[1]) for d in sorted(d2) for x in d2[d]], [("2026-10-10", {"day": 2, "of": 3}), ("2026-10-11", {"day": 3, "of": 3})])
r3, d3 = _days([_trip], "2026-10-04", "2026-10-08")
check("…and one wholly outside the range is on no day", d3, {})
rk, dk = _days([_trip], "2026-10-08", "2026-10-14", viewer="ava", role="kid")
check("a viewer who can't see an item (it's not in their projection): on no day, nowhere in the JSON",
      ("event:priv" in json.dumps(rk), "zzprivatetrip" in json.dumps(rk)), (False, False))
check("…its owner sees it on both its days", [d for d in sorted(_days([_priv], "2026-10-08", "2026-10-14", "robin")[1])],
      ["2026-10-09", "2026-10-10"])
_decade = I.from_event({"id": "decade", "title": "Lease", "start": "2020-01-01", "end": "2030-01-01", "all_day": True})
_calls = {"n": 0}
_real_mid = I._midnight


def _counting_mid(d, tz):
    _calls["n"] += 1
    return _real_mid(d, tz)


I._midnight = _counting_mid
_, d10 = _days([_decade], "2026-10-08", "2026-10-10")
I._midnight = _real_mid
check("#1163: a 10-year span costs only the range's days, and still counts its whole length",
      ([(d, x[1]) for d in sorted(d10) for x in d10[d]], _calls["n"] < 20),
      ([("2026-10-08", {"day": 2473, "of": 3653}), ("2026-10-09", {"day": 2474, "of": 3653}),
        ("2026-10-10", {"day": 2475, "of": 3653})], True))
r4, _ = _days([_trip, _late], "2026-10-08", "2026-10-14")
check("the repeats are one item: one set of operations, sizeClass counts it once",
      (sorted(o["verb"] for o in r4["operations"] if o["subject"]["id"] == "event:trip"), r4["sizeClass"]),
      (["file", "open"], "few"))   # each once (an adult may file a household event, #1169)
check("…and it's in his next steps (yours, by due date)", any(x["kind"] == "bill" for x in alex_now["next"]), True)
for nm, h in (("Ava (a kid)", AVA), ("the screen", TV)):
    kinds = {m["subject"]["kind"] for v in ("now", f"time&from={today}&to={today + timedelta(days=6)}")
             for g in c.get(f"/v1/items?view={v}", headers=h).json()["groups"] for m in g["members"]}
    check(f"{nm} never sees a bill or anyone's mail", kinds & {"bill", "mail"}, set())
check("Alex's own kept mail is in his Now (subject only), never the admin's, even marked household",
      ([m["slots"]["title"] for g in alex_now["groups"] if g["id"] == "g-new-mail" for m in g["members"]],
       "cx-mail-admin" in json.dumps(alex_now)), ([f"{SECRET['alex']} mail subject"], False))
tv_week = members(TV, f"time&from={today}&to={today + timedelta(days=6)}")
check("the household event is on everyone's Time, the screen's too; Alex's private appointment isn't an event here",
      ("Soccer at Harbor Park" in tv_week, "Soccer at Harbor Park" in members(AVA, f"time&from={today}&to={today + timedelta(days=6)}"),
       any("dentist" in t for t in members(ALEX, f"time&from={today}&to={today + timedelta(days=6)}"))), (True, True, False))
pt = I.from_task({"id": "t5", "goal": "Book the campsite", "status": "running", "owner": "alex", "updated_at": "1",
                  "steps": [{"title": "a", "state": "done"}, {"title": "b", "state": "current"}],
                  "autonomy": {"on": True, "next_at": "2026-10-07T09:00:00-07:00", "halted": False}})
ph = I.from_task({"id": "t6", "goal": "Renew the permit", "status": "running", "owner": "alex", "updated_at": "1",
                  "steps": [{"title": "a", "state": "current"}], "autonomy": {"on": False, "halted": True}})
check("plan steps: moving k/n with the next time titan acts; a halted one waits on its owner",
      (pt["state"], ph["state"]), ({"is": "moving", "step": 2, "of": 2, "next_at": "2026-10-07T09:00:00-07:00"},
                                    {"is": "waiting", "who": "alex"}))
check("a project page carries related[] (#1109), empty when nothing relates",
      c.get(f"/v1/items?view=project:{hp['id']}", headers=ALEX).json().get("related"), [])
check("a route the shared screen may use", hh.route_allowed("household", "GET", "/v1/items"), True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
