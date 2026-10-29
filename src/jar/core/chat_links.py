#!/usr/bin/env python3
"""Chat history threads, step 1 (titan-node#1016): each past conversation is a linker item, and conversations that
belong together get an edge with its why.

Hooks come from what the person said and what the turn's tools touched, never the model's prose:
  - the person's own messages: ids, phones, amounts and mentioned dates (hooks.extract), the asker's world-model
    entities, and household members by name or alias (party:person);
  - the turn's tool targets (note_turn, kept from the turn's own calls): the reminder, todo, task, event, record or
    mail plan it touched, the Home Assistant device or area, a file's project, a repo, a site, an entity;
  - the kind facet: the tool groups the turn used (facet:kind). A facet describes, it never links.
Identifiers and names are keyed under the salt (hooks.keyed); links.db holds no text.

Edges (the chat_links table in links.db, apart from #951's `links`, so neither run drops the other's):
  lineage    a branch and the conversation it came from                          verified
  continues  the next conversation, within 30 min, sharing a hook                accepted
             the same, no shared hook, an ambiguous first message the local model
             (Qwen3.6 on :8198, local only) says continues                        candidate
  record     the same tool target (a record, a reminder, a device) within 72 h   accepted
  entity     the same person, entity or project within 72 h                      candidate
The person's own statement (link_statements, linker.say) outranks every one of them. Whether an edge type bundles
conversations in a view is the API's call, at ≥90% measured precision; until then the views show them as candidates.

Only ever a person's own stream: owner = the stream's person, audience private. A household device keeps no
history, so it has no items.

  python3 scripts/chat_links.py [--days 14] [--apply] [--ask-local]     counts only; --apply writes links.db
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from ops_ports import CODE_MODEL_URL  # noqa: E402

SOURCE = "conversation"
PREFIX = "chat-"
CONTINUE_S = 30 * 60
SHARED_S = 72 * 3600
LOCAL_URL = os.environ.get("CHAT_LINKS_LOCAL_URL", CODE_MODEL_URL)
# MEMORY_DB, else agent_paths.memory_db() (AGENT_MEMORY_DIR, else ~/.config/jellybean/memory): a sandbox box's own
# memory, never the live one (#1464; on titan neither is set, so it's the same file as before)
MEMORY_DB = Path(os.environ.get("MEMORY_DB") or str(__import__("agent_paths").memory_db()))
TURN_LOG = Path(os.environ.get("AGENT_TURN_LOG", "/ai/logs/agent-turns.jsonl"))
EDGE_TYPES = ("lineage", "continues", "record", "entity")
# Evidence a continuation can share: identifiers, parties and targets. A mentioned date or a facet alone never does.
_EVIDENCE = ("id:", "party:", "target:", "mention:entity")
_RECORD = ("id:", "target:")
_ANAPHOR = re.compile(r"(?i)^(?:and|also|what about|how about|ok(?:ay)?|so|then|it|that|this|those|they|them|same|"
                      r"again|now|still|but|or|instead|actually|wait)\b")
_ANAPHOR_WORDS = 6
# A reference the message doesn't resolve itself ("She never got it.", "Do the same for Sunday", "No, I meant almond
# milk"): someone or something named only in an earlier chat. Only opens the local model's yes/no question; never an
# edge on its own.
_UNRESOLVED = re.compile(r"(?i)\b(?:she|he|her|him|they|them|it|that|this|those|these|the same|same one|the other one|"
                         r"again|too|instead|as well)\b|^\s*(?:no,?\s+i meant|i meant|actually|and|also|what about|how about)\b")
# How far back the yes/no question reaches for an unresolved reference: 85% of new chats start within 2 h of the
# previous one (#1016, 14 days). A shared hook still continues only within CONTINUE_S.
REFERENCE_S = 2 * 3600
# The local question runs on a new chat's first turn: a phone's first reply can't wait on a busy :8198. Past this, or
# on any failure, it's "no continuation", logged on the turn as conv_continue: local_timeout so misses can be counted.
LOCAL_TIMEOUT_S = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_links (a TEXT NOT NULL, b TEXT NOT NULL, type TEXT NOT NULL, state TEXT NOT NULL,
    owner TEXT NOT NULL, typed TEXT NOT NULL, measured TEXT NOT NULL, why TEXT NOT NULL, made_at REAL NOT NULL,
    PRIMARY KEY (a, b));
CREATE INDEX IF NOT EXISTS idx_chat_links_owner ON chat_links(owner, type);
CREATE TABLE IF NOT EXISTS chat_turns (person TEXT NOT NULL, msg_id TEXT NOT NULL, thread TEXT, at REAL NOT NULL,
    groups TEXT NOT NULL, targets TEXT NOT NULL, PRIMARY KEY (person, msg_id));
"""


