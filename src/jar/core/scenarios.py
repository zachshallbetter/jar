#!/usr/bin/env python3
"""Tasks that advance on their own, each in its own scenario (titan-node#343; design docs/envelopes-and-scenarios.md
§7; Zach 10-01: "items can advance on their own… in their own scenario… aware of the changes inside and outside
the envelope"). Slice 1: the scenario store, the step envelope, and the consistency decision. Behind
AGENT_TASK_AUTONOMY (default off); the runner and opt-in come in slice 2.

  scenario   a task's own context: goal, owner, cadence, its grant, its dependencies (entities and items it
             relies on) and the fields of them it watches; the person's answers.
  envelope   one autonomous step: task, step, owner, attempt, grant digest, context digest, the changes since
             the last step and the consistency they put the task in. It is also the step's receipt.
Consistency (HCML 10): none → continue · stale_safe (something it depends on changed, nothing it watches)
  → continue and disclose · conflicted (a watched field changed, or a dependency was removed) → freeze, ask
  · unknown (a dependency can't be read) → stop at the boundary.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

import provenance as pv

DB = Path(os.environ.get("TASKS_DB", "/ai/datasets/connectors/tasks.db"))
STATES = ("active", "waiting", "paused", "done", "stopped")
CADENCES = ("off", "daily", "after_each")


def enabled() -> bool:
    return os.environ.get("AGENT_TASK_AUTONOMY", "0").strip().lower() in ("1", "true", "yes", "on")


def owner_allowed(owner: str | None) -> bool:
    """AGENT_TASK_AUTONOMY_OWNERS (comma-separated person ids), when set, limits who may have tasks advance on
    their own (Zach's trial: "zach" only). Unset: every admin who opts a task in."""
    raw = os.environ.get("AGENT_TASK_AUTONOMY_OWNERS", "").strip()
    if not raw:
        return True
    return str(owner or "").strip().lower() in {x.strip().lower() for x in raw.split(",") if x.strip()}


def _db(db: Path | None = None) -> sqlite3.Connection:
    p = db or DB
    p.parent.mkdir(parents=True, exist_ok=True)
    fresh = not p.exists()
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    if fresh:
        os.chmod(p, 0o640)
    con.execute("""CREATE TABLE IF NOT EXISTS scenarios (
        task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, goal TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
        cadence TEXT NOT NULL DEFAULT 'off', daily_cap INTEGER NOT NULL DEFAULT 1, next_due REAL, grant_id TEXT,
        deps TEXT NOT NULL DEFAULT '{}', last_step_at REAL, created_at REAL NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS answers (task_id TEXT NOT NULL, step_key TEXT, text TEXT NOT NULL,
        at REAL NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS envelopes (task_id TEXT NOT NULL, step_key TEXT NOT NULL, attempt INTEGER,
        envelope TEXT NOT NULL, digest TEXT NOT NULL, at REAL NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS steps_per_day (task_id TEXT NOT NULL, day TEXT NOT NULL, n INTEGER NOT NULL,
        PRIMARY KEY (task_id, day))""")
    return con


def create(task_id: str, owner: str, goal: str, *, deps: dict[str, Any] | None = None, cadence: str = "off",
           daily_cap: int = 1, now: float | None = None, db: Path | None = None) -> dict[str, Any]:
    if cadence not in CADENCES:
        raise ValueError(f"cadence is one of {CADENCES}")
    d = {"entities": [], "items": [], "watch": {}, **(deps or {})}
    con = _db(db)
    try:
        con.execute("INSERT OR REPLACE INTO scenarios (task_id, owner, goal, cadence, daily_cap, deps, created_at) "
                    "VALUES (?,?,?,?,?,?,?)", (task_id, owner, goal, cadence, max(1, int(daily_cap)),
                                               json.dumps(d, sort_keys=True), now or time.time()))
        con.commit()
    finally:
        con.close()
    return get(task_id, db)


def get(task_id: str, db: Path | None = None) -> dict[str, Any] | None:
    con = _db(db)
    try:
        r = con.execute("SELECT * FROM scenarios WHERE task_id=?", (task_id,)).fetchone()
        if not r:
            return None
        s = dict(r)
        s["deps"] = json.loads(s["deps"] or "{}")
        s["answers"] = [dict(a) for a in con.execute("SELECT step_key, text, at FROM answers WHERE task_id=? ORDER BY at",
                                                     (task_id,))]
        return s
    finally:
        con.close()


def update(task_id: str, db: Path | None = None, **fields: Any) -> None:
    allowed = {"state", "cadence", "daily_cap", "next_due", "grant_id", "last_step_at", "deps"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"not updatable: {sorted(bad)}")
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"state is one of {STATES}")
    if "deps" in fields:
        fields["deps"] = json.dumps(fields["deps"], sort_keys=True)
    con = _db(db)
    try:
        con.execute(f"UPDATE scenarios SET {', '.join(f'{k}=?' for k in fields)} WHERE task_id=?",
                    (*fields.values(), task_id))
        con.commit()
    finally:
        con.close()


