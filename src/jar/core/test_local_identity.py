#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""The local model's short identity (#627): the same rules in about a third of the words, only on local turns, only
when AGENT_LOCAL_SHORT_IDENTITY=1 or an eval turn asks (eval_local_where: "short"). Gemini's prompt never changes.
Run: python3 scripts/test_local_identity.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["HOUSEHOLD_DB"] = str(Path(tempfile.mkdtemp()) / "household.db")
os.environ["AGENT_TURN_SOURCE"] = "test"
os.environ["AGENT_LOCAL_SHORT_IDENTITY"] = "0"   # the checks below start from it switched off
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


SYS = [{"role": "system", "content": lib.AGENT_IDENTITY + "\n\n## Tools\n(tool list)"}, {"role": "user", "content": "hi"}]
tail = lib._identity_tail()
full = lib._local_messages([dict(m) for m in SYS])[0]["content"]
check("switched off (=0): the local prompt has the full identity", (tail in full, lib.LOCAL_IDENTITY_TAIL in full), (True, False))
lib._TURN_IO.eval_local_where = "short"
short = lib._local_messages([dict(m) for m in SYS])[0]["content"]
lib._TURN_IO.eval_local_where = None
check("an eval turn that asks: the short one instead", (tail in short, lib.LOCAL_IDENTITY_TAIL in short), (False, True))
check("…about a third of the words", round(len(lib.LOCAL_IDENTITY_TAIL) / len(tail), 2) < 0.5, True)
check("…the rest of the prompt unchanged", short.split("## Tools", 1)[1] == full.split("## Tools", 1)[1], True)
os.environ["AGENT_LOCAL_SHORT_IDENTITY"] = "1"
check("AGENT_LOCAL_SHORT_IDENTITY=1: every local turn", lib.LOCAL_IDENTITY_TAIL in lib._local_messages([dict(m) for m in SYS])[0]["content"], True)
check("Gemini's prompt never changes (its messages aren't the local ones)",
      lib._prompt_for_native_tools(SYS[0]["content"]).count("Where you run:") == 1 and tail in SYS[0]["content"], True)
os.environ.pop("AGENT_LOCAL_SHORT_IDENTITY", None)
check("on by default since 10-03 (#627)", lib.LOCAL_IDENTITY_TAIL in lib._local_messages([dict(m) for m in SYS])[0]["content"], True)
os.environ["AGENT_LOCAL_SHORT_IDENTITY"] = "0"

print("── every rule is still there ──")
for words in ("Never say the household's data stays in the house", "approves each in Jellybean", "never claim you'll act",
              "credential or key", "call capabilities", "Find My", "schedule_action", "create_reminder", "health_summary",
              "your Mac didn't wake", "Read before you describe", "dedicated tool's answer", "Live state only",
              "ask a grown-up", "never show tool names", "add_todo for=claude", "never edit a deployed copy",
              "not able yet", "no \"Want me to…?\"", "set_preference", "never promise to check back", "Your Mac",
              "large language model", "iCloud Drive", "run_python has no network"):
    check(f"keeps: {words}", words in lib.LOCAL_IDENTITY_TAIL, True)

src = (Path(lib.__file__).with_name("agent_api_server.py")).read_text()
check("the API takes the eval pin from eval turns only",
      '_turns[turn_id]["source"] == "eval" and body.get("eval_local_where") == "short"' in src, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
