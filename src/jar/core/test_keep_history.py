#!/usr/bin/env python3
"""The conversation stays in the turn (evening goal #4, 10-01). Until then, any memory-recall hit cut the
history to the last exchange, so a long chat lost what was said three messages earlier. Hermetic: memory
recall is faked."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_api_server as api  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


recall = {"facts": "Relevant facts: Ava takes ballet."}
api._MEMORY_AVAILABLE = True
api._get_memory = lambda: SimpleNamespace(
    recall=lambda q, conv_id=None, person_id=None: SimpleNamespace(prependContext=recall["facts"], appendSystemContext=""))


def hist(n: int, size: int) -> list[dict]:
    out = []
    for i in range(n):
        out += [{"role": "user", "content": f"u{i} " + "x" * size}, {"role": "assistant", "content": f"a{i} " + "y" * size}]
    return out


def convo(msgs: list[dict]) -> list[str]:
    return [m["content"] for m in msgs if m["role"] in ("user", "assistant")]


# 1. A long chat under the cap, with a recall hit: everything said stays, recall rides alongside.
h = hist(20, 2000)
h[2]["content"] += " the plumber's name is Dave Ruiz"
h[-6]["content"] += " Ava's recital moved to 6:30 PM"
msgs, avoided = api._build_messages("What time is the recital?", h, False, None, turn_id="kh-1")
text = "\n".join(convo(msgs))
check("a recall hit no longer cuts the conversation", "6:30 PM" in text and "Dave Ruiz" in text and avoided == 0)
check("recalled facts still reach the model", any(m["role"] == "system" and recall["facts"] in m["content"] for m in msgs))
check("the question comes last, as asked", msgs[-1] == {"role": "user", "content": "What time is the recital?"})

# 2. Past the cap: the newest messages stay, the oldest give way to the recalled facts, with a note.
h = hist(60, 2000)    # ~240k chars
msgs, avoided = api._build_messages("and then?", h, False, None, turn_id="kh-2")
kept = convo(msgs)[:-1]
check("past the cap, the newest messages stay verbatim", kept[-1] == h[-1]["content"] and kept[-2] == h[-2]["content"])
check("and stay within the cap", sum(map(len, kept)) <= api.HISTORY_KEEP_CHARS)
note = [m["content"] for m in msgs if m["content"].startswith("[OLDER MESSAGES OMITTED")]
check("older messages are replaced by a note and the recalled facts",
      len(note) == 1 and "[PRIOR CONTEXT]" in note[0] and recall["facts"] in note[0] and avoided > 0, str(note)[:200])
check("the accounting counts what was dropped",
      avoided == sum(len(m["content"]) for m in h) - sum(map(len, kept)))

# 3. One enormous last exchange is still sent whole (the local model trims to fit; Gemini reads it).
h = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "ok"},
     {"role": "user", "content": "z" * (api.HISTORY_KEEP_CHARS + 10)}, {"role": "assistant", "content": "done"}]
msgs, _ = api._build_messages("next?", h, False, None, turn_id="kh-3")
check("the last exchange is never cut", any(m["content"] == h[2]["content"] for m in msgs))

# 4. No recall, a short chat: verbatim, no note.
recall["facts"] = ""
msgs, avoided = api._build_messages("hi", hist(2, 10), False, None, turn_id="kh-4")
check("a short chat without recall is sent as is",
      len(convo(msgs)) == 5 and avoided == 0 and not any("[OLDER MESSAGES OMITTED" in m["content"] for m in msgs))

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all keep-history checks passed'}")
sys.exit(1 if failures else 0)
