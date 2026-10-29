#!/usr/bin/env python3
# gate: hermetic
"""#702's flow-control contract (for #700's M2 capture and M6 live ingest), on the reference Intake in front of any
pipeline (PERCEPTION_PIPELINE=module:factory, as test_perception_privacy.py): bounded RAM buffers, drop-oldest with a
counter, round-robin fairness, stale and future signals dropped, StreamStatus nominal/degraded/missing, status visible
only as the source's records are, and forget discarding queued signals at once. No models, no network, no disk.
Run: python3 scripts/test_perception_backpressure.py"""
from __future__ import annotations

import builtins
import importlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import perception as P  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


PEOPLE = {"alex": {"role": "admin"}, "robin": {"role": "adult"}, "ava": {"role": "kid", "parents": ["alex", "robin"]}}


def pipeline():
    spec = os.environ.get("PERCEPTION_PIPELINE")
    if not spec:
        return P.ReferencePipeline(PEOPLE)
    mod, fn = spec.split(":")
    return getattr(importlib.import_module(mod), fn)(PEOPLE)


T0 = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)


def setup(capacity=4, max_bytes=1000, total_bytes=10_000):
    pl = pipeline()
    it = P.Intake(pl, capacity=capacity, max_bytes=max_bytes, total_bytes=total_bytes, people=PEOPLE)
    watch = P.Source("w-ava", "own_device", person="ava")
    room = P.Source("r-living", "room_sensor", room="living room")
    phone = P.Source("p-robin", "own_device", person="robin")
    pl.opt_in(watch, "alex", "admin")
    pl.opt_in(room, "alex", "admin")
    pl.opt_in(phone, "robin", "adult")
    for s_ in (watch, room, phone):
        it.add(s_, expected_every=10 * S)
    return pl, it, watch, room, phone


def sig(src, n, at, size=10, modality="motion"):
    return P.Signal(src, modality, bytes([n % 256]) * size, at)


print("── bounded, drop-oldest, never blocking ──")
pl, it, watch, room, phone = setup()
for n in range(10):
    check(f"submit {n} never blocks or refuses for load", it.submit(sig(room, n, T0 + n * S), T0 + n * S), True) if n in (0, 9) \
        else it.submit(sig(room, n, T0 + n * S), T0 + n * S)
st = it.status("r-living", T0 + 10 * S)
check("the buffer holds at most `capacity` (4) and counts the 6 dropped", (st.buffered, st.dropped), (4, 6))
check("…and it's the oldest that went: the 4 newest wait", [s_.raw[0] for s_ in it._lanes["r-living"].buf], [6, 7, 8, 9])
pl, it, watch, room, phone = setup(capacity=100, max_bytes=50)
for n in range(8):
    it.submit(sig(room, n, T0 + n * S, size=10), T0 + n * S)
check("per-source bytes are bounded too (50 bytes: 5 signals of 10)", (it.status("r-living", T0 + 8 * S).buffered,
                                                                       it._lanes["r-living"].bytes), (5, 50))
check("one signal larger than the whole lane: dropped, counted, never stored",
      (it.submit(sig(room, 99, T0 + 9 * S, size=51), T0 + 9 * S), it._lanes["r-living"].bytes), (False, 50))
pl, it, watch, room, phone = setup(capacity=100, max_bytes=1000, total_bytes=100)
for n in range(9):
    it.submit(sig(room, n, T0 + n * S, size=10), T0 + n * S)
for n in range(3):
    it.submit(sig(phone, n, T0 + n * S, size=10), T0 + n * S)
check("all sources together stay under total_bytes (100)", it._total() <= 100, True)
check("…the fullest source gave way, not the quiet one", (it.status("p-robin", T0).buffered, it.status("r-living", T0).dropped > 0),
      (3, True))
writes = []
real_open = builtins.open
builtins.open = lambda f, mode="r", *a, **k: (writes.append(str(f)) if any(c in mode for c in "wax+") else None, real_open(f, mode, *a, **k))[1]
try:
    pl2, it2, w2, r2, p2 = setup()
    for n in range(50):
        it2.submit(sig(r2, n, T0 + n * S, size=100), T0 + n * S)
    it2.drain(T0 + 60 * S, budget=50)
