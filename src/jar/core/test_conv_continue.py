#!/usr/bin/env python3
# gate: hermetic
"""Context carry-over between chats (titan-node#1016 step 5, conv_continue.py): a new chat that continues the asker's
own recent one gets its name, last exchange and what its tools did, as untrusted text. Off unless CONV_CONTINUE=1;
an eval turn picks its arm and brings its own previous chat. Never another person's chat, never on a kid's turn, a
shared screen or a temporary chat, never once the chat has history. KB's continues edge wins when it exists (own,
verified or accepted). The eval runner's scoring. Fictional people; throwaway stores.
Run: python3 scripts/test_conv_continue.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-convcont-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "SPACES_DB": "spaces.db", "LINK_SALT_FILE": "link_salt", "LINKS_DB": "links.db",
             "MEMORY_DB": "no-memory.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("CONV_CONTINUE", None)
sys.path.insert(0, str(Path(__file__).parent))

import conv_continue as cc  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = 1_790_000_000.0


def chat(cid, owner, mins_ago, name="Remind Ava about her library books", tools=("create_reminder",)):
    return {"id": cid, "owner": owner, "name": name, "last_at": NOW - mins_ago * 60, "tools": list(tools),
            "messages": [{"role": "user", "text": "Remind Ava to return her library books tomorrow at 4."},
                         {"role": "assistant", "text": "Done: Ava gets a reminder tomorrow at 4:00 PM."}]}


print("── which chat continues ──")
check("off unless exactly 1", [cc.on() for v in (None,) for _ in [os.environ.pop("CONV_CONTINUE", None)]], [False])
for v, want in (("1", True), ("true", False), (" 1", False), ("0", False)):
    os.environ["CONV_CONTINUE"] = v
    check(f"CONV_CONTINUE={v!r}", cc.on(), want)
os.environ.pop("CONV_CONTINUE", None)
mine = chat("a", "p_alex", 12)
check("the asker's own chat that ended 12 min ago", cc.previous([mine], "p_alex", NOW)["id"], "a")
check("the newest own one of several", cc.previous([chat("old", "p_alex", 25), chat("new", "p_alex", 3)], "p_alex", NOW)["id"],
      "new")
check("never another person's chat, however recent", cc.previous([chat("b", "p_robin", 1)], "p_alex", NOW), None)
check("…nor a kid's", cc.previous([chat("k", "eval-kid", 1)], "p_alex", NOW), None)
check("…and the asker's own wins over a newer one of someone else's",
      cc.previous([chat("b", "p_robin", 1), chat("a", "p_alex", 20)], "p_alex", NOW)["id"], "a")
check("older than 30 min: nothing (the stand-in)", cc.previous([chat("a", "p_alex", 31)], "p_alex", NOW), None)
check("the chat this turn is in isn't 'previous'", cc.previous([chat("th-1", "p_alex", 2)], "p_alex", NOW, current="th-1"), None)
check("no asker: nothing", cc.previous([chat("a", "", 2)], "", NOW), None)
check("a chat without messages: nothing", cc.previous([{**chat("a", "p_alex", 2), "messages": []}], "p_alex", NOW), None)
check("KB's edge wins, even past 30 min", cc.previous([chat("e", "p_alex", 300), chat("n", "p_alex", 2)], "p_alex", NOW,
                                                      edge="e")["id"], "e")
check("an edge to someone else's chat is never followed", cc.previous([chat("x", "p_robin", 2)], "p_alex", NOW, edge="x"), None)

print("\n── KB's edge read (linker.continues_from, when it exists) ──")
fake = types.ModuleType("linker")
calls = []
fake.continues_from = lambda owner, cid, first=None: calls.append((owner, cid)) or STATE.get(cid)
STATE = {"th-v": {"id": "e1", "state": "verified"}, "th-a": {"id": "e2", "state": "accepted"},
         "th-c": {"id": "e3", "state": "candidate"}, "th-bad": "nonsense"}
real = sys.modules.get("linker")
sys.modules["linker"] = fake
try:
    check("verified and accepted edges are used; a candidate, junk or none isn't",
          [cc._edge("p_alex", c) for c in ("th-v", "th-a", "th-c", "th-bad", "th-none")], ["e1", "e2", None, None, None])
    check("…asked for the asker's own conversation", calls[0], ("p_alex", "th-v"))
    check("no current thread: not asked", (cc._edge("p_alex", None), len(calls)), (None, 5))
    fake.continues_from = lambda owner, cid, first=None: (_ for _ in ()).throw(RuntimeError("boom"))
    check("an edge read that fails is no edge (the stand-in still works)", cc._edge("p_alex", "th-v"), None)
finally:
    if real is not None:
        sys.modules["linker"] = real
    else:
        sys.modules.pop("linker", None)
check("a linker without continues_from: no edge", (lambda: (sys.modules.__setitem__("linker", types.ModuleType("linker")),
                                                            cc._edge("p_alex", "th-v"))[1])(), None)
sys.modules.pop("linker", None)
seen = []
fake2 = types.ModuleType("linker")
fake2.continues_from = lambda owner, cid, first=None: seen.append((owner, cid, first)) or None
sys.modules["linker"] = fake2
cc._edge("p_alex", None, "Did she get it?")
cc._edge("p_alex", "th-9", "Did she get it?")
check("the chat's first message goes along (KB's live check on a first turn), a placeholder id without a thread",
      seen, [("p_alex", "new-chat", "Did she get it?"), ("p_alex", "th-9", "Did she get it?")])
check("no thread and no message: not asked", (cc._edge("p_alex", None, "  "), len(seen)), (None, 2))
sys.modules.pop("linker", None)
check("edge only (no stand-in): a recent chat without an edge isn't carried",
      (cc.previous([chat("a", "p_alex", 2)], "p_alex", NOW, standin=False),
       cc.previous([chat("a", "p_alex", 2)], "p_alex", NOW, edge="a", standin=False)["id"]), (None, "a"))

print("\n── the block ──")
b = cc.block_text(mine, NOW, trace=lambda names: "Added a reminder" if "create_reminder" in names else None)
check("name, how long ago, the last exchange and what it did", b.splitlines(),
      ["Previous chat: Remind Ava about her library books (ended 12 min ago)",
       "They said: Remind Ava to return her library books tomorrow at 4.",
       "titan replied: Done: Ava gets a reminder tomorrow at 4:00 PM.",
       "What it did: Added a reminder"])
long = {**mine, "messages": [{"role": "user", "text": "word " * 400}, {"role": "assistant", "text": "x" * 3000}]}
check("each side is clipped", max(len(x) for x in cc.block_text(long, NOW).splitlines()) <= cc.LAST_CHARS + 20, True)
check("tool names never appear, only phrases", "create_reminder" in b, False)
check("the head says to use it only for references", "only to work out what a reference" in cc.HEAD, True)

print("\n── an eval turn's previous chat ──")
bank = [json.loads(x) for x in (Path(__file__).parent / "evalsets" / "conv_continue.jsonl").read_text().splitlines() if x]
ec = cc.eval_chats(bank[0]["prior"], "p_eval", NOW)
check("owner self is the asker; gap → last_at; tool names only", (ec[0]["owner"], NOW - ec[0]["last_at"], ec[0]["tools"]),
      ("p_eval", bank[0]["prior"]["gap_min"] * 60.0, ["create_reminder"]))
check("owner other / kid are someone else", [cc.eval_chats(c["prior"], "p_eval", NOW)[0]["owner"] for c in bank
                                             if c["prior"]["owner"] != "self"], ["eval-other", "eval-kid"])
check("…so the privacy cases find nothing", [cc.previous(cc.eval_chats(c["prior"], "p_eval", NOW), "p_eval", NOW)
                                            for c in bank if c["prior"]["owner"] != "self"], [None, None])
check("junk prior: no chats", (cc.eval_chats("x", "p", NOW), cc.eval_chats(None, "p", NOW), cc.eval_chats([3], "p", NOW)),
      ([], [], []))

print("\n── the asker's own stream (live) ──")
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
mem = api._get_memory()
alex = hh.Identity({"id": "p_alex", "name": "Alex", "role": "adult"}, {"id": "d1", "name": "Alex's iPhone", "platform": "ios"},
                   "adult", "device")
robin = hh.Identity({"id": "p_robin", "name": "Robin", "role": "adult"}, {"id": "d2", "name": "Robin's iPhone", "platform": "ios"},
                    "adult", "device")
kid = hh.Identity({"id": "p_leo", "name": "Leo", "role": "kid"}, {"id": "d3", "name": "Leo's iPad", "platform": "ios"},
                  "kid", "device")
tv = hh.Identity({"id": "p_tv", "name": "Living room", "role": "household"}, {"id": "d4", "name": "TV", "platform": "tvos"},
                 "household", "device")
mem.add_message(hh.conv_id_for("p_alex"), "user", "Text Jordan that I'll pick him up at 5:15.", thread="th-a1")
mem.add_message(hh.conv_id_for("p_alex"), "assistant", "Sent to Jordan.", thread="th-a1", tools=["send_message"])
mem.add_message(hh.conv_id_for("p_robin"), "user", "Order the Lumen desk lamp as a surprise gift.", thread="th-r1")
mem.add_message(hh.conv_id_for("p_robin"), "assistant", "Added to your list.", thread="th-r1")
mem.add_message(hh.conv_id_for("p_leo"), "user", "Help me with fractions homework.", thread="th-k1")
mem.add_message(hh.conv_id_for("p_leo"), "assistant", "3/4 is 6/8.", thread="th-k1")
live = cc.live_chats(mem._db, hh.conv_id_for("p_alex"), "p_alex")
check("Alex's stream: his chat, with the reply's tool names", [(c["id"], c["owner"], c["tools"]) for c in live],
      [("th-a1", "p_alex", ["send_message"])])
check("…never Robin's or Leo's words", any("Lumen" in m["text"] or "fractions" in m["text"] for c in live for m in c["messages"]),
      False)
import sqlite3  # noqa: E402
con = sqlite3.connect(str(Path(os.environ["AGENT_MEMORY_DIR"]) / "titan_memory.db"))
check("the stored reply keeps tool names only (no arguments)", con.execute(
      "SELECT tools FROM messages WHERE content='Sent to Jordan.'").fetchone()[0], '["send_message"]')
con.close()

print("\n── the turn's block (agent_api_server._continue_block) ──")
import agent_runtime_lib as lib  # noqa: E402


def turn(tid, ident, **kw):
    api._turns[tid] = {"identity": ident, "source": kw.pop("source", None), "thread": kw.pop("thread", "th-new"), **kw}
    lib._TURN_IO.untrusted_nonce = None
    lib.begin_turn_info()
    return tid


def blk(tid, ident, history=None):
    return api._continue_block(tid, ident, history or [])


tid = turn("t1", alex)
check("flag off: a real turn gets nothing", blk(tid, alex), None)
os.environ["CONV_CONTINUE"] = "1"
out = blk(turn("t2", alex), alex)
check("flag on: Alex's new chat continues his own", (out or "").startswith(cc.HEAD), True)
check("…wrapped as untrusted chats text, with his last exchange and what it did",
      ("⟦untrusted" in out, "source=chats" in out or "chats" in out.split("⟦untrusted", 1)[1][:60],
       "pick him up" in out, "Sent a message" in out), (True, True, True, True))
check("…and the turn hands the signal to its log, no content", lib._TURN_IO.conv_continue, "recent")
check("…never Robin's words in Alex's block", "Lumen" in out or "gift" in out, False)
check("once the chat has history: nothing", blk(turn("t3", alex), alex, [{"role": "user", "content": "hi"}]), None)
check("a kid's turn: nothing (their own chat included)", blk(turn("t4", kid), kid), None)
check("a shared screen: nothing", blk(turn("t5", tv), tv), None)
check("a temporary chat: nothing", blk(turn("t6", alex, source="temporary"), alex), None)
out_r = blk(turn("t7", robin), robin)
check("Robin gets her own, never Alex's", ("gift" in (out_r or ""), "Jordan" in (out_r or "")), (True, False))
check("the chat she's in isn't carried into itself", blk(turn("t8", robin, thread="th-r1"), robin), None)
os.environ.pop("CONV_CONTINUE", None)

print("\n── an eval turn picks its arm and brings its own previous chat ──")
ev = lambda **kw: turn(kw.pop("tid"), alex, source="eval", **kw)  # noqa: E731
self_prior = bank[0]["prior"]
other_prior = next(c["prior"] for c in bank if c["prior"]["owner"] == "other")
kid_prior = next(c["prior"] for c in bank if c["prior"]["owner"] == "kid")
on = blk(ev(tid="e1", eval_prior=self_prior, eval_continue=True), alex)
check("arm on: the bank's previous chat, not the real stream", ("library books" in (on or ""), "Jordan" in (on or "")),
      (True, False))
check("arm off: nothing", blk(ev(tid="e2", eval_prior=self_prior, eval_continue=False), alex), None)
check("no arm: nothing (the flag doesn't apply to evals)", blk(ev(tid="e3", eval_prior=self_prior), alex), None)
check("another adult's chat: nothing, arm on", blk(ev(tid="e4", eval_prior=other_prior, eval_continue=True), alex), None)
check("a kid's chat: nothing, arm on", blk(ev(tid="e5", eval_prior=kid_prior, eval_continue=True), alex), None)
check("an eval turn with no prior never reads the real stream",
      blk(ev(tid="e6", eval_continue=True), alex), None)
check("the pins parse on eval turns only",
      (api._parse_eval_continue({"eval_prior": self_prior, "eval_continue": True}, "eval")[1],
       api._parse_eval_continue({"eval_prior": self_prior, "eval_continue": True}, "user"),
       api._parse_eval_continue({"eval_continue": "yes"}, "eval")), (True, (None, None, None), (None, None, None)))
check("eval_signal: edge, else the stand-in", [api._parse_eval_continue({"eval_signal": v}, "eval")[2] for v in ("edge", "x", None)],
      ["edge", None, None])
check("both turn paths set them", open(Path(api.__file__)).read().count(
      '_turns[turn_id]["eval_prior"], _turns[turn_id]["eval_continue"], _turns[turn_id]["eval_signal"] = \\'), 2)
REAL_EVAL_CHATS = cc.eval_chats
check("a failure never breaks the turn", (lambda: (setattr(cc, "eval_chats", lambda *a: 1 / 0),
                                                   blk(ev(tid="e7", eval_prior=self_prior, eval_continue=True), alex))[1])(),
      None)

print("\n── the edge arm: KB's live check over the eval's own chats (in-memory stores) ──")
cc.eval_chats = REAL_EVAL_CHATS
hh.get_store().add_person("Ava Quarry", "kid")   # a fictional household member, so her name is a hook
own = cc.eval_chats(self_prior, "p_alex", time.time())
check("a follow-up sharing a hook with the earlier chat (a person's name): KB's check links it",
      cc.eval_edge(own, "p_alex", "Did Ava get the reminder?"), "eval-prior-0")
check("…one sharing nothing: no edge", cc.eval_edge(own, "p_alex", "What's 15% of 84?"), None)
check("…someone else's chat is never put in its store", cc.eval_edge(cc.eval_chats(other_prior, "p_alex", time.time()),
                                                                     "p_alex", "Did Robin's gift get ordered?"), None)
on_edge = blk(ev(tid="e8", eval_prior=self_prior, eval_continue=True, eval_signal="edge"), alex)
check("the edge arm with no shared hook carries nothing (no stand-in behind it)", on_edge, None)
api._turns["e9"] = {"identity": alex, "source": "eval", "thread": "th-new", "eval_prior": self_prior, "eval_continue": True,
                    "eval_signal": "edge"}
out9 = api._continue_block("e9", alex, [], "Did Ava get the reminder?")
check("…and with one, the block, signal edge", ((out9 or "").startswith(cc.HEAD), lib._TURN_IO.conv_continue), (True, "edge"))
api._continue_block("e2", alex, [], "x")
check("a turn without a block clears the signal (nothing stale reaches the next log)", lib._TURN_IO.conv_continue, None)
src = (Path(lib.__file__)).read_text()
check("the runtime copies the signal into the turn log after its record starts, then clears it",
      ('_TURN_INFO.info["conv_continue"] = _TURN_IO.conv_continue' in src, "_TURN_IO.conv_continue = None" in src), (True, True))

print("\n── why a real turn did or didn't carry one in (content-free, every real turn the check runs on) ──")


def why_of(tid, ident, history=None, question=""):
    api._continue_block(tid, ident, history or [], question)
    return lib._TURN_IO.conv_continue_why


os.environ.pop("CONV_CONTINUE", None)
check("flag off: off", why_of(turn("w1", alex), alex), "off")
os.environ["CONV_CONTINUE"] = "1"
check("a new chat with a recent one of his own: carried", why_of(turn("w2", alex), alex), "carried")
check("a chat with history: not_first_turn", why_of(turn("w3", alex), alex, [{"role": "user", "content": "hi"}]), "not_first_turn")
check("a kid: kid_or_screen", why_of(turn("w4", kid), kid), "kid_or_screen")
check("a shared screen: kid_or_screen", why_of(turn("w5", tv), tv), "kid_or_screen")
check("a temporary chat: temporary", why_of(turn("w6", alex, source="temporary"), alex), "temporary")
check("the chat she's in is her only one: no_recent", why_of(turn("w7", robin, thread="th-r1"), robin), "no_recent")
check("an eval turn is never counted (it picks its own arm)",
      why_of(turn("w8", alex, source="eval", eval_prior=self_prior, eval_continue=True), alex), None)
REAL_PREVIOUS = cc.previous
cc.previous = lambda *a, **k: 1 / 0
check("a failure: error, and the turn goes on", (why_of(turn("w9", alex), alex), api._continue_block(turn("w10", alex), alex, [])),
      ("error", None))
cc.previous = REAL_PREVIOUS
os.environ.pop("CONV_CONTINUE", None)
src = (Path(lib.__file__)).read_text()
check("the runtime logs it beside the signal, lists it in the turn log's keys, then clears it",
      ('_TURN_INFO.info["conv_continue_why"] = _TURN_IO.conv_continue_why' in src, '"conv_continue_why"' in src,
       "_TURN_IO.conv_continue_why = None" in src), (True, True, True))

print("\n── the eval runner's scoring ──")
import conv_continue_eval as ce  # noqa: E402
check("all checks met", ce.score({"expect_all": ["ava"], "expect_any": ["library", "x"], "expect_tools_any": ["send_message"]},
                                 "Ava's library reminder", {"send_message"}), [])
check("each miss names its key, never the text", ce.score(
      {"expect_all": ["sunday"], "expect_any": ["quarry"], "expect_any_also": ["swim"], "expect_tools_any": ["create_calendar_event"],
       "forbid": ["saturday"], "forbid_tools": ["home_control"]}, "Saturday it is", {"home_control"}),
      ["expect_all", "expect_any", "expect_any_also", "expect_tools_any", "forbid", "forbid_tools"])
rows = [{"id": "cc01", "kind": "pronoun", "continues": True, "with": [], "without": ["expect_any"]},
        {"id": "cc14", "kind": "control-unrelated", "continues": False, "with": [], "without": []},
        {"id": "cc17", "kind": "privacy-other-person", "continues": False, "with": [], "without": []}]
rep = ce.report(rows, bank)
check("the bar: continue gain, controls agree and pass, privacy both arms", rep["bar"],
      {"continue_gain": 1, "controls_agree": "1/1", "controls_pass": "1/1", "privacy_both_arms": "1/1"})
rows2 = rows + [{"id": "cc15", "kind": "control-unrelated", "continues": False, "with": ["expect_any"], "without": ["expect_any"]},
                {"id": "cc16", "kind": "control-complete", "continues": False, "with": ["forbid"], "without": []}]
check("a control that misses the same way in both arms agrees; one the block changed doesn't",
      (ce.report(rows2, bank)["bar"]["controls_agree"], ce.report(rows2, bank)["bar"]["controls_pass"]), ("2/3", "1/3"))


class FakeTurn:
    def __init__(self, text, called=(), device=()):
        self.text, self.called, self.device, self.error, self.ms = text, list(called), list(device), "", 5

    def tools(self):
        return {t for t, _ in self.device}


sent = []
fake_ask = lambda q, h, extra=None: sent.append((q, h, extra)) or FakeTurn("Ava's library reminder", device=[("list_reminders", {})])  # noqa: E731
r = ce.run_case(bank[0], fake_ask)
check("one case, both arms: the prior, the arm and the signal travel as eval pins, no history",
      [(h, e["eval_continue"], e["eval_prior"]["name"], e["eval_signal"]) for _, h, e in sent],
      [([], True, bank[0]["prior"]["name"], "recent"), ([], False, bank[0]["prior"]["name"], "recent")])
check("…scored per arm", (r["with"], r["without"]), ([], ["expect_any"]))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
