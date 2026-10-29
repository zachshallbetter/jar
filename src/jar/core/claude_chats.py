#!/usr/bin/env python3
"""Claude conversations: each person's own chats with Claude, searchable by titan for them alone.

09-30: Robin asked about her fantasy football conversation with Claude, and titan had no way to
look, not even from the Mac. Two sources, both sent by the person's own device, so the owner is
whoever's device sent them:

  claude-code  Claude Code sessions on a Mac (~/.claude/projects/*/*.jsonl). Jellybean sends the
               lines that carry words (the person's prompts, Claude's text, titles, PR links) with
               tool calls, tool output and thinking already dropped; titan condenses them here.
  claude-ai    a claude.ai data export (Settings › Privacy › Export data): the zip, or the
               conversations.json inside it, imported from Jellybean on the Mac or the iPhone.

Kept out of the household knowledge base on purpose: hundreds of sessions would crowd household
facts out of every kb_retrieve. Instead a full-text store (SQLite FTS5) at $CLAUDE_CHATS_DB,
where every row has its owner and every read is WHERE owner = the asker. Nobody else's chats are
ever searched, the admin's included. Keys and passwords pasted into a chat are removed before
anything is stored (redact).

  python3 scripts/claude_chats.py status [--owner zach]
  python3 scripts/claude_chats.py forget --owner zach [--source claude-code] [--apply]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
import local_time

DB_PATH = Path(os.environ.get("CLAUDE_CHATS_DB", "/ai/datasets/connectors/claude_chats.db"))
SOURCES = ("claude-code", "claude-ai")
SOURCE_LABEL = {"claude-code": "Claude Code", "claude-ai": "claude.ai"}
MAX_MESSAGE = 8000          # chars kept per message (a pasted log doesn't need all of itself)
MAX_SESSIONS = 50           # per claude-code request
MAX_EXPORT_BYTES = 300_000_000
_lock = threading.Lock()


# ── secrets never stored ──────────────────────────────────────────────────────────────────────
REDACTED = "[secret removed]"
_SECRET_RXS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:sk-ant-|sk-proj-|sk-)[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),     # JWTs
    re.compile(r"(?i)\b(?:bearer)\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)\b((?:api[_ -]?key|secret|token|password|passwd|passcode|pin)\s*[:=]\s*)[\"']?[^\s\"',;]{4,}"),
    re.compile(r"\b[A-Fa-f0-9]{48,}\b"),                     # long hex keys (a 40-char git SHA stays)
    # long base64 runs with digits and both cases (a key), not a long word or a row of x's
    re.compile(r"(?<![A-Za-z0-9+/])(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])[A-Za-z0-9+/]{48,}={0,2}(?![A-Za-z0-9+/=])"),
]


def redact(text: str) -> str:
    """Remove keys, tokens, private keys and "password: …" values from a message."""
    out = str(text or "")
    for rx in _SECRET_RXS:
        if rx.groups:
            out = rx.sub(lambda m: m.group(1) + REDACTED, out)
        else:
            out = rx.sub(REDACTED, out)
    return out


# ── store ─────────────────────────────────────────────────────────────────────────────────────

def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("CLAUDE_CHATS_DB") or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS convs (
            owner TEXT NOT NULL, source TEXT NOT NULL, conv_id TEXT NOT NULL, title TEXT,
            place TEXT, started TEXT, updated TEXT, n INTEGER, digest TEXT, synced_at REAL,
            PRIMARY KEY (owner, source, conv_id));
        CREATE VIRTUAL TABLE IF NOT EXISTS msgs USING fts5(
            owner UNINDEXED, source UNINDEXED, conv_id UNINDEXED, seq UNINDEXED, role UNINDEXED,
            at UNINDEXED, text, tokenize = 'porter unicode61');
    """)
    # #1140: a Claude Code session's raw line count and its condenser state, so the Mac sends only new lines
    cols = {r[1] for r in con.execute("PRAGMA table_info(convs)")}
    for col, typ in (("raw_lines", "INTEGER"), ("state", "TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE convs ADD COLUMN {col} {typ}")
    return con


def _digest(conv: dict[str, Any]) -> str:
    body = json.dumps([conv.get("title"), conv.get("place"), conv.get("messages")], sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:24]


def store(owner: str, source: str, convs: Iterable[dict[str, Any]], path: Path | None = None) -> dict[str, int]:
    """Write one person's conversations (upsert by id; unchanged ones write nothing)."""
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}; expected one of {list(SOURCES)}")
    if not owner:
        raise ValueError("owner is required")
    stats = {"written": 0, "unchanged": 0, "empty": 0}
    with _lock:
        con = _db(path)
        try:
            for c in convs:
                cid = str(c.get("conv_id") or "").strip()
                msgs = [m for m in (c.get("messages") or []) if str(m.get("text") or "").strip()]
                if not cid or not msgs:
                    stats["empty"] += 1
                    continue
                c = {**c, "messages": msgs}
                d = _digest(c)
                row = con.execute("SELECT digest FROM convs WHERE owner=? AND source=? AND conv_id=?",
                                  (owner, source, cid)).fetchone()
                if row and row["digest"] == d:
                    if c.get("raw_lines") is not None:   # the same words, maybe more lines (tool calls)
                        con.execute("UPDATE convs SET raw_lines=?, state=? WHERE owner=? AND source=? AND conv_id=?",
                                    (int(c["raw_lines"]), json.dumps(c.get("state") or {}), owner, source, cid))
                    stats["unchanged"] += 1
                    continue
                con.execute("DELETE FROM msgs WHERE owner=? AND source=? AND conv_id=?", (owner, source, cid))
                title = redact(str(c.get("title") or ""))[:200]
                rows = [(owner, source, cid, -1, "title", c.get("started") or "", title)] if title else []
                rows += [(owner, source, cid, i, m.get("role") or "", m.get("at") or "",
                          redact(str(m["text"]))[:MAX_MESSAGE]) for i, m in enumerate(msgs)]
                con.executemany("INSERT INTO msgs (owner, source, conv_id, seq, role, at, text) VALUES (?,?,?,?,?,?,?)", rows)
                con.execute("""INSERT INTO convs (owner, source, conv_id, title, place, started, updated, n, digest, synced_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner, source, conv_id) DO UPDATE SET
                               title=excluded.title, place=excluded.place, started=excluded.started,
                               updated=excluded.updated, n=excluded.n, digest=excluded.digest,
                               synced_at=excluded.synced_at""",
                            (owner, source, cid, title, c.get("place") or "",
                             c.get("started") or "", c.get("updated") or "", len(msgs), d, time.time()))
                con.execute("UPDATE convs SET raw_lines=?, state=? WHERE owner=? AND source=? AND conv_id=?",
                            (c.get("raw_lines"), json.dumps(c["state"]) if c.get("state") else None,
                             owner, source, cid))
                stats["written"] += 1
            con.commit()
        finally:
            con.close()
    return stats


