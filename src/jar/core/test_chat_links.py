#!/usr/bin/env python3
# gate: hermetic
"""Conversations as linker items (#1016 step 1): hooks from the person's own words and the turn's tool targets (never
the model's prose), the four edge types, the local-only continuation question, nothing but keys in links.db, and only
ever the asker's own conversations. Fictional people only (Alex, Robin, Ava); a temporary links.db, salt and memory.
Run: python3 scripts/test_chat_links.py"""
from __future__ import annotations

import json
import re
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-chat-links-test-"))
for k, v in {"LINK_SALT_FILE": "link_salt", "LINKS_DB": "links.db", "WORLD_DIR": "world", "WORLD_DB": "world.db",
             "HOUSEHOLD_DB": "household.db", "KB_ACL_DB": "kb_acl.db", "AGENT_MEMORY_DIR": "memory", "FENCES_DB": "fences.db",
             "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl", "CONNECTORS_LEDGER": "ledger.db",
             "SPACES_DB": "spaces.db", "MEMORY_DB": "memory.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import chat_links as CL  # noqa: E402
import hooks as H  # noqa: E402
import linker as L  # noqa: E402
import threadview as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


T0 = 1_790_000_000.0
MIN = 60.0


def conv(iid, start_min, end_min, keys=(), owner="alex", first="", last="", parent=None):
    return CL.Conv(CL.item_id(iid), owner, T0 + start_min * MIN, T0 + end_min * MIN, set(keys), set(), first, last,
                   CL.item_id(parent) if parent else None)


def edges(cs, said=None, ask=None):
    return {(e.a, e.b): (e.type, e.state) for e in CL.score(cs, said, ask)}


def pair(a, b):
    return L._pair(CL.item_id(a), CL.item_id(b))


REM = CL.targets("update_reminder", {"reminder_id": "R-7", "new_title": "Call Harbor Gas"})[0]
GARAGE = ("target:home", H.keyed("ha", "garage door"))
ROBIN = ("party:person", H.keyed("person", "robin"))

print("── what a tool call touched ──")
t = CL.targets("update_reminder", {"reminder_id": "R-7", "new_title": "Call Harbor Gas"})
check("a reminder's id and its title, as keyed record targets", [k for k, _ in t], ["target:record", "target:record"])
check("no argument's text in a key", any("harbor" in v.lower() or "R-7" in v for _, v in t), False)
check("the same reminder by id, from another tool, is the same key",
      CL.targets("complete_reminder", {"reminder_id": "r-7"})[0] == t[0], True)
check("a file's project, never the file", [k for k, _ in CL.targets("read_file", {"path": "~/Projects/harbor-app/src/a.py"})],
      ["party:project"])
check("project_of", (CL.project_of("/Users/alex/Projects/Harbor-App/x/y.md"), CL.project_of("/tmp/notes.txt")),
      ("harbor-app", None))
check("a site, by its registrable domain", CL.targets("fetch_url", {"url": "https://www.harborgas.example/bill"})[0][0],
      "party:site")
check("'household' and 'me' are no one", CL.targets("add_todo", {"title": "milk", "for": "household"}) ==
      CL.targets("add_todo", {"title": "milk"}), True)
check("a device in the home", CL.targets("home_control", {"device": "Garage Door", "action": "close"}), [GARAGE])
check("the kind facet comes from the agent's own tool groups",
      CL.kinds(["create_reminder", "home_control", "web_search", "no_such_tool"]) == {"calendar", "home", "web"}, True)

print("── continues: within 30 min and a shared hook ──")
a, b = conv("a", 0, 10, [REM]), conv("b", 25, 30, [REM])
check("15 min later, the same reminder: continues, accepted", edges([a, b])[pair("a", "b")], ("continues", "accepted"))
c = conv("c", 41, 45, [REM])
check("31 min later: not a continuation (still the same record, within 72 h)", edges([a, c])[pair("a", "c")],
      ("record", "accepted"))
check("within 30 min, nothing shared: no edge", edges([conv("a", 0, 10, [REM]), conv("b", 12, 14, [GARAGE])]), {})
check("a shared facet or mentioned date never continues",
      edges([conv("a", 0, 10, [("facet:kind", "home"), ("date:mention", "2026-10-09")]),
             conv("b", 12, 14, [("facet:kind", "home"), ("date:mention", "2026-10-09")])]), {})
check("never across owners", edges([conv("a", 0, 10, [REM]), conv("b", 12, 14, [REM], owner="robin")]), {})

print("── the ambiguous first message: the local model's yes/no ──")
asked = []


def yes(prev, nxt):
    asked.append(nxt.item_id)
    return True


amb = [conv("a", 0, 10, [GARAGE], last="close the garage"), conv("b", 12, 13, first="and the side one?")]
check("an ambiguous first message the local model says continues: accepted (Zach's decision 1), marked local",
      (edges(amb, ask=yes)[pair("a", "b")], next(e.measured.get("local") for e in CL.score(amb, None, yes))),
      (("continues", "accepted"), "yes"))
asked.clear()
edges([conv("a", 0, 10, [GARAGE]), conv("b", 12, 13, first="What's the weather like in Lisbon next week for a trip?")],
      ask=yes)
check("a self-contained first message is never asked", asked, [])
edges([conv("a", 0, 10, [GARAGE]), conv("b", 131, 132, first="and the side one?")], ask=yes)
check("beyond 2 h it's never asked", asked, [])
check("an unresolved reference reaches back 2 h", edges([conv("a", 0, 10, [GARAGE]), conv("b", 80, 81, first="She never got it.")],
                                                        ask=yes)[pair("a", "b")], ("continues", "accepted"))
check("a shared hook alone still continues only within 30 min", edges([conv("a", 0, 10, [REM]), conv("b", 50, 55, [REM])]).get(
    pair("a", "b")), ("record", "accepted"))

print("── unresolved references ──")
for text, want in (("She never got it.", True), ("Do the same for Sunday.", True), ("No, I meant almond milk.", True),
                   ("Can you check again?", True), ("Tell him I'm running 10 minutes late.", True),
                   ("What's 15% of 84?", False), ("Add Leo's soccer practice on Tuesday at 5 to my calendar.", False),
                   ("What time does the library open on Saturday?", False)):
    check(f"{text!r} leans on an earlier chat: {want}", CL.unresolved(text), want)
check("no answer (model down): no edge", edges(amb, ask=lambda p, n: None), {})
try:
    CL.ask_local(amb[0], amb[1], url="https://api.example.com")
    check("the question is only ever asked locally", "asked", "refused")
except ValueError:
    check("the question is only ever asked locally", "refused", "refused")
sent = []


def fake_post(url, payload, timeout):
    sent.append(payload)
    return {"model": "local", "choices": [{"message": {"content": '{"continues": "yes"}'}}]}


check("the local answer parses", CL.ask_local(amb[0], amb[1], post=fake_post), True)
check("it sees only the person's own two messages", ("close the garage" in sent[0]["messages"][0]["content"],
                                                     "and the side one?" in sent[0]["messages"][0]["content"]), (True, True))

print("── lineage, records and entities over 72 h ──")
check("a branch and its parent: verified", edges([conv("p", 0, 5), conv("q", 300, 305, parent="p")])[pair("p", "q")],
      ("lineage", "verified"))
check("the same record two days apart: accepted",
      edges([conv("a", 0, 5, [REM]), conv("b", 48 * 60, 48 * 60 + 5, [REM])])[pair("a", "b")], ("record", "accepted"))
check("the same person two days apart: a candidate",
      edges([conv("a", 0, 5, [ROBIN]), conv("b", 48 * 60, 48 * 60 + 5, [ROBIN])])[pair("a", "b")], ("entity", "candidate"))
check("four days apart: nothing", edges([conv("a", 0, 5, [REM]), conv("b", 96 * 60, 96 * 60 + 5, [REM])]), {})
many = [conv(f"m{i}", i * 120, i * 120 + 5, [ROBIN]) for i in range(12)]
check("a hook in more conversations than demote_at links nothing", edges(many), {})
said = {pair("a", "b"): False}
check("the person's 'different' outranks the code",
      edges([conv("a", 0, 10, [REM]), conv("b", 12, 14, [REM])], said)[pair("a", "b")], ("continues", "rejected"))
said = {pair("a", "b"): True}
check("their 'same' stands where the code saw nothing",
      edges([conv("a", 0, 10), conv("b", 12, 14)], said)[pair("a", "b")], ("continues", "verified"))

print("── conversations from the stream: the person's words only ──")
mem = sqlite3.connect(":memory:")
mem.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, ts REAL, thread TEXT, "
            "device TEXT, client_msg_id TEXT)")