def item_id(conversation: str) -> str:
    """A conversation's linker item id: "chat-" + its id in /v1/conversations."""
    return PREFIX + str(conversation)


def ensure(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


# ── what a turn's tools touched ──────────────────────────────────────────────────────────────────

def tool_groups() -> dict[str, str]:
    """tool name → its group (calendar, home, tasks, later, web, files…), from the agent's own TOOL_GROUPS."""
    try:
        import agent_runtime_lib as L
        return {t: g for g, (_w, _s, tools) in L.TOOL_GROUPS.items() for t in tools}
    except Exception:  # noqa: BLE001 - without the agent's table, no facet
        return {}


def kinds(names: Iterable[str], groups: dict[str, str] | None = None) -> set[str]:
    g = tool_groups() if groups is None else groups
    return {g[n] for n in names if n in g}


_ID_ARGS = {"reminder_id": "reminder", "event_id": "event", "task_id": "task", "record_id": "record", "todo": "todo",
            "id": "scheduled", "plan_id": "mail-plan", "preview_id": "mail-plan", "job_id": "job"}
_TITLED = {"add_todo": "todo", "complete_todo": "todo", "create_reminder": "reminder", "update_reminder": "reminder",
           "complete_reminder": "reminder", "delete_reminder": "reminder", "create_calendar_event": "event",
           "update_calendar_event": "event", "delete_calendar_event": "event"}
_SKIP_FOR = {"", "me", "household", "claude", "everyone", "all"}


def _norm(v: Any) -> str:
    return " ".join(str(v or "").lower().split())[:120]


def project_of(path: str) -> str | None:
    """A file path's project: the folder under Projects/ (or ~/Code, ~/src), else None. Never the file itself."""
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p and p != "~"]
    for i, p in enumerate(parts[:-1]):
        if p.lower() in ("projects", "code", "src", "repos", "dev"):
            return _norm(parts[i + 1]) or None
    return None


def targets(name: str, args: dict[str, Any]) -> list[tuple[str, str]]:
    """What one tool call touched, as (kind, keyed value): never its text, only which thing."""
    import hooks
    a = args or {}
    out: list[tuple[str, str]] = []
    for arg, what in _ID_ARGS.items():
        if arg in a and _norm(a[arg]):
            out.append(("target:record", hooks.keyed("id", f"{what}:{_norm(a[arg])}")))
    if name in _TITLED:
        for arg in ("title", "new_title"):
            if _norm(a.get(arg)):
                out.append(("target:record", hooks.keyed("id", f"{_TITLED[name]}:{_norm(a[arg])}")))
    if name in ("home_control", "home_status", "home_why"):
        for arg in ("device", "entity_id", "area", "name"):
            if _norm(a.get(arg)):
                out.append(("target:home", hooks.keyed("ha", _norm(a[arg]))))
    for arg in ("path", "paths"):
        for p in (a.get(arg) if isinstance(a.get(arg), list) else [a.get(arg)]):
            proj = project_of(str(p or ""))
            if proj:
                out.append(("party:project", hooks.keyed("project", proj)))
    if _norm(a.get("repo")):
        out.append(("party:project", hooks.keyed("project", _norm(a["repo"]))))
    if _norm(a.get("url")):
        host = urllib.parse.urlparse(str(a["url"])).hostname or ""
        if host:
            out.append(("party:site", hooks.keyed("site", hooks.registrable(host.lower()))))
    if name in ("about", "kb_relations", "set_fact") and _norm(a.get("entity")):
        out.append(("party:entity", hooks.keyed("entity", _norm(a["entity"]))))
    for arg in ("person", "for"):
        if name in ("where_is", "add_todo", "list_todos", "log_event", "create_calendar_event") and \
                _norm(a.get(arg)) not in _SKIP_FOR and a.get(arg):
            out.append(("party:person", hooks.keyed("person", _norm(a[arg]).split(" ")[0])))
    return list(dict.fromkeys(out))


