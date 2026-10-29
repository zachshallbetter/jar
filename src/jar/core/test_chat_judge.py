#!/usr/bin/env python3
# gate: hermetic
"""The local model labels chat pairs and titles threads (#1016, Zach 10-06), in place of hand-labelling: its labels are
statements with source "judge" (ids and yes/no, no text), the person's own always win, and they count toward precision
only once a spot-check agrees with the person; titles live in their own per-person store, never links.db, and an
earned name still wins. Fictional chats (Alex, Robin); fake judge and titler; temporary stores.
Run: python3 scripts/test_chat_judge.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-chat-judge-test-"))
for k, v in {"LINK_SALT_FILE": "link_salt", "LINKS_DB": "links.db", "WORLD_DIR": "world", "WORLD_DB": "world.db",
             "HOUSEHOLD_DB": "household.db", "SPACES_DB": "spaces.db", "MEMORY_DB": "memory.db", "PLACES_DB": "places.db",
             "AGENT_TURN_LOG": "turns.jsonl", "CHAT_TITLES_DB": "titles.db", "CHAT_LINKS_LOCAL_URL": "http://127.0.0.1:9"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import chat_judge as J  # noqa: E402
import chat_links as CL  # noqa: E402
import chat_threads as CT  # noqa: E402
import hooks as H  # noqa: E402
import linker as L  # noqa: E402
import threadview as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


T0, MIN = 1_790_000_000.0, 60.0
NOW = T0 + 86400
mem = sqlite3.connect(":memory:")
mem.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, ts REAL, thread TEXT, "
            "device TEXT, client_msg_id TEXT)")
CHATS = [("t1", 0, "Book the vet for Biscuit"), ("t2", 20, "Move the vet to Friday"),
         ("t3", 300, "What's the weather this weekend?"), ("t4", 320, "Draft a note to the Harbor Gas office"),
         ("t5", 340, "Send the Harbor Gas note")]
for i, (th, m, q) in enumerate(CHATS, 1):
    mem.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,NULL,NULL)", (2 * i, "chat", "user", q, T0 + m * MIN, th))
    mem.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,NULL,NULL)", (2 * i + 1, "chat", "assistant", "Done.", T0 + m * MIN + 3, th))
con = H.connect()
CL.ensure(con)
pid = __import__("household_identity").ADMIN_PERSON_ID
same_subject = lambda a, b: any(w in " ".join(b["texts"]).lower() for w in ("vet", "harbor") if w in " ".join(a["texts"]).lower())  # noqa: E731
print("── the judge's labels ──")
r = J.run(pid, 30, apply=True, judge=same_subject, titler=lambda texts: "Vet visit for Biscuit" if "vet" in " ".join(texts).lower()
          else "Harbor Gas note", con=con, mem=mem, now=NOW)
check("every adjacent pair judged", (r["pairs"], r["judged"]["yes"] + r["judged"]["no"]), (4, 4))
rows = con.execute("SELECT a, b, related, by, source FROM link_statements").fetchall()
check("stored as 'judge' statements: ids and yes/no only", ({x[4] for x in rows}, {x[3] for x in rows},
                                                           all(x[0].startswith("chat-") for x in rows)), ({"judge"}, {"judge:local"}, True))
check("no text in links.db", any(w in str(rows).lower() for w in ("vet", "harbor", "weather")), False)
again = J.run(pid, 30, apply=True, judge=lambda a, b: 1 / 0, titler=lambda t: None, con=con, mem=mem, now=NOW)
check("a second run asks nothing already judged", again["judged"]["skipped (already judged)"], 4)

print("── trust: only once it agrees with the person ──")
judge_labels = J.judged(con, {CL.item_id(c[0]) for c in CHATS})
person = {}
check("no labels of the person's yet: not trusted", J.agreement(person, judge_labels)["trusted"], False)
v = CT.build(pid, "admin", mem, con=con, now=NOW)
check("…so precision has nothing labelled", v["precision"]["continues"]["labelled"], 0)
check("the view reports both label sets", (v["labels"]["yours"], v["labels"]["judge"]), (0, 4))
pairs = sorted(judge_labels)
for a, b in pairs:   # the person agrees on all four, then on four fictional ones more
    L.say(con, a, b, judge_labels[(a, b)], pid, "baseline")
agree = J.agreement(CT.baseline(con, {CL.item_id(c[0]) for c in CHATS}), judge_labels)
check("4 agreeing labels aren't enough yet (8 needed)", (agree["compared"], agree["rate"], agree["trusted"]), (4, 1.0, False))
check("trusted at 8 agreeing of 8", J.agreement({(f"x{i}", f"y{i}"): True for i in range(8)},
                                                {(f"x{i}", f"y{i}"): True for i in range(8)})["trusted"], True)
check("…not at 7 of 8", J.agreement({(f"x{i}", f"y{i}"): True for i in range(8)},
                                    {(f"x{i}", f"y{i}"): i != 0 for i in range(8)})["trusted"], False)

print("── the spot-check: where the judge and the code disagree ──")
code = {pairs[0]: "continues"}
jl = {pairs[0]: False, pairs[1]: True, pairs[2]: False}
sc = J.spotcheck(code, jl, {})
check("disagreements first: linked but judged different, then judged same but not linked",
      [(x["a"], x["b"]) for x in sc[:2]], [pairs[0], pairs[1]])
check("never a pair the person already labelled", J.spotcheck(code, jl, {pairs[0]: True})[0]["a"] != pairs[0][0]
      or J.spotcheck(code, jl, {pairs[0]: True})[0]["b"] != pairs[0][1], True)
check("at most 10", len(J.spotcheck({}, {(f"a{i}", f"b{i}"): True for i in range(30)}, {})), 10)

print("── titles ──")
# threads form from the person's and the code's links only (the judge's labels never bundle, #1125), so the threads
# their labels above made are titled on the next run
J.run(pid, 30, apply=True, judge=lambda a, b: 1 / 0, titler=lambda texts: "Vet visit for Biscuit"
      if "vet" in " ".join(texts).lower() else "Harbor Gas note", con=con, mem=mem, now=NOW)
v = CT.build(pid, "admin", mem, con=con, now=NOW)
names = sorted((t["name"], t["name_source"]) for t in v["threads"])
check("a thread with no earned name gets the model's title", ("Harbor Gas note", "model") in names or
      ("Vet visit for Biscuit", "model") in names, True)
check("titles live in their own store, per person", sorted(J.titles(pid).values()) != [] and J.titles("p_robin") == {}, True)
check("titles never in links.db", "Vet visit" in str(con.execute("SELECT * FROM link_statements").fetchall()), False)
J.save_title(pid, "ct-earned", "Model title")
check("the key is the thread's own id (members, any order)", J.thread_key(["chat-b", "chat-a"]) == J.thread_key(["chat-a", "chat-b"]), True)

print("── local only, and clients can't send 'judge' ──")
for fn in (lambda: J.judge_pair({"texts": [], "start": 0, "end": 0}, {"texts": [], "start": 0, "end": 0}, url="https://api.example.com"),
           lambda: J.title_for(["x"], url="https://api.example.com")):
    try:
        fn()
        check("refuses a non-local model", "asked", "refused")
    except ValueError:
        check("refuses a non-local model", "refused", "refused")
try:
    T.relate(pid, "admin", CL.item_id("t1"), CL.item_id("t2"), True, "judge")
    check("an app can't send a 'judge' statement", "accepted", 400)
except T.Refused as e:
    check("an app can't send a 'judge' statement", e.status, 400)

print("── the spot-check only names chats the view shows (jellybean#941) ──")
v = CT.build(pid, "admin", mem, con=con, now=NOW, limit=2)
shown = {r["item_id"] for r in v["recent"]} | {r["item_id"] for t in v["threads"] for r in t["conversations"]}
check("every spot-check pair's chats are among the view's rows", all(x["a"] in shown and x["b"] in shown for x in v["spotcheck"]),
      True)
check("…even with a small limit (pairs outside the rows are left out, not sent)",
      len(CT.build(pid, "admin", mem, con=con, now=NOW, limit=200)["spotcheck"]) >= len(v["spotcheck"]), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
