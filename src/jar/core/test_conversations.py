#!/usr/bin/env python3
"""A person's past conversations as topics (jellybean#117): split by the app's thread or a pause, titled
by the first question, readable only by their owner. Hermetic.
Run: python3 scripts/test_conversations.py"""
from __future__ import annotations
import os, sys, sqlite3, tempfile
from pathlib import Path
_TMP = Path(tempfile.mkdtemp(prefix="titan-conversations-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "SPACES_DB": "spaces.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import conversations as cv  # noqa: E402
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


def db_with(rows):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, ts REAL, thread TEXT, device TEXT)")
    db.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", rows)
    return db


T = 1_790_000_000.0
rows = [("m1", "chat", "user", "When is Ava's recital?", T, None, "Zach's iPhone"),
        ("m2", "chat", "assistant", "Thursday at 6:00 PM.", T + 5, None, "Zach's iPhone"),
        ("m3", "chat", "user", "It moved to 6:30", T + 60, None, "Zach's iPhone"),
        ("m4", "chat", "assistant", "Noted: Thursday at 6:30 PM.", T + 65, None, "Zach's iPhone"),
        # an hour later, a new topic
        ("m5", "chat", "user", "Describe the image at /ai/tmp/IMG_1.png.", T + 3700, None, "MacBook Pro"),
        ("m6", "chat", "assistant", "A furnace filter label: 16x25x1.", T + 3705, None, "MacBook Pro"),
        # the app's own thread, picked up on two devices with a long pause between
        ("m7", "chat", "user", "Plan the weekend trip to the coast", T + 9000, "th-coast", "MacBook Pro"),
        ("m8", "chat", "assistant", "Here's a plan…", T + 9010, "th-coast", "MacBook Pro"),
        ("m9", "chat", "user", "Add a stop for lunch", T + 20000, "th-coast", "Zach's iPhone"),
        ("m10", "chat", "assistant", "Added lunch in Cannon Beach.", T + 20010, "th-coast", "Zach's iPhone"),
        # Robin's stream
        ("x1", "chat:p_mir", "user", "Gift ideas for my sister", T + 100, None, "Robin's iPhone")]
db = db_with(rows)

print("── topics ──")
z = cv.list_for(db, "chat")
check("three conversations, newest first", [c["id"] for c in z], ["th-coast", "s-m5", "s-m1"])
check("titles: the first question (an attachment line becomes 'Attachment')", [c["title"] for c in z],
      ["Plan the weekend trip to the coast", "Attachment", "When is Ava's recital?"])
check("a thread stays one conversation across a pause and two devices", (z[0]["count"], z[0]["device"]), (4, "MacBook Pro"))
check("preview: the last reply", z[2]["preview"], "Noted: Thursday at 6:30 PM.")
check("a pause under 10 min stays in the conversation", z[2]["count"], 4)

print("\n── reading one ──")
m = cv.messages(db, "chat", "s-m1")
check("oldest first, with when", ([x["text"] for x in m], m[0]["at"].startswith("2026")),
      (["When is Ava's recital?", "Thursday at 6:00 PM.", "It moved to 6:30", "Noted: Thursday at 6:30 PM."], True))
check("limit keeps the newest", [x["text"] for x in cv.messages(db, "chat", "s-m1", limit=1)], ["Noted: Thursday at 6:30 PM."])
check("another person's conversation id isn't found in mine", cv.messages(db, "chat", "s-x1"), None)

print("── paging with a (at, id) cursor (#1290) ──")
S = 1_790_500_000.0
same = [("q9", "chat:p_alex", "user", "first", S, "th-page", "Alex's iPhone"),
        ("q2", "chat:p_alex", "assistant", "second, same moment", S + 1, "th-page", "Alex's iPhone"),
        ("q7", "chat:p_alex", "user", "third, same moment", S + 1, "th-page", "Alex's iPhone"),   # ids aren't in order
        ("q1", "chat:p_alex", "assistant", "fourth", S + 2, "th-page", "Alex's iPhone")]
pdb = db_with(same)
full = cv.messages(pdb, "chat:p_alex", "th-page")
check("every message says its id, oldest first in stored order", [(m["id"], m["text"]) for m in full],
      [("q9", "first"), ("q2", "second, same moment"), ("q7", "third, same moment"), ("q1", "fourth")])
page1 = cv.messages(pdb, "chat:p_alex", "th-page", limit=2)
check("page 1 (the newest two) starts at the third, whose at it shares with the second",
      [m["id"] for m in page1], ["q7", "q1"])
oldest = page1[0]
check("before alone (the shipped app): the strict < loses the second (the bug, kept for old clients)",
      [m["id"] for m in cv.messages(pdb, "chat:p_alex", "th-page", before=oldest["at"], limit=2)], ["q9"])
page2 = cv.messages(pdb, "chat:p_alex", "th-page", before=oldest["at"], before_id=oldest["id"], limit=2)
check("before + before_id: page 2 has the second too, nothing repeated",
      [m["id"] for m in page2], ["q9", "q2"])
check("…all four across the two pages, once each", sorted(m["id"] for m in page1 + page2), ["q1", "q2", "q7", "q9"])
check("a before_id no longer there falls back to before alone",
      [m["id"] for m in cv.messages(pdb, "chat:p_alex", "th-page", before=oldest["at"], before_id="gone", limit=2)], ["q9"])
check("a before_id whose at doesn't match isn't used (no jump to another message)",
      [m["id"] for m in cv.messages(pdb, "chat:p_alex", "th-page", before=full[3]["at"], before_id="q2", limit=5)],
      ["q9", "q2", "q7"])
check("Robin sees only hers", [c["title"] for c in cv.list_for(db, "chat:p_mir")], ["Gift ideas for my sister"])
old = sqlite3.connect(":memory:")
old.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, ts REAL)")
old.execute("INSERT INTO messages VALUES ('o1','chat','user','hi',1.0)")
check("a store from before thread/device still lists", [c["id"] for c in cv.list_for(old, "chat")], ["s-o1"])
check("…with no platform, on the list and on its messages (#1287)",
      ([c["platform"] for c in cv.list_for(old, "chat")], [m["platform"] for m in cv.messages(old, "chat", "s-o1")]),
      ([None], [None]))
