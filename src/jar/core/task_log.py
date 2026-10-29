#!/usr/bin/env python3
"""What moved on titan's multi-step tasks (titan-node#283), and the daily card that says so.

Every step completed (complete_step, also run by run_task) or found blocked (run_task: the step
needs the person) is one line here: task, goal, step, state, note, owner, time. The daily digest
sends each owner one card for what moved in the last 24 hours, keyed by day so a re-run updates it,
and nothing at all on a day nothing moved.

  python3 scripts/task_log.py digest [--dry-run]     (titan-task-digest.timer, 18:00 PT)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

import local_time

DB = Path(os.environ.get("TASK_LOG_DB", "/ai/datasets/connectors/task_log.db"))


def _db(db: Path | None = None) -> sqlite3.Connection:
    p = db or DB
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE IF NOT EXISTS steps (task_id TEXT, goal TEXT, step TEXT, state TEXT, note TEXT, "
                "owner TEXT, ts REAL)")
    return con


def record(task_id: str, goal: str, step: str, state: str, note: str, owner: str | None,
           db: Path | None = None) -> None:
    """state: done | blocked. Never raises: the log must not break a task."""
    try:
        con = _db(db)
        with con:
            con.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?)",
                        (task_id, goal[:200], step[:200], state, " ".join(str(note or "").split())[:300],
                         owner or "zach", time.time()))
        con.close()
        # The owner's devices see the task moved (jellybean#138): metadata only, never the note.
        import continuity
        continuity.emit(owner or "zach", "task", {"task_id": task_id, "title": goal[:200], "step": step[:200], "state": state})
    except Exception:  # noqa: BLE001
        pass


def moved(since: float, db: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """owner → [{task_id, goal, step, state, note, ts}] since `since`, oldest first."""
    con = _db(db)
    try:
        rows = con.execute("SELECT task_id, goal, step, state, note, owner, ts FROM steps WHERE ts>=? ORDER BY ts",
                           (since,)).fetchall()
    finally:
        con.close()
    out: dict[str, list[dict[str, Any]]] = {}
    for t, g, s, st, n, o, ts in rows:
        out.setdefault(o, []).append({"task_id": t, "goal": g, "step": s, "state": st, "note": n, "ts": ts})
    return out


def latest(task_ids: list[str], db: Path | None = None) -> dict[str, dict[str, Any]]:
    """task_id → its newest line {step, state, ts}: what the task list shows as "moved" (jellybean#228). The
    note stays out (content-free, like the continuity event)."""
    ids = [t for t in task_ids if t]
    if not ids:
        return {}
    con = _db(db)
    try:
        rows = con.execute(f"SELECT task_id, step, state, ts FROM steps WHERE task_id IN ({','.join('?' * len(ids))}) "
                           "ORDER BY ts", ids).fetchall()
    finally:
        con.close()
    return {t: {"step": s, "state": st, "ts": ts} for t, s, st, ts in rows}


def digest_card(items: list[dict[str, Any]]) -> tuple[str, str]:
    """(title, detail) for one owner's day."""
    tasks: dict[str, list[dict[str, Any]]] = {}
    for i in items:
        tasks.setdefault(i["task_id"], []).append(i)
    done = sum(1 for i in items if i["state"] == "done")
    blocked = [i for i in items if i["state"] == "blocked"]
    title = f"Tasks today: {done} step{'' if done == 1 else 's'} done"
    if blocked:
        title += f", {len(blocked)} waiting on you"
    lines = []
    for steps in tasks.values():
        goal = steps[-1]["goal"]
        bits = [f"done “{s['step']}”" for s in steps if s["state"] == "done"]
        last = steps[-1]
        if last["state"] == "blocked":
            bits.append(f"waiting on you for “{last['step']}”" + (f": {last['note']}" if last["note"] else ""))
        lines.append(f"“{goal}”: " + "; ".join(bits))
    return title, "\n".join(lines)


def digest(send: Callable[..., Any] | None, now: float | None = None, db: Path | None = None) -> list[str]:
    """One card per owner for the last 24 hours (send=None: report only)."""
    now = now or time.time()
    out = []
    day = local_time.day(now)
    for owner, items in moved(now - 86400, db).items():
        title, detail = digest_card(items)
        if send:
            send(title, detail, kind="report", urgency=1, source="tasks", to=[owner], key=f"tasks-{day}-{owner}"[:80],
                 expires_in=36 * 3600)
        out.append(f"{owner}: {title}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["digest"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    send = None
    if not a.dry_run:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import proactive
        send = proactive.add_card
    sent = digest(send)
    print("\n".join(sent) or "task digest: nothing moved in the last 24 hours")
    return 0


if __name__ == "__main__":
    sys.exit(main())
