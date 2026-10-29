#!/usr/bin/env python3
"""A box's status light (#407 item 5): green, amber or red, and one plain sentence, from device health's own findings
(device_health.py, every 15 minutes). No new checks: the light is what those checks already concluded.

  red     something the household relies on is down: a titan service (the agent, a store's runtime), or a listener on
          the box that shouldn't be (security)
  amber   something needs attention but nothing is down: a device or sync source not reaching titan, a battery or disk
          low, a backup due to verify, a task stalled, Home Assistant unreachable; also any finding this file doesn't
          know (never green by default), checks that haven't run lately, or none yet
  green   every check passed on the last run

Only a confirmed problem counts (seen CONFIRM_RUNS runs in a row, as the cards wait for). The sentence is the worst
problem's own card title (plus how many more): it can name a device or a person, so only the admin gets it; anyone
else, and a shared screen, gets the colour alone (the light on the box shows colour only too).

  GET /v1/box/status  → {"light": "green"|"amber"|"red", "checked_at": iso|None, ["sentence", "problems"] for the admin}
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STALE_S = 3600          # device health runs every 15 min; an hour without a run is itself something to look at
RED = ("health-household-service-", "-listeners")
AMBER = ("-device-", "-battery-", "-disk-", "-source-", "-mac-", "-sync-", "-task-", "-backup-verify",
         "health-household-home")
RANK = {"green": 0, "amber": 1, "red": 2}


def colour_of(key: str) -> str:
    """A confirmed problem's colour by its finding key; one this file doesn't know is amber, never green."""
    if any(m in key for m in RED):
        return "red"
    return "amber"


def light(state: dict[str, Any] | None, checked_at: float | None, now: float | None = None) -> dict[str, Any]:
    """{light, sentence, problems, checked_at} from device health's state (its reconcile() rows) and when it ran."""
    import device_health as dh
    now = time.time() if now is None else now
    when = datetime.fromtimestamp(checked_at, timezone.utc).isoformat() if checked_at else None
    if state is None or checked_at is None:
        return {"light": "amber", "sentence": "Health checks haven't run yet.", "problems": 0, "checked_at": when}
    if now - checked_at > STALE_S:
        return {"light": "amber", "sentence": "Health checks haven't run in over an hour.", "problems": 0,
                "checked_at": when}
    problems = [(colour_of(k), float(s.get("since") or 0), str(s.get("title") or ""))
                for k, s in state.items() if isinstance(s, dict) and s.get("problem")
                and int(s.get("runs") or 0) >= dh.CONFIRM_RUNS]
    if not problems:
        return {"light": "green", "sentence": "Everything is working.", "problems": 0, "checked_at": when}
    problems.sort(key=lambda p: (-RANK[p[0]], p[1]))   # the worst, then the longest-standing
    worst = problems[0]
    more = len(problems) - 1
    sentence = (worst[2] or "Something needs a look.") + (f" (and {more} more)" if more else "")
    return {"light": worst[0], "sentence": sentence, "problems": len(problems), "checked_at": when}


def status(role: str | None, path: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """The light for one caller: the admin gets the sentence; anyone else (an adult, a kid, a shared screen) gets the
    colour and when it was checked, never the sentence (findings name devices and people)."""
    import device_health as dh
    p = Path(path or dh.STATE_PATH)
    try:
        state = json.loads(p.read_text())
        checked = p.stat().st_mtime
    except (OSError, ValueError):
        state, checked = None, None
    out = light(state, checked, now)
    if role == "admin":
        return out
    return {"light": out["light"], "checked_at": out["checked_at"]}
