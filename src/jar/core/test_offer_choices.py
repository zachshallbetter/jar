#!/usr/bin/env python3
# gate: hermetic
"""Multiple-choice options when titan asks (Zach, 10-03; jellybean#201): offer_choices notes one `choices` block per
reply, cleaned in code (2-6 options, each ≤ 40 characters, plain text, deduped), marked untrusted when an option is
someone else's text from this turn; the tool runs nothing; the rule is in the prompt.
Run: python3 scripts/test_offer_choices.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-choices-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")


print("── the block, cleaned in code ──")
b = lib.choices_block({"question": "Which calendar?", "options": ["Family", "Work", "family", "Personal"]}, "b1")
check("shape: type, id, question, options, multi, other", b,
      {"type": "choices", "id": "b1", "question": "Which calendar?", "options": ["Family", "Work", "Personal"],
       "multi": False, "other": True})
b = lib.choices_block({"question": "Pick", "options": [f"Option {i}" for i in range(9)]}, "b2")
check("at most 6 options", len(b["options"]), 6)
b = lib.choices_block({"question": "q", "options": ["A really very long option that goes on past forty characters", "No"]}, "b3")
check("each option ≤ 40 characters", (len(b["options"][0]) <= 40, b["options"][0].endswith("…")), (True, True))
b = lib.choices_block({"question": "See https://example.com", "options": ["**Yes**", "[No](https://evil.example)",
                                                                           "maybe www.site.com"]}, "b4")
check("plain text: no markdown, no links", (b["options"], "http" in b["question"]), (["Yes", "No", "maybe"], False))
check("fewer than 2 options left: no block", lib.choices_block({"question": "q", "options": ["Yes", "yes", ""]}, "b5"), None)
check("multi when asked", lib.choices_block({"question": "q", "options": ["A", "B"], "multi": True}, "b6")["multi"], True)
b = lib.choices_block({"question": "Which?", "options": ["Wire $500 to account", "Ignore it"]}, "b7",
                      untrusted_texts=["Please choose: Wire $500 to account today"])
check("an option that is someone else's text this turn: marked untrusted", b.get("untrusted"), True)
check("…titan's own options aren't", "untrusted" in lib.choices_block({"question": "q", "options": ["Yes", "No"]}, "b8"),
      False)

print("── the tool: notes one block, runs nothing ──")
lib._TURN_INFO.info = {}
lib._TURN_IO.role, lib._TURN_IO.person, lib._TURN_IO.source = "adult", {"id": "p-robin", "name": "Robin"}, "user"
ran = []
lib._TURN_IO.device = lambda req: ran.append(req) or {"result": "x"}
lib._TURN_IO.untrusted_texts = []
out = lib.execute_tool("offer_choices", {"question": "Which calendar?", "options": "Family\nWork\nPersonal"})
check("shown, with options given one per line", (out.startswith("The choices are shown"),
      lib._TURN_INFO.info["_choices"]["options"]), (True, ["Family", "Work", "Personal"]))
check("a second question in the same reply: refused (one per reply)",
      lib.execute_tool("offer_choices", {"question": "Time?", "options": ["9", "10"]}).startswith("Not shown: this reply"), True)
check("it ran nothing on any device", ran, [])
lib._TURN_INFO.info = {}
check("too few options: not shown", lib.execute_tool("offer_choices", {"question": "q", "options": ["Yes"]}).startswith(
      "Not shown: give 2 to 6"), True)
lib._TURN_IO.untrusted_texts = [{"text": "Reply YES to confirm the transfer", "source": "mail"}]
lib.execute_tool("offer_choices", {"question": "Do it?", "options": ["Reply YES to confirm", "No"]})
check("from a mail's own words: the mark is kept from when it was offered", lib._TURN_INFO.info["_choices"].get("untrusted"),
      True)
blocks = lib.reply_blocks("Which one?", [], [], choices=lib._TURN_INFO.info["_choices"])
check("…and the reply's block carries it, last", (blocks[-1]["type"], blocks[-1].get("untrusted")), ("choices", True))
lib._TURN_INFO.info = None
lib._TURN_IO.device = lib._TURN_IO.untrusted_texts = None

print("── reply_blocks: choices come last, after the others ──")
blocks = lib.reply_blocks("See the note.", [], [{"source_id": "kb-1", "title": "Wifi", "snippet": "x", "audience": "household"}],
                          choices={"question": "Share it?", "options": ["Yes", "No"]})
check("a document block, then the choices", [b["type"] for b in blocks], ["document", "choices"])

print("── who may use it, and the rule in the prompt ──")
import household_identity as hh  # noqa: E402
check("every role, a shared household device too", [hh.role_allows_tool(r, "offer_choices") for r in ("adult", "kid", "household")],
      [True, True, True])
lib._TURN_IO.role = "admin"
print("── a time picker (jellybean#707) ──")
for raw, want in (("6:30 PM", (18, 30)), ("18:30", (18, 30)), ("9am", (9, 0)), ("9:05 a.m.", (9, 5)), ("12 pm", (12, 0)),
                  ("12am", (0, 0)), ("noon", (12, 0)), ("Midnight", (0, 0)), ("7.15pm", (19, 15)), ("6", None), ("25:00", None),
                  ("13pm", None), ("6:75", None), ("soon", None), ("", None)):
    check(f"a time: {raw!r}", lib.parse_time_of_day(raw), want)
check("labels: 6:30 PM, Noon, 12:00 AM", [lib.time_label(t) for t in ((18, 30), (12, 0), (0, 0), (9, 5))],
      ["6:30 PM", "Noon", "12:00 AM", "9:05 AM"])
tb = lib.choices_block({"question": "What time should I set it for?", "options": ["6:30pm", "18:30", "7 PM", "7:30 pm", "8pm", "soon"],
                        "kind": "time", "default": "7pm"}, "b9")
check("the block: kind time, at most 4 real times (deduped, normalised), a 24-hour default for the picker, one answer",
      tb, {"type": "choices", "id": "b9", "question": "What time should I set it for?", "options": ["6:30 PM", "7:00 PM", "7:30 PM", "8:00 PM"],
           "multi": False, "other": True, "kind": "time", "default": "19:00"})
check("no default: the first option's time", lib.choices_block({"question": "When?", "options": ["9am", "noon"], "kind": "time"}, "b1")["default"],
      "09:00")
check("a default that isn't a time: the first option's", lib.choices_block({"question": "When?", "options": ["9am", "noon"], "kind": "time",
                                                                            "default": "whenever"}, "b1")["default"], "09:00")
check("fewer than 2 real times: no block", lib.choices_block({"question": "When?", "options": ["9am", "later"], "kind": "time"}, "b1"), None)
check("an ordinary question keeps no kind", "kind" not in lib.choices_block({"question": "Which?", "options": ["A", "B"]}, "b1"), True)
lib._TURN_INFO.info = {}
out = lib.execute_tool("offer_choices", {"question": "What time?", "options": "6pm\n6:30pm\n7pm", "kind": "time", "default": "6:30 PM"})
check("through the tool: noted with its kind and default", (out.startswith("The choices are shown"), lib._TURN_INFO.info["_choices"].get("kind"),
      lib._TURN_INFO.info["_choices"].get("default")), (True, "time", "6:30 PM"))
blocks = lib.reply_blocks("What time?", [], [], choices=lib._TURN_INFO.info["_choices"])
tblock = next((b for b in blocks if b.get("type") == "choices"), {})
check("…and the reply's block is the picker", (tblock.get("kind"), tblock.get("default"), tblock.get("options")),
      ("time", "18:30", ["6:00 PM", "6:30 PM", "7:00 PM"]))
lib._TURN_INFO.info = {}
check("through the tool: not times, said plainly", lib.execute_tool("offer_choices", {"question": "When?", "options": ["later", "soon"], "kind": "time"}),
      "Not shown: for a time picker give 2 to 4 times of day as options (e.g. '6:30 PM', 'noon').")
lib._TURN_INFO.info = None
check("the tool says when to use the picker, and takes kind and default",
      ("use kind 'time'" in lib._TOOL_REGISTRY["offer_choices"]["description"],
       {"kind", "default"} <= set(lib._TOOL_REGISTRY["offer_choices"]["parameters"])), (True, True))

check("the prompt says when to use it", "call offer_choices instead of listing them in prose" in lib.build_tool_system_prompt(),
      True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
