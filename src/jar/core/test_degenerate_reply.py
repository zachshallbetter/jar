#!/usr/bin/env python3
"""A reply with no words is asked again once (truth eval: "." in r5, "0.25" in r6, after right calendar reads).
Hermetic: Gemini is faked."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_runtime_lib as lib  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


D = lib._degenerate_reply
check("'0.25' to 'What's on Saturday?' is degenerate", D("0.25", "What's on Saturday?"))
check("'.' is degenerate", D(".", "When is my dentist appointment?"))
check("'4' to 'How many days until the trip?' is an answer", not D("4", "How many days until the Spokane trip?"))
check("'3:00' to 'What time is piano?' is an answer", not D("3:00", "What time is piano tomorrow?"))
check("words are never degenerate", not D("Team dinner at 7 PM.", "What's on Saturday?"))
check("'10' to the canary's question is an answer (the 10-01 rollback)",
      not D("10", "Use the count_chars tool on the text 'canary bird' and reply with only the total number of letters."))
check("'555-0142' to a phone question is an answer", not D("555-0142", "What's the vet's phone?"))
check("'2026' to a year question is an answer", not D("2026", "What year did we move in?"))
check("an empty reply is _never_empty's, not this", not D("", "What's on Saturday?"))

calls = []
os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY") or "test-key"
os.environ["AGENT_TOOL_BRAIN"] = "gemini"
lib._situational_context = lambda query=None: "Now: Thursday"
lib._TURN_IO.brain = None


def run(replies, question="What's on Saturday?", wrote=False):
    calls.clear()
    seq = list(replies)

    def fake(messages, **k):
        calls.append(messages[-1]["content"])
        return seq.pop(0)
    lib._run_gemini_agent = fake
    lib._TURN_IO.outcomes = [{"tool": "create_reminder", "ok": True, "write": True, "text": "Added"}] if wrote else []
    lib._TURN_INFO.info = {}
    return lib._run_agent_turn_inner([{"role": "system", "content": "rules"}, {"role": "user", "content": question}],
                                     allowed_tools=set())


out = run(["0.25", "Saturday has a team dinner from 7 to 9 PM."])
check("a degenerate reply is asked again once, and the answer is used", out.startswith("Saturday has a team dinner")
      and len(calls) == 2 and "no words in it" in calls[1], str(calls))
out = run(["0.25", "0.25"])
check("only once: a second degenerate reply becomes the plain empty-reply message",
      len(calls) == 2 and lib._never_empty(out) != "0.25" and lib._TURN_INFO.info.get("degenerate_retries") == 1, repr(out))
out = run(["4"], "How many days until the Spokane trip?")
check("a number asked for is not retried", len(calls) == 1 and out == "4")
out = run(["."], "Remind me to call mom", wrote=True)
check("never retried after a write in the turn", len(calls) == 1)
lib._TURN_IO.outcomes = None
import json, tempfile  # noqa: E401,E402
log = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
os.environ["AGENT_TURN_LOG"] = log
lib._TURN_INFO.info = {"degenerate_retries": 1, "answer_corrections": 2, "schedule_prefetch_ms": 900}
lib._log_turn(0.0, 5, None)
rec = json.loads(open(log).read().splitlines()[-1])
check("the turn log carries the retries and the reply-check counts",
      rec.get("degenerate_retries") == 1 and rec.get("answer_corrections") == 2 and rec.get("schedule_prefetch_ms") == 900, str(rec))

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all degenerate-reply checks passed'}")
sys.exit(1 if failures else 0)
