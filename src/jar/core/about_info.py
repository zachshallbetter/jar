#!/usr/bin/env python3
"""Settings › Assistant and › About (titan-node#1076; Zach OK'd 10-06). Everything here is the caller's own.

  more asked     a real turn that asked for the long answer (the words, or Think harder) is noted: when, and why
                 (phrase | think_harder); GET /v1/me/assistant counts the caller's last 7 days
  about          GET /v1/about: titan's deployed commit and when, the last canary (when, passed/total, failing check
                 names), the caller's own devices' app versions, which brain answers chat and that background work is
                 local-only. Never a hostname, a path, a PID or the environment.
  reports        POST /v1/reports {text, device, app_version, log_tail}: cleaned (control characters, markers, personal
                 numbers, home paths), any other field refused, never chats, memory, mail, files or places; stored per
                 person, admins see the queue. A ticket is filed by hand (Coordination), never automatically.

Store: ABOUT_DB (default /ai/datasets/connectors/about.db, 0600).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

DB = Path(os.environ.get("ABOUT_DB", "/ai/datasets/connectors/about.db"))
CANARY = Path(os.environ.get("AGENT_CANARY_LAST", "/ai/logs/agent-canary.json"))
REPO = Path(__file__).resolve().parent.parent
REPORT_FIELDS = frozenset({"text", "device", "app_version", "log_tail"})
LIMITS = {"text": 4000, "device": 80, "app_version": 40, "log_tail": 20000}
REPORTS_PER_DAY = 20   # a device in a loop can't fill about.db (Coordination's review, #1078)
_lock = threading.Lock()


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("ABOUT_DB") or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE IF NOT EXISTS more_asked (person TEXT NOT NULL, at REAL NOT NULL, why TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS persona (person TEXT PRIMARY KEY, persona TEXT, at REAL NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, person TEXT NOT NULL, at REAL NOT NULL, "
                "text TEXT NOT NULL, device TEXT, app_version TEXT, log_tail TEXT, state TEXT NOT NULL DEFAULT 'new')")
    return con


# ── Assistant ─────────────────────────────────────────────────────────────────────────────────────────────────────

def note_turn(person: str, why: str | None, persona: str | None, path: Path | None = None) -> None:
    """A real turn of the person's: the reason it asked for more (if it did), and the persona it chose (if any)."""
    if not person:
        return
    with _lock:
        con = _db(path)
        try:
            if why in ("phrase", "think_harder"):
                con.execute("INSERT INTO more_asked VALUES (?,?,?)", (person, time.time(), why))
            if persona:
                con.execute("INSERT OR REPLACE INTO persona VALUES (?,?,?)", (person, str(persona)[:20], time.time()))
            con.execute("DELETE FROM more_asked WHERE at < ?", (time.time() - 30 * 86400,))
            con.commit()
        finally:
            con.close()


def assistant(person: str, path: Path | None = None, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    con = _db(path)
    try:
        n = con.execute("SELECT COUNT(*) FROM more_asked WHERE person=? AND at >= ?", (person, now - 7 * 86400)).fetchone()[0]
        p = con.execute("SELECT persona FROM persona WHERE person=?", (person,)).fetchone()
    finally:
        con.close()
    # titan's rule (jellybean#699, #1047): short unless the words or Think harder ask for more
    return {"asked_for_more_7d": int(n), "reply_length": "short", "persona": p[0] if p else None}


# ── About ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _deployed(repo: Path) -> dict[str, Any]:
    """The checkout's commit and when it last moved (its reflog), or nulls. Never a path or a hostname."""
    out: dict[str, Any] = {"commit": None, "deployed_at": None}
    try:
        out["commit"] = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True,
                                       text=True, timeout=5).stdout.strip() or None
        log = (repo / ".git" / "logs" / "HEAD")
        if log.exists():
            last = log.read_text().rstrip("\n").splitlines()[-1]
            ts = int(last.split("\t")[0].split()[-2])
            out["deployed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    except Exception:  # noqa: BLE001
        pass
    return out


def _canary(path: Path) -> dict[str, Any] | None:
    try:
        c = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    checks = c.get("checks") or {}
    return {"at": c.get("ts"), "status": c.get("status"), "passed": sum(1 for v in checks.values() if v.get("ok")),
            "total": len(checks), "failing": sorted(k for k, v in checks.items() if not v.get("ok"))}


def about(person: str | None, people: list[dict[str, Any]], repo: Path | None = None,
          canary: Path | None = None) -> dict[str, Any]:
    me = next((p for p in people if str(p.get("id")) == str(person)), None) if person else None
    devices = [{"name": d.get("name"), "platform": d.get("platform"), "app_version": d.get("app_version"),
                "last_seen": d.get("last_seen")} for d in (me or {}).get("devices") or [] if not d.get("revoked")]
    brain = os.environ.get("AGENT_TOOL_BRAIN", "gemini").strip().lower()
    chat = "gemini" if brain in ("gemini", "auto") and os.environ.get("GEMINI_API_KEY", "").strip() else "local"
    return {"titan": _deployed(repo or REPO), "canary": _canary(canary or CANARY), "devices": devices,
            "brain": {"chat": chat, "background_local_only": True}}


# ── Reports ───────────────────────────────────────────────────────────────────────────────────────────────────────

_HOME = re.compile(r"(/Users/|/home/)[^/\s]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(field: str, v: Any) -> str:
    import redact
    import untrusted
    t = _CONTROL.sub(" ", untrusted.neutralise(str(v or "")))
    t = _HOME.sub(lambda m: m.group(1) + "~", t)
    t = redact.redact_personal(t)
    return t[:LIMITS[field]]


def report(person: str | None, body: Any, path: Path | None = None, admin_name: str | None = None) -> dict[str, Any]:
    if not person:
        raise Refused(403, "reports are from a person")
    if not isinstance(body, dict):
        raise Refused(400, "expected {text, device?, app_version?, log_tail?}")
    extra = sorted(set(body) - REPORT_FIELDS)
    if extra:
        raise Refused(400, "unknown fields: " + ", ".join(extra[:5]))
    text = _clean("text", body.get("text")).strip()
    if not text:
        raise Refused(400, "text is required")
    rid = "rp-" + uuid.uuid4().hex[:12]
    with _lock:
        con = _db(path)
        try:
            n = con.execute("SELECT COUNT(*) FROM reports WHERE person=? AND at >= ?",
                            (person, time.time() - 86400)).fetchone()[0]
            if n >= REPORTS_PER_DAY:
                raise Refused(429, f"That's {REPORTS_PER_DAY} reports today. Try again tomorrow.")
            con.execute("INSERT INTO reports (id, person, at, text, device, app_version, log_tail) VALUES (?,?,?,?,?,?,?)",
                        (rid, person, time.time(), text, _clean("device", body.get("device")) or None,
                         _clean("app_version", body.get("app_version")) or None,
                         _clean("log_tail", body.get("log_tail")) or None))
            con.commit()
        finally:
            con.close()
    # who reads it: the admin sees every report's text (by design), and About says so
    return {"id": rid, "stored": True, "goes_to": admin_name}


def reports(role: str | None, person: str | None, path: Path | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """The admin's queue: every report, newest first. Anyone else: their own."""
    con = _db(path)
    con.row_factory = sqlite3.Row
    try:
        q, args = "SELECT * FROM reports", ()
        if role != "admin":
            q, args = q + " WHERE person=?", (person,)
        return [dict(r) for r in con.execute(q + " ORDER BY at DESC LIMIT ?", args + (max(1, min(int(limit), 200)),))]
    finally:
        con.close()
