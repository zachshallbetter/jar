#!/usr/bin/env python3
# gate: hermetic
"""A note's numbers survive the local brain (titan-node#1252): "What does my note “X” say?" answered with a number,
year or time from the note dropped gets the note's own sentences that hold it quoted as "From your note:" (no second
model call). Only from a passage THIS turn showed the asker (the passive context or a kb_retrieve read). Local brain
only. Fictional notes; throwaway stores.
Run: python3 scripts/test_note_numbers.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-notenums-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


NOTE = ("Leo and Ava are with their dad until Thursday, October 8, 2026 at 9:00 AM. Thursday is a late start: school "
        "begins at 9:45 AM. Recycling goes out on the 1st Wednesday.")
Q = "What does my note “This Week 2 Plans” say?"
lib.kb_sources = lambda *a, **k: [{"source_id": "note-week", "title": "This Week 2 Plans"},
                                  {"source_id": "note-robin-week", "title": "Robin plans"}]


def fresh(shown=True, wrote=False):
    lib._TURN_IO.kb_shown = [{"source_id": "note-week", "text": NOTE}] if shown else []
    lib._TURN_IO.outcomes = [{"tool": "add_todo", "ok": True, "write": True}] if wrote else []
    lib._TURN_INFO.info = {}


def quote(reply, q=Q):
    return lib._note_numbers_quote(reply, [{"role": "user", "content": q}])


print("── a complete reply is left alone ──")
fresh()
full = ("They're with their dad until Thursday, October 8, 2026 at 9:00 AM; school starts at 9:45 AM on the late start, "
        "and recycling goes out on the 1st Wednesday.")
check("nothing missing: unchanged, nothing counted", (quote(full), lib._TURN_INFO.info.get("note_quote_added")), (full, None))

print("── a dropped year: the note's own sentence is quoted ──")
fresh()
short = "They're with their dad until Thursday, October 8 at 9:00 AM. School starts at 9:45 AM; recycling on the 1st Wednesday."
out = quote(short)
check("the reply stays, with only the sentence holding 2026 quoted from the note",
      (out.startswith(short), "From your note: \u201cLeo and Ava are with their dad until Thursday, October 8, 2026" in out,
       "late start" in out.split("From your note:")[1]), (True, True, False))
check("…counted", lib._TURN_INFO.info.get("note_quote_added"), 1)
fresh()
two = "They're with their dad until Thursday; school starts at 9:45. Recycling goes out on Wednesdays."
out = quote(two).split("From your note:")[-1]
check("two sentences missing numbers: both quoted, the complete one not", ("2026" in out, "1st Wednesday" in out,
      "9:45" in out), (True, True, False))
fresh(wrote=True)
check("after a write the quote still comes from the shown note", "From your note:" in quote(short), True)

print("── only a passage this turn showed the asker ──")
fresh(shown=False)
check("the note wasn't shown (kb_shown empty): nothing", quote(short), short)
fresh()
lib._TURN_IO.kb_shown = None
check("…nor when nothing recorded this turn", quote(short), short)
fresh()
lib._TURN_IO.kb_shown = [{"source_id": "note-robin-week", "text": "Robin's 2026 plans: 4 PM."}]
check("a different note's passage doesn't stand in for it", quote(short), short)
fresh()
check("not a note question: nothing", quote("ok", "What's for dinner?"), "ok")

print("── edges ──")
fresh()
lib.kb_sources = lambda *a, **k: [{"source_id": "note-wk2", "title": "Week 2 checklist"}]
lib._TURN_IO.kb_shown = [{"source_id": "note-wk2", "text": "Week 2 checklist: bring 3 forms."}]
check("a number only in the note's title ('Week 2') isn't required in the reply",
      quote("Bring 3 forms.", "What does my note \u201cWeek 2 checklist\u201d say?"), "Bring 3 forms.")
src = Path(lib.__file__).read_text()
local = src[src.index("text = _run_local_agent(messages, allowed_tools=allowed_tools, max_rounds=max_rounds, **opts)"):]
local = local[:local.index("except TurnBudgetExceeded")]
check("wired into the local brain's chain only, with no second model call",
      ("text = _note_numbers_quote(text, messages)" in local, src.count("_note_numbers_quote(text,"), "_note_numbers_retry" in src),
      (True, 1, False))
check("the passive context and kb_retrieve record what they showed", (src.count("_TURN_IO.kb_shown = shown"),
      "_TURN_IO.kb_shown = (getattr(_TURN_IO, \"kb_shown\", None) or []) + [" in src), (1, True))
lib._TURN_IO.kb_shown = lib._TURN_IO.outcomes = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