def note_turn(person: str, msg_id: Any, thread: str | None, at: float, tool_names: Iterable[str],
              tool_targets: Iterable[Iterable[str]], con: sqlite3.Connection | None = None) -> None:
    """Keep one turn's tool groups and keyed targets beside its question's message id (no text, no arguments)."""
    import hooks
    con = con or hooks.connect()
    ensure(con)
    con.execute("INSERT OR REPLACE INTO chat_turns VALUES (?,?,?,?,?,?)",
                (str(person).lower(), str(msg_id), thread or None, float(at), json.dumps(sorted(kinds(tool_names or []))),
                 json.dumps(sorted({tuple(t)[:2] for t in (tool_targets or []) if len(tuple(t)) >= 2}))))
    con.commit()


# ── conversations as items ───────────────────────────────────────────────────────────────────────

@dataclass
class Conv:
    item_id: str
    owner: str
    start: float
    end: float
    keys: set[tuple[str, str]] = field(default_factory=set)
    groups: set[str] = field(default_factory=set)
    first: str = ""          # the person's first message here (for the local question only; never stored)
    last: str = ""           # the person's last message here (the same)
    parent: str | None = None   # the conversation it branched from (spaces.lineage), as an item id
    reply: str = ""          # titan's last reply here: context for the local question only; never a hook

    def evidence(self) -> set[tuple[str, str]]:
        return {k for k in self.keys if k[0].startswith(_EVIDENCE) and k[0] != "party:to"}   # a recipient is no evidence (#1399)


def members_finder(person_ids: list[dict[str, Any]]) -> Callable[[str], set[str]]:
    """Household members a text names, by first name or alias (word-bounded), as their person ids."""
    pats = []
    for p in person_ids:
        words = {str(p.get("name") or "").split(" ")[0].lower(), *(str(a).lower() for a in p.get("aliases") or [])} - {""}
        if words:
            pats.append((str(p.get("id")), re.compile(r"(?i)\b(?:" + "|".join(map(re.escape, sorted(words))) + r")\b")))
    return lambda text: {pid for pid, rx in pats if rx.search(text or "")}


def hooks_for(user_texts: list[str], day: str, entities: Any = None,
              members: Callable[[str], set[str]] | None = None) -> set[tuple[str, str]]:
    """A conversation's hooks from the person's own messages only."""
    import hooks
    text = "\n".join(user_texts)
    keys = {(h.kind, h.key) for h in hooks.extract({"text": text, "at": day}, None)
            if h.kind.startswith("id:") or h.kind in ("party:phone", "amount", "date:mention")}
    if entities is not None:
        try:
            keys |= {("party:entity", str(e)) for e in (entities(text) or [])}
        except Exception:  # noqa: BLE001 - no world model: no entity hooks
            pass
    if members is not None:
        keys |= {("party:person", hooks.keyed("person", pid)) for pid in members(text)}
    return keys


def load_convs(mem: sqlite3.Connection, conv_id: str, owner: str, since: float, entities: Any = None,
               members: Callable[[str], set[str]] | None = None, turns: list[dict[str, Any]] | None = None,
               noted: dict[str, dict[str, Any]] | None = None, lineage: dict[str, Any] | None = None,
               groups: dict[str, str] | None = None) -> list[Conv]:
    """One person's conversations since `since`, oldest first, with their hooks. turns: the turn log's rows for this
    person (tool names, joined by time: the backfill); noted: chat_turns rows by message id (names and targets)."""
    import conversations as C
    out = []
    for gid, rs in C._groups(C._rows(mem, conv_id)):
        if rs[-1]["ts"] < since:
            continue
        users = [r for r in rs if r["role"] == "user"]
        texts = [C._clean(r["content"]) for r in users]
        day = datetime.fromtimestamp(rs[0]["ts"], timezone.utc).date().isoformat()
        c = Conv(item_id(gid), owner, float(rs[0]["ts"]), float(rs[-1]["ts"]),
                 hooks_for(texts, day, entities, members), set(), texts[0] if texts else "", texts[-1] if texts else "",
                 reply=next((C._clean(r["content"]) for r in reversed(rs) if r["role"] == "assistant"), ""))
        for r in users:
            n = (noted or {}).get(str(r["id"]))
            if n:
                c.groups |= set(json.loads(n["groups"] or "[]"))
                c.keys |= {tuple(t) for t in json.loads(n["targets"] or "[]")}
        if not c.groups and turns:
            c.groups = kinds((n for t in turns if c.start - 5 <= t["ts"] <= c.end + 5 for n in t.get("tool_names") or []),
                             groups)
        c.keys |= {("facet:kind", g) for g in c.groups}
        par = (lineage or {}).get(gid)
        if par and par.get("thread_id"):
            c.parent = item_id(par["thread_id"])
        out.append(c)
    return sorted(out, key=lambda c: c.start)


