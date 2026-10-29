#!/usr/bin/env python3
"""The perception ingestion interface and its privacy contract (titan-node#702; blocks #700's M2, M6 and M7).

No perception code exists yet. This file fixes the shape any pipeline must have (Source, Signal, Record and the
Pipeline protocol) and a ReferencePipeline that keeps #702's rules with no models and no capture. Every future
pipeline (room satellites, the watch, cameras) implements the same protocol and must pass
scripts/test_perception_privacy.py: `PERCEPTION_PIPELINE=module:factory python3 scripts/test_perception_privacy.py`.

The rules (#702), as the reference keeps them:
  raw        audio, video and frames are processed in RAM and never stored; only a derived label or latent is kept,
             with a retention limit per kind (RETENTION); nothing leaves titan (egress kind 0; no network at all)
  ownership  every record has an owner and an audience; a person's own device is private to them (a kid's: the kid
             and their parents); a room sensor is household-scoped and never says who
  presence   never on a shared screen; a person's whereabouts only to them (and a kid's to their parents)
  kids       no continuous audio or video of a kid's room without an adult's decision; kids can't opt a source in
  fences     health-class records (anything from a watch or health data) only to the person and a kid's parents
  opt-in     every source is off until an adult opts it in; forget(source) retracts every record from it
  safety     a camera stores nothing until Zach decides that separately (#413)
  untrusted  a derived label that reaches a prompt goes through the #473 markers (a phrase heard is data)

Flow control (#702, for #700's M2 capture and M6 live ingest), as the reference Intake keeps them:
  bounded    every source has a RAM ring buffer of at most `capacity` signals and `max_bytes` raw bytes, and all
             sources together at most `total_bytes`: memory never grows with load
  drop       on overflow the OLDEST waiting signal is dropped and counted (never the newest, never by blocking the
             device, never by spilling to disk: a dropped signal's raw bytes are gone)
  fair       drain() takes sources round-robin, one signal each per turn, so a busy source never starves a quiet one
  stale      a signal older than its kind's retention, or from the future, is dropped and counted (`late`)
  status     StreamStatus per source: nominal | degraded (drops or late in the window, or arrivals under half the
             expected rate) | missing (nothing for 3x the expected interval). Degraded means fewer records, never
             invented ones; the model's valid-mask path handles the gaps
  private    a source's status and counters are derived from it, so they carry that source's owner and audience
             (a person's device's status is theirs, a kid's also their parents'; a room sensor's the household's)
  forget     forget(source) also discards that source's waiting signals at once: nothing queued is processed after
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

DAY = timedelta(days=1)
# How long a derived record may live, by kind: presence is about now, health and activity summaries a little longer.
RETENTION = {"presence": DAY, "activity": 7 * DAY, "health": 30 * DAY, "sound": DAY, "motion": DAY}
CONTINUOUS = frozenset({"audio", "video", "frame"})        # what a kid's room never has without an adult's decision
HEALTH_MODALITIES = frozenset({"heart_rate", "sleep", "steps", "hrv", "blood_oxygen"})


@dataclass
class Source:
    id: str
    kind: str                         # own_device | room_sensor | camera
    person: str | None = None         # an own device's person
    room: str | None = None           # a room sensor's room
    opted_in: bool = False            # off until an adult opts it in
    kid_room: bool = False            # a room sensor in a kid's room
    adult_decision: bool = False      # an adult decided continuous audio/video may run in that kid's room
    safety_decision: bool = False     # Zach's separate decision on cameras (#413)


@dataclass
class Signal:
    source: Source
    modality: str                     # audio | video | frame | heart_rate | sleep | motion | …
    raw: bytes                        # in RAM only: never stored, never kept after ingest returns
    at: datetime


@dataclass
class Record:
    """What may be stored: a derived label or latent, never the signal."""
    kind: str                         # presence | activity | health | sound | motion
    label: str = ""                   # derived words ("someone is in the living room", "resting heart rate 61")
    latent: tuple[float, ...] = ()    # a derived vector, never the signal
    owner: str | None = None
    audience: str = "private"         # audience.py: private | household | adults | admins | a JSON list of ids
    subject: str | None = None        # the person it's about (own devices only; never for a room sensor)
    room: str | None = None
    classes: tuple[str, ...] = ()     # fence classes (health)
    source_id: str = ""
    expires_at: datetime | None = None


class Refused(Exception):
    pass


class Pipeline(Protocol):
    def opt_in(self, source: Source, by: str, role: str) -> None: ...
    def ingest(self, signal: Signal, now: datetime) -> list[Record]: ...
    def records(self) -> list[Record]: ...
    def visible(self, viewer: str, role: str, people: dict[str, dict[str, Any]]) -> list[Record]: ...
    def forget(self, source_id: str) -> int: ...
    def for_prompt(self, record: Record, nonce: str) -> str: ...


def _parents(person: str | None, people: dict[str, dict[str, Any]]) -> list[str]:
    return list((people.get(person or "") or {}).get("parents") or [])


class ReferencePipeline:
    """#702's rules with no models: it derives a label (and a small latent) from the signal's kind and keeps nothing
    else. The yardstick: a real pipeline adds models behind the same protocol and passes the same contract."""

    def __init__(self, people: dict[str, dict[str, Any]] | None = None):
        self.people = people or {}
        self._records: list[Record] = []
        self._sources: dict[str, Source] = {}

    def opt_in(self, source: Source, by: str, role: str) -> None:
        if role not in ("admin", "adult"):
            raise Refused("only an adult opts a source in")
        if source.kind == "own_device" and by not in (source.person,) + tuple(_parents(source.person, self.people)):
            raise Refused("a person's own device is opted in by them (or a kid's parent)")
        source.opted_in = True
        self._sources[source.id] = source

    def ingest(self, signal: Signal, now: datetime) -> list[Record]:
        s = signal.source
        if not s.opted_in:
            return []
        if s.kind == "camera" and not s.safety_decision:
            return []
        if s.kid_room and signal.modality in CONTINUOUS and not s.adult_decision:
            return []
        recs = self._derive(signal, now)
        self._records += recs
        return recs     # the signal (and its raw bytes) goes out of scope here: nothing keeps it

    def _derive(self, signal: Signal, now: datetime) -> list[Record]:
        s, m = signal.source, signal.modality
        # a derived latent: a few numbers summarising the signal, from which the signal can't be rebuilt
        h = hashlib.sha256(signal.raw).digest()
        latent = tuple(round(b / 255.0, 2) for b in h[:4])
        if s.kind == "own_device":
            kid_aud = [s.person] + _parents(s.person, self.people)
            aud = json.dumps(sorted(kid_aud)) if len(kid_aud) > 1 else "private"
            if m in HEALTH_MODALITIES:
                return [Record("health", f"{m} summary", latent, owner=s.person, audience=aud, subject=s.person,
                               classes=("health",), source_id=s.id, expires_at=now + RETENTION["health"])]
            return [Record("presence", "at a place", latent, owner=s.person, audience=aud, subject=s.person,
                           source_id=s.id, expires_at=now + RETENTION["presence"])]
        # a room sensor (or a decided camera): household-scoped, never who
        kind = "presence" if m in ("motion", "frame", "video") else "sound"
        label = f"someone is in the {s.room}" if kind == "presence" else f"sound in the {s.room}"
        return [Record(kind, label, latent, owner="household", audience="household", subject=None, room=s.room,
                       source_id=s.id, expires_at=now + RETENTION[kind])]

    def records(self) -> list[Record]:
        return list(self._records)

    def visible(self, viewer: str, role: str, people: dict[str, dict[str, Any]]) -> list[Record]:
        import audience
        out = []
        for r in self._records:
            if r.kind == "presence" and role == "household":
                continue                                            # presence never on a shared screen
            if "health" in r.classes and viewer not in ([r.subject] + _parents(r.subject, people)):
                continue                                            # the health fence: the person and a kid's parents
            if audience.visible(r.audience, viewer, role, owner=r.owner):
                out.append(r)
        return out

    def forget(self, source_id: str) -> int:
        before = len(self._records)
        self._records = [r for r in self._records if r.source_id != source_id]
        self._sources.pop(source_id, None)
        return before - len(self._records)

    def for_prompt(self, record: Record, nonce: str) -> str:
        import untrusted
        return untrusted.wrap(record.label, "perception", nonce, inline=True)


# ── flow control (#702 for #700's M2/M6) ───────────────────────────────────────────────────────────────────────────
from collections import deque  # noqa: E402

NOMINAL, DEGRADED, MISSING = "nominal", "degraded", "missing"
LATE_SLACK = timedelta(seconds=30)        # a device clock this far ahead is accepted; further is "from the future"


@dataclass
class StreamStatus:
    source_id: str
    state: str                            # nominal | degraded | missing
    buffered: int = 0
    dropped: int = 0                      # oldest signals dropped on overflow, in the window
    late: int = 0                         # stale or future signals dropped, in the window
    arrived: int = 0                      # signals accepted, in the window
    owner: str | None = None              # the source's own owner and audience: status is derived from it
    audience: str = "private"


@dataclass
class _Lane:
    source: Source
    expected_every: timedelta
    buf: deque = field(default_factory=deque)
    bytes: int = 0
    dropped: int = 0
    late: int = 0
    arrived: int = 0
    last_at: datetime | None = None


class Intake:
    """The reference flow control in front of any Pipeline: bounded per-source RAM ring buffers with drop-oldest,
    round-robin draining, stale/future drops, StreamStatus per source, and a forget that empties the buffer. The raw
    bytes live only in these buffers until drain() hands them to the pipeline (and are dropped, never stored)."""

    def __init__(self, pipeline: Pipeline, *, capacity: int = 64, max_bytes: int = 4 << 20,
                 total_bytes: int = 32 << 20, people: dict[str, dict[str, Any]] | None = None):
        self.pipeline, self.capacity, self.max_bytes, self.total_bytes = pipeline, capacity, max_bytes, total_bytes
        self.people = people or {}
        self._lanes: dict[str, _Lane] = {}
        self._turn: list[str] = []          # round-robin order

    def add(self, source: Source, expected_every: timedelta) -> None:
        """A source the pipeline already opted in, with the interval it's expected to send at."""
        if not source.opted_in:
            raise Refused("a source is added only after an adult opts it in")
        self._lanes[source.id] = _Lane(source, expected_every)
        if source.id not in self._turn:
            self._turn.append(source.id)

    def _total(self) -> int:
        return sum(l.bytes for l in self._lanes.values())

    def _drop_oldest(self, lane: _Lane) -> None:
        old = lane.buf.popleft()
        lane.bytes -= len(old.raw)
        lane.dropped += 1

    def submit(self, signal: Signal, now: datetime) -> bool:
        """Never blocks. False when the signal was refused (unknown source, stale or future); overflow drops the
        oldest waiting signal of that source (then of the fullest source, for the total cap), never this one."""
        lane = self._lanes.get(signal.source.id)
        if lane is None:
            return False
        kind_age = max(RETENTION.values())
        if signal.at > now + LATE_SLACK or signal.at < now - kind_age:
            lane.late += 1
            return False
        if len(signal.raw) > self.max_bytes:
            lane.dropped += 1                 # a single signal larger than the lane: dropped, never stored
            return False
        lane.buf.append(signal)
        lane.bytes += len(signal.raw)
        lane.arrived += 1
        lane.last_at = signal.at
        while len(lane.buf) > self.capacity or lane.bytes > self.max_bytes:
            self._drop_oldest(lane)
        while self._total() > self.total_bytes:
            fullest = max((l for l in self._lanes.values() if l.buf), key=lambda l: l.bytes)
            self._drop_oldest(fullest)
        return True

    def drain(self, now: datetime, budget: int) -> list[Record]:
        """Up to `budget` signals into the pipeline, one per source per turn (round-robin)."""
        out: list[Record] = []
        while budget > 0 and any(self._lanes[sid].buf for sid in self._turn if sid in self._lanes):
            for sid in list(self._turn):
                lane = self._lanes.get(sid)
                if not lane or not lane.buf or budget <= 0:
                    continue
                sig = lane.buf.popleft()
                lane.bytes -= len(sig.raw)
                out += self.pipeline.ingest(sig, now)
                budget -= 1
        return out

    def status(self, source_id: str, now: datetime) -> StreamStatus | None:
        lane = self._lanes.get(source_id)
        if lane is None:
            return None
        s = lane.source
        if s.kind == "own_device":
            aud_ids = [s.person] + _parents(s.person, self.people)
            owner, aud = s.person, (json.dumps(sorted(aud_ids)) if len(aud_ids) > 1 else "private")
        else:
            owner, aud = "household", "household"
        if lane.last_at is None or now - lane.last_at > 3 * lane.expected_every:
            state = MISSING
        elif lane.dropped or lane.late:
            state = DEGRADED
        else:
            state = NOMINAL
        return StreamStatus(source_id, state, len(lane.buf), lane.dropped, lane.late, lane.arrived, owner, aud)

    def window_reset(self) -> None:
        """A new status window: the counters start again (buffers and last arrival stay)."""
        for lane in self._lanes.values():
            lane.dropped = lane.late = lane.arrived = 0

    def visible_status(self, viewer: str, role: str, now: datetime) -> list[StreamStatus]:
        """The statuses this viewer may see: same rule as the records (a status says a device is sending)."""
        import audience
        out = []
        for sid in self._turn:
            st = self.status(sid, now)
            if st is None:
                continue
            if self._lanes[sid].source.kind == "own_device" and role == "household":
                continue                      # a person's device status never on a shared screen (presence)
            if audience.visible(st.audience, viewer, role, owner=st.owner):
                out.append(st)
        return out

    def forget(self, source_id: str) -> int:
        """The pipeline's forget, and this source's waiting signals discarded at once (never processed later)."""
        lane = self._lanes.pop(source_id, None)
        if lane:
            lane.buf.clear()
        if source_id in self._turn:
            self._turn.remove(source_id)
        return self.pipeline.forget(source_id)

