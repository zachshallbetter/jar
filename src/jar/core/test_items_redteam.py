#!/usr/bin/env python3
# gate: hermetic   (the red team seeds a fictional household into throwaway stores in child processes)
"""The items surface's nightly red team (items_redteam.py): it finds nothing on today's code, and it does find a leak
when one is put in, both a plain one (every to-do list shown to everyone: canaries) and a derived one (a hidden count in
Now's sentence, no hidden string: noninterference). The nightly runs it, pages on a leak or on no result, and the
scorecard reads its line. Fictional names only.
Run: python3 scripts/test_items_redteam.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


def run(inject: str = "", log: Path | None = None) -> tuple[int, str]:
    env = {**os.environ, "ITEMS_REDTEAM_INJECT": inject}
    p = subprocess.run([sys.executable, str(HERE / "items_redteam.py"), "--source", "test"] + (["--log", str(log)] if log else []),
                       capture_output=True, text=True, env=env, timeout=900)
    return p.returncode, p.stdout


print("── today's code: nothing leaks ──")
log = Path(tempfile.mkdtemp(prefix="titan-items-rt-")) / "rt.jsonl"
rc, out = run(log=log)
check("every view × viewer, canaries and noninterference: 0 leaks", (rc, out.strip().splitlines()[-1]), (0, "items privacy leaks: 0/88"))
if out.strip().splitlines()[-1] != "items privacy leaks: 0/88":
    # a leak on today's code: show each one (viewer · view · check: detail), so a failure explains itself (10-09: a
    # deploy rolled back on "1/88" with nothing else to go on). Fixture data, fictional names only; never live data.
    for ln in out.splitlines():
        if " · " in ln:
            print("    leak:" + ln.rstrip())
line = json.loads(log.read_text().splitlines()[-1])
check("…and its own line (0600) with counts", (line["leaks"], line["views"], oct(log.stat().st_mode & 0o777)), (0, 88, "0o600"))

print("── a child sees only its own stores (10-09: tasks, grants, surfaces and more came from the shared TEST_STATE) ──")
import items_redteam as _R  # noqa: E402
_saved = dict(os.environ)
try:
    os.environ["TASKS_DB"] = "/ai/datasets/connectors/tasks.db"        # a live-looking path
    os.environ.pop("GRANTS_DB", None)                                   # unset: would default to a live path
    os.environ["SOME_NEW_CACHE"] = "/ai/datasets/somewhere.json"        # a store-like name the gate doesn't list yet
    _t = Path(tempfile.mkdtemp(prefix="titan-items-rt-iso-"))
    _R._isolate(_t, {"HOUSEHOLD_DB": "household.db"})
    stores = _R._gate_store_names() | {k for k in os.environ if _R._STORE_SUFFIX.search(k)}
    outside = sorted(k for k in stores if not os.environ.get(k, "").startswith(str(_t)))
    check("every gate store and store-like setting points into the child's own directory",
          (len(_R._gate_store_names()) > 50, outside), (True, []))
    check("…a live path and an unset store included", (os.environ["TASKS_DB"].startswith(str(_t)),
                                                       os.environ.get("GRANTS_DB", "").startswith(str(_t)),
                                                       os.environ["SOME_NEW_CACHE"].startswith(str(_t))), (True, True, True))
finally:
    os.environ.clear()
    os.environ.update(_saved)

print("── it catches a leak put in on purpose ──")
rc, out = run("todos")
last = out.strip().splitlines()[-1]
check("every to-do list to everyone: leaks found", last.startswith("items privacy leaks: ") and last != "items privacy leaks: 0/88", True)
check("…by canary, on the screen's views", "screen · now · canary" in out, True)
rc, out = run("count")
check("a hidden count in Now's sentence (no hidden string): found by noninterference, on Now only",
      ("noninterference: " in out and "sentence/facts/waiting_on_you" in out,
       all(" · now · " in l for l in out.splitlines() if " · " in l)), (True, True))
check("…and never by a canary (there's nothing to string-match)", "canary" in out, False)

rc, out = run("cards")
check("every card to everyone: caught on the cards list, by canary", "· /v1/proactive · canary" in out, True)

print("── the nightly and the scorecard ──")
import household_eval_nightly as N  # noqa: E402
check("a leak or no result pages like world privacy", {"items-privacy", "items-redteam"} <= set(N.PRIVACY_KEYS), True)
sent, alerts = None, []


class Edge:
    def update(self, rest):
        return False


sent = N.notify({"items-privacy": "the items surface leaked"}, rate=1.0, floor=0.85, summary="x",
                alert=lambda *a, **k: alerts.append((a, k)), edge=Edge())
check("…urgent, on its own", (sent, alerts[0][1].get("priority")), (["privacy"], "urgent"))
src = (HERE / "household_eval_nightly.py").read_text()
check("the nightly runs it with its own log and parses its line",
      ('"items_redteam.py"' in src, "/ai/logs/items-redteam.jsonl" in src, "items privacy leaks:" in src), (True, True, True))
import scorecard  # noqa: E402
sc = (HERE / "scorecard.py").read_text()
check("the scorecard reads the nightly line and makes a leak a miss",
      ('"items-redteam.jsonl"' in sc, "nightly items red team:" in sc), (True, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