check("rows from before the platform column still list (thread/device kept, platform null)",
      [(c["id"], c["device"], c["platform"]) for c in cv.list_for(db, "chat")],
      [("th-coast", "MacBook Pro", None), ("s-m5", "MacBook Pro", None), ("s-m1", "Zach's iPhone", None)])

print("\n── platform per message (#1287) ──")
check("the pairing platforms map to the app's kinds; other and unknown are null",
      [cv.platform_of({"platform": p}) for p in ("mac", "ios", "ipados", "watchos", "tvos", "other", "", "Linux")],
      ["mac", "iphone", "ipad", "watch", "tv", None, None, None])
check("no device (the master token, scripts): null", cv.platform_of(None), None)
pl = sqlite3.connect(":memory:")
pl.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, ts REAL, thread TEXT, "
           "device TEXT, platform TEXT)")
pl.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
               [("p1", "chat:p_ava", "user", "Play the cartoon", T, "th-tv", "Living room", "tv"),
                ("p2", "chat:p_ava", "assistant", "Playing it.", T + 3, "th-tv", "Living room", "tv"),
                ("p3", "chat:p_ava", "user", "And louder", T + 30, "th-tv", "Ava's iPhone", "iphone"),
                ("p4", "chat:p_ava", "assistant", "Louder.", T + 33, "th-tv", "Living room", "tv")])
check("a conversation's platform is the device it shows (a TV named just 'Living room')",
      [(c["device"], c["platform"]) for c in cv.list_for(pl, "chat:p_ava")], [("Living room", "tv")])
check("each message says its own", [m["platform"] for m in cv.messages(pl, "chat:p_ava", "th-tv")],
      ["tv", "tv", "iphone", "tv"])
pl.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
               [("n1", "chat:p_ava", "user", "From a script", T + 5000, "th-mix", "mac", None),
                ("n2", "chat:p_ava", "assistant", "Done.", T + 5003, "th-mix", "mac", None),
                ("n3", "chat:p_ava", "user", "And on the TV", T + 5030, "th-mix", "Living room", "tv")])