def status(owner: str, path: Path | None = None) -> dict[str, Any]:
    con = _db(path)
    try:
        rows = con.execute("SELECT source, COUNT(*) n, MAX(updated) latest FROM convs WHERE owner=? AND n > 0 GROUP BY source",
                           (owner,)).fetchall()
        return {"owner": owner, "sources": {r["source"]: {"conversations": r["n"], "latest": r["latest"]} for r in rows}}
    finally:
        con.close()


def forget(owner: str, source: str | None = None, apply: bool = False, path: Path | None = None) -> dict[str, Any]:
    """Remove one person's conversations (one source, or all). Dry run unless apply."""
    if source and source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    where, args = ("owner=? AND source=?", (owner, source)) if source else ("owner=?", (owner,))
    with _lock:
        con = _db(path)
        try:
            n = con.execute(f"SELECT COUNT(*) FROM convs WHERE {where}", args).fetchone()[0]
            if apply:
                con.execute(f"DELETE FROM msgs WHERE {where}", args)
                con.execute(f"DELETE FROM convs WHERE {where}", args)
                con.commit()
            return {"owner": owner, "source": source or "all", "conversations": n, "removed": bool(apply)}
        finally:
            con.close()


# ── claude.ai export ──────────────────────────────────────────────────────────────────────────

