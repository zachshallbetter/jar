#!/usr/bin/env python3
"""A person's past conversations with titan, as topics (jellybean#117: "Chat history as topic cards on
mobile, allowing reading and continuing previous conversations between desktop and mobile").

titan keeps each person's chat as one stream (titan_memory.db `messages`, conv_id "chat" for Zach,
"chat:<person>" for everyone else). A conversation is a run of that stream: the app's own thread id
when it sent one (`thread`, from 10-01), otherwise a pause of GAP_S (10 min) or more starts a new one. Ids are
stable: the thread id, or "s-" + the first message's id.

  list_for(db, conv_id)                      newest first: {id, title, last_at, started_at, count, preview, device,
                                             platform}
  messages(db, conv_id, cid, before, limit, before_id)
                                             {id, role, text, at, platform}, oldest first; None when it isn't this
                                             person's.
                                             Paging (#1290): before = the oldest loaded message's at; with
                                             before_id (its id) too, the page is everything ahead of that message
                                             in the conversation's own order, so two messages with the same at
                                             across a page boundary are both returned
  search(db, conv_id, query, limit)          newest first: {conversation, title, last_at, role, at, snippet} per
                                             matching message (jellybean#827: the iPhone's search; the Mac's ⌘K
                                             can use it for chats it doesn't hold, #820)

Only ever the asker's own conv_id: the API passes household_identity.conv_id_for(the asker). Messages
are stored redacted (#268), so nothing here can show an account number or a code.

`platform` (#1287): the kind of device a message was said on, as the device paired (household_identity's
platform): mac, iphone, ipad, watch or tv; null when unknown (the master token, scripts, a store from before).
A conversation's is its device's, so the app's icon matches the name it shows ("Living room" is a TV).
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import Any

# 10-01 on Zach's real stream (two days, ~300 messages a day): a 45-minute pause gave 8 conversations
# with a median of 72 messages; 10 minutes gives 32 with a median of 14, about seven exchanges a topic.
GAP_S = 10 * 60
# What the apps add around a question that isn't the question: attachment blocks, the Mac's file lists,
# "Earlier messages: …" lines.
_NOISE = [re.compile(r"\[(?:Attached|Scanned)[^\]]*\]"), re.compile(r"\n\nI attached \d+ files?;.*", re.S),
          re.compile(r"^(?:Describe|Read) the (?:image|document) at \S+\.?", re.I)]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def _clean(text: str) -> str:
    t = str(text or "")
    for rx in _NOISE:
        t = rx.sub(" ", t)
    return " ".join(t.split())


def _title(rows: list[sqlite3.Row]) -> str:
    for r in rows:
        if r["role"] == "user":
            t = _clean(r["content"])
            if t:
                return t if len(t) <= 60 else t[:59].rsplit(" ", 1)[0] + "…"
    return "Attachment" if rows else "Conversation"


# household_identity's pairing platform → what a conversation says (#1287). "other" and anything else: unknown.
PLATFORMS = {"mac": "mac", "ios": "iphone", "ipados": "ipad", "watchos": "watch", "tvos": "tv"}


def platform_of(device: dict | None) -> str | None:
    """A paired device's platform as a conversation records it: mac, iphone, ipad, watch, tv, or None (no device: the
    master token, scripts; or a platform paired as "other")."""
    return PLATFORMS.get(str((device or {}).get("platform") or "").strip().lower())


def _rows(db: sqlite3.Connection, conv_id: str) -> list[sqlite3.Row]:
    db.row_factory = sqlite3.Row
    cols = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
    # Stores from before a column (thread/device 10-01, platform #1287) still list, with nulls.
    extra = "".join(f", {c}" if c in cols else f", NULL AS {c}" for c in ("thread", "device", "platform"))
    return db.execute(f"SELECT id, role, content, ts{extra} FROM messages WHERE conv_id=? ORDER BY ts, rowid",
                      (conv_id,)).fetchall()


def _platform(rs: list[sqlite3.Row], device: str | None) -> str | None:
    """A conversation's platform: the most common one among the messages from the device it shows, so the icon never
    belongs to another device than the name; None when that device has none (the master token's client tag)."""
    seen = Counter(r["platform"] for r in rs if device and r["device"] == device and r["platform"])
    return seen.most_common(1)[0][0] if seen else None


def _groups(rows: list[sqlite3.Row], gap_s: float = GAP_S) -> list[tuple[str, list[sqlite3.Row]]]:
    out: list[tuple[str, list[sqlite3.Row]]] = []
    for r in rows:
        if out:
            last = out[-1][1][-1]
            same = (r["thread"] == last["thread"]) if (r["thread"] and last["thread"]) else \
                (not r["thread"] and not last["thread"] and r["ts"] - last["ts"] < gap_s)
            if same:
                out[-1][1].append(r)
                continue
        out.append((str(r["thread"]) if r["thread"] else f"s-{r['id']}", [r]))
    # A thread the app comes back to later is one conversation, not two.
    merged: dict[str, list[sqlite3.Row]] = {}
    for gid, rs in out:
        merged.setdefault(gid, []).extend(rs)
    return list(merged.items())


def list_for(db: sqlite3.Connection, conv_id: str, limit: int = 50, gap_s: float = GAP_S) -> list[dict[str, Any]]:
    convs = []
    for gid, rs in _groups(_rows(db, conv_id), gap_s):
        replies = [r for r in rs if r["role"] == "assistant"]
        devices = Counter(r["device"] for r in rs if r["device"])
        device = devices.most_common(1)[0][0] if devices else None
        convs.append({"id": gid, "title": _title(rs), "started_at": _iso(rs[0]["ts"]), "last_at": _iso(rs[-1]["ts"]),
                      "count": len(rs), "preview": _clean(replies[-1]["content"])[:140] if replies else "",
                      "device": device, "platform": _platform(rs, device)})
    convs.sort(key=lambda c: c["last_at"], reverse=True)
    return convs[:max(1, min(int(limit), 200))]


SEARCH_MIN = 2
SEARCH_MAX = 100
SNIPPET = 140


def terms(query: str) -> list[str]:
    """The words a search has to find, lowercased (every one of them, anywhere in a message)."""
    return [t for t in str(query or "").lower().split() if t][:8]


def snippet(text: str, words: list[str], width: int = SNIPPET) -> str:
    """The cleaned message around its first match, cut at words, with … where it's cut."""
    t = _clean(text)
    low = t.lower()
    at = min((low.find(w) for w in words if low.find(w) >= 0), default=0)
    start = max(0, at - width // 3)
    end = min(len(t), start + width)
    if start > 0:
        sp = t.find(" ", start)
        start = sp + 1 if 0 <= sp < at else start
    if end < len(t):
        sp = t.rfind(" ", start, end)
        end = sp if sp > at else end
    return ("…" if start > 0 else "") + t[start:end] + ("…" if end < len(t) else "")


def search(db: sqlite3.Connection, conv_id: str, query: str, limit: int = 30, gap_s: float = GAP_S) -> list[dict[str, Any]]:
    """Messages in this person's own stream that hold every word of the query, newest first, at most `limit`. Only
    ever `conv_id`'s rows: the API passes the asker's own."""
    words = terms(query)
    if not words:
        return []
    hits = []
    for gid, rs in _groups(_rows(db, conv_id), gap_s):
        title, last = _title(rs), _iso(rs[-1]["ts"])
        for r in rs:
            text = _clean(r["content"]).lower()
            if text and all(w in text for w in words):
                hits.append({"conversation": gid, "title": title, "last_at": last, "role": r["role"],
                             "at": _iso(r["ts"]), "snippet": snippet(r["content"], words)})
    hits.sort(key=lambda h: h["at"], reverse=True)
    return hits[:max(1, min(int(limit), 100))]


def messages(db: sqlite3.Connection, conv_id: str, cid: str, before: str | None = None, limit: int = 100,
             gap_s: float = GAP_S, before_id: str | None = None) -> list[dict[str, Any]] | None:
    """A conversation's messages, oldest first (ts, then the order they were stored). `before`: only those strictly
    earlier than that at. With `before_id` too (#1290): only those ahead of that message, by position in this same
    order, so a message sharing its at isn't lost at a page boundary (ids are text, so comparing them wouldn't follow
    the order); when that id isn't in the conversation (deleted since), as with `before` alone."""
    for gid, rs in _groups(_rows(db, conv_id), gap_s):
        if gid != cid:
            continue
        out = [{"id": r["id"], "role": r["role"], "text": r["content"], "at": _iso(r["ts"]), "platform": r["platform"]}
               for r in rs]
        if before:
            at = next((i for i, m in enumerate(out) if before_id and m["id"] == before_id and m["at"] == before), None)
            out = out[:at] if at is not None else [m for m in out if m["at"] < before]
        return out[-max(1, min(int(limit), 500)):]
    return None
