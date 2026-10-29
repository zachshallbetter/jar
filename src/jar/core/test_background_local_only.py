#!/usr/bin/env python3
# gate: hermetic   (no network: Jev is a fake that records whether it was asked)
"""A background trigger turn (proactive.py) keeps everything on titan (10-03, #748): it never asks Jev, and its
tools never send anything out (no web search, fetching, image description or jev_decide). A person's turn is
unchanged, even one the router sent to titan's own model.
Run: python3 scripts/test_background_local_only.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["HOUSEHOLD_DB"] = str(Path(tempfile.mkdtemp()) / "household.db")
os.environ["AGENT_TURN_SOURCE"] = "test"
os.environ["AGENT_JEV_TURN_GATES"] = "1"
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.200} expected={expected!r:.200}"))


asked: list[str] = []


class FakeJev:
    def decide(self, *a, **k):
        asked.append("decide")
        return {"choice": "calendar", "confidence": 0.99, "answer": "yes"}


lib._jev_client = lambda: FakeJev()   # noqa: E731

print("── a person's turn: the gates are on (even pinned local, as an eval measures it) ──")
lib._TURN_IO.jev_source = None
lib._TURN_IO.brain = "local"
check("gates on", lib._jev_gates_on(), True)
lib.jev_pick_skill("what's on tomorrow?", {"calendar": "events", "media": "music"})
check("the skill pick asked Jev", len(asked) >= 1, True)

print("\n── a background trigger turn: never ──")
asked.clear()
lib._TURN_IO.jev_source = "proactive"
check("gates off", lib._jev_gates_on(), False)
check("the skill pick gives no verdict", lib.jev_pick_skill("what's on tomorrow?", {"calendar": "events"}), None)
check("the lasting-fact gate gives no verdict", lib._jev_lasting_fact("Remember the plumber is AquaFix"), None)
check("Jev was never asked", asked, [])
lib._TURN_IO.jev_source = lib._TURN_IO.brain = None

print("\n── a background turn's tools ──")
check("none of them sends anything out", lib.BACKGROUND_TOOLS & lib.EGRESS_TOOLS, frozenset())
for t in ("web_search", "fetch_url", "describe_image", "jev_decide"):
    check(f"no {t}", t in lib.BACKGROUND_TOOLS, False)
for t in ("kb_retrieve", "find_records", "recall_events", "run_check", "get_datetime"):
    check(f"still has {t}", t in lib.BACKGROUND_TOOLS, True)
check("every egress tool is a real step tool (the list didn't drift)", lib.EGRESS_TOOLS <= lib._SAFE_STEP_TOOLS, True)

print("\n── wired in the loop ──")
src = Path(lib.__file__).with_name("proactive.py").read_text()
check("proactive's turn offers and allows only BACKGROUND_TOOLS",
      ("build_tool_system_prompt(lib.BACKGROUND_TOOLS)" in src, "allowed_tools=set(lib.BACKGROUND_TOOLS)" in src), (True, True))
check("…and marks itself a background turn", 'lib._TURN_IO.jev_source = "proactive"' in src, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
