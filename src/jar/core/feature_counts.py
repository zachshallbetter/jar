#!/usr/bin/env python3
"""Content-free counts for things that happen outside a turn, so a feature's live outcome can be checked (10-09: an
Ask-about 404 answers before any turn, and a kid's refused original is refused before any audit, so neither showed
anywhere). One JSON line per event: {ts, event, role, and at most a kind or status}. Never an id, title, name, path or
text: an event name from EVENTS and a role from ROLES, or the line isn't written.

  python3 scripts/feature_counts.py report [--days 1]     counts by event and role (and kind/status), no content

The report also counts real turns' device-tool calls by platform from the turn log's tool_devices (#1366):
"device/<platform>/<tool>": n, for mac, iphone, ipad, watch and tv only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

PATH = Path(os.environ.get("FEATURE_COUNTS", "/ai/logs/feature-counts.jsonl"))
EVENTS = frozenset({"about_404", "about_400", "original_refused"})
ROLES = frozenset({"admin", "adult", "kid", "household", "engineering", "none"})
KINDS = frozenset({"document", "event", "screen", "kid"})


def note(event: str, role: str | None, kind: str | None = None, path: Path | None = None) -> None:
    """Count one event. Anything outside the allowlists is dropped, so free text can never land here; never raises."""
    r = str(role or "none")
    if event not in EVENTS or r not in ROLES:
        return
    row: dict[str, Any] = {"ts": round(time.time(), 1), "event": event, "role": r}
    if kind in KINDS:
        row["kind"] = kind
    try:
        p = path or PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def report(days: float = 1.0, path: Path | None = None, now: float | None = None) -> dict[str, int]:
    """{"event/role[/kind]": n} over the last `days`."""
    cut = (now or time.time()) - days * 86400
    out: Counter[str] = Counter()
    try:
        lines = (path or PATH).read_text().splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if float(r.get("ts") or 0) >= cut and r.get("event") in EVENTS:
            out["/".join(str(x) for x in (r["event"], r.get("role"), r.get("kind")) if x)] += 1
    return dict(sorted(out.items()))


TURN_LOG = Path(os.environ.get("AGENT_TURN_LOG", "/ai/logs/agent-turns.jsonl"))
DEVICE_PLATFORMS = frozenset({"mac", "iphone", "ipad", "watch", "tv"})


def by_device(days: float = 1.0, path: Path | None = None, now: float | None = None) -> dict[str, int]:
    """{"device/<platform>/<tool>": n}: a person's own turns' device-tool calls over the last `days` (#1366)."""
    cut = (now or time.time()) - days * 86400
    out: Counter[str] = Counter()
    try:
        lines = (path or TURN_LOG).read_text().splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if float(r.get("ts") or 0) < cut or r.get("source", "user") != "user" or r.get("pin"):
            continue
        for x in r.get("tool_devices") or []:
            if isinstance(x, list) and len(x) == 2 and x[1] in DEVICE_PLATFORMS:
                out[f"device/{x[1]}/{x[0]}"] += 1
    return dict(sorted(out.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("--days", type=float, default=1.0)
    a = ap.parse_args()
    print(json.dumps({**report(a.days), **by_device(a.days)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
