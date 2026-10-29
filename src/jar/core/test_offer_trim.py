#!/usr/bin/env python3
# gate: hermetic
"""Offers off, held after the model (titan-node#1202): a reply that ends by offering more ("Want the full list?",
"Would you like me to check?") loses that sentence while the asker's `offers` preference is off (the default), for
every brain. Never a choice question ("Family or Home?"), never a reply that is only the offer, never while offers are
on. Streaming holds an offer sentence back for the end of the turn. The prompts are unchanged: a rephrase without the
quoted examples was measured and dropped (10-07: the trim alone took the box's offer failures 23 → 0). The endings bank is what the box's 8B wrote on
10-07, behind fictional answers. Run: python3 scripts/test_offer_trim.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-offers-"))
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
os.environ["AGENT_MEMORY_DIR"] = str(_TMP / "memory")
os.environ["AGENT_TURN_LOG"] = str(_TMP / "turns.jsonl")   # every turn this test runs logs here, never /ai/logs
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


store = hh.HouseholdStore(_TMP / "household.db")
hh.reset_store(store)
robin = store.add_person("Robin Hale", "adult")
lib._TURN_IO.person = {"id": hh.ADMIN_PERSON_ID}
lib._TURN_INFO.info = {}

ANSWER = "Tomorrow you have a piano lesson at 3 PM and a team dinner at 7 PM."
ENDINGS = [   # what the box's local 8B closed with on 10-07 (turn_eval), most common first
    "Want the full list?", "Want the full list of what I need?", "Want the full list of venues shortlisted?",
    "Want me to look it up for you?", "Want me to search your messages for mentions of the iPad?",
    "Would you like me to try a different approach?", 'Would you like me to search for "CV" instead?',
    "Would you like me to request access to run the tests?", "Would you like me to play it on the iPhone instead?",
    "Would you like me to check the status?", "Would you like me to check your printer's status through your Mac?",
    "Let me know if you'd like to adjust this setting later.", "Should I add it to your reminders?",
    "Do you want me to text Robin?",
]
print("── the 10-07 endings, offers off (the default) ──")
trimmed = [lib._without_trailing_offer(f"{ANSWER} {e}") for e in ENDINGS]
check(f"all {len(ENDINGS)} endings trimmed to the answer", [t == ANSWER for t in trimmed], [True] * len(ENDINGS))
check("…on its own line too", lib._without_trailing_offer(f"{ANSWER}\n\nWant the full list?"), ANSWER)
check("the turn log counts a trim", lib._TURN_INFO.info.get("offer_trimmed"), 1)

print("── what must stay ──")
for q in ("Should I put it on Family or Home?", "Do you want the morning slot or the afternoon one?",
          "Which calendar should it go on, Family or Work?"):
    check(f"a choice stays: {q!r}", lib._without_trailing_offer(f"I can book it. {q}"), f"I can book it. {q}")
check("a missing-detail question stays", lib._without_trailing_offer("I'll set it up. What time should it be?"),
      "I'll set it up. What time should it be?")
check("an offer that IS the reply stays", lib._without_trailing_offer("Want me to look it up?"), "Want me to look it up?")
mid = "Want me to add the others too? I added the dentist on Friday at 10:15."
check("an offer mid-reply isn't touched (only a closing one)", lib._without_trailing_offer(mid), mid)
check("a plain answer is untouched", lib._without_trailing_offer(ANSWER), ANSWER)
check("not a string: passed through", lib._without_trailing_offer(None), None)

lib._TURN_INFO.info = {"later_held": 1}
ASKQ = "Nothing is scheduled yet. Would you like me to schedule it for 11 PM tonight?"
check("#1251 B: the later guard's confirm question stays (the later guard held the action)",
      lib._without_trailing_offer(ASKQ), ASKQ)
lib._TURN_INFO.info = {"later_ask_held": 1}
check("…and when it held schedule_action", lib._without_trailing_offer(ASKQ), ASKQ)
lib._TURN_INFO.info = {}
check("…with nothing held it's trimmed like any offer", lib._without_trailing_offer(ASKQ), "Nothing is scheduled yet.")

print("── the asker's preference ──")
store.set_pref(hh.ADMIN_PERSON_ID, "offers", "on")
check("offers on: the offer stays", lib._without_trailing_offer(f"{ANSWER} Want the full list?"),
      f"{ANSWER} Want the full list?")
lib._TURN_IO.person = {"id": robin["id"]}
check("…it's per person: Robin's are still off", lib._without_trailing_offer(f"{ANSWER} Want the full list?"), ANSWER)
lib._TURN_IO.person = {"id": hh.ADMIN_PERSON_ID}
store.set_pref(hh.ADMIN_PERSON_ID, "offers", "off")

print("── streaming and the prompts ──")
check("streaming holds an offer sentence for the end of the turn", lib.stream_stops("Want the full list?"), True)
check("…but not a plain sentence", lib.stream_stops("Tomorrow you have a piano lesson at 3 PM."), False)
import subprocess as _sp  # noqa: E402
main_src = _sp.run(["git", "show", "origin/main:scripts/agent_runtime_lib.py"], capture_output=True, text=True,
                   cwd=str(Path(__file__).parent)).stdout
if main_src:   # the rephrase was measured and dropped (10-07 split): the prompts stay main's, the cached prefix unchanged
    import re as _re
    rule = lambda src: _re.findall(r"^(?:Acting, not asking|Act, don't offer):.*?(?=\n\S)", src, _re.M | _re.S)
    check("the prompts' offer rules are main's, byte for byte (the trim alone carries #1202)",
          rule(Path(lib.__file__).read_text()) == rule(main_src), True)

print("── through a whole turn ──")
lib._run_agent_turn_inner = lambda m, *a, **k: f"{ANSWER} Want the full list?"
check("a turn's model reply loses its closing offer", lib._run_agent_turn([{"role": "user", "content": "what's tomorrow?"}]),
      ANSWER)
lib._run_agent_turn_inner = lambda m, *a, **k: "I've added that to Claude's list."
out = lib._run_agent_turn([{"role": "user", "content": "add it for Claude"}])
check("titan's own Correction keeps its offer (it's titan's, after the model)", out.endswith("Want me to?"), True)

import json as _json  # noqa: E402
lib._run_agent_turn_inner = lambda m, *a, **k: f"{ANSWER} Want the full list?"
lib._run_agent_turn([{"role": "user", "content": "what's tomorrow?"}])
lines = [_json.loads(l) for l in open(_TMP / "turns.jsonl")] if (_TMP / "turns.jsonl").exists() else []
check("the turn log itself records the trim (offer_trimmed), so an A/B can judge the turns it reached",
      bool(lines) and lines[-1].get("offer_trimmed"), 1)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
