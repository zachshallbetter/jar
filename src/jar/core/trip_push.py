#!/usr/bin/env python3
"""A trip's Live Activity stays right with the app closed (jellybean#753; the contract is on that issue).

The phone's trip activity registers its push token with ref = "c" + sha256(source_id)[:32], where source_id is the
event's key as the Mac syncs calendar items: "<iCalendar UID>@<occurrence start, ISO 8601 UTC>". When a calendar batch
from that same person's device removes that item, titan says where it went:
  - moved: the person has an item with the same UID, on titan now, starting within 7 days of the old start → an update
    with the new leave-by (new start − the lead the phone registered), and the token follows the event to its new ref;
  - cancelled: no such item → the activity ends with state "cancelled" (iOS's default dismissal: it says so a while).
A removal waits GRACE_S before it's judged, so a move the Mac sends as two batches (the new item after the deletion)
isn't called a cancel.

How often: a push goes only when what the Lock Screen shows changes (leaveBy, minutes, start or state differ from the
last one sent to that device's token), and at most once per MIN_GAP_S (5 min) per token. A change inside the gap is
held, the newest replacing an older one, and a timer sends it when the gap is up even if nothing else happens, so the
Lock Screen is never left stale. An `end` always goes at once and drops anything held for that token. The last-sent
state is kept in memory only (dropped on an end; the oldest go past MAX_TOKENS): a restart forgets it, which costs at
most one extra push.

What's pushed: leaveBy, minutes, start and state. Never the event's title or place (owner rule, 10-04). Only the token
owner's own synced items are ever matched (another person's calendar never drives someone's trip push). Nothing is
stored here: the lead and minutes live with the token in push.db, and a pending check lives in memory.
"""
from __future__ import annotations

import hashlib
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

GRACE_S = 120.0
MIN_GAP_S = 300.0             # one push per token per 5 minutes at most (an end excepted)
MAX_TOKENS = 1000             # last-sent states kept in memory
MOVE_WINDOW_S = 7 * 86400
REFERENCE_EPOCH = 978307200   # a Swift Codable Date in content-state: seconds since 2001-01-01 UTC
INCLUDE_START = True          # owner, 10-04: the new start time may travel (a time, not a title or place)


def ref_for(source_id: str) -> str:
    return "c" + hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:32]


def split(source_id: str) -> tuple[str, datetime] | None:
    """"<uid>@<ISO start>" → (uid, start); None when it isn't one of those (a UID may itself contain "@")."""
    uid, sep, when = str(source_id or "").rpartition("@")
    if not sep or not uid:
        return None
    try:
        start = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        return None
    return uid, (start if start.tzinfo else start.replace(tzinfo=timezone.utc))


def replacement(old_sid: str, live: list[str]) -> tuple[str, datetime] | None:
    """Where an event went: the same UID's item on titan now that starts nearest the old start, within 7 days."""
    old = split(old_sid)
    if old is None:
        return None
    best: tuple[float, str, datetime] | None = None
    for sid in live:
        if sid == old_sid:
            continue
        got = split(sid)
        if got is None or got[0] != old[0]:
            continue
        gap = abs((got[1] - old[1]).total_seconds())
        if gap <= MOVE_WINDOW_S and (best is None or gap < best[0]):
            best = (gap, sid, got[1])
    return (best[1], best[2]) if best else None


def state(lead: float, minutes: int, start: datetime | None, what: str) -> dict[str, Any]:
    """TripActivityAttributes.ContentState as titan sends it: leaveBy, minutes, start and state only."""
    st: dict[str, Any] = {"minutes": int(minutes), "state": what}
    if start is not None:
        st["leaveBy"] = start.timestamp() - float(lead) - REFERENCE_EPOCH
        if INCLUDE_START:
            st["start"] = start.timestamp() - REFERENCE_EPOCH
    return st


def judge(owner: str, removed: str, *, live: Callable[[str], list[str]], devices: list[dict[str, Any]],
          send: Callable[[dict[str, Any], dict[str, Any]], tuple[int, str]] | None = None, db=None,
          now: float | None = None) -> str:
    """One removed item of `owner`'s: moved, cancelled, or nothing (no trip activity on it). `live(prefix)` is the
    owner's own items on titan now. Returns what was done."""
    import push
    ref = ref_for(removed)
    targets = push.trip_targets(owner, ref, devices, db)
    planned = push.trip_planned(owner, ref, db)
    if not targets and not planned:
        return "none"
    parts = split(removed)
    went = replacement(removed, live(parts[0] + "@")) if parts else None
    # A trip planned to start while the app is closed (push-to-start) follows its event, or doesn't start at all.
    if planned:
        if went:
            push.trip_plan_moved(owner, ref, ref_for(went[0]), went[1].timestamp(), db)
        else:
            push.trip_plan_gone(owner, ref, db)
    if not targets:
        return "plan moved" if went else "plan dropped"
    for t in targets:
        if went:
            THROTTLE.deliver(t, state(t["lead"], t["minutes"], went[1], "moved"), "update", ref_for(went[0]),
                             send=send, db=db, now=now)
        else:
            old_start = parts[1] if parts else None
            THROTTLE.deliver(t, state(t["lead"], t["minutes"], old_start, "cancelled"), "end", ref,
                             send=send, db=db, now=now)
    if went:
        push.rekey_trip(owner, ref, ref_for(went[0]), db)
        return "moved"
    push.forget_activities(kind="trip", ref=ref, db=db)
    push._forget_starts(ref, db)
    return "cancelled"