finally:
    builtins.open = real_open
check("overflow and draining never write anything to disk (no spill)", writes, [])

print("── fair: a busy source never starves a quiet one ──")
pl, it, watch, room, phone = setup(capacity=100)
for n in range(40):
    it.submit(sig(room, n, T0 + n * S), T0 + n * S)
it.submit(sig(phone, 1, T0 + 5 * S), T0 + 5 * S)
recs = it.drain(T0 + 41 * S, budget=3)
check("round-robin: the phone's one signal is in the first turn, not after the room's 40",
      sorted({r.source_id for r in recs}), ["p-robin", "r-living"])

print("── stale and future signals ──")
pl, it, watch, room, phone = setup()
check("from the future (past the 30 s slack): refused, counted late",
      (it.submit(sig(room, 1, T0 + 120 * S), T0), it.status("r-living", T0).late), (False, 1))
check("older than any kind's retention: refused, counted late",
      it.submit(sig(room, 2, T0 - timedelta(days=40)), T0), False)
check("a little clock skew is fine", it.submit(sig(room, 3, T0 + 10 * S), T0), True)

print("── StreamStatus: nominal, degraded, missing ──")
pl, it, watch, room, phone = setup(capacity=100)
for n in range(5):
    it.submit(sig(phone, n, T0 + n * 10 * S), T0 + n * 10 * S)
check("arriving on schedule, nothing dropped: nominal", it.status("p-robin", T0 + 45 * S).state, P.NOMINAL)
check("nothing for over 3x the expected interval: missing", it.status("p-robin", T0 + 40 * S + 31 * S).state, P.MISSING)
check("a source that never sent: missing", it.status("w-ava", T0).state, P.MISSING)
pl, it, watch, room, phone = setup(capacity=2)
for n in range(5):
    it.submit(sig(room, n, T0 + n * S), T0 + n * S)
check("drops in the window: degraded", it.status("r-living", T0 + 5 * S).state, P.DEGRADED)
it.drain(T0 + 5 * S, budget=10)        # the backlog is worked off
it.window_reset()
it.submit(sig(room, 9, T0 + 6 * S), T0 + 6 * S)
check("a new window without drops: nominal again", it.status("r-living", T0 + 6 * S).state, P.NOMINAL)
before = len(pl.records())
it.drain(T0 + 7 * S, budget=10)
check("degraded means fewer records, never invented ones (at most one per signal that survived)",
      len(pl.records()) - before <= 1, True)

print("── status is private as the source's records are ──")
pl, it, watch, room, phone = setup()
for s_, n in ((watch, 1), (room, 2), (phone, 3)):
    it.submit(sig(s_, n, T0), T0)
vis = lambda who, role: sorted(st.source_id for st in it.visible_status(who, role, T0 + S))  # noqa: E731
check("Robin (Ava's parent) sees their own phone, the room, and their kid's watch",
      vis("robin", "adult"), ["p-robin", "r-living", "w-ava"])
check("Ava (the kid) sees her own watch and the room, never Robin's phone", vis("ava", "kid"), ["r-living", "w-ava"])
check("the shared screen sees the room only, never a person's device", vis("household", "household"), ["r-living"])
check("a stranger adult in the house (not a parent) never sees the kid's watch status",
      "w-ava" in [s_.source_id for s_ in P.Intake(pl, people=PEOPLE).visible_status("jordan", "adult", T0)], False)

print("── forget empties the queue at once ──")
pl, it, watch, room, phone = setup(capacity=100)
for n in range(10):
    it.submit(sig(watch, n, T0 + n * S, modality="heart_rate"), T0 + n * S)
it.forget("w-ava")
recs = it.drain(T0 + 20 * S, budget=50)
check("nothing queued from a forgotten source is processed afterwards", [r.source_id for r in recs if r.source_id == "w-ava"], [])
check("…and it no longer has a status or takes signals", (it.status("w-ava", T0), it.submit(sig(watch, 1, T0), T0)), (None, False))
try:
    it.add(P.Source("x", "room_sensor", room="den"), 10 * S)
    refused = False
except P.Refused:
    refused = True
check("a source joins the intake only after an adult opted it in", refused, True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
