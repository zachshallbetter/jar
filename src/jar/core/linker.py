#!/usr/bin/env python3
"""The linker (titan-node#951 (b)): items that belong together, settled in code from their hooks (hooks.py).

Zach, 10-05: threading is automatic and general; the association is the idea. Two kinds of evidence, scored apart
(rule 5):
  typed     explicit and checkable: the same identifier (order, case, tracking … the key ignores the label), the same
            mail thread (Message-ID / References), a record filed from that mail, the same party (sender domain,
            address, phone or short code, vendor, world entity);
  measured  normalised against this house's own items: how rare a shared amount is among every amount titan has, and
            whether one item mentions the other's date.

Each edge has a state and a one-line why, templated from the evidence, never model prose (rule 2):
  verified   the person said they're related; a record filed from the mail; the same mail thread in one owner's mail
             (across owners a forwarded mail carries someone else's References: at most accepted)
  accepted   the same identifier; or the same party and a shared amount rarer than AMOUNT_FLOOR of the house's
             amounts (a mentioned date alone never accepts: it only raises cohesion)
  candidate  the same party within CANDIDATE_DAYS and nothing more; offered as "possibly related", never stated
  rejected   the person said they're not related: it outranks everything here and stays in the history
A key in more than demote_at(n) items (10% of the house's items, at least 8, at most DEMOTE: a support line, a
no-reply domain, a store every order comes from) is too common to identify anything: as an identifier it counts as a
party, as a party it links nothing.

Threads are the connected components of verified and accepted edges, computed per viewer over the items that viewer
may see, so an item someone can't open never joins two they can (rule 8: threads are views, never stored). Kids and
shared screens get none. A thread shows a machine description until it earns a name (rule 1).

  python3 scripts/linker.py            dry run: edges by state, threads by size (counts only)
  python3 scripts/linker.py --apply    write the edges to links.db
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
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEMOTE = 25                  # at most; relative to the corpus: 10% of items, at least 8 (demote_at)
AMOUNT_FLOOR = 0.95          # a shared amount rarer than 95% of the house's amounts
WINDOW_DAYS = 60             # typed party + measured evidence within this many days
CANDIDATE_DAYS = 14          # the same party alone, this close: a candidate
COHESION_FLOOR = 0.5
RARE_PHRASE = 0.05           # a subject phrase names a thread only if at most 5% of the house's titles have it
STATES = ("verified", "accepted", "candidate", "rejected")
SOURCES = ("baseline", "correction", "judge")   # what a statement came from ("judge": the local model, #1016)
CLIENT_SOURCES = ("baseline", "correction")    # what a person's app may send (never "judge")
_KIND_WORDS = {"id:order": "order number", "id:case": "case number", "id:ticket": "ticket number",
               "id:confirmation": "confirmation number", "id:reference": "reference", "id:claim": "claim number",
               "id:request": "request number", "id:rma": "RMA", "id:invoice": "invoice number",
               "id:booking": "booking", "id:reservation": "reservation", "id:account": "account",
               "id:tracking": "tracking number", "id:trade-in": "trade-in number", "id:return": "return number",
               "id:policy": "policy number", "id:member": "member number", "id:transaction": "transaction",
               "id:receipt": "receipt number", "id:imei": "device (IMEI)", "id:message": "mail thread",
               "id:source": "filed from this mail", "party:domain": "sender", "party:addr": "sender address",
               "party:phone": "phone number", "party:vendor": "vendor", "party:entity": "named"}
PARTY_KINDS = ("party:domain", "party:addr", "party:phone", "party:vendor", "party:entity")
# Hooks the graph reads but the linker never links by (#1399): recipients are mostly the household's own addresses,
# which are on nearly every mail, so a shared recipient is no evidence that two mails are about the same thing.
NOT_LINKING = frozenset({"party:to"})
# Account-level numbers name an account, not a matter: one member number joined a bank's alerts, statements and declines
# into one thread (10-05, titan). They link like a party: only with measured evidence beside them.
ACCOUNT_IDS = frozenset({"id:account", "id:member", "id:policy"})
MIN_AMOUNT = 1.00            # a shared amount under a dollar is noise ("$0.14" in promotions, "$0.01" fees), never evidence

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (a TEXT NOT NULL, b TEXT NOT NULL, state TEXT NOT NULL, typed TEXT NOT NULL,
    measured TEXT NOT NULL, why TEXT NOT NULL, made_at REAL NOT NULL, PRIMARY KEY (a, b));
CREATE TABLE IF NOT EXISTS link_statements (a TEXT NOT NULL, b TEXT NOT NULL, related INTEGER NOT NULL,
    by TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS link_log (a TEXT NOT NULL, b TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
    why TEXT NOT NULL, at REAL NOT NULL);
"""