# ── edges ────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Edge:
    a: str
    b: str
    type: str
    state: str
    owner: str
    typed: list[str]
    measured: dict[str, Any]
    why: str


_WORDS = {"party:entity": "named", "party:person": "person", "party:phone": "phone number", "party:project": "project",
          "party:site": "site", "target:record": "record touched", "target:home": "home device",
          "mention:entity": "named"}


def _words(keys: Iterable[tuple[str, str]]) -> list[str]:
    return sorted({"same " + _WORDS.get(k, k.split(":", 1)[-1].replace("_", " ")) for k, _ in keys})


def unresolved(text: str) -> bool:
    """Whether a message leans on something it doesn't name ("She never got it.", "Do the same for Sunday")."""
    return bool(_UNRESOLVED.search(str(text or "")))


def ambiguous(c: Conv) -> bool:
    """A first message that leans on what came before ("and the other one?", "she never got it") and names nothing of
    its own."""
    first = c.first.strip()
    return bool(first) and not c.evidence() and (len(first.split()) <= _ANAPHOR_WORDS or bool(_ANAPHOR.match(first))
                                                 or unresolved(first))


def ask_local(prev: Conv, nxt: Conv, url: str = LOCAL_URL, post: Any = None) -> bool | None:
    """Does nxt's first message continue prev? Asked of the local model only (never a cloud model): yes/no, or None."""
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the continuation question is only ever asked locally")
    import typed_local
    mins = max(0, round((nxt.start - prev.end) / 60))
    prompt = ("A person is starting a new chat with their assistant. Does their new message refer back to the earlier "
              "chat (someone or something only named there, or 'do the same', 'again', a correction), or is it "
              "self-contained or about something new?\n\n"
              f"Earlier chat, ended {mins} min before:\nPerson: {prev.last[:600]}\n"
              + (f"Assistant: {prev.reply[:400]}\n" if prev.reply else "")
              + f"\nNew message: {nxt.first[:600]}\n\n"
              'Answer {"continues": "yes"} if it refers back, or {"continues": "no"}.')
    try:
        got, _ = typed_local.ask(prompt, typed_local.obj({"continues": typed_local.enum(("yes", "no"))}, ["continues"]),
                                 url, max_tokens=20, timeout=LOCAL_TIMEOUT_S, retries=0, post=post)
    except Exception:  # noqa: BLE001 - slow, busy or down: no answer, no edge, and the miss is counted
        _local_timeout()
        return None
    return None if not got else got.get("continues") == "yes"


def _local_timeout() -> None:
    """Mark the turn (if this runs inside one) conv_continue: local_timeout, for the turn log; never raises."""
    print("[chat_links] conv_continue: local_timeout", file=sys.stderr)
    lib = sys.modules.get("agent_runtime_lib")
    io = getattr(lib, "_TURN_IO", None) if lib else None
    if io is not None:
        try:
            io.conv_continue = "local_timeout"
        except Exception:  # noqa: BLE001
            pass