def parse_claude_ai_export(data: bytes) -> list[dict[str, Any]]:
    """The export's zip (or its conversations.json) → conversations."""
    if len(data) > MAX_EXPORT_BYTES:
        raise ValueError(f"the export is over {MAX_EXPORT_BYTES // 1_000_000} MB")
    raw: Any
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next((n for n in z.namelist() if n.rsplit("/", 1)[-1] == "conversations.json"), None)
            if not name:
                raise ValueError("no conversations.json in the zip; is it a claude.ai data export?")
            raw = json.loads(z.read(name))
    else:
        raw = json.loads(data)
    if not isinstance(raw, list):
        raise ValueError("conversations.json should be a list of conversations")
    out = []
    for c in raw:
        if not isinstance(c, dict) or not c.get("uuid"):
            continue
        msgs = []
        for m in c.get("chat_messages") or []:
            parts = [b.get("text") for b in (m.get("content") or []) if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(p for p in parts if p) or str(m.get("text") or "")
            files = [str(f.get("file_name")) for f in (m.get("files") or []) + (m.get("attachments") or [])
                     if isinstance(f, dict) and f.get("file_name")]
            if files:
                text = (text + "\n" if text else "") + "[attached: " + ", ".join(files[:10]) + "]"
            if text.strip():
                msgs.append({"role": "user" if m.get("sender") == "human" else "claude", "text": text.strip(),
                             "at": m.get("created_at") or ""})
        out.append({"conv_id": str(c["uuid"]), "title": str(c.get("name") or "").strip() or _first_words(msgs),
                    "place": "claude.ai", "started": c.get("created_at") or "",
                    "updated": c.get("updated_at") or (msgs[-1]["at"] if msgs else ""), "messages": msgs})
    return out


# ── Claude Code sessions ──────────────────────────────────────────────────────────────────────
# What Jellybean sends per session: {"session_id", "lines": [jsonl lines of type user / assistant /
# custom-title / summary / pr-link, with tool_use, tool_result and thinking blocks dropped]}. Extra
# lines are fine: this is where they're filtered, so the two sides can't drift.
_NOISE_RX = re.compile(r"<(system-reminder|local-command-stdout|local-command-stderr|command-message|"
                       r"task-notification|cross-session-message|user-prompt-submit-hook)\b[^>]*>.*?</\1>", re.S)
_COMMAND_RX = re.compile(r"<command-name>\s*(/?[\w:.-]+)\s*</command-name>")
_SKIP_PREFIXES = ("Caveat: The messages below", "[Request interrupted", "This session is being continued from a previous")


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def condense_claude_code(session_id: str, lines: Iterable[str], state: dict[str, Any] | None = None) -> dict[str, Any]:
    """One Claude Code session's jsonl lines → a conversation (prompts and Claude's words only). state: what earlier
    lines of the same session left (an append, #1140); the result carries the new state and its raw line count."""
    st = state or {}
    msgs, title, summary = [], str(st.get("title") or ""), str(st.get("summary") or "")
    cwd, branch, prs = str(st.get("cwd") or ""), str(st.get("branch") or ""), list(st.get("prs") or [])
    started, updated = str(st.get("started") or ""), str(st.get("updated") or "")
    raw = 0
    for line in lines:
        raw += 1
        try:
            d = json.loads(line) if isinstance(line, str) else line
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if t == "custom-title" and d.get("customTitle"):
            title = str(d["customTitle"])
            continue
        if t == "summary" and d.get("summary"):
            summary = str(d["summary"])
            continue
        if t == "pr-link" and d.get("prUrl"):
            if d["prUrl"] not in prs:
                prs.append(str(d["prUrl"]))
            continue
        if t not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
            continue
        cwd, branch = d.get("cwd") or cwd, d.get("gitBranch") or branch
        at = str(d.get("timestamp") or "")
        started, updated = started or at, at or updated
        text = _text_of((d.get("message") or {}).get("content"))
        cmd = _COMMAND_RX.search(text)
        text = _NOISE_RX.sub("", text)
        text = re.sub(r"</?command-(?:name|args)>", "", text).strip()
        if t == "user" and (not text or text.startswith(_SKIP_PREFIXES)):
            continue
        if t == "user" and cmd and not re.sub(r"\s+", "", text.replace(cmd.group(1), "")):
            text = cmd.group(1)
        if text:
            msgs.append({"role": "user" if t == "user" else "claude", "text": text, "at": at})
    project = cwd.rstrip("/").rsplit("/", 1)[-1] if cwd else ""
    place = "Claude Code" + (f" · {project}" if project else "") + (f" ({branch})" if branch and branch not in ("main", "master", "HEAD") else "")
    if prs:
        msgs.append({"role": "note", "text": "Pull requests: " + ", ".join(prs), "at": updated})
    first = str(st.get("first_words") or "") or _first_words(msgs)
    first_kept = "" if first == "(untitled)" else first   # a later prompt can still name it
    return {"conv_id": str(session_id), "title": title or summary or first, "place": place,
            "started": started, "updated": updated, "messages": msgs, "raw_lines": raw,
            # stored beside the session (#1140): the words in it are redacted like the messages
            "state": {"title": redact(title)[:200], "summary": redact(summary)[:200], "cwd": cwd, "branch": branch,
                      "prs": prs, "started": started, "updated": updated, "first_words": redact(first_kept)[:200]}}


def append_claude_code(owner: str, session_id: str, from_line: int, lines: list[Any],
                       path: Path | None = None) -> dict[str, Any]:
    """Lines from_line onward of one Claude Code session (#1140). Applied only when titan holds exactly from_line raw
    lines of it; otherwise {"conflict": True, "have": M} and nothing changes (the Mac re-sends from M, or replaces).
    A session stored before line counts were kept has 0, and an append from 0 writes the whole session afresh."""
    cid = str(session_id or "").strip()
    if not owner or not cid:
        raise ValueError("owner and session_id are required")
    src, from_line = "claude-code", int(from_line)
    where = "owner=? AND source=? AND conv_id=?"
    with _lock:
        con = _db(path)
        try:
            row = con.execute(f"SELECT raw_lines, state, started FROM convs WHERE {where}", (owner, src, cid)).fetchone()
            have = int(row["raw_lines"]) if row and row["raw_lines"] is not None else 0
            if from_line != have:
                return {"conflict": True, "have": have}
            state = json.loads(row["state"]) if from_line and row and row["state"] else None
            conv = condense_claude_code(cid, lines, state)
            new = [m for m in conv["messages"] if m.get("role") != "note" and str(m.get("text") or "").strip()]
            note = next((m for m in conv["messages"] if m.get("role") == "note"), None)
            if from_line == 0:
                con.execute(f"DELETE FROM msgs WHERE {where}", (owner, src, cid))
            else:   # the title and the pull-request note are rewritten; the words so far stay
                con.execute(f"DELETE FROM msgs WHERE {where} AND role IN ('title', 'note')", (owner, src, cid))
            base = con.execute(f"SELECT COUNT(*) FROM msgs WHERE {where}", (owner, src, cid)).fetchone()[0]
            title = redact(str(conv.get("title") or ""))[:200]
            rows = [(owner, src, cid, -1, "title", conv.get("started") or "", title)] if title else []
            rows += [(owner, src, cid, base + i, m.get("role") or "", m.get("at") or "",
                      redact(str(m["text"]))[:MAX_MESSAGE]) for i, m in enumerate(new)]
            if note:
                rows.append((owner, src, cid, base + len(new), "note", note.get("at") or "",
                             redact(str(note["text"]))[:MAX_MESSAGE]))
            con.executemany("INSERT INTO msgs (owner, source, conv_id, seq, role, at, text) VALUES (?,?,?,?,?,?,?)", rows)
            n = base + len(new) + (1 if note else 0)
            total = from_line + conv["raw_lines"]
            # digest NULL: a later replace of the same session always rewrites it
            con.execute("""INSERT INTO convs (owner, source, conv_id, title, place, started, updated, n, digest, synced_at,
                                              raw_lines, state)
                           VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?) ON CONFLICT(owner, source, conv_id) DO UPDATE SET
                           title=excluded.title, place=excluded.place, started=excluded.started,
                           updated=excluded.updated, n=excluded.n, digest=NULL, synced_at=excluded.synced_at,
                           raw_lines=excluded.raw_lines, state=excluded.state""",
                        (owner, src, cid, title, conv.get("place") or "", conv.get("started") or "",
                         conv.get("updated") or "", n, time.time(), total, json.dumps(conv["state"])))
            con.commit()
            return {"appended": len(new), "have": total}
        finally:
            con.close()

def _first_words(msgs: list[dict[str, Any]]) -> str:
    first = next((m["text"] for m in msgs if m.get("role") == "user"), "")
    words = " ".join(str(first).split())
    return (words[:70].rsplit(" ", 1)[0] + "…") if len(words) > 70 else words or "(untitled)"


# ── reading (always one person's own) ─────────────────────────────────────────────────────────

def _when(at: str, tz: str | None = None, now: datetime | None = None) -> str:
    """"Oct 3" this year, "Oct 3, 2025" otherwise: both years in the household zone (#1515; the host runs UTC)."""
    try:
        d = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return ""
    zone = local_time.zone(tz)
    d = d.astimezone(zone)
    year = (datetime.now(zone) if now is None else now.astimezone(zone)).year
    return f"{d:%b} {d.day}, {d.year}" if d.year != year else f"{d:%b} {d.day}"


def _fts_query(q: str, any_word: bool = False) -> str:
    words = re.findall(r"[\w'’-]+", q.lower())
    stop = {"the", "a", "an", "my", "i", "me", "to", "of", "about", "with", "and", "or", "in", "on", "for", "what",
            "did", "was", "is", "claude", "chat", "chats", "conversation", "conversations", "asked", "talked"}
    terms = [w.strip("'’-") for w in words if w.strip("'’-") and w not in stop] or [w for w in words if w]
    return (" OR " if any_word else " ").join(f'"{t}"' for t in terms[:12])


def search(owner: str, query: str, source: str | None = None, limit: int = 6,
           path: Path | None = None) -> str:
    """The asker's own conversations matching a query: titles, where and when, best lines."""
    if not owner:
        return "Claude conversations belong to a person; this device has none."
    con = _db(path)
    try:
        total = con.execute("SELECT COUNT(*) FROM convs WHERE owner=? AND n > 0", (owner,)).fetchone()[0]
        if not total:
            return ("None of your Claude conversations are on titan yet. On your Mac, Jellybean › Settings › "
                    "Claude can send your Claude Code sessions; for claude.ai chats, export your data from claude.ai "
                    "(Settings › Privacy › Export data) and import the zip in Jellybean › Settings › Claude.")
        src_sql, src_args = ("AND m.source=?", (source,)) if source in SOURCES else ("", ())
        hits: list[sqlite3.Row] = []
        for any_word in (False, True):
            fq = _fts_query(query, any_word)
            if not fq:
                break
            hits = con.execute(f"""
                SELECT m.source, m.conv_id, m.role, m.at,
                       snippet(msgs, 6, '«', '»', '…', 18) AS snip, bm25(msgs) AS score
                FROM msgs m WHERE msgs MATCH ? AND m.owner=? {src_sql}
                ORDER BY score LIMIT 200""", (fq, owner, *src_args)).fetchall()
            if hits:
                break
        if not hits:
            return f"Nothing in your {total} Claude conversations matches “{query}”."
        by: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for h in hits:
            by.setdefault((h["source"], h["conv_id"]), []).append(h)
        out = [f"Your Claude conversations matching “{query}” ({len(by)} of {total}; only yours are searched):"]
        for n, ((src, cid), rows) in enumerate(list(by.items())[:limit], 1):
            c = con.execute("SELECT title, place, updated, n FROM convs WHERE owner=? AND source=? AND conv_id=?",
                            (owner, src, cid)).fetchone()
            if not c:
                continue
            out.append(f"{n}. “{c['title']}” — {c['place']}, {_when(c['updated'])}, {c['n']} messages [id: {src}:{cid}]")
            for r in [r for r in rows if r["role"] != "title"][:2]:
                who = "You" if r["role"] == "user" else "Claude" if r["role"] == "claude" else "Note"
                out.append(f"   {who}: {' '.join(str(r['snip']).split())}")
        out.append("To read one, call search_claude_chats with conversation=<id>.")
        return "\n".join(out)
    finally:
        con.close()


def read(owner: str, conv_ref: str, budget: int = 6000, path: Path | None = None) -> str:
    """One of the asker's own conversations: how it started and how it ended, within budget."""
    src, _, cid = str(conv_ref).partition(":")
    if src not in SOURCES or not cid:
        return "Give the conversation's id as search_claude_chats returned it (source:id)."
    con = _db(path)
    try:
        c = con.execute("SELECT title, place, started, updated, n FROM convs WHERE owner=? AND source=? AND conv_id=?",
                        (owner, src, cid)).fetchone()
        if not c:
            return "No conversation of yours has that id."
        rows = con.execute("SELECT role, text FROM msgs WHERE owner=? AND source=? AND conv_id=? AND role != 'title' "
                           "ORDER BY CAST(seq AS INTEGER)",
                           (owner, src, cid)).fetchall()
        lines = [f"{'You' if r['role'] == 'user' else 'Claude' if r['role'] == 'claude' else 'Note'}: {r['text']}" for r in rows]
        head = f"“{c['title']}” — {c['place']}, {_when(c['started'])} to {_when(c['updated'])}, {c['n']} messages"
        if sum(len(x) for x in lines) <= budget:
            return head + "\n" + "\n\n".join(lines)
        first = lines[0][:budget // 3]
        tail, used = [], len(first)
        for x in reversed(lines[1:]):
            x = x[:budget // 3]
            if used + len(x) > budget:
                break
            tail.insert(0, x)
            used += len(x)
        skipped = len(lines) - 1 - len(tail)
        return "\n\n".join([head, first, f"[… {skipped} messages in between; search with a query to find a part]"] + tail)
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--owner", default="zach")
    f = sub.add_parser("forget")
    f.add_argument("--owner", required=True)
    f.add_argument("--source", choices=SOURCES)
    f.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.cmd == "status":
        print(json.dumps(status(a.owner), indent=1))
    else:
        print(json.dumps(forget(a.owner, a.source, a.apply), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