@dataclass
class Item:
    item_id: str
    source: str
    at: date | None
    owner: str | None
    audience: str | None
    keys: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


@dataclass
class Edge:
    a: str
    b: str
    state: str
    typed: list[str]
    measured: dict[str, Any]
    why: str


def _d(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v or "")[:10])
    except ValueError:
        return None


def load(con: sqlite3.Connection) -> dict[str, Item]:
    import hooks
    hooks.ensure(con)
    items: dict[str, Item] = {}
    # Conversations are items of their own scorer (chat_links, #1016): they never join #951's threads.
    for r in con.execute("SELECT item_id, source, kind, key, at, owner, audience FROM hooks WHERE source != 'conversation'"):
        it = items.get(r[0])
        if it is None:
            it = items[r[0]] = Item(r[0], r[1], _d(r[4]), r[5], r[6])
        it.keys[r[2]].add(r[3])
    return items


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def statements(con: sqlite3.Connection, include_judge: bool = False) -> dict[tuple[str, str], bool]:
    """The person's last word on each pair: related (True) or not (False). Only a person's own word by default (a
    labelling pass or a correction): the local judge's labels (source "judge", 75% agreement, not trusted, #1016/#1125)
    never verify or reject an edge; chat_judge reads them apart, as candidates and spot-check items."""
    _ensure(con)
    has_source = "source" in {r[1] for r in con.execute("PRAGMA table_info(link_statements)")}
    q = "SELECT a, b, related FROM link_statements"
    if has_source and not include_judge:
        q += " WHERE COALESCE(source, '') <> 'judge'"
    out: dict[tuple[str, str], bool] = {}
    for a, b, rel in con.execute(q + " ORDER BY at"):
        out[_pair(a, b)] = bool(rel)
    return out


_READY: set[tuple[str, int, int]] = set()   # links.db files whose SCHEMA ran in this process (as hooks._READY)


def _ensure(con: sqlite3.Connection) -> None:
    """SCHEMA once per process and file, not on every read (it ran on every scored() rebuild and statements read)."""
    import hooks
    key = hooks._file_key(hooks._main_path(con))
    if key is not None and key in _READY and hooks._has_schema(con, ("links", "link_statements", "link_log")):
        return   # remembered, and its tables there (a file made again can reuse an inode: hooks._READY)
    con.executescript(SCHEMA)
    if key is not None:
        with hooks._READY_LOCK:
            _READY.add(key)


def demote_at(n_items: int) -> int:
    """How many items a key may be in and still link: common is relative to this house's own corpus."""
    return min(DEMOTE, max(8, int(0.10 * n_items)))


def topic_ctx(con: sqlite3.Connection) -> dict[str, Any] | None:
    """The house's topic vectors, their neighbour pairs and pairwise distribution (#951 (e)); None without them."""
    try:
        import topics
        vecs = topics.load(con)
        if len(vecs) < 3:
            return None
        return {"vecs": vecs, "near": topics.neighbours(vecs), "dist": topics.distribution(vecs)}
    except Exception:  # noqa: BLE001 - no numpy, no vectors: no topic
        return None


_SCORED: dict[str, Any] = {"key": None, "at": 0.0, "val": None, "rebuilding": False}
_SCORED_LOCK = __import__("threading").Lock()
_IDLE = __import__("threading").Event()   # set while no background re-score runs (wait_rebuilt)
_IDLE.set()
# A caller that must see a just-indexed item's links at once (a test that writes and reads back; a one-off tool) sets
# this: a content change is then scored on the spot instead of in the background.
SCORE_SYNC = os.environ.get("LINKER_SCORE_SYNC", "").strip() in ("1", "true", "yes")
SCORED_MAX_AGE_S = 600.0


# The counts behind _db_key cost a scan of each table (~20 ms for links.db's ~200k hooks rows) on every scored() call.
# A watcher connection per file reads PRAGMA data_version, which changes whenever any other connection (this process
# or another) commits to the file: while it hasn't, the file is byte-for-byte what it was, so the last key is reused.
# Any commit at all re-counts, so the key is exactly what counting every time gave.
_WATCH: dict[str, tuple[tuple[int, int], sqlite3.Connection]] = {}   # path → ((device, inode), watcher)
_WATCH_KEY: dict[str, tuple[tuple, tuple]] = {}                      # path → (version seen, key counted then)
_WATCH_LOCK = __import__("threading").Lock()
_WATCH_MAX = 8                                                        # files watched at once (tests open many)


