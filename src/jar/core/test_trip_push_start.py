#!/usr/bin/env python3
# gate: hermetic   (temp push.db; a fake APNs sender, nothing leaves the process)
"""jellybean#753 push-to-start: a trip's Live Activity starts while the app is closed. The phone sends each upcoming
trip's plan (POST v1/devices/self/trips {ref, leave_by, lead, minutes, mode}) and its TripActivityAttributes push-to-start
token (kind "trip-start"). An hour before leave-by, titan starts the trip on that device if it shows none yet, once.
Option A (10-07): no title and no place, ever. A moved event's plan follows it; a cancelled one never starts; a plan
goes once its event has started. Owner-only, never a shared household device.
Run: python3 scripts/test_trip_push_start.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-tps-test-"))
os.environ["PUSH_DB"] = str(_TMP / "push.db")
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))

import push  # noqa: E402
import trip_push as tp  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


BUNDLE = "com.example.jellybean"
DEVS = [{"id": "alex-phone", "person_id": "alex", "role": "adult", "revoked": False},
        {"id": "robin-phone", "person_id": "robin", "role": "adult", "revoked": False},
        {"id": "kitchen", "person_id": "household", "role": "household", "revoked": False}]
UID = "0F3C9A2E-7D41-4C8B-9E55-2B0D4A0E0001"
OLD = f"{UID}@2026-10-05T16:00:00Z"
NEW = f"{UID}@2026-10-05T18:30:00Z"
REF = tp.ref_for(OLD)
START = 1_791_216_000.0              # 2026-10-05 16:00 UTC
LEAD = 1740.0
LEAVE = START - LEAD
sent: list = []


def rec(target, body):
    sent.append((target["device_id"], body))
    return 200, ""


def plan(device="alex-phone", person="alex", role="adult", **kw):
    body = {"ref": REF, "leave_by": LEAVE, "lead": LEAD, "minutes": 24, "mode": "transit", **kw}
    return push.register_trip_plan(device, person, role, body, now=LEAVE - 6 * 3600)


print("── the plan: times only, checked ──")
plan()
for label, kw in (("a ref that isn't one", {"ref": "../x"}), ("leave_by far ahead", {"leave_by": LEAVE + 30 * 86400}),
                  ("a mode that isn't one", {"mode": "teleport"}), ("minutes not whole", {"minutes": 2.5}),
                  ("a title sneaking in is ignored, not stored", None)):
    if kw is None:
        continue
    try:
        plan(**kw)
        check(f"refused: {label}", "kept", "refused")
    except ValueError:
        check(f"refused: {label}", "refused", "refused")
try:
    plan(device="kitchen", person="household", role="household")
    check("a shared household screen has none", "kept", "refused")
except ValueError:
    check("a shared household screen has none", "refused", "refused")
con = push._db()
cols = [r[1] for r in con.execute("PRAGMA table_info(trip_plans)")]
con.close()
check("what's stored: times, minutes and the mode only", cols,
      ["device_id", "person", "ref", "leave_by", "lead", "minutes", "mode", "updated"])

print("── no start before the hour, nor without a trip-start token ──")
check("too early", push.send_trip_starts(LEAVE - 2 * 3600, devices=DEVS, send=rec), [])
check("within the hour but no trip-start token", push.send_trip_starts(LEAVE - 1800, devices=DEVS, send=rec), [])
push.register_activity("alex-phone", "alex", "adult", "trip-start", None, "ab" * 32, "development", BUNDLE)
push.register_activity("robin-phone", "robin", "adult", "trip-start", None, "cd" * 32, "development", BUNDLE)

print("── the start: once, on the planning device only, no title or place ──")
out = push.send_trip_starts(LEAVE - 1800, devices=DEVS, send=rec)
check("one start, to Alex's phone", [d for d, _ in sent], ["alex-phone"])
aps = sent[0][1]["aps"]
check("a liveactivity start of TripActivityAttributes that hands the app its update token",
      (aps["event"], aps["attributes-type"], aps["input-push-token"]), ("start", "TripActivityAttributes", 1))
check("attributes: the ref, an empty title and place, the event's start",
      aps["attributes"], {"eventID": REF, "eventTitle": "", "place": "", "eventStart": START - push.REFERENCE_EPOCH, "pushRef": REF})
check("its first state: leave-by, minutes and the mode's symbol",
      aps["content-state"], {"leaveBy": LEAVE - push.REFERENCE_EPOCH, "minutes": 24, "symbol": "tram.fill"})
check("a fixed alert", aps["alert"], push.TRIP_START_ALERT)
wire = json.dumps(sent)
check("never a title, place or UID on the wire", [w in wire for w in ("Dentist", "Pearl", UID)], [False] * 3)
sent.clear()
check("not twice", push.send_trip_starts(LEAVE - 1500, devices=DEVS, send=rec), [])

print("── a device already showing the trip gets no start ──")
push._forget_starts(REF)
push.register_activity("alex-phone", "alex", "adult", "trip", REF, "ef" * 32, "development", BUNDLE,
                       trip={"lead": LEAD, "minutes": 24})
check("the app started it itself: nothing", push.send_trip_starts(LEAVE - 1200, devices=DEVS, send=rec), [])
push.unregister_activity("alex-phone", "trip", REF)

print("── someone else's device, or a device that changed hands, never ──")
push.register_trip_plan("robin-phone", "alex", "adult", {"ref": "c" + "1" * 32, "leave_by": LEAVE, "lead": LEAD, "minutes": 9,
                                                       "mode": "car"}, now=LEAVE - 6 * 3600)
sent.clear()
push.send_trip_starts(LEAVE - 1100, devices=DEVS, send=rec)
check("a plan on Robin's phone under Alex's name starts nothing", [d for d, _ in sent if d == "robin-phone"], [])

print("── moved: the plan follows the event; cancelled: it never starts ──")
push._forget_starts(REF)
check("moved (no live token: the plan alone)",
      tp.judge("alex", OLD, live=lambda p: [NEW] if NEW.startswith(p) else [], devices=DEVS, send=rec), "plan moved")
con = push._db()
row = con.execute("SELECT ref, leave_by FROM trip_plans WHERE device_id='alex-phone'").fetchall()
con.close()
NEW_START = START + 2.5 * 3600
check("its ref and leave-by follow the new start", row, [(tp.ref_for(NEW), NEW_START - LEAD)])
sent.clear()
check("nothing starts at the old time", push.send_trip_starts(LEAVE - 1800, devices=DEVS, send=rec), [])
check("…it starts an hour before the new leave-by",
      (push.send_trip_starts(NEW_START - LEAD - 1800, devices=DEVS, send=rec) != [], sent[0][1]["aps"]["attributes"]["pushRef"]),
      (True, tp.ref_for(NEW)))
check("cancelled", tp.judge("alex", NEW, live=lambda p: [], devices=DEVS, send=rec), "plan dropped")
con = push._db()
check("its plan is gone", con.execute("SELECT COUNT(*) FROM trip_plans WHERE person='alex' AND ref=?", (tp.ref_for(NEW),)).fetchone()[0], 0)
con.close()

print("── plans go once the event starts, and with the device ──")
plan()
push.send_trip_starts(START + 1, devices=DEVS, send=rec)
con = push._db()
check("past its start: gone", con.execute("SELECT COUNT(*) FROM trip_plans WHERE ref=?", (REF,)).fetchone()[0], 0)
con.close()
plan()
push.forget_activities("alex-phone")
con = push._db()
check("a revoked device's plans go", con.execute("SELECT COUNT(*) FROM trip_plans WHERE device_id='alex-phone'").fetchone()[0], 0)
con.close()
plan()
check("DELETE withdraws it", (push.remove_trip_plan("alex-phone", REF), push.remove_trip_plan("alex-phone", REF)), (True, False))

print("── a dead trip-start token goes ──")
plan()
push.register_activity("alex-phone", "alex", "adult", "trip-start", None, "ab" * 32, "development", BUNDLE)
push._forget_starts(REF)
push.send_trip_starts(LEAVE - 1800, devices=DEVS, send=lambda t, b: (410, "Unregistered"))
con = push._db()
check("410: its trip-start token is forgotten",
      con.execute("SELECT COUNT(*) FROM activity_tokens WHERE device_id='alex-phone' AND kind='trip-start'").fetchone()[0], 0)
con.close()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
