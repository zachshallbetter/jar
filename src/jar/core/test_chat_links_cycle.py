#!/usr/bin/env python3
# gate: hermetic
"""The chat links cycle (#1125): skipped inside the nightly and model-eval windows and under a deploy hold; otherwise
links (never asking a model), then filing; a dry run writes nothing and asks filing only for what it would file; every
run logs counts, no titles. Fakes for chat_links and file_suggest; a temporary log and hold.
Run: python3 scripts/test_chat_links_cycle.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-clcycle-"))
os.environ["CHAT_LINKS_CYCLE_LOG"] = str(_TMP / "cycle.jsonl")
os.environ["TITAN_DEPLOY_HOLD"] = str(_TMP / "deploy.hold")
os.environ["LINKS_DB"] = str(_TMP / "links.db")
os.environ["LINK_SALT_FILE"] = str(_TMP / "link_salt")
sys.path.insert(0, str(Path(__file__).parent))
import chat_links_cycle as C  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def at(hm):
    return datetime(2026, 10, 7, int(hm[:2]), int(hm[3:]), tzinfo=timezone.utc).timestamp()


CALLS = []
links = lambda apply: (CALLS.append(("links", apply)), {"edges": {"continues/accepted": 3}, "conversations": 9,  # noqa: E731
                                                        "hook_kinds": {"id:order": 2},
                                                        **({"auto_filed": {"people": 2, "filed": 1}} if apply else {})})[1]
filing = lambda apply: (CALLS.append(("filing", apply)), {"people": 2, "would_file": 1})[1]  # noqa: E731

for hm in ("07:30", "08:40"):   # 00:30 and 01:40 PDT: the household and the model eval (10-14 move)
    check(f"{hm}Z: skipped (a protected window), nothing run", (C.cycle(True, now=at(hm), links=links, filing=filing)
                                                                .get("skipped", "")[:6], CALLS), ("inside", []))
Path(os.environ["TITAN_DEPLOY_HOLD"]).write_text(f"{int(at('14:00')) + 600} model-eval\n")
check("under a deploy hold: skipped", C.cycle(True, now=at("14:00"), links=links, filing=filing).get("skipped"),
      "a deploy hold is set")
check("…a past hold doesn't block", "skipped" in C.cycle(False, now=at("14:20"), links=links, filing=filing), False)
CALLS.clear()
dry = C.cycle(False, now=at("16:05"), links=links, filing=filing)
check("a dry run: links counted, filing only asked what it would file", (CALLS, dry["filing"]),
      ([("links", False), ("filing", False)], {"people": 2, "would_file": 1}))
CALLS.clear()
got = C.cycle(True, now=at("16:05"), links=links, filing=filing)
check("applied: links once (they file inside), filing never called again", (CALLS, got["filing"]),
      ([("links", True)], {"people": 2, "filed": 1}))
lines = [json.loads(x) for x in Path(os.environ["CHAT_LINKS_CYCLE_LOG"]).read_text().splitlines()]
check("every run logged: counts only (no hook kinds, no titles)", (len(lines), "hook_kinds" in json.dumps(lines)), (6, False))
import inspect  # noqa: E402
check("the real links step never asks a model (ask_locally False)", "chat_links.run(14, apply, False)" in inspect.getsource(C.cycle),
      True)
print("── never the judge's labels ──")
import hooks  # noqa: E402
import linker  # noqa: E402
con = hooks.connect()
linker.say(con, "chat-a", "chat-b", False, "judge:local", "judge")
linker.say(con, "chat-c", "chat-d", True, "p_alex", "baseline")
linker.say(con, "chat-e", "chat-f", False, "p_alex", "correction")
check("statements: only a person's own word by default (baseline, corrections)", sorted(linker.statements(con).items()),
      [(("chat-c", "chat-d"), True), (("chat-e", "chat-f"), False)])
check("…the judge's only when asked for", ("chat-a", "chat-b") in linker.statements(con, include_judge=True), True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
