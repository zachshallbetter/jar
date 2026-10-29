#!/usr/bin/env python3
"""Every read an engineering agent makes (#355), as counts: the route, how many items came back, and how many of
those weren't titan's own system items. Never a query, a title or a body.

The leak check is separate from the rules that decide what the engineering identity sees (kb_privacy, audience):
a world entity must be in the system bubble and a KB entry unowned with the admins' audience. A nonzero `leaks`
on any line is a miss on the scorecard (scorecard.system), and the leaked items are dropped from the reply."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

LOG = Path(os.environ.get("ENGINEERING_AUDIT_LOG", "/ai/logs/engineering-reads.jsonl"))


def _system_entity(e: Any) -> bool:
    if not isinstance(e, dict):
        return False
    if "bubble" in e:   # a list row names its bubble; a page has only its id ("<bubble>:<entity>")
        return e["bubble"] == "system"
    return str(e.get("id") or "").startswith("system:")


def _system_note(n: Any) -> bool:
    return isinstance(n, dict) and n.get("owner") is None and n.get("audience") == "admins"


def leaks(kind: str, items: list[Any]) -> int:
    """How many of these items an engineering agent should never have seen. kind: "world" or "kb"."""
    ok = _system_entity if kind == "world" else _system_note
    return sum(1 for i in items if not ok(i))


def only_system(kind: str, items: list[Any]) -> list[Any]:
    """The items an engineering agent may get; a leak is dropped here as well as counted."""
    ok = _system_entity if kind == "world" else _system_note
    return [i for i in items if ok(i)]


def record(route: str, kind: str, items: list[Any], log: Path | None = None) -> dict[str, Any]:
    row = {"ts": time.time(), "route": route, "items": len(items), "leaks": leaks(kind, items)}
    try:
        path = log or LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass   # the audit never fails a read
    return row
