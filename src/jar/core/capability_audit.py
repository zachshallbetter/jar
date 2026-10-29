#!/usr/bin/env python3
"""What titan did for each person, one row per tool call (#1628 slice 4; design in docs/design/governance-chokepoint.md).

A row says when, who asked, through which surface and source, which tool (and its domain and risk), whether it ran or
was refused and why, and how it came out. Never the arguments, the result or anything quoted from them: the record says
that titan searched the person's mail, not what for or what it found.

Kept in spaces.db's capability_audit, each row its person's own (delete-my-data covers it), for 30 days. A turn
without a person (the CLI, a background job) is recorded under "system". Eval, canary and test turns aren't
recorded: they aren't anyone's.

GET /v1/me/capability-log reads the caller's own rows, newest first. summary() is the "what I did" view (#1645): what
titan changed for the person, split by whether they asked (a turn of theirs) or it acted on its own (a routine, a
schedule, the proactive loop).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

KEEP_DAYS = 30
OUTCOMES = ("ok", "failed", "refused", "declined", "timeout", "unavailable")   # docs/design/executor-protocol.md
_lock = threading.Lock()
_last_prune = 0.0


def _db(path: Path | None = None) -> sqlite3.Connection:
    import spaces
    con = spaces._db(path)
    con.execute("CREATE TABLE IF NOT EXISTS capability_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                "person TEXT NOT NULL, role TEXT, surface TEXT, source TEXT, tool TEXT NOT NULL, domain TEXT, risk TEXT, "
                "outcome TEXT NOT NULL, why TEXT, ms INTEGER)")
    con.execute("CREATE INDEX IF NOT EXISTS capability_audit_person ON capability_audit (person, ts)")
    return con


def record(*, person: str | None, role: str | None, surface: str | None, source: str | None, tool: str,
           domain: str | None, risk: str | None, outcome: str, why: str | None = None, ms: int | None = None,
           now: float | None = None, path: Path | None = None) -> None:
    """One row. Never raises: a record that can't be written must not break the tool call it describes."""
    global _last_prune
    if source in ("eval", "canary", "test") or outcome not in OUTCOMES:
        return
    ts = time.time() if now is None else now
    try:
        con = _db(path)
        try:
            with con:
                con.execute("INSERT INTO capability_audit (ts, person, role, surface, source, tool, domain, risk, "
                            "outcome, why, ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (ts, person or "system", role, surface, source, str(tool)[:60], domain, risk, outcome,
                             (why or None) and str(why)[:40], ms))
                with _lock:
                    due = ts - _last_prune > 3600
                    if due:
                        _last_prune = ts
                if due:
                    con.execute("DELETE FROM capability_audit WHERE ts < ?", (ts - KEEP_DAYS * 86400,))
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return


# How an executor's answer came out (#1631 slice 1): the fixed sentences each one already returns, read in one place, so
# a person's log says "you said no" or "your Mac didn't wake" rather than "didn't work". Free text otherwise.
def _xd():
    import cross_device
    return cross_device


def executor_outcome(text: str, *, device: bool = False, executor: bool = False) -> tuple[str, str] | None:
    """(outcome, why) for an executor's answer, or None when it isn't one of their fixed shapes. `device`: a Mac or
    iPhone device tool's text (the bridge, cross-device); `executor`: one of titan-exec's tools."""
    t = " ".join(str(text or "").split())
    xd = _xd()
    if device:
        if t == "No device is connected to this turn, so device actions can't run here.":
            return "unavailable", "no_device"
        body = t[len("Device action was not completed: "):] if t.startswith("Device action was not completed: ") else t
        if body.startswith((xd.DECLINED, "Declined by the user.")):
            return "declined", "person"
        if body.startswith((xd.NO_ANSWER, "Confirmation timed out.", "No result from the device.")) \
                or t.startswith("Device action timed out"):   # the bridge's own wait (agent_api_server's device_hook)
            return "timeout", "device"
        if body.startswith(xd.NO_WAKE):
            return "unavailable", "no_wake"
        if body.startswith("The confirmation expired before the user answered"):
            return "timeout", "person"          # the Mac's confirm card ran out (ChatViewModel.handleDevice)
        if body.startswith("Not allowed: "):
            return "refused", "device_never"    # the person's Trust tier on that device is Never, or a hard limit
            #                                     (TrustRules.neverMessage): as an error, or as a plain result
        return None
    if executor:
        if t.startswith("Not done: titan's executor isn't running"):
            return "unavailable", "executor"
        if t.startswith(("Not done:", "Not run")):
            return "refused", "executor_rule"   # titan-exec's own rules (a window, the hold, the list, a disabled unit)
    return None


ASKED_SOURCES = ("user", "temporary")   # a turn the person started (a temporary chat is theirs too)
ACTED_ALONE = ("act_tell", "act_quiet")   # autonomy.LEVELS that run without asking: `why` on an ok row (#1645)


def summary(person: str, *, since: float, path: Path | None = None) -> dict[str, Any]:
    """#1645 "what I did": the person's own rows since `since`. {asked: {tool: n}, own: {tool: n}} for changes that went
    through (risk write or dangerous, outcome ok): own when a turn they didn't start made it (scheduled, proactive, a
    task step) or it ran under an act-alone level; reads: how many lookups; not_done: {outcome: n} for changes that
    didn't (refused, failed, timeout, unavailable); declined: their own no's; waiting: changes held for their OK."""
    con = _db(path)
    try:
        rows = con.execute("SELECT source, tool, risk, outcome, why FROM capability_audit WHERE person=? AND ts >= ?",
                           (person, float(since))).fetchall()
    finally:
        con.close()
    out: dict[str, Any] = {"asked": {}, "own": {}, "reads": 0, "not_done": {}, "declined": 0, "waiting": 0}
    for source, tool, risk, outcome, why in rows:
        if risk not in ("write", "dangerous"):
            out["reads"] += outcome == "ok"
            continue
        if outcome == "ok":
            side = out["asked"] if source in ASKED_SOURCES and why not in ACTED_ALONE else out["own"]
            side[tool] = side.get(tool, 0) + 1
        elif outcome == "declined":
            out["declined"] += 1
        elif outcome == "refused" and why == "held":   # waiting for their Do it, not a failure
            out["waiting"] += 1
        else:
            out["not_done"][outcome] = out["not_done"].get(outcome, 0) + 1
    return out


def log(person: str, *, limit: int = 200, since: float | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    """The person's own rows, newest first."""
    con = _db(path)
    try:
        q = "SELECT ts, role, surface, source, tool, domain, risk, outcome, why, ms FROM capability_audit WHERE person=?"
        args: list[Any] = [person]
        if since is not None:
            q += " AND ts >= ?"
            args.append(float(since))
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        return [dict(r) for r in con.execute(q, args)]
    finally:
        con.close()
