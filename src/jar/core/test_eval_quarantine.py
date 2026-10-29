#!/usr/bin/env python3
# gate: hermetic
"""The eval quarantine list (#1517): an unexpired case is known, not new, but still chronic; expired ones are reported. Fictional cases, temp file via EVAL_QUARANTINE. Run: python3 scripts/test_eval_quarantine.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))

import time  # noqa: E402

import household_eval_nightly as N  # noqa: E402

NOW = time.mktime(time.strptime("2030-06-15 12:00", "%Y-%m-%d %H:%M")) - time.timezone
with tempfile.TemporaryDirectory() as d:
    q = Path(d) / "q.json"
    q.write_text(json.dumps({"pie-bake": {"owner": "kb", "until": "2030-06-20", "why": "oven fake"},
                             "lamp-on": {"owner": "box", "until": "2030-06-01", "why": "old"},
                             "kite-fly": {"owner": "ios", "until": "2030-06-15", "why": "last day counts"}}))
    os.environ["EVAL_QUARANTINE"] = str(q)
    active, expired = N.quarantine_state([], now=NOW)
    check("unexpired (including the last day) are active", sorted(active), ["kite-fly", "pie-bake"])
    check("expired are reported with owner and date", expired, ["lamp-on (box, until 2030-06-01)"])
    new, _c = N.new_and_chronic([{"real": []}], ["pie-bake", "gnome-sit"])
    check("without quarantine the case is new", new, ["gnome-sit", "pie-bake"])
    check("with quarantine it is not new", [n for n in new if n not in active], ["gnome-sit"])
    _n, chronic = N.new_and_chronic([{"real": ["pie-bake"]}] * 2, ["pie-bake"])
    check("…but still listed as chronic", chronic, ["pie-bake"])
    q.write_text("not json")
    check("a broken file quarantines nothing", N.quarantine_state([], now=NOW), (set(), []))
    q.unlink()
    check("a missing file quarantines nothing", N.quarantine_state([], now=NOW), (set(), []))

os.environ.pop("EVAL_QUARANTINE")
shipped = json.loads((Path(N.__file__).parent / "eval_quarantine.json").read_text())
check("the shipped list is valid; each entry has owner, until, why", all({"owner", "until", "why"} <= set(v) for v in shipped.values()), True)
src = Path(N.__file__).read_text()
check("main() filters new by quarantine and reports the expired", ("n not in quarantined" in src, "quarantine-expired" in src), (True, True))
check("the history line keeps the check text per case", '"reasons":' in src, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
