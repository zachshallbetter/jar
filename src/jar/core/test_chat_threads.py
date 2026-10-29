#!/usr/bin/env python3
# gate: hermetic
"""The threads view of chat history (#1016, GET /v1/conversations?view=threads): threads from the linker's strong
edges, earned names or a description, facets and filters, the person's statements, bundling only at measured
precision, and only ever the asker's own stream. Fictional people only (Alex, Robin, Ava); temporary stores.
Run: python3 scripts/test_chat_threads.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-chat-threads-test-"))
for k, v in {"LINK_SALT_FILE": "link_salt", "LINKS_DB": "links.db", "WORLD_DIR": "world", "WORLD_DB": "world.db",
             "HOUSEHOLD_DB": "household.db", "SPACES_DB": "spaces.db", "MEMORY_DB": "memory.db",
             "AGENT_TURN_LOG": "turns.jsonl", "CHAT_LINKS_LOCAL_URL": "http://127.0.0.1:9", "PLACES_DB": "places.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["PLACES_JITTER_S"] = "0"
sys.path.insert(0, str(Path(__file__).parent))
import chat_links as CL  # noqa: E402
import chat_threads as CT  # noqa: E402
import hooks as H  # noqa: E402
import linker as L  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


T0 = 1_790_000_000.0
MIN = 60.0
NOW = T0 + 3 * 86400
PEOPLE = [{"id": "alex", "name": "Alex Hale"}, {"id": "robin", "name": "Robin Hale"}, {"id": "ava", "name": "Ava Hale"}]
mem = sqlite3.connect(":memory:")
mem.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, ts REAL, thread TEXT, "
            "device TEXT, client_msg_id TEXT)")
con = H.connect()
_n = 0


def chat(conv_id, thread, at_min, said, device="Alex's iPhone", tools=(), targets=()):
    """One exchange: the person's message, titan's reply, and the turn's noted tools and targets."""
    global _n
    _n += 1
    uid = _n
    mem.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,NULL)", (uid, conv_id, "user", said, T0 + at_min * MIN, thread, device))
    _n += 1
    mem.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,NULL)", (_n, conv_id, "assistant", "Done.", T0 + at_min * MIN + 3,
                                                                     thread, device))
    person = conv_id.split(":", 1)[1] if ":" in conv_id else "alex"
    CL.note_turn(person, uid, thread, T0 + at_min * MIN, list(tools), list(targets), con)


REM = CL.targets("update_reminder", {"reminder_id": "R-31"})[0]
GARAGE = CL.targets("home_control", {"device": "garage door"})[0]
# Alex: a reminder handled over two chats 20 min apart; a garage pair; an unrelated weather chat; Robin named twice
chat("chat:alex", "th-a1", 0, "Remind me to call Lakeside Clinic about the referral", tools=["create_reminder"], targets=[REM])
chat("chat:alex", "th-a2", 20, "Move the Lakeside Clinic referral reminder to Friday", tools=["update_reminder"], targets=[REM])
chat("chat:alex", "th-a3", 300, "Close the garage", device="Alex's Mac", tools=["home_control"], targets=[GARAGE])
chat("chat:alex", "th-a4", 315, "Is the garage closed now?", device="Alex's Mac", tools=["home_status"], targets=[GARAGE])
chat("chat:alex", "th-a5", 900, "What's the weather this weekend?", tools=["get_weather"])
chat("chat:alex", "th-a6", 1500, "What does Robin have on Thursday?", tools=["list_calendar_events"])
chat("chat:alex", "th-a7", 2600, "Did Robin confirm the dinner?", tools=["search_messages"])
# Robin's own chat touches the same reminder id: never joins Alex's
chat("chat:robin", "th-r1", 25, "Did the clinic reminder move?", device="Robin's iPhone", tools=["list_reminders"], targets=[REM])