check("the icon never comes from another device than the name: a master-token 'mac' tag with a TV aside is null",
      {c["id"]: (c["device"], c["platform"]) for c in cv.list_for(pl, "chat:p_ava")}["th-mix"], ("mac", None))

print("\n── the API: only the asker's own ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api.db"); hh.reset_store(st)
c = TestClient(api.app); M = {"X-Agent-Token": MASTER}
s = c.post("/v1/pair/start", json={"new_person": {"name": "Robin", "role": "adult"}}, headers=M).json()
MIR = {"X-Agent-Token": c.post("/v1/pair/claim", json={"code": s["code"], "device_name": "Robin's iPhone", "platform": "ios"}).json()["token"]}
mid = s["person"]["id"]
mem = api._get_memory()
mem.add_message("chat", "user", "Zach's question about the furnace", thread="th-z", device="MacBook Pro")
mem.add_message("chat", "assistant", "Change it every 3 months.", thread="th-z", device="MacBook Pro")
mem.add_message(hh.conv_id_for(mid), "user", "Robin asks about gifts", device="Robin's iPhone")
zl = c.get("/v1/conversations", headers=M).json()["conversations"]
ml = c.get("/v1/conversations", headers=MIR).json()["conversations"]
check("Zach's list: his only", [x["title"] for x in zl], ["Zach's question about the furnace"])
check("Robin's list: hers only", [x["title"] for x in ml], ["Robin asks about gifts"])
check("Robin can't read Zach's thread", c.get("/v1/conversations/th-z/messages", headers=MIR).status_code, 404)
r = c.get("/v1/conversations/th-z/messages", headers=M)
check("Zach reads his", (r.status_code, [x["role"] for x in r.json()["messages"]]), (200, ["user", "assistant"]))
check("thread and device are stored", sqlite3.connect(str(Path(os.environ["AGENT_MEMORY_DIR"]) / "titan_memory.db")).execute(
      "SELECT thread, device FROM messages WHERE content LIKE 'Zach%'").fetchone(), ("th-z", "MacBook Pro"))
print("\n── the API: the platform a turn came from (#1287) ──")
s2 = c.post("/v1/pair/start", json={"person_id": mid, "label": "Living room"}, headers=M).json()
TV = c.post("/v1/pair/claim", json={"code": s2["code"], "device_name": "Living room", "platform": "tvos"}).json()["token"]
tv = hh.resolve(TV, MASTER)
check("the TV resolves to Robin, on a tvos device", (tv.person_id, (tv.device or {}).get("platform")), (mid, "tvos"))
api._turns["t-tv"] = {"thread": "th-tv", "identity": tv, "remember": True}
api._remember_turn("t-tv", tv, "Put on the nature show", "On the living room TV now.")
ml = {x["id"]: x for x in c.get("/v1/conversations", headers=MIR).json()["conversations"]}
check("a paired TV's turn: the list says tv, beside its name", (ml["th-tv"]["device"], ml["th-tv"]["platform"]),
      ("Living room", "tv"))
check("…and so does each of its messages",
      [x["platform"] for x in c.get("/v1/conversations/th-tv/messages", headers=MIR).json()["messages"]], ["tv", "tv"])
zach = hh.owner_identity()
api._turns["t-master"] = {"thread": "th-master", "client": "mac", "identity": zach, "remember": True}
api._remember_turn("t-master", zach, "A question from a script", "An answer.")
zl = {x["id"]: x for x in c.get("/v1/conversations", headers=M).json()["conversations"]}
check("the master token: platform null (the client tag stays the device, as before)",
      (zl["th-master"]["device"], zl["th-master"]["platform"]), ("mac", None))
check("…and null on its messages",
      [x["platform"] for x in c.get("/v1/conversations/th-master/messages", headers=M).json()["messages"]], [None, None])
check("Zach's list never shows Robin's TV", "th-tv" in zl or any(x.get("device") == "Living room" for x in zl.values()), False)
check("Robin can't read the master token's conversation", c.get("/v1/conversations/th-master/messages", headers=MIR).status_code, 404)
check("the platform is stored on the row", sqlite3.connect(str(Path(os.environ["AGENT_MEMORY_DIR"]) / "titan_memory.db")).execute(
      "SELECT DISTINCT platform FROM messages WHERE thread='th-tv'").fetchall(), [("tv",)])

