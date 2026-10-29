#!/usr/bin/env python3
"""jellybean#329 MessageText: the reply's steps line says what the turn did, in code, from the tools that ran.
# gate: hermetic
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as L  # noqa: E402

failures: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{'' if ok else f': {got!r} != {want!r}'}")
    if not ok:
        failures.append(name)


check("no tools: no line", L.turn_trace([]), None)
check("one tool", L.turn_trace(["list_calendar_events"]), "Checked your calendar")
check("each phrase once, in order", L.turn_trace(["kb_retrieve", "kb_retrieve", "list_calendar_events"]),
      "Searched your notes · Checked your calendar")
check("unknown tools are steps, never their names", L.turn_trace(["get_weather", "x_internal_tool"]),
      "Checked the weather and 1 more step")
check("only unknown tools", L.turn_trace(["a", "b"]), "Took 2 steps")
long = L.turn_trace(["web_search", "fetch_url", "generate_image", "get_weather", "travel_time", "home_status"])
check("never past 80 characters", len(long) <= 80, True)
check("…and says how many it left out", long.endswith("more steps"), True)
src = (Path(__file__).resolve().parent / "agent_api_server.py").read_text()
check("the done event and /v1/prompt carry it", '"trace": turn.get("trace")' in src and 'done["trace"]' in src, True)

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed")
sys.exit(1 if failures else 0)