KW = dict(mem=mem, now=NOW, con=con, entities=None, people=PEOPLE, tags={"th-a3": {"project": "pj-house", "group": None}},
          lineage={}, projects=[{"id": "pj-house", "name": "House"}])


def build(pid="alex", role="admin", **kw):
    return CT.build(pid, role, **{**KW, **kw})


v = build()
print("── threads ──")
by_members = {tuple(sorted(r["id"] for r in t["conversations"])): t for t in v["threads"]}
check("the reminder's two chats are one thread", ("th-a1", "th-a2") in by_members, True)
check("the garage pair is another", ("th-a3", "th-a4") in by_members, True)
check("the weather chat is alone (in recent, in no thread)", any("th-a5" in m for m in by_members), False)
t = by_members[("th-a1", "th-a2")]
check("its edge says why", (t["edges"][0]["type"], t["edges"][0]["state"], "same record touched" in t["edges"][0]["why"]),
      ("continues", "accepted", True))
check("a name is earned from a phrase both titles share", (t["name"] or "").lower(), "lakeside clinic")
check("not bundled before any labels", t["bundled"], False)
g = by_members[("th-a3", "th-a4")]
check("no shared rare phrase: no name, a description", (g["name"], g["description"].startswith("2 chats")), (None, True))
check("the same members always get the same id", build()["threads"][0]["id"] == v["threads"][0]["id"], True)
rp = [t for t in v["threads"] if {"th-a6", "th-a7"} <= {r["id"] for r in t["conversations"]}]
check("Robin named in two chats a day apart: possibly related (fringe), never a thread", rp, [])

print("── rows and facets ──")
check("recent lists every conversation, newest first, each with its item_id",
      [(r["id"], r["item_id"]) for r in v["recent"]][:2], [("th-a7", "chat-th-a7"), ("th-a6", "chat-th-a6")])
check("rows carry kinds and people", (v["recent"][0]["kinds"], v["recent"][0]["people"]), (["files"], ["robin"]))
check("kind facet counts", {f["value"]: f["count"] for f in v["facets"]["kind"]}["calendar"], 3)
check("person facet, labelled by first name", v["facets"]["person"], [{"value": "robin", "label": "Robin", "count": 2}])
check("device facet", {f["value"]: f["count"] for f in v["facets"]["device"]}, {"Alex's iPhone": 5, "Alex's Mac": 2})
check("project facet, labelled", v["facets"]["project"], [{"value": "pj-house", "label": "House", "count": 1}])
check("a manual tag is the row's project, not automatic", next(r["project"] for r in v["recent"] if r["id"] == "th-a3"),
      {"id": "pj-house", "name": "House", "auto": False})
check("room is in the contract, empty until recorded; no place events, no place", (v["facets"]["room"], v["facets"]["place"],
                                                                                   v["recent"][0]["place"]), ([], [], None))
f = build(kind="home")
check("a filter narrows recent and threads", ([r["id"] for r in f["recent"]], [len(t["conversations"]) for t in f["threads"]],
                                              f["filters"]), (["th-a4", "th-a3"], [2], [{"axis": "kind", "value": "home"}]))
check("filter by person", [r["id"] for r in build(person="robin")["recent"]], ["th-a7", "th-a6"])

print("── the person's word ──")
L.say(con, "chat-th-a1", "chat-th-a2", False, "alex", "correction")
check("'different' splits a thread", any({"th-a1", "th-a2"} <= {r["id"] for r in t["conversations"]} for t in build()["threads"]),
      False)
L.say(con, "chat-th-a6", "chat-th-a7", True, "alex", "correction")
j = [t for t in build()["threads"] if {"th-a6", "th-a7"} <= {r["id"] for r in t["conversations"]}]
check("'same' joins two chats the code didn't", (len(j), j and j[0]["edges"][0]["state"]), (1, "verified"))

print("── bundling: only at measured precision ──")
E = CL.Edge
code = [E(f"chat-x{i}", f"chat-y{i}", "continues", "accepted", "alex", [], {}, "") for i in range(25)] + \
    [E("chat-e1", "chat-e2", "entity", "candidate", "alex", [], {}, "")]
