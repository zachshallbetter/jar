#!/usr/bin/env python3
# gate: hermetic
"""The nightly's world privacy probe (world_eval.private_entities) asks only about genuinely private things: every
claim private to the admin AND a source no one else may read. 10-04..06 it asked about a fact from a household note
(a household-audience note whose fact sits in its saver's bubble as a private claim), and counted the household
reading the household's own note as a leak. Fictional entities; a fake world; no network.
Run: python3 scripts/test_world_eval_privacy.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-world-eval-privacy-"))
for k, v in {"WORLD_DIR": "world", "WORLD_DB": "world.db", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import world_eval as WE  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r} expected={expected!r}"))


class Row(dict):
    def keys(self):
        return super().keys()


def ev(value, owner="zach", aud="private", ref=""):
    return Row(value=value, attr="schedule", owner=owner, audience=aud, ref=ref)


class FakeWorld:
    ENT = {"zach:harbor-gas": [ev("$84.12", ref="mail:cx-mail-1#harbor-gas:amount")],
           "zach:ava-school-mornings": [ev("weekdays by 9:10 AM", ref="note:note-ava-school#ava-school-mornings:schedule")],
           "zach:lakeside-clinic": [ev("Tue 3 PM", ref="note:note-private-clinic#lakeside-clinic:schedule")],
           "zach:said-in-chat": [ev("Fridays", ref="")],
           "household:garbage": [ev("Tuesday", aud="household")]}

    def entities(self):
        return [(e, {"name": e.split(":")[1].replace("-", " ").title()}) for e in self.ENT]

    def evidence_of(self, eid):
        return self.ENT[eid]


KB = {"note-ava-school": "household", "note-private-clinic": "private"}
got = [e for e, _, _ in WE.private_entities(FakeWorld(), "zach", source_audience=KB.get)]
check("a fact from a household note is no privacy probe (the household may read the note)",
      "zach:ava-school-mornings" in got, False)
check("a fact from a private note still is", "zach:lakeside-clinic" in got, True)
check("private mail and things said in chat still are", ("zach:harbor-gas" in got, "zach:said-in-chat" in got), (True, True))
check("household facts never were", "household:garbage" in got, False)
check("a note that's gone or unreadable stays a probe (fail closed)",
      "zach:lakeside-clinic" in [e for e, _, _ in WE.private_entities(FakeWorld(), "zach", source_audience=lambda s: None)],
      True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
