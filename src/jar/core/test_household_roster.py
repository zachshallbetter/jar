#!/usr/bin/env python3
"""titan-node#423: every turn knows who lives here. "Who lives in our household?" names no one, so the world model
added no people and both brains answered from memory without Jordan (10-02 nightly). The roster is names and
adult/kid only. Hermetic: the people store is faked.
# gate: hermetic
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


HOUSE = [{"id": "zach", "name": "Zach Hale", "role": "admin"}, {"id": "p1", "name": "Robin", "role": "adult"},
         {"id": "p2", "name": "Leo", "role": "kid"}, {"id": "p3", "name": "Ava", "role": "kid"},
         {"id": "p4", "name": "Jordan", "role": "adult"}]
lib._household_people = lambda: HOUSE
r = lib.household_roster()
check("every member, adults then kids, first names", r == "The household: Zach, Robin and Jordan (adults); Leo and Ava (kids).", r)
check("nothing but names and adult/kid", "admin" not in r and "Hale" not in r and "p4" not in r, r)
lib._household_people = lambda: HOUSE[:1]
check("one person", lib.household_roster() == "The household: Zach (adult).", lib.household_roster())
lib._household_people = lambda: []
check("no store: nothing said", lib.household_roster() == "")

src = (Path(__file__).resolve().parent / "agent_runtime_lib.py").read_text()
ctx = src[src.index("def _situational_context_inner"):]
check("the turn's context carries it", "household_roster()" in ctx[:ctx.index("\ndef ", 10)])
check("the tool-protocol example names no household chore (the real garbage night is Wednesday)",
      "trash goes out" not in src.lower())

check("#378: the turn log counts world-model entities, carried from the prompt build",
      '"world_entities", "prefix"' in src and 'info_w["world_entities"] = len(_ids or [])' in src
      and '_TURN_INFO.info["world_entities"] = world_n' in src)

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed")
sys.exit(1 if failures else 0)