def score(convs: list[Conv], said: dict[tuple[str, str], bool] | None = None,
          ask: Callable[[Conv, Conv], bool | None] | None = None) -> list[Edge]:
    """Every edge between one owner's conversations, with its type, state and why. Never across owners."""
    import linker
    said = said or {}
    by_key = Counter(k for c in convs for k in c.evidence())
    cap = linker.demote_at(len(convs))
    out: dict[tuple[str, str], Edge] = {}

    def put(a: Conv, b: Conv, typ: str, state: str, typed: list[str], measured: dict[str, Any]) -> None:
        pair = linker._pair(a.item_id, b.item_id)
        if pair in out and EDGE_TYPES.index(out[pair].type) <= EDGE_TYPES.index(typ):
            return
        stmt = said.get(pair)
        st = state if stmt is None else ("verified" if stmt else "rejected")
        bits = ([("you said they're related" if stmt else "you said they're not related")] if stmt is not None else []) + \
            [f"{t} (typed)" for t in typed] + [_measured_words(measured)]
        out[pair] = Edge(pair[0], pair[1], typ, st, a.owner, typed, measured, "; ".join(b for b in bits if b))

    owners: dict[str, list[Conv]] = {}
    for c in convs:
        owners.setdefault(c.owner, []).append(c)
    for owner, cs in owners.items():
        cs = sorted(cs, key=lambda c: c.start)
        ids = {c.item_id: c for c in cs}
        for c in cs:
            if c.parent and c.parent in ids:
                put(ids[c.parent], c, "lineage", "verified", ["branched from it"], {"earlier": c.parent})
        for prev, nxt in zip(cs, cs[1:]):
            gap = max(0.0, nxt.start - prev.end)
            if gap > REFERENCE_S:
                continue
            shared = {k for k in prev.evidence() & nxt.evidence() if by_key[k] <= cap}
            m = {"minutes_apart": round(gap / 60, 1), "earlier": prev.item_id}
            if shared and gap <= CONTINUE_S:
                put(prev, nxt, "continues", "accepted", _words(shared), m)
            elif not shared and ask is not None and ambiguous(nxt):
                # Zach's decision 1 (10-05): code plus the local model's yes/no for an ambiguous first message. Marked
                # "local" so the labelling pass measures it apart from the code's own edges.
                if ask(prev, nxt):
                    put(prev, nxt, "continues", "accepted", [], {**m, "local": "yes"})
        for a, b in itertools.combinations(cs, 2):
            if b.start - a.end > SHARED_S or a.start - b.end > SHARED_S:
                continue
            shared = {k for k in a.evidence() & b.evidence() if by_key[k] <= cap}
            rec = {k for k in shared if k[0].startswith(_RECORD)}
            m = {"hours_apart": round(max(0.0, max(a.start, b.start) - min(a.end, b.end)) / 3600, 1)}
            if rec:
                put(a, b, "record", "accepted", _words(rec), m)
            elif shared:
                put(a, b, "entity", "candidate", _words(shared), m)
    for (a, b), rel in said.items():   # a statement on a pair the code didn't link still stands
        if (a, b) not in out and a.startswith(PREFIX) and b.startswith(PREFIX):
            ca, cb = next((c for c in convs if c.item_id == a), None), next((c for c in convs if c.item_id == b), None)
            if ca and cb and ca.owner == cb.owner:
                put(ca, cb, "continues", "candidate", [], {})
    return list(out.values())


def _measured_words(m: dict[str, Any]) -> str:
    if "minutes_apart" in m:
        s = f"{m['minutes_apart']:g} min later (measured)"
        return s + ("; the local model read it as a continuation" if m.get("local") == "yes" else "")
    if "hours_apart" in m:
        return f"{m['hours_apart']:g} h apart (measured)"
    return ""


def save(con: sqlite3.Connection, edges: Iterable[Edge]) -> dict[str, int]:
    """Write the chat edges; a changed state goes in link_log with its why. → counts by type/state."""
    import linker
    ensure(con)
    con.executescript(linker.SCHEMA)
    was = {(a, b): s for a, b, s in con.execute("SELECT a, b, state FROM chat_links")}
    now, counts, keep = time.time(), Counter(), set()
    for e in edges:
        counts[f"{e.type}/{e.state}"] += 1
        keep.add((e.a, e.b))
        if was.get((e.a, e.b)) != e.state:
            con.execute("INSERT INTO link_log VALUES (?,?,?,?,?,?)", (e.a, e.b, was.get((e.a, e.b)), e.state, e.why, now))
        con.execute("INSERT OR REPLACE INTO chat_links VALUES (?,?,?,?,?,?,?,?,?)",
                    (e.a, e.b, e.type, e.state, e.owner, json.dumps(e.typed), json.dumps(e.measured), e.why, now))
    for (a, b), s in was.items():
        if (a, b) not in keep:
            con.execute("INSERT INTO link_log VALUES (?,?,?,?,?,?)", (a, b, s, "gone", "no longer supported", now))
            con.execute("DELETE FROM chat_links WHERE a=? AND b=?", (a, b))
    con.commit()
    return dict(counts)


