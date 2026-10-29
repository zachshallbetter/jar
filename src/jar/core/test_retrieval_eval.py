#!/usr/bin/env python3
"""retrieval_eval scores both ways: a right item found, a wrong one missed; nothing in the context when
nothing is stored, and a miss when something leaks in (hermetic: search and context are faked).

Run: python3 scripts/test_retrieval_eval.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import retrieval_eval as re_  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


right = {q: [{"chunk_text": " ".join(words)}] for _, q, words in re_.FOUND}
r = re_.run(query=lambda q, k, passive: right.get(q, []), context=lambda q: "Now: Thursday")
check("all right items and a quiet context: full marks", (r["found"], r["not_stored"], r["failing"]), (len(re_.FOUND), 3, []))
r = re_.run(query=lambda q, k, passive: [{"chunk_text": "something unrelated"}],
            context=lambda q: "Relevant memory (household facts only…):\n- [decision] the proactive loop triages triggers")
check("wrong items and unrelated memory: every check fails", (r["found"], r["not_stored"], len(r["failing"])), (0, 0, len(re_.FOUND) + 3))
r = re_.run(query=lambda q, k, passive: right.get(q, []),
            context=lambda q: ("Relevant memory (household facts only…):\n- [note] Trash and recycling go out Tuesday night"
                               if "trash" in q else "Now: Thursday"))
check("memory that answers a question skips it (now stored), the rest still count",
      (r["not_stored"], r["not_stored_total"], r["skipped"], r["failing"]), (2, 2, ["trash-day"], []))

# #366: the in-process context is an eval turn, so the world block records no mentions or gaps from it
import types  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import world  # noqa: E402
import world_eval  # noqa: E402
seen = []
saved = (world.get, world_eval.lookups, lib._situational_context, world_eval.mentions)
world.get = lambda: object()
world_eval.lookups = lambda w, limit, unknown=0: [types.SimpleNamespace(values=["555"], name="vet.phone",
                                                                         question="What's the vet's number?")]
lib._situational_context = lambda q: seen.append(lib.real_turn()) or "vet: 555"
world_eval.mentions = lambda ctx, v, attr: v in ctx
lib._TURN_IO.source = None
try:
    w = re_.run_world()
finally:
    world.get, world_eval.lookups, lib._situational_context, world_eval.mentions = saved
check("run_world scores, as an eval turn (never a person's)", (w.get("world"), seen), (1, [False]))
lib._TURN_IO.source = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
