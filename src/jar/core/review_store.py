#!/usr/bin/env python3
"""Where the bulk review of scanned mail keeps its decisions (titan-node#1507, epic #1498 slice 8).

One row per person per document in a batch: what they did with titan's proposed connection (filed it, had it filed for
them on a hard key, left it out, or took an automatic filing back) and the filing itself. A filing is a pointer, never
the thing: a link (a thread), a cycle (a bill account's period), a bundle (a kept collection), an event plan (to add to
the calendar) or an archive plan (the junk move, with its undo). Scan ids and keys only: no body, no account number, no
name a scan printed, and nothing is ever deleted (an undo puts back what the row held before, or removes the row).

This module names no scans module, so the undo path (surfaces.undo) can use it without reading a scan.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("REVIEW_DB", "/ai/datasets/connectors/scan_review.db"))
FILED, AUTO, EXCLUDED, MANUAL = "filed", "auto", "excluded", "manual"
STATES = (FILED, AUTO, EXCLUDED, MANUAL)
FILING_KINDS = ("link", "cycle", "bundle", "event", "archive")
UNDO_KIND = "scans-review.change"
_lock = threading.Lock()


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("REVIEW_DB") or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS review_item (batch TEXT NOT NULL, person TEXT NOT NULL, item TEXT NOT NULL, "
                "state TEXT NOT NULL, kind TEXT, key TEXT, plan TEXT, token TEXT, at REAL NOT NULL, "
                "PRIMARY KEY (batch, person, item))")
    return con


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {"item": r["item"], "state": r["state"], "kind": r["kind"], "key": r["key"],
            "plan": json.loads(r["plan"]) if r["plan"] else None, "token": r["token"], "at": r["at"]}


def rows(batch: str, person: str, path: Path | None = None) -> dict[str, dict[str, Any]]:
    """{item: row} for one person in one batch. Only their own: the person is part of the key."""
    with _lock:
        con = _db(path)
        try:
            return {r["item"]: _row(r) for r in con.execute(
                "SELECT * FROM review_item WHERE batch=? AND person=?", (batch, person))}
        finally:
            con.close()


def put(batch: str, person: str, items: list[dict[str, Any]], path: Path | None = None) -> dict[str, dict[str, Any] | None]:
    """Write rows ({item, state, kind?, key?, plan?, token?, at}); returns what each held before (None: no row), which is
    exactly what an undo puts back."""
    before: dict[str, dict[str, Any] | None] = {}
    with _lock:
        con = _db(path)
        try:
            for it in items:
                if it["state"] not in STATES or (it.get("kind") and it["kind"] not in FILING_KINDS):
                    raise ValueError("unknown review state or filing kind")
                old = con.execute("SELECT * FROM review_item WHERE batch=? AND person=? AND item=?",
                                  (batch, person, it["item"])).fetchone()
                before[it["item"]] = _row(old) if old else None
                con.execute("INSERT OR REPLACE INTO review_item VALUES (?,?,?,?,?,?,?,?,?)",
                            (batch, person, it["item"], it["state"], it.get("kind"), it.get("key"),
                             json.dumps(it["plan"], sort_keys=True) if it.get("plan") is not None else None,
                             it.get("token"), float(it["at"])))
            con.commit()
        finally:
            con.close()
    return before


def restore(person: str, payload: dict[str, Any], path: Path | None = None) -> int:
    """Undo one change: each item goes back to the row it held (or has no row); `instead` leaves a "manual" row for an
    automatic filing, so it is not made again. Only the token's owner's rows."""
    batch, n = str(payload["batch"]), 0
    with _lock:
        con = _db(path)
        try:
            for item, old in sorted((payload.get("before") or {}).items()):
                con.execute("DELETE FROM review_item WHERE batch=? AND person=? AND item=?", (batch, person, item))
                if old:
                    con.execute("INSERT INTO review_item VALUES (?,?,?,?,?,?,?,?,?)",
                                (batch, person, item, old["state"], old.get("kind"), old.get("key"),
                                 json.dumps(old["plan"], sort_keys=True) if old.get("plan") is not None else None,
                                 old.get("token"), float(old["at"])))
                n += 1
            for item, row in sorted((payload.get("instead") or {}).items()):
                # an automatic filing taken back is remembered as the person's own decision, so it is not made again
                con.execute("INSERT OR REPLACE INTO review_item VALUES (?,?,?,?,?,?,?,?,?)",
                            (batch, person, item, MANUAL, None, None, None, None, float(row["at"])))
            con.commit()
        finally:
            con.close()
    return n
