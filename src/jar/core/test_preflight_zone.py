#!/usr/bin/env python3
# gate: hermetic
"""titan_preflight.sh warns when the host zone is not UTC (#1569). The check lives inside an ssh heredoc, so this pulls
its two functions out of the script text and runs them against a stub timedatectl on PATH; nothing touches titan.
Run: python3 scripts/test_preflight_zone.py"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
SRC = (HERE / "titan_preflight.sh").read_text()
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def grab(name):
    m = re.search(rf"^def {name}\(.*?(?=^\S)", SRC, re.S | re.M)
    return m.group(0) if m else ""


ns = {"subprocess": subprocess}
exec(grab("sh") + "\n" + grab("host_zone_note"), ns)
check("both functions are in the script", bool(ns.get("sh") and ns.get("host_zone_note")), True)

bindir = tempfile.mkdtemp(prefix="zone-test-")
stub = Path(bindir) / "timedatectl"
old = os.environ["PATH"]
os.environ["PATH"] = bindir + os.pathsep + old
try:
    for zone, warn in (("Etc/UTC", False), ("UTC", False), ("America/Los_Angeles", True), ("", True)):
        stub.write_text(f"#!/bin/sh\n[ \"$1 $2 $3\" = \"show -p Timezone\" ] && [ \"$4\" = \"--value\" ] && echo '{zone}'\n")
        stub.chmod(0o755)
        note = ns["host_zone_note"]()
        check(f"zone {zone!r} -> {'warning' if warn else 'quiet'}", bool(note), warn)
        if warn:
            check(f"zone {zone!r} warning names Etc/UTC", "Etc/UTC" in note, True)
    stub.write_text("#!/bin/sh\nexit 1\n")   # a timedatectl that fails (a real one may be on PATH, as on titan)
    check("failing timedatectl -> warning, not a crash", bool(ns["host_zone_note"]()), True)
finally:
    os.environ["PATH"] = old
check("the warning is counted", "warns += 1\n    print(_zn)" in SRC, True)
print(f"{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
