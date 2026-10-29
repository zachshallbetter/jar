#!/usr/bin/env python3
# gate: hermetic
"""done.tldr (jellybean#584): a long answer's headline. Written by the model only when the answer already went through
Gemini (nothing new leaves the house), never for a short or local-only answer, and never made up: anything that isn't
one short line is dropped. A fake model; nothing is called."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-tldr-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha-token")
os.environ.pop("TITAN_DISCOVERY", None)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import agent_api_server as api  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.120} expected={expected!r:.120}")


long = "The backup ran at 02:15 and finished cleanly. " * 20
calls = []


def model(answer):
    def call(prompt):
        calls.append(prompt)
        return answer
    return call


check("a long Gemini answer gets the model's headline", lib.reply_tldr(long, egress=["gemini"], call=model("Last night's backup was clean")),
      "Last night's backup was clean")
check("the prompt carries the answer and the rules", "at most 6 words" in calls[-1] and "02:15" in calls[-1], True)
n = len(calls)
check("a local-only turn gets none, and nothing is asked", (lib.reply_tldr(long, egress=[], call=model("x")), len(calls)), (None, n))
check("a short answer gets none", lib.reply_tldr("Yes, at 4.", egress=["gemini"], call=model("x")), None)
check("quotes, a label and ending punctuation come off", lib.clean_tldr('Headline: "Backup ran clean."'), "Backup ran clean")
check("more than one line keeps the first", lib.clean_tldr("Backup ran clean\nIt finished at 02:31"), "Backup ran clean")
check("too long is none, never cut", lib.clean_tldr("This headline is far too long to show over an answer"), None)
check("empty is none", lib.clean_tldr("   "), None)


def boom(_):
    raise TimeoutError("slow")


check("a model error is none", lib.reply_tldr(long, egress=["gemini"], call=boom), None)
check("the limits", (lib.TLDR_MIN_CHARS, lib.TLDR_MAX_CHARS), (600, 40))



# The stream: `done` goes out first, unchanged; the headline is its own `tldr` event after it, within TLDR_WAIT.
order: list[str] = []


def slow(final, egress=()):
    order.append("headline asked")
    time.sleep(0.3)
    return "Last night's backup was clean"


ended: list[float] = []


async def follow(turn_id, make):
    real = lib.reply_tldr
    lib.reply_tldr = make
    out = []
    try:
        async for chunk in api._sse_follow(turn_id):
            event = chunk.split("\n", 1)[0].removeprefix("event: ")
            order.append(event)
            out.append((event, chunk))
        ended.append(time.monotonic())   # when the stream itself closed (a slow headline's thread may run on)
    finally:
        lib.reply_tldr = real
    return out


api._turns["t-long"] = {"state": "done", "text": long, "egress": ["gemini"]}
events = asyncio.run(follow("t-long", slow))
names = [e for e, _ in events]
check("done is sent before the headline is asked for (never held for it)", order.index("done") < order.index("headline asked"), True)
check("then a tldr event with the turn and the headline", names[-2:], ["done", "tldr"])
check("done itself carries no tldr", '"tldr"' in events[names.index("done")][1], False)
check("the tldr event's data", '"turn_id": "t-long"' in events[-1][1] and "backup was clean" in events[-1][1], True)
order.clear()
api._turns["t-local"] = {"state": "done", "text": long, "egress": []}
check("a local-only answer: done, and no tldr event", [e for e, _ in asyncio.run(follow("t-local", slow))][-1:], ["done"])
check("… and nothing is asked", "headline asked" in order, False)


def hang(final, egress=()):
    time.sleep(api.TLDR_WAIT + 1)
    return "too late"


api._turns["t-slow"] = {"state": "done", "text": long, "egress": ["gemini"]}
started = time.monotonic()
names = [e for e, _ in asyncio.run(follow("t-slow", hang))]
check("a slow headline: no tldr event, and the stream ends within the wait", (names[-1], ended[-1] - started < api.TLDR_WAIT + 0.8),
      ("done", True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
