#!/usr/bin/env python3
"""One conversation's turns as the developer inspector shows them (jellybean#1267, part 1), read from the turn log.

The turn log (agent_runtime_lib._log_turn, /ai/logs/agent-turns.jsonl) holds one content-free line per turn. A line
names its asker (`person`) and, from #1267, the app's conversation id (`thread`, the same id GET /v1/conversations
lists). This module picks one person's lines for one conversation and shapes each into a trace: what routed it,
which brain answered, the tools it ran in order (name, ok or failed and the logged failure head, how long, on which
kind of device), the device's confirmation waits, the post-answer checks and retries, what left the house, and
where the time went.

  for_conversation(path, person_id, cid, limit)  → (turns, truncated): newest `limit`, oldest first
  shape(rec)                                     → one turn's trace

Privacy: only lines whose `person` is the asker are read, so a viewer sees their own turns and nobody else's. The
log never holds a message, a tool's arguments or its result; this keeps only names, counts, durations and statuses
(a turn's raw error text is not passed on: `failed` says only that it failed). A temporary chat's line holds no
conversation id (count-only, jellybean#708), so it is never in a trace. Lines from before #1267 have no `thread`
and aren't in one either.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterator

LIMIT_DEFAULT = 50
LIMIT_MAX = 200
SCAN_BYTES = 64 * 1024 * 1024   # the newest 64 MB of the log; an older turn isn't traced
_LABEL_RX = re.compile(r"^[\w .:/+\-]{1,60}$")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _label(v: Any) -> str | None:
    """A short, plain label from the log (a route reason, a brain, a source), else None."""
    return v if isinstance(v, str) and _LABEL_RX.match(v) else None


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _counts(v: Any) -> dict[str, int] | None:
    """{label: count} (the post-answer tallies), labels and integers only."""
    if not isinstance(v, dict):
        return None
    out = {k: int(n) for k, n in v.items() if _label(k) and _int(n) is not None}
    return out or None


def _pairs(v: Any) -> list[list[Any]]:
    return [x for x in v if isinstance(x, list) and x and isinstance(x[0], str)] if isinstance(v, list) else []


def _by_name(entries: list[list[Any]]) -> dict[str, list[list[Any]]]:
    out: dict[str, list[list[Any]]] = {}
    for e in entries:
        out.setdefault(e[0], []).append(e)
    return out


def _tools(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """The turn's tool calls in call order. tool_times and tool_fail_at (#1267) are index-aligned with tool_names; a
    line from before them matches failures, device times and devices to calls by name, in order."""
    names = [n for n in (rec.get("tool_names") or []) if isinstance(n, str)]
    times = rec.get("tool_times") if isinstance(rec.get("tool_times"), list) else []
    fails = _pairs(rec.get("tool_fails"))
    fail_at = rec.get("tool_fail_at") if isinstance(rec.get("tool_fail_at"), list) else None
    failed: dict[int, str] = {}
    if fail_at is not None and len(fail_at) == len(fails):
        for i, f in zip(fail_at, fails):
            if isinstance(i, int) and 0 <= i < len(names):
                failed[i] = str(f[1]) if len(f) > 1 else ""
        unplaced = [f for i, f in zip(fail_at, fails) if not (isinstance(i, int) and 0 <= i < len(names))]
    else:
        unplaced = fails
    queues = {"fail": _by_name(unplaced), "time": _by_name(_pairs(rec.get("device_times"))),
              "dev": _by_name(_pairs(rec.get("tool_devices")))}
    out = []
    for i, name in enumerate(names):
        step: dict[str, Any] = {"name": name, "ok": True}
        if i not in failed and queues["fail"].get(name):
            f = queues["fail"][name].pop(0)
            failed[i] = str(f[1]) if len(f) > 1 else ""
        if i in failed:
            step["ok"], step["reason"] = False, failed[i][:80]
        if i < len(times) and _int(times[i]) is not None:
            step["ms"] = _int(times[i])
        if queues["time"].get(name):   # a device tool's own round trip, and how long its question was up
            d = queues["time"][name].pop(0)
            step["device_ms"] = _int(d[1]) if len(d) > 1 else None
            if len(d) > 2 and _int(d[2]) is not None:
                step["asked_ms"] = _int(d[2])
        if queues["dev"].get(name):
            d = queues["dev"][name].pop(0)
            if len(d) > 1 and _label(d[1]):
                step["device"] = d[1]
        out.append(step)
    return out


def _waits(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The device's confirmation waits: a call whose question was up (asked_ms > 0), or that timed out or was
    declined, with how it ended."""
    out = []
    for i, t in enumerate(tools):
        reason = (t.get("reason") or "").lower()
        ended = ("timed_out" if "timed out" in reason else "declined" if "declined" in reason
                 else "not_completed" if "not completed" in reason else "answered")
        if t.get("asked_ms") or ended != "answered":
            if ended == "not_completed" and "device_ms" not in t:
                continue   # a failure that never reached a device isn't a wait
            out.append({"step": i, "tool": t["name"], "ended": ended, "ms": t.get("device_ms"),
                        "asked_ms": t.get("asked_ms")})
    return out


