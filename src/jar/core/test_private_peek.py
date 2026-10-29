#!/usr/bin/env python3
# gate: hermetic
"""Private peek (jellybean#426): on a shared screen, a question about the asker goes to their own phone as a card;
nothing private comes back to the shared screen, whoever says whose name.

Run: python3 scripts/test_private_peek.py
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-peek-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "PROACTIVE_DB": "proactive.db", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import private_peek as P  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


PEOPLE = [{"id": "alex", "name": "Alex Hale", "role": "admin"}, {"id": "robin", "name": "Robin", "role": "adult"},
          {"id": "ava", "name": "Ava", "role": "kid"}]
sent = []
send = lambda title, detail, **k: sent.append({"title": title, "detail": detail, **k})  # noqa: E731
paired = lambda pid: pid in ("alex", "robin")   # noqa: E731

print("\n── what's personal ──")
for q in ("What's my day tomorrow?", "Did I pay the T-Mobile bill?", "Read me my messages", "When is my dentist?"):
    check(f"personal: {q}", P.personal(q), True)
for q in ("What's for dinner?", "When is garbage night?", "Play Kind of Blue", "Turn on the living room lights",
          "Where are the kids this week?"):
    check(f"not personal: {q}", P.personal(q), False)

print("\n── a name in the question: sent straight away ──")
r = P.handle("It's Alex, what's my day tomorrow?", "tv1", "Living room TV", PEOPLE, paired, send, now=1000)
check("the shared screen says where it went, nothing more", r, "Sent to Alex's phone. Open the card there to see the answer.")
c = sent[-1]
check("a card to Alex only, asking the question on his phone, not learned by memory",
      (c["to"], c["action_prompt"], c["title"]),
      (["alex"], "(from a suggestion card) It's Alex, what's my day tomorrow?", "You asked on the Living room TV"))
check("the card holds the question, never an answer", "answer here" in c["detail"], True)

print("\n── no name: titan asks, and the next words on that screen are the name ──")
sent.clear()
r = P.handle("What's on my calendar?", "tv1", "Living room TV", PEOPLE, paired, send, now=2000)
check("asks who", r.endswith("Who's asking? Say your name."), True)
check("nothing sent yet", sent, [])
check("another screen's answer doesn't count", P.handle("Robin", "kitchen", "Kitchen", PEOPLE, paired, send, now=2010), None)
r = P.handle("It's Robin", "tv1", "Living room TV", PEOPLE, paired, send, now=2020)
check("the name sends the question that was asked", (r, sent[-1]["to"], sent[-1]["action_prompt"].endswith("What's on my calendar?")),
      ("Sent to Robin's phone. Open the card there to see the answer.", ["robin"], True))

print("\n── edges ──")
sent.clear()
P.handle("Do I have practice today?", "tv1", "TV", PEOPLE, paired, send, now=3000)
check("too late: the question is dropped, this turn goes on", P.handle("Ava", "tv1", "TV", PEOPLE, paired, send, now=3000 + P.PENDING_S + 1), None)
P.handle("Do I have practice today?", "tv1", "TV", PEOPLE, paired, send, now=4000)
check("not a name: dropped, and a household question goes on", P.handle("What's for dinner?", "tv1", "TV", PEOPLE, paired, send, now=4005), None)
check("no paired phone: says so, sends nothing",
      (P.handle("It's Ava, when is my recital?", "tv1", "TV", PEOPLE, paired, send, now=5000), sent),
      ("Ava doesn't have a phone paired with titan, so I can't answer that privately. Ask me on your own device.", []))
check("a household question: not a peek", P.handle("When is garbage night?", "tv1", "TV", PEOPLE, paired, send, now=6000), None)
check("two people named: ask instead of guessing", P.named("for Alex and for Robin", PEOPLE), None)

print("\n── through a household-device turn ──")
import household_identity as hh  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import proactive  # noqa: E402
st = hh.get_store()
alex = st.add_person("Alex Hale", "adult")
st._db.execute("INSERT INTO devices (id, person_id, name, platform, token_sha256, created_at) VALUES "
               "('dz', ?, 'Alex''s iPhone', 'ios', 'x1', 0), ('tvd', ?, 'Living room TV', 'tvos', 'x2', 0)",
               (alex["id"], alex["id"]))
lib._TURN_IO.device_id, lib._TURN_IO.role, lib._TURN_IO.person = "tvd", "household", {"id": "household"}
lib._TURN_INFO.info = {}
try:
    r = lib._private_peek("It's Alex, what's on my calendar tomorrow?")
    turn_peek = lib._TURN_INFO.info.get("peek")
finally:
    lib._TURN_IO.device_id = lib._TURN_IO.role = lib._TURN_IO.person = None
cards = proactive.list_cards() if hasattr(proactive, "list_cards") else []
check("sent to Alex's phone", r, "Sent to Alex's phone. Open the card there to see the answer.")
mine = [x for x in cards if "Living room TV" in str(x.get("title"))]
if hasattr(proactive, "list_cards"):
    check("one card, Alex's, from the peek", (len(mine), mine and mine[0].get("source")), (1, "peek"))
    check("a shared device doesn't see it", proactive.visible_to(mine[0].get("audience"), "household", "household"), False)

check("the turn carries the peek as data for the reply (jellybean#426)", turn_peek, {"state": "sent", "name": "Alex"})

print("\n── the outcome as data (jellybean#426): the text is the same with or without it ──")
P._pending.clear()
seen = []
quiet = P.handle("It's Robin, did I pay the bill?", "tv2", "TV", PEOPLE, paired, lambda *a, **k: None, now=7000)
told = P.handle("It's Robin, did I pay the bill?", "tv2", "TV", PEOPLE, paired, lambda *a, **k: None, now=7001,
                report=seen.append)
check("sent: same words as a client without the field gets", told, quiet)
check("sent: as data", seen[-1], {"state": "sent", "name": "Robin"})
P.handle("It's Ava, when's my recital?", "tv2", "TV", PEOPLE, paired, lambda *a, **k: None, now=7002, report=seen.append)
check("no phone: as data", seen[-1], {"state": "no_phone", "name": "Ava"})
q = P.handle("What's my day?", "tv3", "TV", PEOPLE, paired, lambda *a, **k: None, now=7003, report=seen.append)
check("ask: the names of people with a paired phone, first names only",
      (q.endswith("Say your name."), seen[-1]), (True, {"state": "ask", "names": ["Alex", "Robin"]}))
P._pending.clear()
check("not a peek: nothing reported", (P.handle("When is garbage night?", "tv4", "TV", PEOPLE, paired,
                                                  lambda *a, **k: None, now=7004, report=seen.append), len(seen)), (None, 3))
import inspect  # noqa: E402
import agent_api_server as api  # noqa: E402
src = inspect.getsource(api)
check("the reply and the stream's done event carry `peek` only when set (others unchanged)",
      ('**({"peek": turn["peek"]} if turn.get("peek") else {})' in src, 'done["peek"] = _turns[turn_id]["peek"]' in src),
      (True, True))
check("only a household turn can peek (a person's own turn never does)",
      'if not (held_id or cancel_id or refusal) and turn_role() == "household":\n        refusal = _private_peek(last)'
      in inspect.getsource(lib), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
