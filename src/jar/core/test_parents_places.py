#!/usr/bin/env python3
"""jellybean#186/#308 (Zach, 10-02: places only; parents see kids' places): a person's last named place is theirs
and, for a kid, their parents'. Never another adult's, never on a household device, never coordinates.
# gate: hermetic
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["PLACES_DB"] = str(Path(tempfile.mkdtemp()) / "places.db")
import places as P  # noqa: E402

failures: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{'' if ok else f': {got!r} != {want!r}'}")
    if not ok:
        failures.append(name)


FOLKS = [{"id": "zach", "role": "admin", "parents": []}, {"id": "robin", "role": "adult", "parents": []},
         {"id": "jordan", "role": "adult", "parents": []},
         {"id": "ava", "role": "kid", "parents": ["zach", "robin"]},
         {"id": "leo", "role": "kid", "parents": ["zach", "robin"]}]
check("a parent sees themselves and their kids", P.viewable("robin", "adult", FOLKS), ["robin", "ava", "leo"])
check("the admin as a parent too", P.viewable("zach", "admin", FOLKS), ["zach", "ava", "leo"])
check("an adult who isn't a parent: only themselves", P.viewable("jordan", "adult", FOLKS), ["jordan"])
check("a kid: only themselves, never a sibling or parent", P.viewable("ava", "kid", FOLKS), ["ava"])
check("a household device: nobody", P.viewable(None, "household", FOLKS), [])
P.record("e1", "ava", "arrived", "Lincoln Elementary", "2026-10-02T15:12:00-07:00")
P.record("e2", "ava", "left", "Lincoln Elementary", "2026-10-02T15:40:00-07:00")
last = P.where_now("ava")
check("the latest event is what's shown", (last["event"], last["place"]), ("left", "Lincoln Elementary"))
check("no coordinates kept or shown", set(last) <= {"id", "event", "place", "at", "received_at"}, True)
check("someone with no events: nothing", P.where_now("leo"), None)
import household_identity as hh  # noqa: E402
check("the route is for people, not a household device", (hh.route_allowed("kid", "GET", "/v1/presence/places"),
      hh.route_allowed("household", "GET", "/v1/presence/places")), (True, False))

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed")
sys.exit(1 if failures else 0)
