#!/usr/bin/env python3
"""Coding agents on the Mac, seen and stopped from the iPhone (jellybean#699; the contract is on that issue).

The Mac's run_agent (jellybean#209) reports each run here (POST /v1/agent-runs): the repo's name, its state and
counts of what changed. Never the prompt, a branch, a path, a file name or a diff. The person's iPhone reads them
(GET /v1/agent-runs), shows one as a Live Activity that titan moves by push (kind "agent"), and can stop it
(POST /v1/agent-runs/{id}/stop, routed to the Mac that reported it as the device tool agent_stop; the worktree
stays, discarding is the Mac's).

Retention: none. Runs live in this process's memory only, each dropped 1 h after it ends, and all of them on a
restart. Nothing is written to disk or logged with the repo's name.

agent_stop is never one of the model's tools: the Mac runs it without asking (the person confirmed on the phone),
and it can't tell a chat's call from this route's.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

STATES = ("running", "done", "failed", "stopped", "timed_out")
ENDED = frozenset(STATES) - {"running"}
UNKNOWN = "unknown"                 # running, but no report for SILENT_AFTER_S (the phone: "Not heard from your Mac")
SILENT_AFTER_S = 10 * 60
KEEP_AFTER_END_S = 60 * 60
MAX_RUNS = 50                       # per person; the oldest ended ones go first

FIELDS = ("id", "repo", "state", "started_at", "updated_at", "files_changed", "lines_added", "lines_removed",
          "ends_by")
NEVER_REPOS = frozenset({"ase"})    # whatever the Mac's allow-list says (it must show no sign of AI authorship)
_ID_RX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_REPO_RX = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
MAX_COUNT = 10_000_000

REFERENCE_EPOCH = 978307200         # a Swift Codable Date in content-state: seconds since 2001-01-01 UTC
ATTRIBUTES_TYPE = "AgentRunActivityAttributes"
START_TITLE = "Claude on your Mac"


class ReportError(ValueError):
    pass


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RX.fullmatch(value))


def clean_report(body: Any) -> dict[str, Any]:
    """The Mac's report, exactly the contract's fields; anything else is refused rather than dropped, so a field
    that shouldn't travel (a prompt, a path) is noticed on the Mac's side."""
    if not isinstance(body, dict):
        raise ReportError("expected a JSON object")
    extra = sorted(set(body) - set(FIELDS))
    if extra:
        raise ReportError("unknown fields: " + ", ".join(extra)[:200])
    rid = str(body.get("id") or "").strip().lower()
    if not valid_id(rid):
        raise ReportError("id must be the run's UUID")
    repo = str(body.get("repo") or "").strip()
    if not _REPO_RX.fullmatch(repo) or repo in (".", ".."):
        raise ReportError("repo must be the repository's folder name")
    if repo.lower() in NEVER_REPOS:
        raise ReportError("not this repository")
    state = str(body.get("state") or "").strip()
    if state not in STATES:
        raise ReportError("state must be one of: " + ", ".join(STATES))

    def when(key: str, required: bool) -> float | None:
        v = body.get(key)
        if v is None and not required:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 1_500_000_000 <= float(v) <= 4_000_000_000:
            raise ReportError(f"{key} must be Unix seconds")
        return float(v)

    def count(key: str) -> int:
        v = body.get(key, 0)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_COUNT:
            raise ReportError(f"{key} must be a whole number")
        return v

    return {"id": rid, "repo": repo, "state": state, "started_at": when("started_at", True),
            "updated_at": when("updated_at", True), "files_changed": count("files_changed"),
            "lines_added": count("lines_added"), "lines_removed": count("lines_removed"),
            "ends_by": when("ends_by", False)}


def content_state(run: dict[str, Any]) -> dict[str, Any]:
    """AgentRunActivityAttributes.ContentState, as Swift's JSONDecoder reads it. Counts, times and the state only."""
    st: dict[str, Any] = {"state": run["state"], "startedAt": run["started_at"] - REFERENCE_EPOCH,
                          "updatedAt": run["updated_at"] - REFERENCE_EPOCH, "filesChanged": run["files_changed"],
                          "linesAdded": run["lines_added"], "linesRemoved": run["lines_removed"]}
    if run.get("ends_by"):
        st["endsBy"] = run["ends_by"] - REFERENCE_EPOCH
    return st


def start_attributes(run: dict[str, Any]) -> dict[str, Any]:
    """Content-free (10-07, #237; the trips rule, #753 option A): Apple carries the run id only. The app fills in the
    repo and the Mac's name itself when it runs; until then the Lock Screen says "Claude on your Mac"."""
    return {"runID": run["id"], "repo": "", "mac": ""}