def index_items(con: sqlite3.Connection, convs: list[Conv], replace_since: float) -> int:
    """The conversations' hooks into links.db (source "conversation", private to their owner). Items of this
    window that are gone (a forgotten chat) leave."""
    import hooks
    keep = {c.item_id for c in convs}
    for (iid,) in con.execute("SELECT DISTINCT item_id FROM hooks WHERE source=?", (SOURCE,)).fetchall():
        if iid not in keep:
            con.execute("DELETE FROM hooks WHERE item_id=?", (iid,))
    n = 0
    for c in convs:
        day = datetime.fromtimestamp(c.start, timezone.utc).date().isoformat()
        hs = [hooks.Hook(k, v, day, {"part": "conversation"}) for k, v in sorted(c.keys)]
        n += hooks.index(con, c.item_id, SOURCE, hs, c.owner, "private")
    con.commit()
    return n


# ── the read the carry-over uses (#1016 step 4) ──────────────────────────────────────────────────

_RANK = {"verified": 0, "accepted": 1, "candidate": 2}


def continues_from(owner: str, conversation_id: str, first_message: str | None = None, *, now: float | None = None,
                   con: sqlite3.Connection | None = None, mem: sqlite3.Connection | None = None,
                   entities: Any = None, members: Callable[[str], set[str]] | None = None,
                   ask: Callable[[Conv, Conv], bool | None] | None = None) -> dict[str, Any] | None:
    """The earlier conversation this one continues: {"id", "state", "why"}, or None. Only `owner`'s own.
    A stored lineage or continues edge first (a rejected one is no edge). With none and the chat's first message in
    hand (its first turn: no edge can exist yet), the live check over the owner's latest other conversation:
      - it ended at most 30 min ago and shares a hook with that message (the person's own words) → accepted;
      - no shared hook, it ended at most 2 h ago, and the message leans on something it doesn't name ("She never got
        it.") → the local model's yes/no (ask, default ask_local: :8198, local only) → accepted on a yes."""
    import hooks
    import household_identity as hh
    con = con or hooks.connect()
    ensure(con)
    me = item_id(conversation_id)
    best = None
    for typ, state, measured, why in con.execute(
            "SELECT type, state, measured, why FROM chat_links WHERE owner=? AND (a=? OR b=?) AND type IN "
            "('lineage', 'continues') AND state != 'rejected'", (str(owner), me, me)):
        earlier = str(json.loads(measured or "{}").get("earlier") or "")
        if earlier and earlier != me and (best is None or (typ != "lineage", _RANK.get(state, 9)) < best[0]):
            best = ((typ != "lineage", _RANK.get(state, 9)), {"id": earlier[len(PREFIX):], "state": state, "why": why})
    if best:
        return best[1]
    if not str(first_message or "").strip():
        return None
    now = time.time() if now is None else now
    mem = mem or sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)
    noted = {r[1]: {"groups": r[4], "targets": r[5]} for r in con.execute(
        "SELECT * FROM chat_turns WHERE person=? AND at >= ?", (str(owner).lower(), now - 86400))}
    if entities is None:
        entities = hooks.entity_finder(owner, None)
    if members is None:
        members = members_finder([p for p in _people() if str(p.get("id")) != str(owner)])
    prior = [c for c in load_convs(mem, hh.conv_id_for(owner), str(owner), now - REFERENCE_S, entities, members,
                                   None, noted) if c.item_id != me and c.end <= now]
    if not prior:
        return None
    prev = prior[-1]
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    new = Conv(me, str(owner), now, now, hooks_for([str(first_message)], day, entities, members))
    new.first = str(first_message)
    shared = prev.evidence() & new.evidence()
    gap = now - prev.end
    if shared and gap <= CONTINUE_S:
        return {"id": prev.item_id[len(PREFIX):], "state": "accepted",
                "why": "; ".join([f"{t} (typed)" for t in _words(shared)] + [_measured_words(
                    {"minutes_apart": round(gap / 60, 1)})])}
    if not shared and gap <= REFERENCE_S and ambiguous(new) and (ask or ask_local)(prev, new):
        return {"id": prev.item_id[len(PREFIX):], "state": "accepted", "local": True,
                "why": _measured_words({"minutes_apart": round(gap / 60, 1), "local": "yes"})}
    return None