_MS = (("total", "ms"), ("build", "build_ms"), ("recall", "recall_ms"), ("skill", "skill_ms"),
       ("toolprompt", "toolprompt_ms"), ("context", "context_ms"), ("model", "model_ms"), ("prefill", "prefill_ms"),
       ("tools", "tool_ms"), ("device", "device_ms"), ("confirm", "confirm_ms"), ("asked", "asked_ms"),
       ("self_check", "self_check_ms"))
_RETRIES = ("source_retry", "source_retry_fixed", "claim_retry", "claim_retry_fixed", "claim_fix_retry",
            "claim_fix_fixed", "lookup_retry", "action_retry", "degenerate_retries", "required_read_retry",
            "required_act_retry", "no_access_retry", "mac_out_retry", "prose_tool_retry")


def _jev(v: Any) -> dict[str, Any] | None:
    if not isinstance(v, dict):
        return None
    out: dict[str, Any] = {}
    if _label(v.get("choice")):
        out["choice"] = v["choice"]
    for k in ("sure", "needs_tools"):
        if isinstance(v.get(k), (int, float)) and not isinstance(v.get(k), bool):
            out[k] = v[k]
    if _int(v.get("ms")) is not None:
        out["ms"] = _int(v["ms"])
    if v.get("pending") is True:
        out["pending"] = True
    return out or None


def shape(rec: dict[str, Any]) -> dict[str, Any]:
    """One turn log line → its trace: labels, counts, durations and statuses only."""
    ts = float(rec.get("ts") or 0)
    tools = _tools(rec)
    fallback = rec.get("fallback")
    return {
        "turn_id": _label(rec.get("turn_id")),
        "ts": ts,
        "at": _iso(ts),
        "source": _label(rec.get("source")),
        "client": _label(rec.get("client")),
        "route": _label(rec.get("route")),
        "jev": _jev(rec.get("jev")),
        "brain": _label(rec.get("brain")),
        "mode": _label(rec.get("mode")),
        "pin": _label(rec.get("pin")),
        "fallback": (_label(fallback) or True) if fallback else None,
        "effort": _label(rec.get("effort")),
        "effort_why": _label(rec.get("effort_why")),
        "failed": bool(rec.get("error")),
        "budget_hit": bool(rec.get("budget_hit")),
        "empty_reply": bool(rec.get("empty_reply")),
        "tools": tools,
        "device_calls": _int(rec.get("device_calls")) or 0,
        "device_waits": _waits(tools),
        "checks": {
            "self_check": _label(rec.get("self_check")),
            "postanswer": _counts(rec.get("postanswer")),
            "postanswer_skipped": _counts(rec.get("postanswer_skipped")),
            "retries": {k: _int(rec[k]) for k in _RETRIES if _int(rec.get(k))},
        },
        "egress": [k for k in (rec.get("egress") or []) if _label(k)],
        "ms": {name: _int(rec.get(key)) for name, key in _MS if _int(rec.get(key)) is not None},
    }


def _lines(path: str) -> Iterator[str]:
    """The log's lines, oldest first, from at most its newest SCAN_BYTES."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    with open(path, "rb") as fh:
        if size > SCAN_BYTES:
            fh.seek(size - SCAN_BYTES)
            fh.readline()   # a partial line
        for raw in fh:
            yield raw.decode("utf-8", "replace")


def for_conversation(path: str, person_id: str, cid: str,
                     limit: int = LIMIT_DEFAULT) -> tuple[list[dict[str, Any]], bool]:
    """`person_id`'s turns in conversation `cid`, newest `limit`, oldest first; and whether older ones were left out.
    Nobody else's line is ever read into the result, whatever its thread."""
    if not person_id or not cid:
        return [], False
    limit = max(1, min(int(limit), LIMIT_MAX))
    needle = json.dumps(cid)   # the cheap pre-check: the line names this id somewhere
    got: list[dict[str, Any]] = []
    for line in _lines(path):
        if needle not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("person") == person_id and rec.get("thread") == cid:
            got.append(rec)
    got.sort(key=lambda r: float(r.get("ts") or 0))
    return [shape(r) for r in got[-limit:]], len(got) > limit