rows = [("chat:alex", "user", "Did Robin book the dentist for Ava?", T0, "th-1"),
        ("chat:alex", "assistant", "Yes. Call them at (555) 201-3344 to confirm, order #HG-4410.", T0 + 5, "th-1"),
        ("chat:alex", "user", "and the side one?", T0 + 20 * MIN, "th-2"),
        ("chat:alex", "assistant", "Done.", T0 + 20 * MIN + 4, "th-2"),
        ("chat:robin", "user", "Did Alex call about order #HG-4410?", T0 + 30 * MIN, "th-3")]
for i, r in enumerate(rows, 1):
    mem.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,NULL,NULL)", (i, *r))
people = [{"id": "alex", "name": "Alex Hale"}, {"id": "robin", "name": "Robin Hale", "aliases": ["Rob"]},
          {"id": "ava", "name": "Ava Hale"}]
con = H.connect()
CL.note_turn("alex", 3, "th-2", T0 + 20 * MIN, ["home_control"], [GARAGE], con)
noted = {r[1]: {"groups": r[4], "targets": r[5]} for r in con.execute("SELECT * FROM chat_turns")}
cs = CL.load_convs(mem, "chat:alex", "alex", T0 - 1, None, CL.members_finder(people), None, noted)
k1 = {k for k, _ in cs[0].keys}
check("household members named by the person become person hooks", sum(1 for k, _ in cs[0].keys if k == "party:person"), 2)
check("the model's prose adds nothing (its phone, its order number)", ("party:phone" in k1, "id:order" in k1), (False, False))
check("a noted turn brings its targets and facet", (GARAGE in cs[1].keys, ("facet:kind", "home") in cs[1].keys), (True, True))
check("only that person's stream", [c.item_id for c in cs], [CL.item_id("th-1"), CL.item_id("th-2")])
check("the turn table keeps keys, never text", any("garage" in json.dumps(list(r)).lower()
                                                    for r in con.execute("SELECT * FROM chat_turns")), False)