labels = {(f"chat-x{i}", f"chat-y{i}"): i != 0 for i in range(25)}
p = CT.precision(code, labels)
check("continues: 24 of 25 labelled related", (p["continues"]["labelled"], p["continues"]["precision"]), (25, 0.96))
check("…bundles at ≥ 90% over ≥ 20 labels", p["continues"]["bundles"], True)
check("too few labels never bundle", CT.precision(code[:10], {k: True for k in list(labels)[:10]})["continues"]["bundles"], False)
check("an unlabelled type never bundles", p["entity"]["bundles"], False)
check("a lower bound is reported", p["continues"]["low95"] is not None and p["continues"]["low95"] < 0.96, True)

print("── only the asker's own ──")
r = build("robin", "adult")
check("Robin sees only Robin's", [x["id"] for x in r["recent"]], ["th-r1"])
check("Alex's view never holds Robin's chat", any(x["id"] == "th-r1" for x in build()["recent"]), False)
for label, args, code_ in (("a shared screen has no history", ("house", "household"), 403),):
    try:
        build(*args)
        check(label, "shown", code_)
    except CT.Refused as e:
        check(label, e.status, code_)
for i in range(30):
    L.say(con, f"chat-th-k{i}", f"chat-th-k{i}b", True, "ava", "baseline")
chat("chat:ava", "th-k1", 10, "Help me with my science project")
chat("chat:ava", "th-k2", 15, "And the science project poster?")
L.say(con, "chat-th-k1", "chat-th-k2", True, "ava", "correction")
kid = build("ava", "kid")
check("a kid sees their own, never bundled", ([x["id"] for x in kid["recent"]], [t["bundled"] for t in kid["threads"]]),
      (["th-k2", "th-k1"], [False]))
try:
    build(since="not a date")
    check("a bad since is a 400", "accepted", 400)
except CT.Refused as e:
    check("a bad since is a 400", e.status, 400)

print("── automatic projects, with Undo ──")
from datetime import datetime as _dt, timezone as _tz  # noqa: E402
import places as PL  # noqa: E402
PROJ = [{"id": "pj-house", "name": "House"}, {"id": "pj-harbor", "name": "Harbor App"}]
APP = CL.targets("read_file", {"path": "~/Projects/harbor-app/src/main.py"})
chat("chat:alex", "th-a8", 3000, "Why does the build fail?", device="Alex's Mac", tools=["read_file"], targets=APP)
chat("chat:alex", "th-a9", 3010, "Fix the failing test", device="Alex's Mac", tools=["read_file"], targets=APP)
w = build(projects=PROJ)
row8 = next(r for r in w["recent"] if r["id"] == "th-a8")
check("a chat whose own project key names a project gets it, automatic", row8["project"],
      {"id": "pj-harbor", "name": "Harbor App", "auto": True})
th = next(t for t in w["threads"] if {"th-a8", "th-a9"} <= {r["id"] for r in t["conversations"]})
check("a thread whose chats share a project carries it", th["project"], {"id": "pj-harbor", "name": "Harbor App", "auto": True})
check("filter by an automatic project", [r["id"] for r in build(projects=PROJ, project="pj-harbor")["recent"]],
      ["th-a9", "th-a8"])
check("a manual tag always wins over the automatic one",
      next(r["project"] for r in build(projects=PROJ, tags={"th-a8": {"project": "pj-house", "group": None}})["recent"]
           if r["id"] == "th-a8"), {"id": "pj-house", "name": "House", "auto": False})
check("two projects the key could mean: none", next(r["project"] for r in build(
    projects=PROJ + [{"id": "pj-dup", "name": "harbor-app"}])["recent"] if r["id"] == "th-a8"), None)
