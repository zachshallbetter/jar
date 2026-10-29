#!/usr/bin/env python3
"""#423 "who's home": where_is answers by named place only, for the asker and, as a parent, their kids.
# gate: hermetic
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["PLACES_DB"] = str(Path(tempfile.mkdtemp()) / "places.db")
import agent_runtime_lib as L  # noqa: E402
import household_identity as hh  # noqa: E402
import places as P  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{'' if ok else ': ' + detail}")
    if not ok:
        failures.append(name)


FOLKS = [{"id": "zach", "name": "Zach", "role": "admin", "parents": []},
         {"id": "robin", "name": "Robin", "role": "adult", "parents": []},
         {"id": "ava", "name": "Ava", "role": "kid", "parents": ["zach", "robin"]}]


class Store:
    def people(self):
        return FOLKS


hh.get_store = lambda: Store()
P.record("1", "ava", "arrived", "home", "2026-10-02T15:40:00-07:00")
P.record("2", "robin", "arrived", "home", "2026-10-02T17:05:00-07:00")
L._viewer = lambda: ("zach", "admin")
out = L._where_is({})
check("a parent sees their kid", "Ava: arrived home" in out, out)
check("…never another adult", "Robin" not in out, out)
check("asking for another adult by name is refused plainly", "can't see where Robin" in L._where_is({"person": "Robin"}))
L._viewer = lambda: ("ava", "kid")
check("a kid sees only themselves", "Ava: arrived home" in L._where_is({}) and "Robin" not in L._where_is({}))
L._viewer = lambda: ("robin", "adult")
check("a parent asks for their kid by name", "Ava: arrived home" in L._where_is({"person": "ava"}))
check("never on a household device", not hh.role_allows_tool("household", "where_is"))
check("the tool is offered to people", hh.role_allows_tool("kid", "where_is") and hh.role_allows_tool("adult", "where_is"))

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed")
sys.exit(1 if failures else 0)
