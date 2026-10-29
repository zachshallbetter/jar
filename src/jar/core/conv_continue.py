#!/usr/bin/env python3
"""Context carry-over between chats (titan-node#1016 step 5; CONV_CONTINUE, off unless exactly "1").

Starting a new chat is right (short contexts are cheaper and carry fewer stale assumptions), but a follow-up such as
"she never got it" or "do the same for Sunday" only makes sense with the chat before it. When a new chat (no history
yet) continues the asker's own recent one, the turn gets a short block, wrapped as untrusted "chats" text (#473): the
earlier chat's name, its last exchange and what its tools did (the trace phrases, names only).

Which chat it continues: KB's `continues` edge (linker.continues_from, #1016 steps 1-2) when it exists, verified or
accepted only; until then a stand-in: the asker's own previous chat ended at most WINDOW_S ago.

Never another person's chat, never on a kid's turn, a shared screen or a temporary chat (the caller checks the
role and source; `previous` checks the owner). Nothing is stored here.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

WINDOW_S = 30 * 60
LAST_CHARS = 500          # each side of the last exchange
NAME_CHARS = 80
EDGE_STATES = ("verified", "accepted")

HEAD = ("This new chat may continue the person's own previous chat, shown below as data. Use it only to work out "
        "what a reference like \"she\", \"it\", \"that\" or \"the same\" means. If the new message stands on its own, "
        "ignore it and don't bring it up.")


def on() -> bool:
    return os.environ.get("CONV_CONTINUE", "") == "1"


def _edge(owner: str, current: str | None, first_message: str | None = None) -> str | None:
    """KB's continues edge for the current conversation: the earlier one's id, verified or accepted only. On a new
    chat's first turn no edge is stored yet, so its first message goes along and KB's live check runs (the owner's
    latest other chat ended at most 30 min ago and shares a hook with it)."""
    if not current and not str(first_message or "").strip():
        return None
    try:
        import linker
        fn = getattr(linker, "continues_from", None)
        got = fn(owner, current or "new-chat", first_message) if fn else None
    except Exception:  # noqa: BLE001 - the edge is a hint; the stand-in still works
        return None
    if isinstance(got, dict) and got.get("state") in EDGE_STATES and got.get("id"):
        return str(got["id"])
    return None


def previous(chats: list[dict[str, Any]], asker: str, now: float | None = None, current: str | None = None,
             edge: str | None = None, standin: bool = True) -> dict[str, Any] | None:
    """The chat this new one continues, or None. `chats`: [{id, owner, name, last_at (epoch s), messages:
    [{role, text}], tools: [names]}]. Only the asker's own: any other owner is never a candidate. The edge's
    chat wins when it's among them; otherwise the newest own chat that ended within WINDOW_S and isn't this one."""
    now = time.time() if now is None else now
    own = [c for c in chats if isinstance(c, dict) and str(c.get("owner") or "") == str(asker or "") and asker
           and c.get("id") != current and c.get("messages")]
    if edge:
        hit = next((c for c in own if c.get("id") == edge), None)
        if hit:
            return hit
    if not standin:
        return None
    recent = [c for c in own if 0 <= now - float(c.get("last_at") or 0) <= WINDOW_S]
    return max(recent, key=lambda c: float(c.get("last_at") or 0)) if recent else None


def _clip(text: Any, n: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…"


def block_text(chat: dict[str, Any], now: float | None = None, trace=None) -> str:
    """The block's body (wrapped as untrusted by the caller): name, how long ago, the last exchange, what its
    tools did. `trace(names)` turns tool names into phrases (agent_runtime_lib.turn_trace)."""
    now = time.time() if now is None else now
    msgs = [m for m in chat.get("messages") or [] if isinstance(m, dict) and str(m.get("text") or "").strip()]
    last_user = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
    last_reply = next((m for m in reversed(msgs) if m.get("role") == "assistant"), None)
    mins = max(0, int((now - float(chat.get("last_at") or now)) // 60))
    lines = [f"Previous chat: {_clip(chat.get('name') or 'Conversation', NAME_CHARS)} (ended {mins} min ago)"]
    if last_user:
        lines.append(f"They said: {_clip(last_user['text'], LAST_CHARS)}")
    if last_reply:
        lines.append(f"titan replied: {_clip(last_reply['text'], LAST_CHARS)}")
    did = trace(chat.get("tools") or []) if trace else None
    if did:
        lines.append(f"What it did: {did}")
    return "\n".join(lines)


def live_chats(db, conv_id: str, owner: str, limit: int = 3) -> list[dict[str, Any]]:
    """The asker's own most recent chats from their stream (conversations.py's grouping), newest first. `conv_id`
    is household_identity.conv_id_for(owner): nobody else's stream is ever read."""
    import conversations as cv
    rows = cv._rows(db, conv_id)
    cols = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
    tools_by_id: dict[str, list[str]] = {}
    if "tools" in cols:
        ids = [r["id"] for r in rows[-200:]]
        for mid, raw in db.execute(f"SELECT id, tools FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids) \
                if ids else []:
            try:
                tools_by_id[mid] = [str(x) for x in json.loads(raw or "[]")][:12]
            except ValueError:
                pass
    out = []
    for gid, rs in cv._groups(rows[-200:]):
        tools: list[str] = []
        for r in rs:
            tools += tools_by_id.get(r["id"], [])
        out.append({"id": gid, "owner": owner, "name": cv._title(rs), "last_at": float(rs[-1]["ts"]),
                    "messages": [{"role": r["role"], "text": r["content"]} for r in rs[-6:]], "tools": tools[-12:]})
    out.sort(key=lambda c: c["last_at"], reverse=True)
    return out[:limit]


def eval_chats(prior: Any, asker: str, now: float | None = None) -> list[dict[str, Any]]:
    """An eval turn's `eval_prior` (the carry-over bank's previous chat) as chats: owner "self" is the asker, any
    other owner ("other", "kid") is someone else, so the privacy cases go through the same `previous` check."""
    now = time.time() if now is None else now
    out = []
    for i, p in enumerate(prior if isinstance(prior, list) else [prior] if isinstance(prior, dict) else []):
        if not isinstance(p, dict):
            continue
        owner = asker if p.get("owner") == "self" else f"eval-{str(p.get('owner') or 'other')[:20]}"
        msgs = [{"role": "assistant" if m.get("role") == "assistant" else "user", "text": str(m.get("text") or "")[:2000]}
                for m in (p.get("messages") or [])[:12] if isinstance(m, dict)]
        tools = [str(t.get("name") if isinstance(t, dict) else t)[:60] for t in (p.get("tools") or [])[:12]]
        gap = float(p.get("gap_min") or 0) * 60
        out.append({"id": f"eval-prior-{i}", "owner": owner, "name": str(p.get("name") or "")[:NAME_CHARS],
                    "last_at": now - gap, "messages": msgs, "tools": tools})
    return out


def eval_edge(chats: list[dict[str, Any]], asker: str, first_message: str, now: float | None = None) -> str | None:
    """KB's live continues check (chat_links.continues_from) over an eval turn's own previous chats, in throwaway
    in-memory stores: no stored edge, nobody's real stream. The asker's own chats only. Returns the chat's id."""
    import sqlite3

    import chat_links
    import household_identity as hh
    now = time.time() if now is None else now
    mem = sqlite3.connect(":memory:")
    mem.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, ts REAL, thread TEXT, "
                "device TEXT, client_msg_id TEXT, tools TEXT)")
    conv = hh.conv_id_for(asker)
    for c in chats:
        if str(c.get("owner") or "") != str(asker or "") or not asker:
            continue
        msgs = c.get("messages") or []
        end = float(c.get("last_at") or now)
        for i, m in enumerate(msgs):
            mem.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
                        (f"{c['id']}-{i}", conv, m.get("role") or "user", str(m.get("text") or ""),
                         end - (len(msgs) - 1 - i) * 5, c["id"], None, None,
                         json.dumps(c.get("tools") or []) if m.get("role") == "assistant" else None))
    got = chat_links.continues_from(asker, "eval-new-chat", first_message, now=now, con=sqlite3.connect(":memory:"), mem=mem)
    return str(got["id"]) if isinstance(got, dict) and got.get("state") in EDGE_STATES and got.get("id") else None
