#!/usr/bin/env python3
# gate: hermetic
"""#1243 (the box's KB/world misses, 10-07): what was measured to help, and an eval wording fix.
  - The passive KB lines the model reads carry each passage's saved date (kb_privacy.date_label), so "When did I save
    my note …?" answers from the passage: kb_saved_note passed on both clients in the box A/B (A and A' failed).
  - world_eval's "unknown" check accepts "isn't / is not / aren't on record" (straight or curly apostrophe): the 8B's
    right answer "…isn't on record." was scored a miss on every arm. Still refuses a guessed number.
  - The local answer line for notes and world facts was measured and dropped: it reached the 8B (turn log) and changed
    no outcome. The note-number misses are rethought in their own ticket.
Run: python3 scripts/test_kb_passive_dates.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-kbdates-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import kb_privacy  # noqa: E402
import world_eval as we  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── passive KB lines carry the saved date ──")
src = (Path(__file__).parent / "agent_runtime_lib.py").read_text()
block = src[src.index("# 5. Relevant KB memory"):src.index("# 5. Relevant KB memory") + 4000]
check("the passive line's label takes kb_privacy.date_label", ("dated = _kbp.date_label(p)" in block,
      'label = f"{label} · {dated}"' in block), (True, True))
hit = {"source_id": "note-1", "metadata": {"saved": "2026-10-01"}, "chunk_text": "Recycling goes out on the 1st Monday."}
check("a saved note says when it was saved", (kb_privacy.date_label(hit) or "").startswith("saved "), True)
check("…and a passage with no date says nothing", kb_privacy.date_label({"metadata": {}}), None)

print("── world_eval: 'not on record' in any form ──")
check_unknown = we._check_for("unknown", "phone", [], "person")
for reply in ("The phone number for Bedroom TV isn't on record.", "That isn’t on record.", "It is not on record.",
              "Not on record.", "I don't have that on file."):
    check(f"accepted: {reply!r}", check_unknown(reply), [])
check("a guessed number is still refused", check_unknown("The number is 509-555-0100.") != [], True)
check("…and saying nothing about it is still a miss", check_unknown("Sure, here you go.") != [], True)

print("── the answer line that changed nothing is gone ──")
check("no _with_local_answer_line in the agent", "_with_local_answer_line" in src, False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