# ── the run ──────────────────────────────────────────────────────────────────────────────────────

def _people() -> list[dict[str, Any]]:
    try:
        import household_identity as hh
        return [p for p in hh.get_store().people() if p.get("role") != "household"]
    except Exception:  # noqa: BLE001
        return []


def _turn_rows(since: float) -> dict[str, list[dict[str, Any]]]:
    import household_identity as hh
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        with open(TURN_LOG) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", 0) >= since - 3600 and r.get("source") == "user":
                    out.setdefault(str(r.get("person") or hh.ADMIN_PERSON_ID), []).append(r)
    except OSError:
        pass
    return out


def run(days: float = 14, apply: bool = False, ask_locally: bool = False, con: sqlite3.Connection | None = None,
        mem: sqlite3.Connection | None = None) -> dict[str, Any]:
    import hooks
    import household_identity as hh
    import linker
    import spaces
    since = time.time() - days * 86400
    con = con or hooks.connect()
    ensure(con)
    mem = mem or sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)
    people = _people()
    members = members_finder(people)
    turns = _turn_rows(since)
    noted = {r[1]: {"groups": r[4], "targets": r[5]} for r in con.execute("SELECT * FROM chat_turns WHERE at >= ?",
                                                                            (since - 3600,))}
    groups = tool_groups()
    convs: list[Conv] = []
    for p in people:
        pid = str(p.get("id"))
        try:
            lin = spaces.lineage_for(pid)
        except Exception:  # noqa: BLE001
            lin = {}
        convs += load_convs(mem, hh.conv_id_for(pid), pid, since, hooks.entity_finder(pid, p.get("role")), members,
                            turns.get(pid), noted, lin, groups)
    asked: Counter = Counter()

    def ask(prev: Conv, nxt: Conv) -> bool | None:
        yes = ask_local(prev, nxt)
        asked["yes" if yes else ("no" if yes is False else "no answer")] += 1
        return yes
    edges = score(convs, linker.statements(con), ask if ask_locally else None)
    counts = save(con, edges) if apply else dict(Counter(f"{e.type}/{e.state}" for e in edges))
    filed: dict[str, int] = {}
    if apply:
        index_items(con, convs, since)
        try:   # #1087: a verified link files a chat on its own (one undo token each, shown in Now)
            import file_suggest
            filed = file_suggest.run_all(apply=True, con=con)
        except Exception as exc:  # noqa: BLE001 - linking never fails over filing
            filed = {"error": type(exc).__name__}
    adjacent = []
    for owner in {c.owner for c in convs}:
        mine = sorted((c for c in convs if c.owner == owner), key=lambda c: c.start)
        adjacent += list(zip(mine, mine[1:]))
    within = [(a, b) for a, b in adjacent if b.start - a.end <= CONTINUE_S]
    cont = {(e.a, e.b) for e in edges if e.type == "continues"}
    return {"apply": apply, "days": days, "conversations": len(convs),
            "with_evidence": sum(1 for c in convs if c.evidence()),
            "hook_kinds": dict(Counter(k for c in convs for k, _ in c.keys)),
            "edges": dict(sorted(counts.items())),
            "adjacent_pairs": len(adjacent), "within_30_min": len(within),
            "continues_of_adjacent": sum(1 for a, b in adjacent if linker._pair(a.item_id, b.item_id) in cont),
            "ambiguous_in_window": sum(1 for a, b in within if ambiguous(b)),
            **({"local_answers": dict(asked)} if ask_locally else {}), **({"auto_filed": filed} if apply else {})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Conversations as linker items (#1016 step 1); counts only")
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--ask-local", action="store_true", help="ask the local model about ambiguous first messages")
    a = ap.parse_args(argv)
    print(json.dumps(run(a.days, a.apply, a.ask_local), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
