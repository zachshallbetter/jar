#!/usr/bin/env python3
# gate: hermetic
"""A trip's pushes, rate-limited (jellybean#753 follow-up, trip_push.Throttle): only a change the Lock Screen would
show, at most one per token per 5 minutes; a held update goes out by itself when its gap is up; an end always goes at
once and drops what's held; a restart costs at most one extra push. And a kid's trip reaches only the kid's own
phone, never a parent's or a shared screen. APNs and the timer are fakes; no network.
Run: python3 scripts/test_trip_push_rate.py"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-trip-rate-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "PUSH_DB": "push.db", "CONNECTORS_LEDGER": "ledger.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import push  # noqa: E402
import trip_push as tp  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.160} expected={expected!r:.160}")


sent: list = []
timers: list = []


def rec(t, b):
    sent.append((t["device_id"], b["aps"]["event"], b["aps"]["content-state"], b["aps"]["timestamp"]))
    return 200, ""


def fresh():
    sent.clear()
    timers.clear()
    return tp.Throttle(schedule=lambda delay, fn: timers.append((delay, fn)))


T = {"device_id": "alex-phone", "token": "ab" * 32}
A = {"minutes": 24, "state": "moved", "leaveBy": 1000.0, "start": 2740.0}
B = {**A, "leaveBy": 1600.0, "start": 3340.0}
C = {**A, "leaveBy": 2200.0, "start": 3940.0}
T0 = time.time() - 10_000          # fake clock, in the past: a real timer's "now" is always past its due

print("── only a change the Lock Screen would show ──")
th = fresh()
check("the first update goes", th.deliver(T, A, "update", "r1", send=rec, now=T0), "sent")
check("the same state again: nothing", [th.deliver(T, A, "update", "r1", send=rec, now=T0 + d) for d in (10, 400, 900)],
      ["same", "same", "same"])
check("…one push in all", len(sent), 1)

print("── at most one per 5 minutes; a held update flushes by itself ──")
th = fresh()
th.deliver(T, A, "update", "r1", send=rec, now=T0)
check("a change a minute later is held", th.deliver(T, B, "update", "r1", send=rec, now=T0 + 60), "held")
check("…one timer, due at the 5-minute mark", [round(d) for d, _ in timers], [240])
check("…nothing before the mark", (th.flush_due(T0 + 299), len(sent)), (0, 1))
check("…at the mark the held update goes, with nothing else happening", (th.flush_due(T0 + 300), len(sent)), (1, 2))
check("…and it's the second state, stamped when it went", (sent[-1][2], sent[-1][3]), (B, int(T0 + 300)))
check("…the next change is held again until 5 minutes after that", th.deliver(T, C, "update", "r1", send=rec, now=T0 + 360), "held")

th = fresh()
th.deliver(T, A, "update", "r1", send=rec, now=T0)
th.deliver(T, B, "update", "r1", send=rec, now=T0 + 60)
th.deliver(T, C, "update", "r1", send=rec, now=T0 + 120)
check("two changes inside the gap: still one timer", len(timers), 1)
timers[0][1]()                     # the timer firing on its own (its own clock is real time, past the due)
check("the timer alone sends the NEWEST held state, once", [s[2] for s in sent], [A, C])
check("…and nothing is left held", (th.flush_due(T0 + 10_000), len(sent)), (0, 2))

th = fresh()
th.deliver(T, A, "update", "r1", send=rec, now=T0)
th.deliver(T, B, "update", "r1", send=rec, now=T0 + 60)
check("back to what's shown while held: the held one is dropped", th.deliver(T, A, "update", "r1", send=rec, now=T0 + 90), "same")
check("…so the mark sends nothing", (th.flush_due(T0 + 300), len(sent)), (0, 1))

print("── an end always goes at once and supersedes ──")
th = fresh()
th.deliver(T, A, "update", "r1", send=rec, now=T0)
th.deliver(T, B, "update", "r1", send=rec, now=T0 + 60)
check("an end inside the gap is sent at once", th.deliver(T, {**A, "state": "cancelled"}, "end", "r1", send=rec, now=T0 + 61), "sent")
check("…as an end", sent[-1][1], "end")
check("…and the held update never follows it", (th.flush_due(T0 + 300), [s[1] for s in sent]), (0, ["update", "end"]))
timers[0][1]()
check("…not even when its timer fires", [s[1] for s in sent], ["update", "end"])

print("── per token; a restart; a dead token ──")
th = fresh()
th.deliver(T, A, "update", "r1", send=rec, now=T0)
check("another phone isn't held by this one's gap",
      th.deliver({"device_id": "robin-phone", "token": "cd" * 32}, B, "update", "r1", send=rec, now=T0 + 5), "sent")
th2 = fresh()
check("a restart forgets: at most one extra push", th2.deliver(T, A, "update", "r1", send=rec, now=T0 + 10), "sent")
tp.MAX_TOKENS = 2
for i in range(4):
    th.deliver({"device_id": f"d{i}", "token": f"t{i}"}, A, "update", "r1", send=rec, now=T0 + 20 + i)
check("memory is bounded: past MAX_TOKENS the oldest go", sorted(k[0] for k in th.last), ["d2", "d3"])
tp.MAX_TOKENS = 1000
BUNDLE = "com.zachshallbetter.jellybean.ios"
devs0 = [{"id": "alex-phone", "person_id": "alex", "role": "adult", "revoked": False}]
push.register_activity("alex-phone", "alex", "adult", "trip", "c" + "1" * 32, "ab" * 32, "development", BUNDLE,
                       trip={"lead": 600, "minutes": 10})
th = fresh()
th.deliver(T, A, "update", "c" + "1" * 32, send=rec, now=T0)
th.deliver(T, B, "update", "c" + "1" * 32, send=lambda t, b: (410, "Unregistered"), now=T0 + 60)
th.flush_due(T0 + 300)
check("a held update to a dead token unregisters it under the activity's ref",
      push.trip_targets("alex", "c" + "1" * 32, devs0), [])

print("── a kid's trip: the kid's own phone only ──")
UID = "0F3C9A2E-7D41-4C8B-9E55-2B0D4A0E0753"
OLD, NEW = f"{UID}@2026-10-05T16:00:00Z", f"{UID}@2026-10-05T18:30:00Z"
REF = tp.ref_for(OLD)
devs = [{"id": "ava-phone", "person_id": "ava", "role": "kid", "revoked": False},
        {"id": "alex-phone", "person_id": "alex", "role": "adult", "revoked": False},
        {"id": "kitchen", "person_id": "household", "role": "household", "revoked": False}]
push.register_activity("ava-phone", "ava", "kid", "trip", REF, "aa" * 32, "development", BUNDLE, trip={"lead": 900, "minutes": 12})
push.register_activity("alex-phone", "alex", "adult", "trip", REF, "bb" * 32, "development", BUNDLE, trip={"lead": 900, "minutes": 12})
try:
    push.register_activity("kitchen", "household", "household", "trip", REF, "cc" * 32, "development", BUNDLE,
                           trip={"lead": 900, "minutes": 12})
    check("a shared screen can't hold a trip activity", "kept", "refused")
except ValueError:
    check("a shared screen can't hold a trip activity", "refused", "refused")
check("…and is never a trip target", [t["device_id"] for t in push.trip_targets("household", REF, devs)], [])
sent.clear()
tp.THROTTLE = tp.Throttle(schedule=lambda d, f: None)
check("the kid's own event moved", tp.judge("ava", OLD, live=lambda p: [NEW], devices=devs, send=rec), "moved")
check("…one update, to the kid's phone only (not the parent's, not the kitchen)", [s[0] for s in sent], ["ava-phone"])
NEW2 = f"{UID}@2026-10-05T19:00:00Z"
sent.clear()
tp.judge("alex", OLD, live=lambda p: [NEW2], devices=devs, send=rec)
check("the parent's own copy moving: the parent's phone only", [s[0] for s in sent], ["alex-phone"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