print("── links.db: keys only, and apart from #951's threads ──")
rcs = CL.load_convs(mem, "chat:robin", "robin", T0 - 1, None, CL.members_finder(people))
CL.index_items(con, cs + rcs, T0 - 1)
CL.save(con, CL.score(cs + rcs))
dump = json.dumps([list(r) for q in ("SELECT kind, key, locator FROM hooks", "SELECT typed, measured, why FROM chat_links",
                                     "SELECT groups, targets FROM chat_turns", "SELECT why FROM link_log")
                   for r in con.execute(q)]).lower()   # owner columns hold person ids, as for every item
# Whole tokens only: a score or hash in the dump can contain the digits "4410" (10-08 deploy failed on 0.4410…).
check("no text in links.db (no names, words, numbers)", [w for w in ("robin", "dentist", "side one", "hg-4410", "4410", "garage")
                                                        if re.search(r"(?<![\w.])" + re.escape(w) + r"(?![\w.])", dump)], [])
check("owners are the streams' people", sorted({r[0] for r in con.execute(
    "SELECT owner FROM hooks WHERE source='conversation'")}), ["alex", "robin"])
check("#951's linker never loads a conversation", any(i.startswith(CL.PREFIX) for i in L.load(con)), False)

print("── relate: only the asker's own conversations ──")
a1, a2, r1 = CL.item_id("th-1"), CL.item_id("th-2"), CL.item_id("th-3")
r = T.relate("alex", "admin", a1, a2, True, "baseline")
check("Alex's two conversations: the statement stands", (r["related"], r["state"]), (True, "verified"))
check("its source is kept", con.execute("SELECT source FROM link_statements ORDER BY at DESC LIMIT 1").fetchone()[0],
      "baseline")
check("the default source is a correction", T.relate("alex", "admin", a1, a2, False)["state"], "rejected")
check("…stored as one", con.execute("SELECT source FROM link_statements ORDER BY at DESC LIMIT 1").fetchone()[0],
      "correction")
for label, args, code in (("Robin's conversation is no item of Alex's", ("alex", "admin", a1, r1, True), 404),
                          ("nor is it for an admin", ("alex", "admin", r1, a2, True), 404),
                          ("a kid gets none", ("ava", "kid", a1, a2, True), 403),
                          ("a shared screen gets none", ("house", "household", a1, a2, True), 403),
                          ("an unknown source", ("alex", "admin", a1, a2, True, "guess"), 400)):
    try:
        T.relate(*args)
        check(label, "allowed", code)
    except T.Refused as e:
        check(label, e.status, code)

print("── continues_from: the read the carry-over uses ──")
cf = lambda *a, **k: CL.continues_from(*a, con=con, mem=mem, entities=None, members=CL.members_finder(people), **k)  # noqa: E731
CL.save(con, CL.score(CL.load_convs(mem, "chat:alex", "alex", T0 - 1, None, CL.members_finder(people), None, noted) + [
    CL.Conv(CL.item_id("th-1b"), "alex", T0 + 15 * MIN, T0 + 16 * MIN, {("party:person", H.keyed("person", "robin"))})]))