def _data_version(path: str) -> tuple | None:
    """((device, inode), data_version) of a links.db file, None when it can't be watched (in memory, unreadable)."""
    if not path:
        return None
    try:
        st = os.stat(path)
        ident = (st.st_dev, st.st_ino)
        with _WATCH_LOCK:
            held = _WATCH.get(path)
            if held is None or held[0] != ident:     # first sight, or the file was made again: a new watcher
                for old in ([path] if held is not None else []) + list(_WATCH)[:max(0, len(_WATCH) - _WATCH_MAX + 1)]:
                    if old in _WATCH:
                        _WATCH.pop(old)[1].close()
                        _WATCH_KEY.pop(old, None)   # a new watcher's versions are its own: never compared with these
                held = _WATCH[path] = (ident, sqlite3.connect(path, check_same_thread=False))
            return (ident, held[1].execute("PRAGMA data_version").fetchone()[0])
    except (OSError, sqlite3.Error):
        return None


def _db_key(con: sqlite3.Connection) -> tuple | None:
    """What the scored graph is made of, as numbers: each input table's row count and highest rowid (a write, a
    re-index or a statement changes one). Not file stats: titan's WAL is checkpointed as connections come and go.
    Counted again only after a commit to the file (_data_version); unchanged, the last count is the answer."""
    path = _path_of(con)
    # read before counting (a commit after it is seen next time); never for a connection holding its own unsaved rows,
    # which the count must see
    version = None if con.in_transaction else _data_version(path)
    if version is not None:
        with _WATCH_LOCK:
            held = _WATCH_KEY.get(path)
        if held and held[0] == version:
            return held[1]
    out: list[Any] = [path]                   # which links.db (tests and tools may open another)
    for table in ("hooks", "link_statements", "topics"):
        try:
            out += list(con.execute(f"SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM {table}").fetchone())
        except sqlite3.Error:
            out += [None, None]
    key = tuple(out)
    if version is not None:
        with _WATCH_LOCK:
            _WATCH_KEY[path] = (version, key)
    return key


def _build(con: sqlite3.Connection) -> tuple[tuple, tuple[dict[str, Item], list[Edge]]]:
    items = load(con)
    edges = score(items, statements(con), topic_ctx(con)) if items else []
    return _db_key(con), (items, edges)


def _rebuild_in_background(path: str) -> None:
    """Score the graph again on its own connection, once at a time, and swap it in."""
    with _SCORED_LOCK:
        if _SCORED.get("rebuilding"):
            return
        _SCORED["rebuilding"] = True
        _IDLE.clear()

    def run() -> None:
        try:
            con = sqlite3.connect(path, check_same_thread=False)
            try:
                key, val = _build(con)
            finally:
                con.close()
            with _SCORED_LOCK:
                _SCORED.update(key=key, at=time.monotonic(), val=val)
        except Exception as exc:  # noqa: BLE001 - the next request tries again
            print(f"[linker] background rebuild failed: {type(exc).__name__}", file=sys.stderr)
        finally:
            with _SCORED_LOCK:
                _SCORED["rebuilding"] = False
                _IDLE.set()
    __import__("threading").Thread(target=run, name="linker-rebuild", daemon=True).start()


def wait_rebuilt(timeout: float = 60.0) -> bool:
    """Block until no background re-score is running, for a test or a tool that must read what it just wrote
    (#1152: a signal, not a sleep). → False on timeout."""
    return _IDLE.wait(timeout)


def _path_of(con: sqlite3.Connection) -> str:
    return next((r[2] for r in con.execute("PRAGMA database_list") if r[1] == "main"), "")