def answer(task_id: str, text: str, step_key: str | None = None, *, now: float | None = None,
           db: Path | None = None) -> None:
    """The person's reply to "waiting on you": kept in the scenario, and the task may continue."""
    con = _db(db)
    try:
        con.execute("INSERT INTO answers (task_id, step_key, text, at) VALUES (?,?,?,?)",
                    (task_id, step_key, str(text)[:1000], now or time.time()))
        con.execute("UPDATE scenarios SET state='active' WHERE task_id=? AND state='waiting'", (task_id,))
        con.commit()
    finally:
        con.close()


def consistency(scenario: dict[str, Any], changes: list[dict[str, Any]] | None) -> tuple[str, list[dict[str, Any]]]:
    """(state, the changes that decided it). changes=None: the dependencies couldn't be read."""
    if changes is None:
        return "unknown", []
    if not changes:
        return "none", []
    watch = (scenario.get("deps") or {}).get("watch") or {}
    hard = []
    for c in changes:
        dep = c.get("item_id") or c.get("entity_id")
        if c.get("change") == "removed":
            hard.append(c)
        elif set((c.get("diff") or {})) & set(watch.get(dep, [])):
            hard.append(c)
    return ("conflicted", hard) if hard else ("stale_safe", changes)


def envelope(scenario: dict[str, Any], step_key: str, attempt: int, *, grant_digest: str | None,
             changes: list[dict[str, Any]] | None, current: dict[str, Any] | None = None,
             now: float | None = None) -> dict[str, Any]:
    """The step's dispatch envelope (IEPE DispatchEnvelope + ICP fields), with the consistency decision."""
    state, deciding = consistency(scenario, changes)
    ctx = {"goal": scenario["goal"], "deps": scenario.get("deps"), "answers": scenario.get("answers", []),
           "current": current or {}}
    return {"task_id": scenario["task_id"], "step_key": step_key, "owner": scenario["owner"], "attempt": int(attempt),
            "grant_digest": grant_digest, "context_sha256": pv.digest(ctx), "since": scenario.get("last_step_at"),
            "changes": changes or [], "consistency": state, "deciding": deciding, "at": now or time.time()}


def _content_free(env: dict[str, Any]) -> dict[str, Any]:
    """A receipt carries ids, digests, fields named and outcomes, never values or anyone's text: task_log and
    these receipts may be read by review tooling (the owner's note on #343)."""
    def strip(c: dict[str, Any]) -> dict[str, Any]:
        return {"dep": c.get("item_id") or c.get("entity_id"), "change": c.get("change"),
                "fields": sorted((c.get("diff") or {}).keys()), "at": c.get("at")}
    return {**env, "changes": [strip(c) for c in env.get("changes") or []],
            "deciding": [strip(c) for c in env.get("deciding") or []]}


def record(env: dict[str, Any], outcome: dict[str, Any], db: Path | None = None) -> str:
    """The receipt: the envelope (content-free) with the step's outcome, digested and kept."""
    rec = {**_content_free(env), "outcome": outcome}
    d = pv.digest(rec)
    con = _db(db)
    try:
        con.execute("INSERT INTO envelopes (task_id, step_key, attempt, envelope, digest, at) VALUES (?,?,?,?,?,?)",
                    (env["task_id"], env["step_key"], env["attempt"], json.dumps(rec, sort_keys=True, default=str), d,
                     env["at"]))
        con.commit()
    finally:
        con.close()
    return d


