#!/usr/bin/env python3
# gate: hermetic
"""Settings › Assistant and › About (#1076): the "asked for more" count is the caller's own and only from real turns;
About shows the caller's own devices and no hostnames, paths or environment; a report refuses unknown fields, is
cleaned, and the admin sees the queue. Fictional household (Alex, Robin, Ava); temporary stores.
Run: python3 scripts/test_about_info.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-about-test-"))
os.environ["ABOUT_DB"] = str(_TMP / "about.db")
os.environ.pop("AGENT_TURN_SOURCE", None)
sys.path.insert(0, str(Path(__file__).parent))
import about_info as A  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── Assistant: the caller's own count, real turns only ──")
for _ in range(5):
    A.note_turn("p_robin", "phrase", "warm")
A.note_turn("p_robin", "think_harder", None)
A.note_turn("p_ava", "phrase", None)
A.note_turn("p_robin", None, "concise")
check("Robin's 7 days: 6, the persona she last chose", A.assistant("p_robin"),
      {"asked_for_more_7d": 6, "reply_length": "short", "persona": "concise"})
check("the kid's: only theirs", A.assistant("p_ava")["asked_for_more_7d"], 1)
check("older than 7 days isn't counted", A.assistant("p_robin", now=__import__("time").time() + 8 * 86400)["asked_for_more_7d"], 0)

print("── the turn path notes only real turns ──")
for k, v in {"HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ.setdefault(k, str(_TMP / v))
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
JORDAN = hh.Identity(person={"id": "p_jordan", "name": "Jordan Hale", "role": "adult"}, device=None, role="adult", via="device")
SCREEN = hh.Identity(person={"id": "house", "name": "TV", "role": "household"}, device=None, role="household", via="device")
api._note_more_asked({"prompt": "tell me more about the trip"}, {"source": None}, JORDAN)
api._note_more_asked({"prompt": "plan it", "effort": "high"}, {"source": None}, JORDAN)
api._note_more_asked({"prompt": "tell me more"}, {"source": "eval"}, JORDAN)
api._note_more_asked({"prompt": "tell me more"}, {"source": None, "eval_brain": "local"}, JORDAN)
api._note_more_asked({"prompt": "tell me more"}, {"source": None}, SCREEN)
api._note_more_asked({"prompt": "what's the weather"}, {"source": None}, JORDAN)
check("the words and Think harder count; eval, pinned, the screen and plain turns don't",
      A.assistant("p_jordan")["asked_for_more_7d"], 2)

print("── About: own devices, nothing about the host ──")
canary = _TMP / "canary.json"
canary.write_text(json.dumps({"ts": "2026-10-06T08:00:00Z", "status": "fail",
                              "checks": {"brain": {"ok": True}, "kb": {"ok": True}, "home": {"ok": False, "detail": "x"}}}))
PEOPLE = [{"id": "alex", "name": "Alex", "devices": [{"name": "Alex's Mac", "platform": "macos", "app_version": "0.13.54",
                                                      "last_seen": "t", "revoked": False}]},
          {"id": "p_ava", "name": "Ava", "devices": [{"name": "Ava's iPad", "platform": "ios", "app_version": "0.13.53",
                                                      "last_seen": "t", "revoked": False},
                                                     {"name": "old", "platform": "ios", "app_version": "0.12", "revoked": True}]}]
a = A.about("p_ava", PEOPLE, canary=canary)
check("the kid's own devices only, revoked ones left out", [d["name"] for d in a["devices"]], ["Ava's iPad"])
check("the canary: when, passed/total, failing names", (a["canary"]["passed"], a["canary"]["total"], a["canary"]["failing"]),
      (2, 3, ["home"]))
check("brain and background", (a["brain"]["chat"] in ("gemini", "local"), a["brain"]["background_local_only"]), (True, True))
out = json.dumps(a)
check("no hostnames, paths or environment in it", [x for x in ("/Users/", "/home/", "/ai/", "titan.", "GEMINI", "TOKEN") if x in out],
      [])
check("a deployed commit when the checkout has one", a["titan"]["commit"] is None or len(a["titan"]["commit"]) >= 7, True)

print("── reports ──")
try:
    A.report("p_robin", {"text": "broken", "chats": ["x"]})
    check("an unknown field is refused", "stored", 400)
except A.Refused as e:
    check("an unknown field is refused", e.status, 400)
r = A.report("p_robin", {"text": "The sheet froze\x07 after I tapped Save", "device": "Robin's Mac",
                          "app_version": "0.13.54", "log_tail": "open /Users/robin/Library/x.log\ncard 4111 1111 1111 1111"})
row = A.reports("admin", "alex")[0]
check("stored, cleaned: control characters and the home folder", ("\x07" in row["text"], "/Users/robin" in row["log_tail"]),
      (False, False))
check("…a card number masked", "4111 1111 1111 1111" in row["log_tail"], False)
A.report("p_ava", {"text": "the game crashed"})
check("the admin's queue has everyone's", len(A.reports("admin", "alex")), 2)
check("anyone else sees only their own", [x["person"] for x in A.reports("adult", "p_robin")], ["p_robin"])
check("roles: a kid may report; a shared screen may not",
      (hh.route_allowed("kid", "POST", "/v1/reports"), hh.route_allowed("household", "POST", "/v1/reports")), (True, False))

check("the reply says who reads it", A.report("p_robin", {"text": "one more"}, admin_name="Alex")["goes_to"], "Alex")
for i in range(A.REPORTS_PER_DAY - 1):   # the kid has 1 already: 20 in all
    A.report("p_ava", {"text": f"loop {i}", "log_tail": "x" * 100})
try:
    A.report("p_ava", {"text": "one too many"})
    check("20 a day, then 429", "stored", 429)
except A.Refused as e:
    check("20 a day, then 429", e.status, 429)
check("another person isn't capped by the kid's loop", A.report("p_jordan", {"text": "hi"})["stored"], True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
