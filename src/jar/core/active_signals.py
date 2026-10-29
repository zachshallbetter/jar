#!/usr/bin/env python3
"""What's live for a person right now, as titles (#366 signal B): their open to-dos, their pending scheduled actions
and the household's events in the next day, each read through its store's own per-viewer reader (access projection
first). world.active_b bumps the entities these name. Titles stay in memory; nothing here is stored."""
from __future__ import annotations

from datetime import timedelta
from typing import Any


def live_titles(person: str | None, role: str | None, limit: int = 60) -> list[str]:
    if not person or role not in ("admin", "adult", "kid"):
        return []
    out: list[str] = []
    try:
        import todos
        out += [str(t.get("title") or "") for t in todos.list_for(todos.lists_visible(person, role))]
    except Exception:  # noqa: BLE001 - a store down: fewer titles, never a failed turn
        pass
    try:
        import scheduled
        out += [str(r.get("what") or "") for r in scheduled.list_for(person, role)]
    except Exception:  # noqa: BLE001
        pass
    try:
        import local_time
        import schedule_view
        today = local_time.now().date()
        v: dict[str, Any] = schedule_view.for_viewer(today, today + timedelta(days=1), person, role, {"household"})
        out += [str(e.get("title") or "") for e in (v.get("household") or {}).get("events", [])]
    except Exception:  # noqa: BLE001
        pass
    return [t for t in out if t.strip()][:limit]