print("\n── where a branch came from (titan-node#895) ──")
import spaces  # noqa: E402
check("a branch's origin: the parent thread and the message, opaque ids",
      spaces.clean_branch({"thread_id": "th-z", "upto_message_id": "6F1D2C39-6C8B-4B7B-9C55-2B0D4A0E0001"}),
      {"parent": "th-z", "upto": "6F1D2C39-6C8B-4B7B-9C55-2B0D4A0E0001"})
check("…a whole fork: no message", spaces.clean_branch({"thread_id": "th-z"}), {"parent": "th-z", "upto": None})
for bad in ({}, {"thread_id": ""}, {"thread_id": "a b"}, {"thread_id": "x" * 81}, {"thread_id": "th", "upto_message_id": "<script>"},
            "th-z", None):
    check(f"refused: {bad!r:.40}", spaces.clean_branch(bad), None)
zach = hh.owner_identity()
api._turns["t-b1"] = {"thread": "th-branch", "branched_from": {"thread_id": "th-z", "upto_message_id": "m-1"},
                      "identity": zach, "remember": True}
api._remember_turn("t-b1", zach, "And in the fall?", "Every 3 months, fall too.")
zl = {x["id"]: x for x in c.get("/v1/conversations", headers=M).json()["conversations"]}
check("the branch's first turn keeps where it came from; the list says so",
      zl.get("th-branch", {}).get("branched_from"), {"thread_id": "th-z", "upto_message_id": "m-1"})
check("a conversation that isn't a branch says null", zl["th-z"]["branched_from"], None)
api._turns["t-b2"] = {"thread": "th-branch", "branched_from": {"thread_id": "th-other", "upto_message_id": "m-9"},
                      "identity": zach, "remember": True}
api._remember_turn("t-b2", zach, "One more?", "Sure.")
check("a later turn never rewrites it", c.get("/v1/conversations", headers=M).json()["conversations"][0].get("branched_from"),
      {"thread_id": "th-z", "upto_message_id": "m-1"})
check("a branch of itself is nothing", spaces.note_branch(zach.person_id, "th-self", {"parent": "th-self", "upto": None}), False)
check("Robin's list never shows Zach's lineage", {x.get("branched_from") for x in c.get("/v1/conversations", headers=MIR).json()["conversations"]},
      {None})
api._turns["t-b3"] = {"thread": "th-bad", "branched_from": {"thread_id": "no spaces allowed"}, "identity": zach, "remember": True}
api._remember_turn("t-b3", zach, "q", "a")
check("a malformed origin keeps nothing (the turn is kept)", {x["id"]: x for x in c.get("/v1/conversations", headers=M).json()["conversations"]}
      .get("th-bad", {}).get("branched_from", "missing"), None)

print("\n── the live store before #1287: the column is added, old rows still list ──")
import agent_memory as am  # noqa: E402
pre = _TMP / "pre-1287.db"
o = sqlite3.connect(str(pre))
o.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, "
          "ts REAL NOT NULL, thread TEXT, device TEXT, client_msg_id TEXT, tools TEXT)")
o.execute("INSERT INTO messages VALUES ('old1','chat:p_leo','user','From before',?,'th-old','Leo''s iPhone',NULL,NULL)", (T,))
o.commit(); o.close()
was, am._DB_PATH = am._DB_PATH, pre
try:
    fresh = am.AgentMemory()
finally:
    am._DB_PATH = was
check("the platform column is added in place", "platform" in {r[1] for r in fresh._db.execute("PRAGMA table_info(messages)")}, True)
check("the old row still lists, platform null", [(x["id"], x["device"], x["platform"]) for x in cv.list_for(fresh._db, "chat:p_leo")],
      [("th-old", "Leo's iPhone", None)])
fresh.add_message("chat:p_leo", "user", "From the watch", thread="th-old", device="Leo's Watch", platform="watch")
check("a new message beside it records its platform", [x["platform"] for x in cv.messages(fresh._db, "chat:p_leo", "th-old")],
      [None, "watch"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