def start_alert(run: dict[str, Any]) -> dict[str, str]:
    """Fixed words (10-07, #237): no repo or Mac name in what Apple carries; the app shows the details itself."""
    return {"title": START_TITLE, "body": "Claude started on your Mac"}


def public(run: dict[str, Any], now: float) -> dict[str, Any]:
    """A run as GET /v1/agent-runs gives it: the report, the Mac's name, and `unknown` for a silent running one."""
    out = {k: run[k] for k in FIELDS}
    out["mac"] = run.get("mac") or "your Mac"
    if run["state"] == "running" and now - run["received_at"] > SILENT_AFTER_S:
        out["state"] = UNKNOWN
    return out


class Runs:
    """In memory only (see the module's docstring). Thread-safe: routes run in worker threads too."""

    def __init__(self, now=time.time) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}

    def _prune(self) -> None:
        now = self._now()
        for rid in [r for r, v in self._runs.items()
                    if v.get("ended_at") is not None and now - v["ended_at"] > KEEP_AFTER_END_S]:
            del self._runs[rid]

    def report(self, person: str, device_key: str | None, mac_name: str | None,
               report: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        """Keep a cleaned report. Returns (event, run) for the Live Activity: "start" (the first report of a running
        run), "update" (a count or the state changed), "end" (it ended), or None (nothing to tell: a heartbeat, or a
        report for a run that already ended). A run id another person reported is refused (ReportError)."""
        person = str(person or "").lower()
        with self._lock:
            self._prune()
            now = self._now()
            prev = self._runs.get(report["id"])
            if prev is not None and prev["person"] != person:
                raise ReportError("not your run")
            if prev is not None and prev["state"] in ENDED:
                return None   # over is over: a late heartbeat never brings it back
            run = {**report, "person": person, "device_key": device_key or (prev or {}).get("device_key"),
                   "mac": (mac_name or (prev or {}).get("mac") or "").strip()[:60] or None, "received_at": now,
                   "ended_at": now if report["state"] in ENDED else None}
            self._runs[report["id"]] = run
            mine = sorted((v for v in self._runs.values() if v["person"] == person),
                          key=lambda v: (v["state"] == "running", v["updated_at"]))
            for old in mine[:max(0, len(mine) - MAX_RUNS)]:
                self._runs.pop(old["id"], None)
            if prev is None:
                return ("start" if run["state"] == "running" else "end"), dict(run)
            if run["state"] in ENDED:
                return "end", dict(run)
            changed = any(run[k] != prev[k] for k in ("files_changed", "lines_added", "lines_removed", "ends_by"))
            return ("update", dict(run)) if changed else None

    def get(self, rid: str) -> dict[str, Any] | None:
        with self._lock:
            self._prune()
            r = self._runs.get(str(rid or "").lower())
            return dict(r) if r else None

    def for_person(self, person: str) -> list[dict[str, Any]]:
        """Newest first."""
        person = str(person or "").lower()
        with self._lock:
            self._prune()
            now = self._now()
            mine = [public(v, now) for v in self._runs.values() if v["person"] == person]
        return sorted(mine, key=lambda r: r["started_at"], reverse=True)

    def mark_stopped(self, rid: str) -> dict[str, Any] | None:
        """The Mac said it stopped (the route's answer; its own report follows)."""
        with self._lock:
            r = self._runs.get(rid)
            if r is None or r["state"] in ENDED:
                return None
            now = self._now()
            r.update(state="stopped", updated_at=max(r["updated_at"], now), ended_at=now, received_at=now)
            return dict(r)

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()


RUNS = Runs()

# The Mac's answer to agent_stop (one JSON line, the contract's §4) → the route's status.
STOP_TIMEOUT_S = 45.0   # waking (≤ 25 s) and the stop itself (TERM, KILL after 5 s, a last count): never under 15 s


def stop_outcome(text: str) -> tuple[int, dict[str, Any]]:
    try:
        doc = json.loads(str(text or "").strip().splitlines()[0]) if str(text or "").strip() else None
    except (json.JSONDecodeError, IndexError):
        doc = None
    if not isinstance(doc, dict):
        return 502, {"error": "Your Mac answered in a way titan doesn't understand."}
    if doc.get("error") == "unknown":
        return 404, {"error": "Your Mac doesn't know that run."}
    state = doc.get("state")
    if state == "stopped":
        return 200, {"state": "stopped"}
    if state in ENDED:
        return 409, {"state": state}
    return 502, {"error": "Your Mac answered in a way titan doesn't understand."}