def scored(con: sqlite3.Connection) -> tuple[dict[str, Item], list[Edge]]:
    """(items, edges) for the whole house, scored off the request path (stale-while-revalidate). Scoring every pair
    takes seconds (10-06: 2.7 s for 4,940 items), so:
      - nothing cached yet (the first call; the server warms it at start): scored here, once
      - the content unchanged and under SCORED_MAX_AGE_S: the cached graph
      - past the age only: the cached graph, and a rebuild starts in the background
      - the content changed (a new item, a re-index, a statement): the ITEMS are loaded fresh here (cheap: who owns
        each and who may see it is always current, so a projection never acts on a stale audience), with the cached
        edges between items that still exist; the edges are re-scored in the background and swapped in
    Callers project per viewer on every call (threads_for(…, visible)); this is the house before projection."""
    key = _db_key(con)
    now = time.monotonic()
    with _SCORED_LOCK:
        cached, ckey, age = _SCORED.get("val"), _SCORED.get("key"), now - _SCORED.get("at", 0.0)
    if cached is None:
        key2, val = _build(con)
        with _SCORED_LOCK:
            _SCORED.update(key=key2, at=now, val=val)
        return val
    if key == ckey and age < SCORED_MAX_AGE_S:
        return cached
    if SCORE_SYNC and key != ckey:
        key2, val = _build(con)
        with _SCORED_LOCK:
            _SCORED.update(key=key2, at=now, val=val)
        return val
    path = _path_of(con)
    if path:
        _rebuild_in_background(path)
    if key == ckey:
        return cached                         # only aged: the same content, served while it's re-scored
    items = load(con)                         # the content changed: current items (owners, audiences, what exists)
    edges = [e for e in cached[1] if e.a in items and e.b in items]
    return items, _with_statements(edges, statements(con), items)


def _with_statements(edges: list[Edge], said: dict[tuple[str, str], bool], items: dict[str, Item]) -> list[Edge]:
    """A person's word, applied at once over cached edges, exactly as score() applies it: related → verified, not
    related → rejected (it outranks everything), a pair with no edge yet gets one. Their correction never waits for
    the background re-score."""
    if not said:
        return edges
    out, seen = [], set()
    for e in edges:
        p = _pair(e.a, e.b)
        stmt = said.get(p)
        if stmt is None:
            out.append(e)
        else:
            out.append(Edge(e.a, e.b, "verified" if stmt else "rejected", e.typed, e.measured, why(e.typed, e.measured, stmt)))
        seen.add(p)
    for (a, b), stmt in said.items():
        if (a, b) not in seen and a in items and b in items:
            out.append(Edge(a, b, "verified" if stmt else "rejected", [], {}, why([], {}, stmt)))
    return out


def score(items: dict[str, Item], said: dict[tuple[str, str], bool] | None = None,
          topic: dict[str, Any] | None = None) -> list[Edge]:
    """Every edge between items that share a key, with its state and why. topic (topic_ctx): a measured topic score
    on each edge, and topic-only neighbours as candidates, never more (phase 1)."""
    said = said or {}
    cap = demote_at(len(items))
    by_key: dict[tuple[str, str], list[str]] = defaultdict(list)
    for it in items.values():
        for kind, keys in it.keys.items():
            if kind.startswith(("id:", "party:")) and kind not in NOT_LINKING:
                for k in keys:
                    by_key[(kind, k)].append(it.item_id)
    amounts = Counter(k for it in items.values() for k in it.keys.get("amount", ()))
    n_amounts = sum(amounts.values()) or 1
    pairs: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for (kind, key), ids in by_key.items():
        if len(ids) < 2 or (kind.startswith("party:") and len(ids) > cap):
            continue                                   # alone, or too common to link anything
        for a, b in itertools.combinations(sorted(set(ids)), 2):
            pairs[(a, b)].append((kind, key))
    for (a, b) in said:
        if a in items and b in items:
            pairs.setdefault((a, b), [])
    near = (topic or {}).get("near") or {}
    for (a, b) in near:
        if a in items and b in items:
            pairs.setdefault((a, b), [])
    out = []
    for (a, b), shared in pairs.items():
        ia, ib = items[a], items[b]
        gap = abs((ia.at - ib.at).days) if ia.at and ib.at else None
        typed: list[str] = []
        ident = verified = False
        party = False
        for kind, key in shared:
            if kind in ACCOUNT_IDS:
                party = True
                typed.append(f"same {_KIND_WORDS.get(kind, kind)}")
                continue
            if kind.startswith("id:"):
                if len(by_key[(kind, key)]) > cap:
                    party = True
                    typed.append(f"same {_KIND_WORDS.get(kind, kind)} (common)")
                    continue
                if kind == "id:source":
                    verified = True
                elif kind == "id:message":
                    if (ia.owner or "") == (ib.owner or ""):
                        verified = True                # one owner's mail thread; across owners a forward: accepted
                    else:
                        ident = True
                else:
                    ident = True
                typed.append(_KIND_WORDS.get(kind, kind) if kind == "id:source" else f"same {_KIND_WORDS.get(kind, kind)}")
            else:
                party = True
                typed.append(f"same {_KIND_WORDS.get(kind, kind)}")
        measured: dict[str, Any] = {}
        common_amounts = {k for k in ia.keys.get("amount", set()) & ib.keys.get("amount", set())
                          if _as_float(k) >= MIN_AMOUNT}
        if common_amounts:
            rarest = min(common_amounts, key=lambda k: amounts[k])
            rarity = 1 - amounts[rarest] / n_amounts
            measured["amount"] = {"value": rarest, "rarer_than": round(rarity, 3)}
        mentions = (ia.keys.get("date:mention", set()) & {ib.at.isoformat()} if ib.at else set()) | \
                   (ib.keys.get("date:mention", set()) & {ia.at.isoformat()} if ia.at else set())
        if mentions:
            measured["mentions_date"] = True
        if gap is not None:
            measured["days_apart"] = gap
        if topic:
            import topics as _t
            ts = near.get((a, b)) or _t.pair_score(topic["vecs"], a, b, topic.get("dist"))
            if ts:
                measured["topic"] = ts
        # A mentioned date alone never accepts (10-05: two notifications from one address joined on a coincident
        # date); it's shown in the why and raises cohesion. A corpus-rare shared amount does.
        strong = measured.get("amount", {}).get("rarer_than", 0) >= AMOUNT_FLOOR
        stmt = said.get((a, b))
        if stmt is not None:
            state = "verified" if stmt else "rejected"
        elif verified:
            state = "verified"
        elif ident:
            state = "accepted"
        elif party and strong and (gap is None or gap <= WINDOW_DAYS):
            state = "accepted"
        elif party and gap is not None and gap <= CANDIDATE_DAYS:
            state = "candidate"
        elif (a, b) in near:
            state = "candidate"        # a similar topic alone: possibly related, never more in phase 1
        else:
            continue
        out.append(Edge(a, b, state, typed, measured, why(typed, measured, stmt)))
    return out


