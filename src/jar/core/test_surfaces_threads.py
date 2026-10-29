#!/usr/bin/env python3
# gate: hermetic
"""Chat threads as a ResolvedSurface (#1060 migration step 3: `collection`, GET /v1/conversations?view=threads&as=surface).
Built from chat_threads.build's real payload over fictional chats (test_chat_threads.py's), never a hand-made shape.
S1: golden vector scripts/evalsets/surfaces/threads.alex.json (`--write` rewrites it; review the diff). Rules: one group
per thread named by titan, every recent row exactly once (bundle, never merge), the chat ops. S2 through the real route:
only the asker's own chats, a shared screen refused, filters carried, deterministic, old shape unchanged.
Run: python3 scripts/test_surfaces_threads.py [--write]"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-chat-threads-test-"))
for k, v in {"LINK_SALT_FILE": "link_salt", "LINKS_DB": "links.db", "WORLD_DIR": "world", "WORLD_DB": "world.db",
             "HOUSEHOLD_DB": "household.db", "SPACES_DB": "spaces.db", "MEMORY_DB": "memory.db",
             "AGENT_TURN_LOG": "turns.jsonl", "CHAT_LINKS_LOCAL_URL": "http://127.0.0.1:9", "PLACES_DB": "places.db", "SURFACES_DB": "surfaces.db", "PROACTIVE_DB": "proactive.db",
             "SITTER_DB": "sitter.db", "AGENT_MEMORY_DIR": "memory", "HOUSEHOLD_PREFS": "household.json"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["PLACES_JITTER_S"] = "0"
sys.path.insert(0, str(Path(__file__).parent))
import json  # noqa: E402
import re  # noqa: E402

import chat_links as CL  # noqa: E402
import surfaces as S  # noqa: E402
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
mem = sqlite3.connect(":memory:", check_same_thread=False)   # the route builds in a worker thread
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


VECTORS = Path(__file__).parent / "evalsets" / "surfaces"
WRITE = "--write" in sys.argv


def build(pid="alex", role="admin", **kw):
    return CT.build(pid, role, **{**KW, **kw})


def vector(name, got):
    p = VECTORS / f"{name}.json"
    if WRITE:
        p.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    check(f"S1 golden vector {name}", got, json.loads(p.read_text()) if p.exists() else None)


def ids(s):
    return [m["subject"]["id"] for g in s["groups"] for m in g["members"]]


print("── S1: the real threads view, resolved ──")
body = build()
surf = S.threads(body, "alex", "admin", NOW)
vector("threads.alex", surf)
check("profile collection, purpose browse, jb-1", (surf["profile"], surf["purpose"], surf["profileVersion"]),
      ("collection", "browse", "jb-1"))
check("one group per thread titan showed, then Other chats",
      [g["id"] for g in surf["groups"]], [f"g-thread-{t['id']}" for t in body["threads"]] + ["g-other"])
check("a thread group's label text is titan's name for it (else its description), under one stable key",
      [(g["label"]["key"], g["label"]["text"]) for g in surf["groups"][:-1]],
      [("threads.thread", t["name"] or t["description"]) for t in body["threads"]])
check("…the fixture has both kinds: an earned name and an unnamed thread", sorted(bool(t["name"]) for t in body["threads"]),
      [False, True])
check("Other chats has English text under its key", surf["groups"][-1]["label"], {"key": "threads.other", "text": "Other chats"})
check("bundle, never merge: every recent row is a member exactly once",
      (sorted(ids(surf)) == sorted(set(ids(surf))), set(f"chat:{r['id']}" for r in body["recent"]) <= set(ids(surf))),
      (True, True))
check("a thread's members are its own conversations, newest first",
      [[m["subject"]["id"] for m in g["members"]] for g in surf["groups"][:-1]],
      [[f"chat:{r['id']}" for r in sorted(t["conversations"], key=lambda r: (str(r["last_at"]), str(r["id"])), reverse=True)]
       for t in body["threads"]])
OPS = {o["id"]: o for o in surf["operations"]}
check("every member's actions name an operation the surface carries",
      [a for g in surf["groups"] for m in g["members"] for a in m["slots"]["actions"] if a not in OPS], [])
check("file goes through PATCH /v1/conversations/{id}, open stays in the app, the person's word through relate",
      sorted({(o["verb"], (o.get("route") or {}).get("method"), (o.get("route") or {}).get("path", "").rsplit("/", 1)[0])
              for o in surf["operations"]}),
      [("file", "PATCH", "/v1/conversations"), ("open", None, ""), ("relate", "POST", "/v1/threads"),
       ("unrelate", "POST", "/v1/threads")])
G = [g for g in surf["groups"] if g["id"].startswith("g-thread-")]
check("each chat carries the item id relate takes", sorted({m["item_id"][:5] for g in surf["groups"] for m in g["members"]}),
      ["chat-"])
check("each thread carries its edges verbatim", [g["edges"] for g in G], [t["edges"] for t in body["threads"]])
vb = json.loads(json.dumps(body))
for e in vb["threads"][0]["edges"]:
    e["state"] = "verified"
vs = S.threads(vb, "alex", "admin", NOW)
check("a thread whose every edge the person already confirmed asks nothing: no same_op, no Same thread op",
      ("same_op" in vs["groups"][0], f"op-same-{vb['threads'][0]['id']}" in {o["id"] for o in vs["operations"]},
       "same_op" in vs["groups"][1]), (False, False, True))
check("Same thread: one op per thread, each edge titan joined on its own as a related pair",
      [OPS[g["same_op"]]["params"] for g in G],
      [{"pairs": [[e["a"], e["b"]] for e in t["edges"] if e["state"] != "verified"], "related": True}
       for t in body["threads"]])
check("Not in this thread: on each thread member, every edge it is in, not related",
      [(m["subject"]["id"], OPS[f"op-notin-{g['id'][9:]}-{m['subject']['id']}"]["params"]) for g in G for m in g["members"]],
      [(f"chat:{r['id']}", {"pairs": [[e["a"], e["b"]] for e in t["edges"] if r["item_id"] in (e["a"], e["b"])],
                             "related": False})
       for t in body["threads"] for r in sorted(t["conversations"], key=lambda r: (str(r["last_at"]), str(r["id"])), reverse=True)])
check("…listed in the member's actions", all(f"op-notin-{g['id'][9:]}-{m['subject']['id']}" in m["slots"]["actions"]
                                             for g in G for m in g["members"]), True)
check("relate contracts", sorted({o["contract"] for o in surf["operations"] if o["verb"] in ("relate", "unrelate")}),
      ["jellybean.thread.relate@1", "jellybean.thread.unrelate@1"])
fb = json.loads(json.dumps(body))
e0 = fb["threads"][1]["edges"][0]
other = next(r for r in fb["recent"] if r["item_id"] not in (e0["a"], e0["b"]))
fb["threads"][1]["fringe"] = [{"item_id": other["item_id"], "edge": {**e0, "a": e0["a"], "b": other["item_id"],
                                                                     "state": "candidate"}}]
fs = S.threads(fb, "alex", "admin", NOW)
fg = next(g for g in fs["groups"] if g["id"] == f"g-thread-{fb['threads'][1]['id']}")
check("a fringe chat (possibly this thread) gets a Same thread op for its one pair, listed on the group",
      (fg["fringe"] == fb["threads"][1]["fringe"],
       [{o["id"]: o for o in fs["operations"]}[i]["params"] for i in fg["fringe_ops"]]),
      (True, [{"pairs": [[e0["a"], other["item_id"]]], "related": True}]))
check("…and it stays a member of Other chats, once", ids(fs).count(f"chat:{other['id']}"), 1)
check("a chat filed under a project says so", sorted({p for g in surf["groups"] for m in g["members"]
                                                      for p in m["slots"].get("projects", [])}), ["pj-house"])
check("deterministic (S2-9)", S.threads(build(), "alex", "admin", NOW), surf)
check("an empty history is one empty Other chats group, sizeClass few",
      ([g["id"] for g in S.threads({}, "ava", "kid", NOW)["groups"]], S.threads({}, "ava", "kid", NOW)["sizeClass"]),
      (["g-other"], "few"))
f = S.threads(build(kind="home"), "alex", "admin", NOW)
check("filters ride along as the lens and for the chips", (f["filters"], f["accessProjection"]["viewer"]),
      ([{"axis": "kind", "value": "home"}], "alex"))

print("── S2: through the real route ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

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
    return {"X-Agent-Token": tok}, next(p["id"] for p in st.people() if p["name"] == name)


ROBIN, robin = pair("Robin Quarry", "adult")
TV, _ = pair("Living room", "household", "tvos")
_real = CT.build
asked = []


def fake_build(pid, role, mem_, **kw):   # the route's call, over this test's fictional chats
    asked.append(pid)
    who = "alex" if pid == hh.ADMIN_PERSON_ID else "robin" if pid == robin else pid
    return _real(who, role, **{**KW, "con": H.connect(), **{k: v for k, v in kw.items() if v}})


CT.build = fake_build
api._MEMORY_AVAILABLE = True
api._get_memory = lambda: type("Mem", (), {"_db": mem})()


def surface(h, q=""):
    r = c.get(f"/v1/conversations?view=threads&as=surface{q}", headers=h)
    return r.status_code, r.json()


code, a = surface(M)
check("admin: 200, jb-1 collection", (code, a.get("profileVersion"), a.get("profile")), (200, "jb-1", "collection"))
check("…the route resolves exactly what the resolver does for that view", ids(a), ids(surf))
code, r = surface(ROBIN)
check("Robin: only Robin's own chats, never one of Alex's", (code, [i for i in ids(r) if i in set(ids(a))]), (200, []))
check("…and Robin's own reminder chat is there", len(ids(r)), 1)
check("a shared screen has no history (403, as the plain view)", surface(TV)[0], 403)
check("filters pass through the route", surface(M, "&kind=home")[1]["filters"], [{"axis": "kind", "value": "home"}])
check("same state, same fingerprint (S2-9)", surface(ROBIN)[1]["fingerprint"], surface(ROBIN)[1]["fingerprint"])
paths = {re.sub(r"\{[^}]+\}", "X", rt.path) for rt in api.app.routes if hasattr(rt, "path")}
check("every operation route is one the app has (none invented)",
      sorted({o["route"]["path"] for o in a["operations"] if "route" in o
              and re.sub(r"/v1/conversations/[^/]+$", "/v1/conversations/X", o["route"]["path"]) not in paths}), [])
old = c.get("/v1/conversations?view=threads", headers=M).json()
check("old clients keep the plain threads view", (old.get("view"), "groups" in old), ("threads", False))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
