#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Projects and groups (jellybean#240, #453). Zach, 10-02: projects private by default; groups are homes seen by their
members only; seeds Titan server, Home Projects, Jellybean, Household and Parents (School year in Parents).
The store's rules, then the API: lists, create and change, filing a conversation, "New chat here", and nobody seeing
what isn't theirs. Run: python3 scripts/test_spaces.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-spaces-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "SPACES_DB": "spaces.db",
             "AGENT_TURN_LOG": "turns.jsonl", "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json",
             "PROACTIVE_DB": "proactive.db", "CONTINUITY_DB": "continuity.db", "PLACES_DB": "places.db",
             "TODOS_DB": "todos.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "test"
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import spaces as S  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def err(fn):
    try:
        fn()
    except S.SpaceError as exc:
        return exc.status
    return None


print("── the store ──")
made = S.seed("zach", ["robin"])
check("the seeds Zach chose", made, ["group Household", "group Parents", "project Titan server", "project Home Projects",
                                     "project Jellybean", "project School year"])
check("…idempotent", S.seed("zach", ["robin"]), [])
names = lambda ps: sorted(p["name"] for p in ps)  # noqa: E731
check("Zach sees his three and Parents' School year", names(S.projects_for("zach", "admin")),
      ["Home Projects", "Jellybean", "School year", "Titan server"])
check("Robin: private by default, so only Parents' project", names(S.projects_for("robin", "adult")), ["School year"])
check("Jordan (an adult, not a parent): none", names(S.projects_for("jordan", "adult")), [])
check("groups: Robin is in both, Jordan only Household, a kid only Household",
      ([g["name"] for g in S.groups_for("robin", "adult")], [g["name"] for g in S.groups_for("jordan", "adult")],
       [g["name"] for g in S.groups_for("ava", "kid")]), (["Household", "Parents"], ["Household"], ["Household"]))
check("a shared household device: no groups, no projects", (S.groups_for(None, "household"),
                                                            S.projects_for("kitchen", "household")), ([], []))
mine = S.create_project("jordan", "adult", "Bend trip")
check("a new project is private", (mine["audience"], mine["owner"]), ("private", "jordan"))
check("…Zach (the admin) doesn't see it either", "Bend trip" in names(S.projects_for("zach", "admin")), False)
S.update_project(mine["id"], "jordan", "adult", audience=["zach"])
check("shared with Zach: now he does", "Bend trip" in names(S.projects_for("zach", "admin")), True)
check("only its owner changes it", err(lambda: S.update_project(mine["id"], "zach", "adult", name="x")), 403)
check("someone who can't see it: 404", err(lambda: S.update_project(mine["id"], "ava", "kid", name="x")), 404)
parents = next(g for g in S.groups_for("zach", "admin") if g["name"] == "Parents")
check("a project in a group you're not in: refused", err(lambda: S.create_project("jordan", "adult", "x",
                                                                                  group=parents["id"])), 403)
check("a kid can't make a group", err(lambda: S.create_group("ava", "kid", "Kids", ["ava", "leo"])), 403)
check("names are unique", err(lambda: S.create_group("zach", "admin", "parents", ["zach"])), 409)
g = S.create_group("robin", "adult", "Book club", ["jordan"])
check("the maker is in the group they make", g["members"], ["jordan", "robin"])
check("filing: a project you can see, its group comes with it",
      S.check_target("zach", "admin", project=next(p["id"] for p in S.projects_for("zach", "admin")
                                                    if p["name"] == "School year")), (
          next(p["id"] for p in S.projects_for("zach", "admin") if p["name"] == "School year"), parents["id"]))
check("…one you can't: 403", err(lambda: S.check_target("jordan", "adult", project=mine["id"].replace("pj_", "pj_0"))), 403)