def why(typed: list[str], measured: dict[str, Any], stmt: bool | None = None) -> str:
    """One line, from the evidence: what links them, typed first, then what was measured."""
    bits = []
    if stmt is not None:
        bits.append("you said they're related" if stmt else "you said they're not related")
    bits += [f"{t} (typed)" for t in dict.fromkeys(typed)]
    amt = measured.get("amount")
    if amt:
        bits.append(f"${float(amt['value']):,.2f}, rarer than {amt['rarer_than']:.0%} of amounts (measured)")
    if measured.get("mentions_date"):
        bits.append("one mentions the other's date (measured)")
    tp = measured.get("topic")
    if tp and tp.get("near"):
        bits.append(f"similar topic, closer than {tp['pctl']:.0%} of pairs (measured)")
    if measured.get("days_apart") is not None:
        n = measured["days_apart"]
        bits.append("the same day" if n == 0 else f"{n} day{'s' if n != 1 else ''} apart")
    return " · ".join(bits)


def save(con: sqlite3.Connection, edges: Iterable[Edge]) -> dict[str, int]:
    """Write the edges; a changed state goes in link_log with its why. → counts by state."""
    con.executescript(SCHEMA)
    was = {(a, b): s for a, b, s in con.execute("SELECT a, b, state FROM links")}
    now = time.time()
    counts: Counter = Counter()
    keep = set()
    for e in edges:
        counts[e.state] += 1
        keep.add((e.a, e.b))
        if was.get((e.a, e.b)) != e.state:
            con.execute("INSERT INTO link_log VALUES (?,?,?,?,?,?)", (e.a, e.b, was.get((e.a, e.b)), e.state, e.why, now))
        con.execute("INSERT OR REPLACE INTO links VALUES (?,?,?,?,?,?,?)",
                    (e.a, e.b, e.state, json.dumps(e.typed), json.dumps(e.measured), e.why, now))
    for (a, b), s in was.items():
        if (a, b) not in keep:   # its evidence is gone (an item left): logged, then dropped
            con.execute("INSERT INTO link_log VALUES (?,?,?,?,?,?)", (a, b, s, "gone", "no longer supported", now))
            con.execute("DELETE FROM links WHERE a=? AND b=?", (a, b))
    con.commit()
    return dict(counts)