check("Undo records the person's correction", CT.reject_auto("alex", "admin", mem, "th-a8", con, PROJ), "pj-harbor")
check("…as a 'not related' correction statement", con.execute(
    "SELECT related, source FROM link_statements WHERE a='chat-th-a8' AND b='project:pj-harbor'").fetchone(), (0, "correction"))
check("…and the project is never offered for that chat again",
      (next(r["project"] for r in build(projects=PROJ)["recent"] if r["id"] == "th-a8"),
       next(r["project"]["id"] for r in build(projects=PROJ)["recent"] if r["id"] == "th-a9")), (None, "pj-harbor"))
check("nothing automatic to undo: nothing recorded", CT.reject_auto("alex", "admin", mem, "th-a5", con, PROJ), None)

print("── where the asker was when a chat began ──")
iso = lambda m: _dt.fromtimestamp(T0 + m * MIN, _tz.utc).isoformat()  # noqa: E731
PL.record("ev-1", "alex", "arrived", "Home", iso(-10))
PL.record("ev-2", "alex", "left", "Home", iso(290))
PL.record("ev-3", "alex", "arrived", "Lakeside Clinic", iso(890))
PL.record("ev-r", "robin", "arrived", "Harbor Gym", iso(10))
q = build()
place = {r["id"]: r["place"] for r in q["recent"]}
check("arrived home before the first chat: home (the store's own spelling)", (place["th-a1"], place["th-a2"]), ("home", "home"))
check("left home before it: unknown", place["th-a3"], None)
check("the latest arrival before it", place["th-a5"], "Lakeside Clinic")
check("place facet", {f["value"]: f["count"] for f in q["facets"]["place"]}["home"], 2)
check("filter by place", [r["id"] for r in build(place="home")["recent"]], ["th-a2", "th-a1"])
check("only the asker's own places: Robin's never on Alex's chats", any(r["place"] == "Harbor Gym" for r in q["recent"]), False)
check("Robin's own chat shows Robin's place", build("robin", "adult")["recent"][0]["place"], "Harbor Gym")

print("── rooms and text search (#1016 leftovers) ──")
KITCHEN = CL.targets("home_control", {"device": "kitchen light"})[0]
chat("chat:robin", "th-r2", 40, "Turn off the kitchen light", device="Robin's iPhone", tools=["home_control"], targets=[KITCHEN])
ROOMS = {GARAGE[1]: "Garage", KITCHEN[1]: "Kitchen"}
a = build("alex", "admin", rooms=ROOMS)
check("Alex's room facet: only the rooms his own turns targeted (the garage), never Robin's kitchen",
      [f["value"] for f in a["facets"]["room"]], ["Garage"])
check("room=Garage filters to his garage chats", {c["id"] for c in build("alex", "admin", rooms=ROOMS, room="Garage")["recent"]}
      <= {"th-a3", "th-a4"} and bool(build("alex", "admin", rooms=ROOMS, room="Garage")["recent"]), True)
check("room=Kitchen for Alex: nothing (Robin's room is never his)", build("alex", "admin", rooms=ROOMS, room="Kitchen")["recent"], [])
check("Robin sees her kitchen", [f["value"] for f in build("robin", "adult", rooms=ROOMS)["facets"]["room"]], ["Kitchen"])
check("no Home Assistant: no rooms, no error", build("alex", "admin", rooms={})["facets"]["room"], [])
qa = build("alex", "admin", rooms=ROOMS, q="clinic")
check("q=clinic for Alex: his own clinic chats, matched in his own words", (bool(qa["recent"]),
      all(r["id"].startswith("th-a") for r in qa["recent"]), {"axis": "q", "value": "clinic"} in qa["filters"]), (True, True, True))
qr = build("robin", "adult", rooms=ROOMS, q="clinic")
check("q=clinic for Robin: only hers, never Alex's words or titles", [r["id"] for r in qr["recent"]], ["th-r1"])
check("q matching nothing of theirs: empty, even though Alex said 'referral'",
      build("robin", "adult", rooms=ROOMS, q="referral")["recent"], [])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