def same_blocker(task_id: str, reason: str, context_sha256: str, db: Path | None = None) -> bool:
    """RDP: a blocker isn't retried unchanged: the last halt had this reason under this context."""
    con = _db(db)
    try:
        r = con.execute("SELECT envelope FROM envelopes WHERE task_id=? ORDER BY at DESC LIMIT 1", (task_id,)).fetchone()
    finally:
        con.close()
    if not r:
        return False
    last = json.loads(r["envelope"])
    halt = (last.get("outcome") or {}).get("halt") or {}
    return halt.get("reason") == reason and last.get("context_sha256") == context_sha256


def last_halt(task_id: str, db: Path | None = None) -> str | None:
    """The reason code of the newest receipt's halt, None when the newest step didn't halt."""
    con = _db(db)
    try:
        r = con.execute("SELECT envelope FROM envelopes WHERE task_id=? ORDER BY at DESC LIMIT 1", (task_id,)).fetchone()
    finally:
        con.close()
    if not r:
        return None
    return (((json.loads(r["envelope"]).get("outcome") or {}).get("halt")) or {}).get("reason")


def _day(now: float) -> str:
    """The household's date (titan runs on UTC): a day's cap resets at local midnight."""
    from datetime import datetime
    import local_time
    return datetime.fromtimestamp(now, local_time.zone()).date().isoformat()


def take_step(task_id: str, daily_cap: int, *, now: float | None = None, db: Path | None = None) -> bool:
    """Check and count today's step in one statement, so two runner ticks can't both pass the cap (like
    grants.use). True: this step may run and is counted."""
    day = _day(now or time.time())
    con = _db(db)
    try:
        con.execute("INSERT OR IGNORE INTO steps_per_day (task_id, day, n) VALUES (?,?,0)", (task_id, day))
        cur = con.execute("UPDATE steps_per_day SET n = n + 1 WHERE task_id=? AND day=? AND n < ?",
                          (task_id, day, max(1, int(daily_cap))))
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


def due(now: float, db: Path | None = None) -> list[dict[str, Any]]:
    """Active scenarios with a cadence whose next step is due."""
    con = _db(db)
    try:
        ids = [r["task_id"] for r in con.execute("SELECT task_id FROM scenarios WHERE state='active' AND cadence != 'off' "
                                                 "AND next_due IS NOT NULL AND next_due <= ?", (now,))]
    finally:
        con.close()
    return [s for s in (get(i, db) for i in ids) if s]


def can_step_today(task_id: str, daily_cap: int, *, now: float | None = None, db: Path | None = None) -> bool:
    """The cap is per day: a burst one day never spends the next days' steps."""
    con = _db(db)
    try:
        r = con.execute("SELECT n FROM steps_per_day WHERE task_id=? AND day=?", (task_id, _day(now or time.time()))).fetchone()
    finally:
        con.close()
    return (r["n"] if r else 0) < max(1, int(daily_cap))


def count_step(task_id: str, *, now: float | None = None, db: Path | None = None) -> None:
    con = _db(db)
    try:
        con.execute("INSERT INTO steps_per_day (task_id, day, n) VALUES (?,?,1) ON CONFLICT(task_id, day) DO UPDATE "
                    "SET n = n + 1", (task_id, _day(now or time.time())))
        con.commit()
    finally:
        con.close()


STOP_AFTER_HALTS = 3


def should_stop(task_id: str, db: Path | None = None) -> bool:
    """Three halts in a row on the same blocker (reason + context) stop the task (the owner's note on #343)."""
    con = _db(db)
    try:
        rows = [json.loads(r["envelope"]) for r in con.execute(
            "SELECT envelope FROM envelopes WHERE task_id=? ORDER BY at DESC LIMIT ?", (task_id, STOP_AFTER_HALTS))]
    finally:
        con.close()
    if len(rows) < STOP_AFTER_HALTS:
        return False
    prints = {((r.get("outcome") or {}).get("halt") or {}).get("reason", "") + "|" + str(r.get("context_sha256"))
              for r in rows}
    return all((r.get("outcome") or {}).get("halt") for r in rows) and len(prints) == 1


def halt(reason: str, done: list[str], remaining: list[str], resume_by: str) -> dict[str, Any]:
    """RDP §16: a halt is a deliverable."""
    return {"halt": {"reason": reason, "done": done, "remaining": remaining, "resume_by": resume_by}}


def since_block(changes: list[dict[str, Any]], describe: Callable[[list[dict[str, Any]]], str]) -> str:
    """What the step prompt and the result open with when something changed but the plan still holds."""
    return ("Since the last step:\n" + describe(changes)) if changes else ""
