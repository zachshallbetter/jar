#!/usr/bin/env python3
"""Grants: what a later or autonomous action may do without asking at that moment (titan-node#395 with #382;
design docs/envelopes-and-scenarios.md §7; the owner's amendments of 10-01).

One mechanism for a scheduled device action ("text Ava at 4:30", approved when it's scheduled) and an
autonomous task step (a task told to keep going). A grant binds an owner, a subject (the scheduled action or
the task), an effect class, tools, optionally the exact arguments (args_digest), a window and a use count.

Effect classes (formal-project-bootstrap's E0–E4), from a table here; an unknown tool is E4:
  E0 observe · E1 titan-internal, reversible · E2 outside, reversible · E3 communicates · E4 irreversible
Rules (the owner's amendments):
  - E4 is never granted ahead: it always asks when it runs.
  - E3 only with an exact args_digest (recipient and text), single use, a window of at most 2 h.
  - titan itself issues E0/E1 only. E2/E3 on a device are issued by that device (the person tapped approve
    there), for its own person, and never by a shared or household device.
  - Every grant expires within 30 days. Revoking one also cancels what holds it.
  - Every use appends a content-free audit line (grant id, tool, ok, time); the device keeps its own.
titan keeps the record (to list, show, check, revoke); the device is the authority for what it issued.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import provenance as pv

DB = Path(os.environ.get("GRANTS_DB", "/ai/datasets/connectors/grants.db"))
AUDIT = Path(os.environ.get("GRANTS_AUDIT", "/ai/logs/grants-audit.jsonl"))
MAX_DAYS = 30
E3_WINDOW_S = 2 * 3600
CLASSES = ("E0", "E1", "E2", "E3", "E4")

_E1 = {"add_todo", "complete_todo", "notify_user", "save_routine", "schedule_action", "cancel_scheduled",
       "review_decide", "set_preference", "ingest_text", "send_to_claude"}
_E2 = {"create_calendar_event", "update_calendar_event", "create_reminder", "update_reminder", "complete_reminder",
       "create_contact", "set_alarm", "home_control", "play_music", "music_control", "write_file", "file_write",
       "file_edit", "move_files", "trash_files", "draft_email"}
_E3 = {"send_message"}
# Overwrites aren't reversible: like E3, these are approved ahead only for the exact call, once (owner's review).
EXACT_ONLY = _E3 | {"write_file", "file_write", "file_edit", "move_files", "trash_files"}
_E4 = {"delete_calendar_event", "delete_reminder", "run_shell", "run_applescript", "run_shortcut", "run_skill",
       "create_skill", "ax_click", "ax_focus", "ax_type", "mouse_click", "undo_last_action"}


def effect_class(tool: str, write_tools: frozenset[str] | set[str] = frozenset(), read_tools: set[str] | None = None) -> str:
    """A tool's class. Reads are E0; anything unknown or arbitrary (shells, scripts, UI automation) is E4."""
    if tool in _E4:
        return "E4"
    if tool in _E3:
        return "E3"
    if tool in _E2:
        return "E2"
    if tool in _E1:
        return "E1"
    if read_tools is not None:
        return "E0" if tool in read_tools else "E4"
    return "E4" if tool in write_tools else "E0"


def _rank(c: str) -> int:
    return CLASSES.index(c)


def _db(db: Path | None = None) -> sqlite3.Connection:
    p = db or DB
    p.parent.mkdir(parents=True, exist_ok=True)
    fresh = not p.exists()
    con = sqlite3.connect(p)
    if fresh:
        os.chmod(p, 0o640)   # like the other connector stores
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS grants (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, issued_by TEXT NOT NULL, subject_kind TEXT NOT NULL,
        subject_ref TEXT NOT NULL, effect_class TEXT NOT NULL, tools TEXT NOT NULL, args_digest TEXT,
        not_before REAL NOT NULL, expires_at REAL NOT NULL, uses_left INTEGER NOT NULL, created_at REAL NOT NULL,
        revoked_at REAL, digest TEXT NOT NULL)""")
    return con


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["tools"] = json.loads(d["tools"])
    return d


def issue(owner: str, issued_by: str, subject: tuple[str, str], effect: str, tools: list[str], *,
          args: tuple[str, dict[str, Any]] | None = None, not_before: float | None = None, expires_at: float,
          uses: int = 1, issuer_role: str | None = None, issuer_person: str | None = None,
          subject_deadline: float | None = None, classify: Callable[[str], str] = effect_class,
          now: float | None = None, db: Path | None = None) -> dict[str, Any]:
    """Validate and store a grant. issued_by: "titan" or "mac:<id>" / "iphone:<id>"; issuer_role and
    issuer_person: the issuing device's role and person (a device grant). Raises ValueError with the reason."""
    now = now or time.time()
    nb = not_before if not_before is not None else now
    if effect not in CLASSES:
        raise ValueError(f"unknown effect class {effect!r}")
    if effect == "E4":
        raise ValueError("E4 (irreversible) is never granted ahead: it asks when it runs")
    if not tools:
        raise ValueError("a grant names its tools")
    over = [t for t in tools if _rank(classify(t)) > _rank(effect)]
    if over:
        raise ValueError(f"tools above {effect}: {over}")
    if expires_at <= nb or expires_at > now + MAX_DAYS * 86400:
        raise ValueError(f"a grant expires after it starts and within {MAX_DAYS} days")
    if issued_by == "titan":
        if _rank(effect) > _rank("E1"):
            raise ValueError("titan itself grants E0/E1 only; E2+ is approved on the person's own device")
    else:
        if not issued_by.startswith(("mac:", "iphone:")):
            raise ValueError("issued_by is titan or a device (mac:<id>, iphone:<id>)")
        if issuer_role == "household" or not issuer_person:
            raise ValueError("a shared or household device can't approve ahead")
        if issuer_person != owner:
            raise ValueError("a device approves only for its own person; grants never transfer")
    ad = None
    if args is not None:
        ad = pv.args_digest(*args)
        if len(tools) != 1 or args[0] != tools[0]:
            raise ValueError("exact arguments bind exactly one tool")
    if effect == "E3":
        if ad is None or uses != 1 or expires_at - nb > E3_WINDOW_S:
            raise ValueError("E3 needs the exact recipient and text, one use, and a window of at most 2 hours")
    if any(t in EXACT_ONLY for t in tools) and (ad is None or uses != 1):
        raise ValueError("a message, a file write or a move is approved ahead only for the exact call, once")
    if subject[0] == "scheduled":
        # A scheduled action is exactly one call: its grant binds that call, once, until it's due (plus the
        # runner's lateness allowance). Tool-level grants are for task steps only.
        if ad is None or uses != 1:
            raise ValueError("a scheduled action's grant binds its exact call, once")
        if subject_deadline is not None and expires_at > subject_deadline:
            raise ValueError("a scheduled action's grant ends when the action is due")
    elif subject[0] != "task_step":
        raise ValueError("a grant's subject is a scheduled action or a task step")
    rec = {"id": "gr-" + uuid.uuid4().hex[:12], "owner": owner, "issued_by": issued_by, "subject_kind": subject[0],
           "subject_ref": subject[1], "effect_class": effect, "tools": sorted(tools), "args_digest": ad,
           "not_before": nb, "expires_at": expires_at, "uses_left": max(1, int(uses)), "created_at": now}
    rec["digest"] = pv.digest(rec)
    con = _db(db)
    try:
        con.execute("INSERT INTO grants (id, owner, issued_by, subject_kind, subject_ref, effect_class, tools, args_digest, "
                    "not_before, expires_at, uses_left, created_at, digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rec["id"], owner, issued_by, subject[0], subject[1], effect, json.dumps(rec["tools"]), ad, nb,
                     expires_at, rec["uses_left"], now, rec["digest"]))
        con.commit()
    finally:
        con.close()
    return rec