def say(con: sqlite3.Connection, a: str, b: str, related: bool, by: str, source: str = "correction") -> None:
    """The person's word on two items: it outranks the linker from the next run on (and now). source: "baseline" (a
    labelling pass, #1016) or "correction" (said in passing), kept so the correction rate leaves the baseline out."""
    con.executescript(SCHEMA)
    if "source" not in {r[1] for r in con.execute("PRAGMA table_info(link_statements)")}:
        con.execute("ALTER TABLE link_statements ADD COLUMN source TEXT NOT NULL DEFAULT 'correction'")
    a, b = _pair(a, b)
    con.execute("INSERT INTO link_statements (a, b, related, by, at, source) VALUES (?,?,?,?,?,?)",
                (a, b, int(related), by, time.time(), source if source in SOURCES else "correction"))
    con.commit()


def continues_from(owner: str, conversation_id: str, first_message: str | None = None) -> dict[str, Any] | None:
    """The earlier conversation this one continues (#1016): chat_links.continues_from, here so the carry-over
    (conv_continue._edge) finds it under the linker's name. {"id", "state", "why"} or None; only owner's own."""
    import chat_links
    return chat_links.continues_from(owner, conversation_id, first_message)


# ── threads: views, per viewer ───────────────────────────────────────────────────────────────

@dataclass
class Thread:
    items: list[str]
    edges: list[Edge]
    fringe: list[Edge]
    cohesion: float
    name: str | None
    description: str


def threads_for(items: dict[str, Item], edges: Iterable[Edge], visible: Callable[[Item], bool],
                titles: dict[str, str] | None = None, entity_names: dict[str, str] | None = None,
                vendors: dict[str, str] | None = None) -> list[Thread]:
    """Components of verified and accepted edges over the items this viewer may see. Candidate edges between a
    thread and a visible item are its fringe."""
    seen = {i for i, it in items.items() if visible(it)}
    strong = [e for e in edges if e.state in ("verified", "accepted") and e.a in seen and e.b in seen]
    weak = [e for e in edges if e.state == "candidate" and e.a in seen and e.b in seen]
    parent = {i: i for i in seen}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for e in strong:
        parent[find(e.a)] = find(e.b)
    corpus_n = _corpus(titles) if titles else None
    comps: dict[str, list[str]] = defaultdict(list)
    for i in seen:
        comps[find(i)].append(i)
    out = []
    for members in comps.values():
        if len(members) < 2:
            continue
        ms = set(members)
        es = [e for e in strong if e.a in ms]
        fr = [e for e in weak if (e.a in ms) != (e.b in ms) or (e.a in ms and e.b in ms)]
        cohesion = round(sum(strength(e) for e in es) / len(es), 3)
        order = sorted(members, key=lambda i: (items[i].at or date.min, i))
        out.append(Thread(order, es, fr, cohesion, _name(members, items, cohesion, titles, entity_names, vendors, corpus_n),
                          _describe(order, items, es)))
    return sorted(out, key=lambda t: max((items[i].at or date.min) for i in t.items), reverse=True)


def strength(e: Edge) -> float:
    """How strongly one edge holds: verified 1.0; an identifier 0.9; a party with measured evidence, its amount's
    rarity (or 0.8 for a mentioned date); anything weaker 0."""
    if e.state == "verified":
        return 1.0
    if e.state != "accepted":
        return 0.0
    if any(not t.startswith("same sender") and not t.startswith(("same vendor", "same named", "same phone number",
                                                                    "same sender address", "same account",
                                                                    "same member number", "same policy number"))
           and "(common)" not in t for t in e.typed):
        return 0.9
    return max(e.measured.get("amount", {}).get("rarer_than", 0), 0.8 if e.measured.get("mentions_date") else 0)


_STOP = frozenset("a an and the of for to in on at by with from your you my our is are was be it this that re fw fwd "
                  "has have been we us new update updated".split())
_CLEAN = [re.compile(p, re.I) for p in (r"^\s*(?:re|fwd?|fw)\s*:\s*", r"\[[^\]]*\]", r"^\s*[a-z]+,\s+",
                                          r"[^\w\s'&./-]", r"#?\b[\w-]*\d[\w-]*\b")]


def _clean(title: str) -> list[str]:
    """A title's own words: no Re:/Fwd:, tags, emoji, numbers or ids, or a greeting ("Alex, your card…")."""
    t = str(title or "")
    for _ in range(2):
        for rx in _CLEAN:
            t = rx.sub(" ", t)
    return [w.rstrip(".'-&") for w in re.findall(r"[a-z][a-z'&.-]*", t.lower()) if w.rstrip(".'-&")]