print("── the API ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(body, name, platform="ios"):
    s = c.post("/v1/pair/start", json=body, headers=M).json()
    t = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": name, "platform": platform}).json()["token"]
    return {"X-Agent-Token": t}, s["person"]["id"]


ZP, zid = pair({"person_id": hh.owner_identity().person_id}, "Zach's iPhone")
MIR, mid = pair({"new_person": {"name": "Robin", "role": "adult"}}, "Robin's iPhone")
KID, kid = pair({"new_person": {"name": "Ava", "role": "kid"}}, "Ava's iPhone")
HOUSE, _ = pair({"new_person": {"name": "Kitchen", "role": "household"}}, "Kitchen iPad", "ipados")

os.environ["SPACES_DB"] = str(_TMP / "api-spaces.db")
r = c.post("/v1/projects", headers=ZP, json={"name": "Titan server", "icon": "server", "color": "blue"})
check("POST /v1/projects: 201, private, his", (r.status_code, r.json()["audience"], r.json()["owner"]), (201, "private", zid))
pj = r.json()["id"]
check("GET: his; Robin's list is empty", ([p["name"] for p in c.get("/v1/projects", headers=ZP).json()["projects"]],
                                           c.get("/v1/projects", headers=MIR).json()["projects"]), (["Titan server"], []))
check("PATCH by someone who can't see it: 404", c.patch(f"/v1/projects/{pj}", headers=MIR, json={"name": "x"}).status_code, 404)
r = c.post("/v1/groups", headers=ZP, json={"name": "Parents", "members": [zid, mid]})
gid = r.json()["id"]
got = c.get("/v1/groups", headers=MIR).json()["groups"]
check("a group: Robin sees it with its people by name",
      ([x["name"] for x in got], sorted(p["name"] for p in got[0]["people"])), (["Parents"], ["Robin", "Zach"]))
check("a kid isn't in Parents", c.get("/v1/groups", headers=KID).json()["groups"], [])
check("a household device: 403 on groups and projects", (c.get("/v1/groups", headers=HOUSE).status_code,
                                                         c.get("/v1/projects", headers=HOUSE).status_code), (403, 403))
check("a kid can't make a group", c.post("/v1/groups", headers=KID, json={"name": "K", "members": [kid]}).status_code, 403)

# Conversations: a small memory store with Zach's chat (one thread) and Robin's.
db = sqlite3.connect(str(_TMP / "mem.db"), check_same_thread=False)
db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, ts REAL, thread TEXT, device TEXT)")
now = time.time()
db.executemany("INSERT INTO messages (conv_id, role, content, ts, thread, device) VALUES (?,?,?,?,?,?)",
               [(hh.conv_id_for(zid), "user", "How much VRAM is free?", now - 60, "t-zach-1", "Mac"),
                (hh.conv_id_for(zid), "assistant", "GPU 2 is idle.", now - 50, "t-zach-1", "Mac"),
                (hh.conv_id_for(mid), "user", "Permission slip?", now - 40, "t-rob-1", "iPhone")])
db.commit()


class Mem:
    _db = db

    def add_message(self, conv, role, text, **where):
        db.execute("INSERT INTO messages (conv_id, role, content, ts, thread, device) VALUES (?,?,?,?,?,?)",
                   (conv, role, text, time.time(), where.get("thread"), where.get("device")))
        db.commit()
        return None


api._get_memory = lambda: Mem()
api._MEMORY_AVAILABLE = True
convs = c.get("/v1/conversations", headers=ZP).json()["conversations"]
check("conversations carry project and group (null until filed)", (convs[0]["id"], convs[0]["project"], convs[0]["group"]),
      ("t-zach-1", None, None))
r = c.patch("/v1/conversations/t-zach-1", headers=ZP, json={"project": pj})
check("PATCH files it under his project", (r.status_code, r.json()), (200, {"id": "t-zach-1", "project": pj, "group": None}))
check("…and the list shows it", c.get("/v1/conversations", headers=ZP).json()["conversations"][0]["project"], pj)
check("someone else's conversation: 404", c.patch("/v1/conversations/t-rob-1", headers=ZP, json={"project": pj}).status_code, 404)
check("Robin filing hers under Zach's private project: 403",
      c.patch("/v1/conversations/t-rob-1", headers=MIR, json={"project": pj}).status_code, 403)
