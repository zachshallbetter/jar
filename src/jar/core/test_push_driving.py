#!/usr/bin/env python3
# gate: hermetic
"""Driving now (jellybean#310): a phone that says it's in a car gets only urgent card pushes; the rest wait and go
when it says it stopped (or its driving expires), and a card dealt with meanwhile isn't pushed late. Against a fake
APNs and throwaway stores. Run: python3 scripts/test_push_driving.py"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-driving-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "TASK_LOG_DB": "task_log.db", "FENCES_DB": "fences.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import proactive  # noqa: E402
import push  # noqa: E402
import titan_alert  # noqa: E402

titan_alert.alert = lambda *a, **kw: []   # an urgent card's own alert (log, phone): never from a test

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


DEVICES = [{"id": "dev-a", "person_id": "p_alex", "role": "adult"},
           {"id": "dev-r", "person_id": "p_robin", "role": "adult"}]
for d, tok in (("dev-a", "a" * 64), ("dev-r", "b" * 64)):
    push.register(d, tok, "development", "com.example.jellybean")
sent: list[tuple[str, str]] = []


def fake(target, card):
    sent.append((target["device_id"], card["id"]))
    return 200, ""


def card(title, urgency, key=None):
    c = proactive.add_card(title, "made up", urgency=urgency, to="household", key=key)
    return c


print("── driving ──")
push.set_driving("dev-a", True, send=fake)
check("driving now", push.is_driving("dev-a"), True)
library = card("Library books due Friday", 2)
out = push.push_card(library, devices=DEVICES, send=fake)
check("an important card: held for the driver, sent to the other phone",
      (sorted(out), sent), (["dev-a: held", "dev-r: 200"], [("dev-r", library["id"])]))
sent.clear()
leak = card("Water leak under the sink", 3)
push.push_card(leak, devices=DEVICES, send=fake)
check("an urgent card: through, driving or not", sorted(d for d, _ in sent), ["dev-a", "dev-r"])
sent.clear()
leave = card("Time to leave for the dentist", 2, key="leave-dentist")
push.push_card(leave, devices=DEVICES, send=fake)
check("a leave-now card: through", sorted(d for d, _ in sent), ["dev-a", "dev-r"])
sent.clear()
gone = card("Pick up the dry cleaning", 2)
push.push_card(gone, devices=DEVICES, send=fake)
proactive.ack_card(gone["id"])
sent.clear()

print("── stopped ──")
out = push.set_driving("dev-a", False, send=fake)
check("the held ones still open go now, to the driver only",
      sent, [("dev-a", library["id"])])
check("reported", out, [f"{library['id']}: 200"])
check("not driving", push.is_driving("dev-a"), False)
sent.clear()
check("nothing held twice", push.set_driving("dev-a", False, send=fake), [])

print("── a phone that never says it stopped ──")
push.set_driving("dev-a", True, now=time.time() - push.DRIVING_TTL_S - 1, send=fake)
check("its driving expires: pushes come through again", push.is_driving("dev-a"), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