got = cf("alex", "th-1b")
check("a stored edge names the earlier chat", (got or {}).get("id"), "th-1")
check("never someone else's", cf("robin", "th-1b"), None)
check("the linker's name reads the same edge (the carry-over calls it)",
      (L.continues_from("alex", "th-1b") or {}).get("id"), "th-1")
L.say(con, CL.item_id("th-1"), CL.item_id("th-1b"), False, "alex", "correction")
CL.save(con, CL.score(CL.load_convs(mem, "chat:alex", "alex", T0 - 1, None, CL.members_finder(people), None, noted) + [
    CL.Conv(CL.item_id("th-1b"), "alex", T0 + 15 * MIN, T0 + 16 * MIN, {("party:person", H.keyed("person", "robin"))})],
    L.statements(con)))
check("a 'different' statement leaves no edge", cf("alex", "th-1b"), None)
live = cf("robin", "th-r2", "Order #HG-4410 again, any news?", now=T0 + 40 * MIN)
check("live, on a first turn: the same order number 10 min after the last chat", live and (live["id"], live["state"]),
      ("th-3", "accepted"))
check("live: only the person's own earlier chat", cf("alex", "th-new", "Order #HG-4410 again?", now=T0 + 40 * MIN), None)
check("live: nothing shared, no edge", cf("robin", "th-r2", "What's the weather tomorrow?", now=T0 + 40 * MIN), None)
check("live: past 30 min, no edge", cf("robin", "th-r2", "Order #HG-4410 again, any news?", now=T0 + 80 * MIN), None)

print("── continues_from: an unresolved reference, asked locally ──")
said_to = []


def judge(answer):
    def f(prev, new):
        said_to.append((prev.last, prev.reply, new.first))
        return answer
    return f


got = cf("robin", "th-r3", "Did they ever reply?", now=T0 + 90 * MIN, ask=judge(True))
check("70 min later, the local model says yes: accepted, marked local", got and (got["id"], got["state"], got.get("local")),
      ("th-3", "accepted", True))
check("the model saw the earlier chat's last exchange and the new message",
      said_to[-1][0].startswith("Did Alex call") and said_to[-1][2] == "Did they ever reply?", True)
check("a no is no edge", cf("robin", "th-r3", "Did they ever reply?", now=T0 + 90 * MIN, ask=judge(False)), None)
said_to.clear()
check("past 2 h it's never asked", (cf("robin", "th-r3", "Did they ever reply?", now=T0 + 160 * MIN, ask=judge(True)), said_to),
      (None, []))
check("a message that names its own subject is never asked",
      (cf("robin", "th-r3", "What time does the library open on Saturday?", now=T0 + 40 * MIN, ask=judge(True)), said_to),
      (None, []))

print("── the local question is capped: slow or busy is no continuation, counted ──")
import types  # noqa: E402
_seen_timeout = []


def slow_post(url, payload, timeout):
    _seen_timeout.append(timeout)
    raise TimeoutError("timed out")


fake_lib = types.SimpleNamespace(_TURN_IO=types.SimpleNamespace(conv_continue=None))
sys.modules["agent_runtime_lib"], _real_lib = fake_lib, sys.modules.get("agent_runtime_lib")
try:
    got = cf("robin", "th-r3", "Did they ever reply?", now=T0 + 90 * MIN, ask=lambda p, n: CL.ask_local(p, n, post=slow_post))
    check("a timeout is no continuation", got, None)
    check("asked with the 3 s cap", _seen_timeout, [CL.LOCAL_TIMEOUT_S])
    check("CL.LOCAL_TIMEOUT_S is 3 s", CL.LOCAL_TIMEOUT_S, 3)
    check("the turn is marked conv_continue: local_timeout", fake_lib._TURN_IO.conv_continue, "local_timeout")


    def busy_post(url, payload, timeout):
        raise OSError("503 busy")
    fake_lib._TURN_IO.conv_continue = None
    check("a busy model is the same: no edge, counted",
          (cf("robin", "th-r3", "Did they ever reply?", now=T0 + 90 * MIN, ask=lambda p, n: CL.ask_local(p, n, post=busy_post)),
           fake_lib._TURN_IO.conv_continue), (None, "local_timeout"))
finally:
    if _real_lib is None:
        sys.modules.pop("agent_runtime_lib", None)
    else:
        sys.modules["agent_runtime_lib"] = _real_lib

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
