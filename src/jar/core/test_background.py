#!/usr/bin/env python3
"""jellybean#329 Phase 5: GET /v1/proactive's `background` lists what titan is doing now, per viewer, labels only.
# gate: hermetic
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SCHEDULED_DB", str(Path(tempfile.mkdtemp()) / "scheduled.db"))
import agent_api_server as S  # noqa: E402

failures: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{'' if ok else f': {got!r} != {want!r}'}")
    if not ok:
        failures.append(name)


S._active_units = lambda: {"titan-reflection-dream.service", "titan-reflection-letter.service"}
labels = lambda person, role: [b["label"] for b in S.background_for(person, role)]  # noqa: E731
check("idle household device: nothing", labels(None, "household"), [])
check("the admin sees titan's own jobs", labels("zach", "admin"), ["Reflecting on yesterday", "Writing the evening letter"])
check("an adult sees the household's, not the admin's letter", labels("robin", "adult"), ["Reflecting on yesterday"])
check("a kid sees none of titan's jobs", labels("ava", "kid"), [])
S._background_note("task-t1", "Renew the car tabs: taking a step", "robin")
check("a running step shows to its owner", labels("robin", "adult")[0], "Renew the car tabs: taking a step")
check("…and to no one else", "Renew the car tabs: taking a step" in labels("zach", "admin"), False)
S._background_note("task-t1", None)
check("cleared when the step ends", any("car tabs" in x for x in labels("robin", "adult")), False)
src = (Path(__file__).resolve().parent / "agent_api_server.py").read_text()
check("both card responses carry it", src.count('"background":') >= 3, True)

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed")
sys.exit(1 if failures else 0)