def _send(target: dict[str, Any], st: dict[str, Any], event: str, ref: str, send, db, now: float) -> None:
    """One push to one token; a token APNs says is gone is unregistered under the activity's current ref."""
    import push
    body = (push.activity_payload(st, "end", now=now, dismiss_after=None) if event == "end"
            else push.activity_payload(st, event, now=now))
    try:
        status, reason = (send or push._curl_activity)(target, body)
    except Exception as exc:  # noqa: BLE001 - one phone failing never stops the rest
        status, reason = 0, type(exc).__name__
    if status == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic", "ExpiredToken"):
        push.unregister_activity(target["device_id"], "trip", ref, db)


class Throttle:
    """Per token (device id, APNs token): the last state sent and when, and at most one held update."""

    def __init__(self, gap: float = MIN_GAP_S, schedule: Callable[[float, Callable[[], None]], Any] | None = None):
        self.gap = gap
        self.schedule = schedule or self._timer
        self.lock = threading.Lock()
        self.last: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self.held: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any], str, Any, Any]] = {}
        self.armed: set[tuple[str, str]] = set()

    @staticmethod
    def _timer(delay: float, fn: Callable[[], None]) -> threading.Timer:
        t = threading.Timer(max(0.0, delay), fn)
        t.daemon = True
        t.start()
        return t

    def deliver(self, target: dict[str, Any], st: dict[str, Any], event: str, ref: str, *, send=None, db=None,
                now: float | None = None) -> str:
        """sent | same (nothing changed on the Lock Screen) | held (inside the gap; flushed when it's up)."""
        now = time.time() if now is None else now
        key = (str(target["device_id"]), str(target["token"]))
        with self.lock:
            if len(self.last) >= MAX_TOKENS and key not in self.last:   # bounded: the oldest not holding anything go
                for k, _ in sorted(((k, at) for k, (at, _) in self.last.items() if k not in self.held),
                                   key=lambda x: x[1])[:len(self.last) - MAX_TOKENS + 1]:
                    del self.last[k]
            if event == "end":
                self.held.pop(key, None)
                self.last.pop(key, None)
                go = True
            else:
                prev = self.last.get(key)
                if prev and prev[1] == st:
                    self.held.pop(key, None)                       # the newest is what's already shown
                    return "same"
                go = prev is None or now - prev[0] >= self.gap
                if go:
                    self.held.pop(key, None)
                    self.last[key] = (now, st)
                else:
                    self.held[key] = (target, st, ref, send, db)
                    if key not in self.armed:
                        self.armed.add(key)
                        due = prev[0] + self.gap
                        self.schedule(due - now, lambda: self.flush_due(max(time.time(), due)))
                    return "held"
        _send(target, st, event, ref, send, db, now)
        return "sent"

    def flush_due(self, now: float | None = None) -> int:
        """Send every held update whose gap is up. Returns how many went."""
        now = time.time() if now is None else now
        out = []
        with self.lock:
            for key, (target, st, ref, send, db) in list(self.held.items()):
                at = self.last.get(key, (0.0, None))[0]
                if at + self.gap <= now:
                    del self.held[key]
                    self.armed.discard(key)
                    self.last[key] = (now, st)
                    out.append((target, st, ref, send, db))
            for key in [k for k in self.armed if k not in self.held]:
                self.armed.discard(key)                            # an end dropped it: its timer has nothing to do
        for target, st, ref, send, db in out:
            try:
                _send(target, st, "update", ref, send, db, now)
            except Exception as exc:  # noqa: BLE001
                print(f"[trip-push] held update failed: {type(exc).__name__}", file=sys.stderr)
        return len(out)


THROTTLE = Throttle()

_timers: list[threading.Timer] = []


def after_calendar_batch(owner: str, items: list[dict[str, Any]], *, grace: float | None = None,
                         run: Callable[[str, str], str] | None = None) -> int:
    """The calendar ingest's hook, after the batch is stored: each removed item is judged GRACE_S later. Never raises
    (a trip push must never hurt ingest); returns how many were scheduled."""
    try:
        removed = sorted({str(i.get("source_id") or "") for i in items
                          if isinstance(i, dict) and i.get("deleted") and split(str(i.get("source_id") or ""))})
        if not removed:
            return 0
        go = run or _judge_live
        for sid in removed:
            t = threading.Timer(GRACE_S if grace is None else grace, _safe, args=(go, owner, sid))
            t.daemon = True
            t.start()
            _timers.append(t)
        del _timers[:-200]
        return len(removed)
    except Exception as exc:  # noqa: BLE001
        print(f"[trip-push] not scheduled: {type(exc).__name__}", file=sys.stderr)
        return 0


def _safe(go: Callable[[str, str], str], owner: str, sid: str) -> None:
    try:
        go(owner, sid)
    except Exception as exc:  # noqa: BLE001
        print(f"[trip-push] failed: {type(exc).__name__}", file=sys.stderr)


def _judge_live(owner: str, sid: str) -> str:
    import connectors_runner as cx
    import push
    if not push.configured():
        return "unconfigured"
    out = judge(owner, sid, live=lambda prefix: cx.live_upstream_ids(owner, "calendar", prefix),
                devices=push._household_devices())
    if out != "none":
        print(f"[trip-push] {out}", file=sys.stderr)   # what happened only: never the event, its id or the person
    return out