def _phrases(words: list[str]) -> set[str]:
    out = set()
    for n in range(2, 7):
        for k in range(len(words) - n + 1):
            g = words[k:k + n]
            if g[0] in _STOP or g[-1] in _STOP or sum(w not in _STOP for w in g) < 2:
                continue
            out.add(" ".join(g))
    return out


def _corpus(titles: dict[str, str]) -> tuple[Counter, int]:
    """Every subject phrase in the house's distinct titles, and how many titles: built once per view, not per thread
    (10-05: rebuilt for each of 144 threads over every mail subject, it cost ~40 s a call)."""
    distinct = {" ".join(_clean(t)) for t in titles.values()}
    return Counter(p for t in distinct for p in _phrases(t.split())), max(1, len(distinct))


def _name(members: list[str], items: dict[str, Item], cohesion: float, titles: dict[str, str] | None,
          entity_names: dict[str, str] | None, vendors: dict[str, str] | None = None,
          corpus_n: tuple[Counter, int] | None = None) -> str | None:
    """A name only above the cohesion floor and with provenance (rule 1), in this order: a vendor two members share
    (the record's own spelling); an organisation named in two members' titles or senders; a subject phrase two titles
    share that is rare in this house's titles (at most RARE_PHRASE of them). Never a fragment: no name otherwise."""
    if cohesion < COHESION_FLOOR:
        return None
    filed = {i for i in members for k in items[i].keys.get("id:source", ())
             if sum(k in items[j].keys.get("id:source", ()) for j in members) >= 2}   # a record and the mail it's from
    for kind, labels in (("party:vendor", vendors or {}), ("party:entity", entity_names or {})):
        shared = Counter(k for i in members for k in items[i].keys.get(kind, ()))
        for k, n in shared.most_common():
            # shared by two members, or a record's vendor, which names the mail it was filed from too
            carried = kind == "party:vendor" and any(k in items[i].keys.get(kind, ()) for i in filed)
            if (n >= 2 or carried) and (labels.get(k) or (kind == "party:vendor" and k)):
                return labels.get(k) or k.title()
    if not titles:
        return None
    corpus, n_titles = corpus_n or _corpus(titles)
    # two separate manifestations must share it: a record filed from a mail carries the mail's subject, so the pair
    # counts once; replies in one mail thread are separate messages and each counts
    group: dict[str, str] = {}
    for i in members:
        mine_src = items[i].keys.get("id:source", set())
        group[i] = next((group[j] for j in group if mine_src & items[j].keys.get("id:source", set())), i)
    per_group: dict[str, set[str]] = defaultdict(set)
    for i in members:
        per_group[group[i]] |= _phrases(_clean(titles.get(i) or ""))
    mine = Counter(p for ps in per_group.values() for p in ps)
    # distinctive: two of this thread's titles share it, and at most RARE_PHRASE of the house's other titles have it
    good = [p for p, n in mine.items() if n >= 2 and (corpus[p] - n) / n_titles <= RARE_PHRASE]
    if not good:
        return None
    best = max(good, key=lambda p: (len(p.split()), -corpus[p], p))
    # as a member's own title spells it ("AAF Spokane", not "Aaf spokane")
    rx = re.compile(r"\b" + r"\W+".join(re.escape(w) for w in best.split()) + r"\b", re.I)
    for i in members:
        m = rx.search(titles.get(i) or "")
        if m:
            return m.group(0)[0].upper() + m.group(0)[1:]
    return best[0].upper() + best[1:]


def _describe(order: list[str], items: dict[str, Item], edges: list[Edge]) -> str:
    """The machine description: what most of its edges share, an amount if one recurs, the size and the span."""
    kinds = Counter(t for e in edges for t in e.typed)
    head = kinds.most_common(1)[0][0] if kinds else "linked"
    amts = Counter(e.measured["amount"]["value"] for e in edges if e.measured.get("amount"))
    days = [items[i].at for i in order if items[i].at]
    span = (f" · {days[0]:%b %-d}" + (f"–{days[-1]:%b %-d}" if days[-1] != days[0] else "")) if days else ""
    amt = f" · ${float(amts.most_common(1)[0][0]):,.2f}" if amts else ""
    return f"{head}{amt} · {len(order)} items{span}"


def _kb_titles() -> dict[str, str]:
    """{source_id: title} of the KB: lib.kb_titles (cached, viewer-independent) unless kb_hygiene's reader is a
    stand-in (a test's), which is asked every time."""
    import kb_hygiene
    src = kb_hygiene.list_sources
    if getattr(src, "__module__", None) == kb_hygiene.__name__ and getattr(src, "__name__", "") == "list_sources":
        import agent_runtime_lib as lib
        return lib.kb_titles()
    return {str(s["source_id"]): str(s.get("title") or "") for s in src()}