check("…under Parents, which she's in: fine", c.patch("/v1/conversations/t-rob-1", headers=MIR, json={"group": gid}).json(),
      {"id": "t-rob-1", "project": None, "group": gid})
check("null clears it", c.patch("/v1/conversations/t-zach-1", headers=ZP, json={"project": None}).json()["project"], None)
print("── a PATCH changes only the fields it names ──")
c.patch("/v1/conversations/t-zach-1", headers=ZP, json={"group": gid})
check("project null keeps a manual group (an Undo of an automatic project)",
      c.patch("/v1/conversations/t-zach-1", headers=ZP, json={"project": None}).json(), {"id": "t-zach-1", "project": None,
                                                                                           "group": gid})
c.patch("/v1/conversations/t-zach-1", headers=ZP, json={"project": pj, "group": None})
check("group alone keeps a manual project", c.patch("/v1/conversations/t-zach-1", headers=ZP, json={"group": gid}).json(),
      {"id": "t-zach-1", "project": pj, "group": gid})
check("both null clears both", c.patch("/v1/conversations/t-zach-1", headers=ZP, json={"project": None, "group": None}).json(),
      {"id": "t-zach-1", "project": None, "group": None})

# "New chat here": a new thread's first turn files it.
api._turns["t-new"] = {"source": None, "remember": True, "thread": "t-zach-2", "file_under": {"group": gid}}
api._remember_turn("t-new", hh.owner_identity() if zid == hh.owner_identity().person_id else hh.owner_identity(),
                   "Plan the school year", "Here's a start.")
got = {x["id"]: x["group"] for x in c.get("/v1/conversations", headers=ZP).json()["conversations"]}
check("New chat here: the new thread is filed under the group on its first turn", got.get("t-zach-2"), gid)
api._turns["t-new2"] = {"source": None, "remember": True, "thread": "t-zach-2", "file_under": {"project": pj}}
api._remember_turn("t-new2", hh.owner_identity(), "More", "Sure.")
check("…a later turn doesn't refile it", {x["id"]: x["group"] for x in c.get("/v1/conversations", headers=ZP).json()
                                          ["conversations"]}.get("t-zach-2"), gid)
print("── the group home ──")
r = c.patch(f"/v1/groups/{gid}", headers=ZP, json={"calendar": "Parents"})
check("a group names the calendar its page shows", r.json()["calendar"], "Parents")
check("a member adds to the group's own to-do list", c.post("/v1/todos", headers=MIR, json={"title": "Return permission slip",
                                                                                          "for": gid}).status_code, 200)
check("…someone outside it can't", c.post("/v1/todos", headers=KID, json={"title": "x", "for": gid}).status_code, 403)
sp = c.post("/v1/projects", headers=ZP, json={"name": "School year", "group": gid}).json()["id"]
c.patch("/v1/conversations/t-zach-1", headers=ZP, json={"project": sp})
h = c.get(f"/v1/groups/{gid}/home", headers=ZP).json()
check("Zach's view: the group's project, his chats filed there, its to-dos, its people, the calendar",
      ([p["name"] for p in h["projects"]], sorted(x["id"] for x in h["chats"]), [t["title"] for t in h["todos"]],
       sorted(p["name"] for p in h["people"]), h["calendar"]),
      (["School year"], ["t-zach-1", "t-zach-2"], ["Return permission slip"], ["Robin", "Zach"], "Parents"))
h = c.get(f"/v1/groups/{gid}/home", headers=MIR).json()
check("Robin's view: the same group, but only her own chats", sorted(x["id"] for x in h["chats"]), ["t-rob-1"])
check("someone outside the group: 404", c.get(f"/v1/groups/{gid}/home", headers=KID).status_code, 404)
check("the group's to-dos show in a member's to-do lists, not an outsider's",
      ("Return permission slip" in [t["title"] for t in c.get("/v1/todos", headers=ZP).json()["todos"]],
       "Return permission slip" in [t["title"] for t in c.get("/v1/todos", headers=KID).json()["todos"]]), (True, False))
check("the routes are in the role table for people (a kid's projects list: 200)",
      c.get("/v1/projects", headers=KID).status_code, 200)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
