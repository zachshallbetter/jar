#!/usr/bin/env python3
"""#348: a long chat on the local model (the Gemini-failed fallback) is condensed in code so it answers within
the turn budget, and keeps the facts that matter. Hermetic: no model is called."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_runtime_lib as lib  # noqa: E402
import turn_eval as te  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


SYS = [{"role": "system", "content": "rules"}, {"role": "system", "content": "[SITUATION] Now: Thursday"}]
Q = {"role": "user", "content": "What time is Ava's recital now?"}

short = SYS + [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello."}, Q]
check("a short chat is untouched", lib._compact_for_local(short) == short)

long = SYS + te._LONG + [Q]
out = lib._compact_for_local(long)
cond = next(m["content"] for m in out if m["content"].startswith("[EARLIER"))
check("the earlier conversation is condensed within the budget (the last exchange stays whole)",
      len(cond) <= lib.LOCAL_CONV_TOKENS * lib._CHARS_PER_TOKEN + 200 and sum(len(m["content"]) for m in out) < 12000,
      f"{len(cond)} chars condensed, {sum(len(m['content']) for m in out)} total (was {sum(len(m['content']) for m in long)})")
check("the question and the last reply stay word for word", out[-1] == Q and out[-2] == te._LONG[-1])
check("system messages stay, in order", [m for m in out if m["role"] == "system"][:2] == SYS)
note = next(m["content"] for m in out if m["content"].startswith("[EARLIER"))
check("a late fact at the end of a long message survives", "Ava's recital moved to 6:30 PM" in note)
check("it says how many older messages were left out", "older messages omitted" in note)
check("the condensed lines read in order, newest last", note.rfind("Turn 38") > note.rfind("Turn 30"))

kl = lib._key_line("We talked about many things today. The weather was nice. Rent of $2,950 is due Oct 1. "
                   "We talked about many things today.", 60)
check("a key line keeps the money-and-date sentence first", "$2,950" in kl and len(kl) <= 60, kl)
check("a key line drops repeats", lib._key_line("Same. Same. Same.", 100) == "Same.")

# The fallback: Gemini fails, the local model runs with a plain-words status.
seen = []
orig = (lib._run_gemini_agent, lib._run_local_agent)
os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY") or "test-key"
os.environ["AGENT_TOOL_BRAIN"] = "gemini"
try:
    def boom(*a, **k):
        raise RuntimeError("HTTP Error 503: Service Unavailable")
    lib._run_gemini_agent = boom
    lib._run_local_agent = lambda messages, **k: "Ava's recital is at 6:30 PM on Saturday."
    lib._TURN_IO.status = seen.append
    lib._TURN_IO.brain = None
    lib._situational_context = lambda query=None: "Now: Thursday"
    text = lib._run_agent_turn_inner(long, allowed_tools=set())
    check("Gemini failing: the local model answers", "6:30" in text, text)
    check("and the person sees why it's slower, in plain words",
          any("Gemini isn't answering" in s for s in seen), str(seen))
finally:
    lib._run_gemini_agent, lib._run_local_agent = orig
    lib._TURN_IO.status = None

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all local long-chat checks passed'}")
sys.exit(1 if failures else 0)
