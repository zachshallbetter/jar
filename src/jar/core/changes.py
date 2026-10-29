#!/usr/bin/env python3
"""What changed since a time, for one viewer (titan-node#394, design docs/envelopes-and-scenarios.md §5 F2).

Structured diffs, never prose (SSS: generated prose never replaces the trace; describe() is only a view).
This module covers synced items: the connector ledger's versions table (calendar events, mail, downloads…),
structured fields only. The world model's claim changes come from World.changes / Bubbles.changes
(world.py) in the same row shape (source "world", item_id "bubble:entity", diff keyed by attribute), and
#343's step envelope reads both.

  changes(since, item_ids=None, person=…, role=…) → [{item_id, source, change, at, title, diff, observed_at}]
    change: created | changed | removed;  diff: {field: [before, after]} (title included; a field before the
    first recorded version is None)
Visibility: an item's owner and audience from the KB sharing table (kb_privacy), else the version's own
(private to its owner); the viewer sees only what audience.visible allows, and then only what every fence the
change touches admits (fences.py, on since 10-01: title and both versions' fields are the text it's judged on).
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

LEDGER = Path(os.environ.get("CONNECTORS_LEDGER", "/ai/datasets/connectors/ledger.db"))


def _visible_fn(person: str | None, role: str | None, acl: dict[str, tuple[str | None, str]] | None):
    import audience as aud

    def ok(item_id: str, owner: str | None, audience: str | None) -> bool:
        o, a = (acl or {}).get(item_id) or (owner, audience or "private")
        return aud.visible(a or "private", person, role, owner=o or owner)
    return ok


def changes(since: float, *, item_ids: list[str] | None = None, person: str | None, role: str | None,
            ledger: Path | None = None, acl: dict[str, tuple[str | None, str]] | None = None,
            acl_lookup: Callable[[list[str]], dict] | None = None,
            fence: Callable[..., bool] | None = None) -> list[dict[str, Any]]:
    path = ledger or LEDGER
    if not Path(path).exists():
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        q, a = "SELECT * FROM versions WHERE at >= ?", [since]
        if item_ids:
            q += f" AND item_id IN ({','.join('?' * len(item_ids))})"
            a += list(item_ids)
        rows = [dict(r) for r in con.execute(q + " ORDER BY at", a)]
        if not rows:
            return []
        if acl is None:
            try:
                acl = (acl_lookup or _default_acl)(sorted({r["item_id"] for r in rows}))
            except Exception:  # noqa: BLE001 - no sharing table: each version's own owner and audience
                acl = {}
        ok = _visible_fn(person, role, acl)
        out = []
        for r in rows:
            if not ok(r["item_id"], r["owner"], r["audience"]):
                continue
            prev = con.execute("SELECT title, fields FROM versions WHERE item_id=? AND at < ? AND change != 'removed' "
                               "ORDER BY at DESC LIMIT 1", (r["item_id"], r["at"])).fetchone()
            before = {"title": prev["title"], **json.loads(prev["fields"] or "{}")} if prev else {}
            after = {"title": r["title"], **json.loads(r["fields"] or "{}")} if r["change"] != "removed" else {}
            keys = sorted(set(before) | set(after))
            diff = {k: [before.get(k), after.get(k)] for k in keys if before.get(k) != after.get(k)}
            if r["change"] == "changed" and not diff:
                continue   # a re-sync with nothing structured changed (only the body, which isn't kept)
            owner = ((acl or {}).get(r["item_id"]) or (r["owner"], None))[0] or r["owner"]
            text = " ".join(str(v) for v in [r["title"], *before.values(), *after.values()] if v is not None)
            if not (fence or _default_fence)(person, role, owner, text=text, item=r["item_id"]):
                continue   # a fence this change touches doesn't admit the viewer
            out.append({"item_id": r["item_id"], "source": r["source"], "change": r["change"], "at": r["at"],
                        "title": r["title"] or (before.get("title") if before else None), "diff": diff,
                        "observed_at": r["observed_at"]})
        return out
    finally:
        con.close()


def find_items(name: str, owner: str, *, ledger: Path | None = None, limit: int = 5) -> list[str]:
    """The owner's synced items whose latest title holds the name (a task's dependencies, #343)."""
    path = ledger or LEDGER
    if not Path(path).exists() or not str(name or "").strip():
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT item_id, title, MAX(at) FROM versions WHERE owner=? AND change != 'removed' "
                           "GROUP BY item_id", (owner,)).fetchall()
    finally:
        con.close()
    n = name.strip().lower()
    return [r[0] for r in sorted(rows, key=lambda r: -r[2]) if n in str(r[1] or "").lower()][:limit]


def _default_fence(person: str | None, role: str | None, owner: str | None, *, text: str, item: str) -> bool:
    import fences
    return fences.get().admits(person, role, owner, text=text, item=item)


def _default_acl(ids: list[str]) -> dict:
    import kb_privacy
    return kb_privacy.acl_rows(ids)


def describe(items: list[dict[str, Any]], limit: int = 12) -> str:
    """A plain view of the structured changes (the structure stays authoritative)."""
    lines = []
    for c in items[:limit]:
        name = f"“{c['title']}”" if c.get("title") else c["item_id"]
        if c["change"] == "created":
            lines.append(f"{c['source']}: {name} appeared")
        elif c["change"] == "removed":
            lines.append(f"{c['source']}: {name} was removed")
        else:
            bits = [f"{k} {v[0]!s} → {v[1]!s}" for k, v in c["diff"].items() if k != "title"]
            if "title" in c["diff"]:
                bits.insert(0, f"renamed from “{c['diff']['title'][0]}”")
            lines.append(f"{c['source']}: {name} changed: " + "; ".join(bits))
    if len(items) > limit:
        lines.append(f"…and {len(items) - limit} more")
    return "\n".join(lines) or "Nothing changed."
