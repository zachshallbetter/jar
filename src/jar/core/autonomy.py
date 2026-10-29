#!/usr/bin/env python3
"""Autonomy per operation, first slice (#1645; governance in docs/design/governance-chokepoint.md).

An operation that titan holds for the person's OK (governance.needs_hold: kb_forget, titan's own restarts and canary
runs, a connected server's destructive tools) has a level per person:

  ask         the default: held, a card asks, their Do it runs it (#1659)
  act_tell    run it, then a card says what was done
  act_quiet   run it; it shows in their record (capability_audit) and nowhere else

Only the person sets a level in this slice (`source` "person"). The ticket's earned path (a kind of decision starts in
shadow and moves to acting once its measured accuracy clears a bar) comes later and writes `source` "earned"; it may
never set an EXPLICIT_ONLY operation, which acts only on the person's own word: permanent deletion and a connected
server's destructive tools.

Not here: the operations that confirm on the owner's own device by themselves (tool_descriptors.CONFIRMS_ON_DEVICE:
mail moves, lock up). Their tap is their floor (#955, #413); loosening it is its own decision.

Stored in spaces.db's `autonomy` (person, tool, level, source, updated_at), so delete-my-data covers it.
GET/PUT /v1/me/autonomy reads and sets the caller's own.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

LEVELS = ("ask", "act_tell", "act_quiet")
SOURCES = ("person", "earned")
EXPLICIT_ONLY_TOOLS = frozenset({"kb_forget"})   # permanent; plus any tool whose risk is "dangerous" (explicit_only())
# Held operations that always ask, whatever anyone sets: no level at all (Zach 10-14: stopping and starting a model
# server always asks, #1647).
NEVER_ACT_TOOLS = frozenset({"titan_model_stop", "titan_model_start"})


_made: set[str] = set()


def _db(path: Path | None = None) -> sqlite3.Connection:
    import spaces
    con = spaces._db(path)
    key = str(path)
    if key not in _made:   # the table once per store, not a write on every held call's read
        con.execute("CREATE TABLE IF NOT EXISTS autonomy (person TEXT NOT NULL, tool TEXT NOT NULL, level TEXT NOT NULL, "
                    "source TEXT NOT NULL, updated_at REAL, PRIMARY KEY (person, tool))")
        _made.add(key)
    return con


def explicit_only(tool: str) -> bool:
    """Acts only on the person's own word, never an earned level."""
    import tool_descriptors as T
    d = T.lookup(tool)
    return tool in EXPLICIT_ONLY_TOOLS or bool(d and d.risk == "dangerous")


ACT_ROLES = ("admin", "adult")   # a kid's and a shared screen's held operations always ask


def operations(role: str | None) -> list[str]:
    """The operations a person with this role has a level for: the held ones they may use. A kid or a screen has none:
    their held operations always ask."""
    if role not in ACT_ROLES and role is not None:
        return []
    import governance
    import household_identity as hh
    import tool_descriptors as T
    names = list(T.DESCRIPTORS)
    try:
        import mcp_host
        names += [n for n in mcp_host.specs() if n not in T.DESCRIPTORS]
    except Exception:  # noqa: BLE001
        pass
    out = []
    for n in names:
        if not governance.needs_hold(n) or n in NEVER_ACT_TOOLS:
            continue
        if n.startswith("mcp_") and n not in T.DESCRIPTORS:
            import mcp_host
            ok = mcp_host.role_allows(n, role)
        else:
            ok = hh.role_allows_tool(role, n)
        if ok:
            out.append(n)
    return sorted(out)


def levels(person: str | None, path: Path | None = None) -> dict[str, dict[str, Any]]:
    """{tool: {level, source}} for the person's set levels (anything not here is "ask")."""
    if not person:
        return {}
    con = _db(path)
    try:
        return {t: {"level": lv, "source": src} for t, lv, src in
                con.execute("SELECT tool, level, source FROM autonomy WHERE person=?", (person,))}
    finally:
        con.close()


def level(person: str | None, tool: str, path: Path | None = None, *, role: str | None = None) -> str:
    """This person's level for this operation. Anything that can't be read, or that isn't allowed, is "ask"; with
    `role`, so is an operation that role has no level for now (a server removed and another added under its name)."""
    try:
        row = levels(person, path).get(tool)
    except Exception:  # noqa: BLE001  (unreadable: ask; never act on a guess)
        return "ask"
    if row and role is not None and tool not in operations(role):
        return "ask"
    if not row or row["level"] not in LEVELS or tool in NEVER_ACT_TOOLS:
        return "ask"
    if row["level"] != "ask" and explicit_only(tool) and row["source"] != "person":
        return "ask"
    return row["level"]


def set_level(person: str, tool: str, lv: str, *, source: str = "person", path: Path | None = None) -> dict[str, Any]:
    if lv not in LEVELS:
        raise ValueError("level must be one of " + ", ".join(LEVELS))
    if source not in SOURCES:
        raise ValueError("source must be one of " + ", ".join(SOURCES))
    if source == "earned" and lv != "ask" and explicit_only(tool):
        raise ValueError(f"{tool} acts only on the person's own word")
    con = _db(path)
    try:
        with con:
            if lv == "ask":
                con.execute("DELETE FROM autonomy WHERE person=? AND tool=?", (person, tool))
            else:
                con.execute("INSERT INTO autonomy (person, tool, level, source, updated_at) VALUES (?,?,?,?,?) "
                            "ON CONFLICT(person, tool) DO UPDATE SET level=excluded.level, source=excluded.source, "
                            "updated_at=excluded.updated_at", (person, tool, lv, source, time.time()))
    finally:
        con.close()
    return levels(person, path)