def get(gid: str, db: Path | None = None) -> dict[str, Any] | None:
    con = _db(db)
    try:
        r = con.execute("SELECT * FROM grants WHERE id=?", (gid,)).fetchone()
        return _row(r) if r else None
    finally:
        con.close()


def check(gid: str, owner: str, tool: str, args: dict[str, Any], *, classify: Callable[[str], str] = effect_class,
          now: float | None = None, db: Path | None = None) -> str | None:
    """None when the grant covers this action now; otherwise why not (then the action asks, as before)."""
    now = now or time.time()
    g = get(gid, db)
    if not g:
        return "no such grant"
    if g["owner"] != owner:
        return "the grant is someone else's"
    if g["revoked_at"]:
        return "the grant was revoked"
    if now < g["not_before"] or now >= g["expires_at"]:
        return "outside the grant's window"
    if g["uses_left"] < 1:
        return "the grant is used up"
    if tool not in g["tools"]:
        return f"the grant doesn't cover {tool}"
    c = classify(tool)
    if c == "E4" or _rank(c) > _rank(g["effect_class"]):
        return f"{tool} is {c}, beyond the grant"
    if g["args_digest"] and g["args_digest"] != pv.args_digest(tool, args):
        return "the action isn't exactly what was approved"
    return None


def use(gid: str, owner: str, tool: str, args: dict[str, Any], *, classify: Callable[[str], str] = effect_class,
        now: float | None = None, db: Path | None = None, audit: Path | None = None) -> str | None:
    """check() and, when it passes, spend one use. Either way one content-free audit line."""
    now = now or time.time()
    why = check(gid, owner, tool, args, classify=classify, now=now, db=db)
    if why is None:
        con = _db(db)
        try:
            cur = con.execute("UPDATE grants SET uses_left=uses_left-1 WHERE id=? AND uses_left>0", (gid,))
            con.commit()
            if cur.rowcount != 1:
                why = "the grant is used up"
        finally:
            con.close()
    try:
        with (audit or AUDIT).open("a") as f:
            f.write(json.dumps({"grant": gid, "tool": tool, "ok": why is None, "ts": round(now, 3)}) + "\n")
    except OSError:
        pass
    return why


def list_for(owner: str | None, *, include_inactive: bool = False, now: float | None = None,
             db: Path | None = None) -> list[dict[str, Any]]:
    now = now or time.time()
    con = _db(db)
    try:
        q, a = "SELECT * FROM grants", []
        if owner is not None:
            q, a = q + " WHERE owner=?", [owner]
        rows = [_row(r) for r in con.execute(q + " ORDER BY created_at DESC", a)]
    finally:
        con.close()
    if include_inactive:
        return rows
    return [g for g in rows if not g["revoked_at"] and g["expires_at"] > now and g["uses_left"] > 0]


def revoke(gid: str, by_owner: str | None, *, cancel_holder: Callable[[dict[str, Any]], Any] | None = None,
           now: float | None = None, db: Path | None = None) -> bool:
    """Revoke (the owner, or None for an admin), and cancel what holds it, so nothing runs later on a stale
    card instead."""
    g = get(gid, db)
    if not g or g["revoked_at"] or (by_owner is not None and g["owner"] != by_owner):
        return False
    con = _db(db)
    try:
        con.execute("UPDATE grants SET revoked_at=? WHERE id=?", (now or time.time(), gid))
        con.commit()
    finally:
        con.close()
    if cancel_holder:
        cancel_holder(g)
    return True
