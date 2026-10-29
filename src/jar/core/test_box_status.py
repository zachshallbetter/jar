#!/usr/bin/env python3
# gate: hermetic
"""A box's status light (#407 item 5): green / amber / red and one sentence from device health's own findings, a
confirmed problem only; red for a titan service down or a stray listener, amber for the rest and for anything
unknown, stale or not yet checked; the sentence (a card title, which can name a device or person) for the admin
only, colour alone for anyone else and a shared screen. device_health keeps each finding's title in its state.
Throwaway state; fictional devices. Run: python3 scripts/test_box_status.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-box-status-"))
os.environ["DEVICE_HEALTH_STATE"] = str(_TMP / "dh.json")
sys.path.insert(0, str(Path(__file__).parent))
import box_status as B  # noqa: E402
import device_health as dh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = 1_800_000_000.0


def row(problem=True, runs=2, since=NOW - 3600, title=""):
    return {"problem": problem, "since": since, "runs": runs, "carded": problem, "title": title}


print("── the colour ──")
check("no state yet: amber, said", (B.light(None, None, NOW)["light"], B.light(None, None, NOW)["sentence"]),
      ("amber", "Health checks haven't run yet."))
check("checks not run for over an hour: amber", B.light({}, NOW - 2 * 3600, NOW)["light"], "amber")
check("every check passed: green", B.light({"health-alex-disk-m1": row(problem=False)}, NOW - 60, NOW)["light"], "green")
check("a problem seen once (not confirmed, as cards wait): still green",
      B.light({"health-alex-disk-m1": row(runs=1)}, NOW - 60, NOW)["light"], "green")
amber = B.light({"health-alex-disk-m1": row(title="Robin's MacBook has 4.0 GB free")}, NOW - 60, NOW)
check("a disk low: amber, with its card title as the sentence", (amber["light"], amber["sentence"]),
      ("amber", "Robin's MacBook has 4.0 GB free"))
red = B.light({"health-household-service-retrieval": row(title="The knowledge base runtime is down", since=NOW - 60),
               "health-alex-disk-m1": row(title="disk", since=NOW - 9000),
               "health-alex-source-mail-m1": row(title="mail", since=NOW - 8000)}, NOW - 60, NOW)
check("a titan service down outranks the rest: red, its title, and how many more",
      (red["light"], red["sentence"], red["problems"]), ("red", "The knowledge base runtime is down (and 2 more)", 3))
check("a stray listener on the box is red (security)", B.colour_of("health-alex-listeners"), "red")
check("a finding this file doesn't know is amber, never green", B.colour_of("health-alex-something-new"), "amber")

print("── who sees what ──")
state = {"health-alex-device-d1": row(title="Robin's iPhone hasn't checked in for 3 days")}
Path(os.environ["DEVICE_HEALTH_STATE"]).write_text(json.dumps(state))
admin = B.status("admin")
check("the admin: the colour and the sentence", (admin["light"], "Robin" in admin["sentence"]), ("amber", True))
for role in ("adult", "kid", "household"):
    got = B.status(role)
    check(f"{role}: the colour and when, never the sentence (it names a device or a person)",
          (sorted(got), "Robin" in json.dumps(got), "iPhone" in json.dumps(got)), (["checked_at", "light"], False, False))
os.remove(os.environ["DEVICE_HEALTH_STATE"])
check("no state file: amber for anyone, no raise", B.status("kid")["light"], "amber")

print("── device health keeps the title ──")
st: dict = {}
dh.reconcile([dh.Finding("health-alex-disk-m1", ["p_alex"], True, "MacBook has 4.0 GB free", "")], st, NOW, False,
             lambda *a, **k: None)
check("reconcile stores each finding's title for the light", st["health-alex-disk-m1"].get("title"), "MacBook has 4.0 GB free")

print("── the route ──")
import household_identity as hh  # noqa: E402
for role in ("admin", "adult", "kid", "household"):
    ok = any(m == "GET" and rx.fullmatch("/v1/box/status") and (role in roles or role == "admin")
             for m, rx, roles in hh.ROUTE_ROLES)
    check(f"{role} may call GET /v1/box/status", ok, True)
src = (Path(__file__).parent / "agent_api_server.py").read_text()
check("the route answers through box_status.status with the caller's role",
      "box_status.status, ident.role" in src, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