def _mail_subjects(ids: Iterable[str] | None = None) -> dict[str, str]:
    """Server mail subjects by item id: every one, or only these ids'."""
    import mail_server
    m = sqlite3.connect(f"file:{mail_server.DB}?mode=ro", uri=True)
    try:
        if ids is None:
            return {r[0]: r[1] or "" for r in m.execute("SELECT item_id, subject FROM messages WHERE code=0")}
        want = sorted(set(ids))
        out: dict[str, str] = {}
        for k in range(0, len(want), 500):
            part = want[k:k + 500]
            q = f"SELECT item_id, subject FROM messages WHERE code=0 AND item_id IN ({','.join('?' * len(part))})"
            out.update({r[0]: r[1] or "" for r in m.execute(q, part)})
        return out
    finally:
        m.close()


def labels(items: dict[str, Item]) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """(titles, vendor names, entity names) for naming and showing threads: the KB's titles, the records' titles and
    vendors as filed, server mail subjects, and world entities' names. Each part that can't be read is left empty.
    The titles are the house's (naming a thread weighs a phrase against all of them, _corpus); a caller that only shows
    some items' titles asks titles_for(ids). Nothing here depends on the viewer: each caller shows a label only for an
    item its viewer may see."""
    titles: dict[str, str] = {}
    vendors: dict[str, str] = {}
    names: dict[str, str] = {}
    try:
        titles.update(_kb_titles())
    except Exception:  # noqa: BLE001
        pass
    try:
        import agent_runtime_lib as lib
        import obligations
        for t in ("bill", "receipt"):
            for n in lib.record_nodes(t)[0]:   # cached for a few seconds when read from the runtime (lib.record_nodes)
                d = n.get("data") or {}
                titles[str(n.get("node_id"))] = str(d.get("title") or "")
                if d.get("vendor"):
                    vendors.setdefault(obligations.account_of(d["vendor"]), str(d["vendor"]))
    except Exception:  # noqa: BLE001
        pass
    try:
        titles.update(_mail_subjects())
    except Exception:  # noqa: BLE001
        pass
    try:
        import world
        w = world.get()
        # each entity once (10-10: looked up again for every item that names it), in the items' order as before
        for e in dict.fromkeys(e for it in items.values() for e in it.keys.get("party:entity", ())):
            row = w.entity(e)
            if row is not None:
                names[e] = str(dict(row).get("name") or "")
    except Exception:  # noqa: BLE001
        pass
    return titles, vendors, names


def titles_for(ids: Iterable[str]) -> dict[str, str]:
    """labels()'s titles for just these items (a list of items to show, no thread to name): the same title each would
    get there (a KB title, then a bill's or receipt's, then a mail subject), looked up for these ids alone: the mail
    subjects by id, the KB's titles and the records from their short caches. Each part that can't be read is left
    empty, as in labels()."""
    want = {str(i) for i in ids}
    titles: dict[str, str] = {}
    if not want:
        return titles
    try:
        titles.update({k: v for k, v in _kb_titles().items() if k in want})
    except Exception:  # noqa: BLE001
        pass
    try:
        import agent_runtime_lib as lib
        for t in ("bill", "receipt"):
            for n in lib.record_nodes(t)[0]:
                if str(n.get("node_id")) in want:
                    titles[str(n.get("node_id"))] = str((n.get("data") or {}).get("title") or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        titles.update(_mail_subjects(want))
    except Exception:  # noqa: BLE001
        pass
    return titles


def run(apply: bool, con: sqlite3.Connection | None = None) -> dict[str, Any]:
    import hooks
    con = con or hooks.connect()
    items = load(con)
    edges = score(items, statements(con), topic_ctx(con))
    counts = save(con, edges) if apply else dict(Counter(e.state for e in edges))
    titles, vendors, names = labels(items)
    th = threads_for(items, edges, lambda it: True, titles, names, vendors)
    return {"apply": apply, "items": len(items), "edges": counts,
            "threads": len(th), "thread_sizes": dict(sorted(Counter(len(t.items) for t in th).items())),
            "named": sum(1 for t in th if t.name), "fringe_edges": sum(len(t.fringe) for t in th)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Link items by their hooks (#951 b)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    print(json.dumps(run(a.apply), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
