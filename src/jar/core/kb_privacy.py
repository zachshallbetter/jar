#!/usr/bin/env python3
"""Who may see and change each knowledge-base item: owner + audience (audience.py) for the KB.

Zach, 09-30: "Robin's knowledge (text, mac files, etc) shouldn't mix with mine but we absolutely
shouldn't lose the capability to share or make anything available without much effort if needed."

Every KB write stamps `owner` (a person id, or None for titan's own notes and household.yaml) and
`audience` (audience.py: private / household / adults / admins / [ids]) into the source's metadata:

  chat saves, ingest_*, Jellybean notes   owner = the person saving, "private" unless they share
  connector items                          owner = the person whose device pushed them, "private"
  system notes (kb_seed_*: runbook, decisions, titan, jellybean)   owner None, "admins"
  household.yaml (kb_import)               owner None, "household"

An item with no owner or audience (everything written before 09-30) counts as Zach's and private.

The sharing table. The retrieval runtime can't change a source's metadata in place: re-indexing the
same text returns early on its unchanged checksum and keeps the old metadata
(retrieval-runtime/src/storage/db.rs, upsert_source_and_chunks). So sharing, unsharing and the
backfill (kb_backfill_owner.py) write a row here, keyed by source id, and a row wins over the
metadata. A connector refreshing an item writes "private" metadata again but leaves the row alone,
so a share survives the refresh. Deleting an item through titan drops its row.

Reading. Every read path (kb_retrieve, kb_list, kb_relations' KB fallback, the per-turn KB passages,
cited sources, GET /v1/kb and /v1/kb/{id}) filters with `kb_visible` / `filter_hits` /
`visible_sources`. The runtime's `metadata_filters` match one key by equality only (no OR, no
"missing"), so queries over-fetch and filter here, and a person still gets their top k.

Kids and shared household devices read household items only. An engineering agent (a Claude session or
the autonomous coder, with the engineering token: household_identity.ENGINEERING) reads titan's own system
notes and nothing else: never a household, personal or shared item (#355).
"""
from __future__ import annotations

import json
import os
import sys
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import audience as _aud
import local_time

LEGACY_OWNER = "zach"            # household_identity.ADMIN_PERSON_ID (test_kb_privacy checks they match)
LEGACY_AUDIENCE = "private"
HOUSEHOLD_ONLY_ROLES = ("kid", "household")
ENGINEERING_ROLE = "engineering"           # household_identity.ENGINEERING (test_engineering_scope checks they match)
OVERFETCH = 5                    # query top_k × this, then filter
MAX_FETCH = 60

_DEFAULT_DB = "/ai/datasets/connectors/kb_acl.db"
_lock = threading.Lock()
Runtime = Callable[..., dict]


# ── the sharing table ─────────────────────────────────────────────────────────────────────────

def _db_path() -> Path:
    return Path(os.environ.get("KB_ACL_DB", _DEFAULT_DB))


def _connect(write: bool) -> sqlite3.Connection | None:
    """The table, or None when it doesn't exist yet (reads never create it)."""
    p = _db_path()
    if not write and not p.exists():
        return None
    if write:
        p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=5)
    con.execute("""CREATE TABLE IF NOT EXISTS acl (
        source_id TEXT PRIMARY KEY, owner TEXT, audience TEXT NOT NULL,
        set_by TEXT, set_at REAL NOT NULL, why TEXT)""")
    return con


def acl_rows(ids: Iterable[str] | None = None) -> dict[str, tuple[str | None, str]]:
    """source_id → (owner, audience) from the sharing table (all rows when ids is None)."""
    con = _connect(False)
    if con is None:
        return {}
    try:
        if ids is None:
            rows = con.execute("SELECT source_id, owner, audience FROM acl").fetchall()
        else:
            want = sorted({str(i) for i in ids if i})
            rows = []
            for i in range(0, len(want), 500):
                part = want[i:i + 500]
                rows += con.execute(f"SELECT source_id, owner, audience FROM acl WHERE source_id IN "
                                    f"({','.join('?' * len(part))})", part).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}
    finally:
        con.close()


def acl_details(ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """source_id → {owner, audience, set_at, why} (the household feed: when a note was saved or shared)."""
    con = _connect(False)
    if con is None:
        return {}
    try:
        want = sorted({str(i) for i in ids if i})
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(want), 500):
            part = want[i:i + 500]
            for sid, owner, aud, set_at, why in con.execute(
                    f"SELECT source_id, owner, audience, set_at, why FROM acl WHERE source_id IN "
                    f"({','.join('?' * len(part))})", part):
                out[sid] = {"owner": owner, "audience": aud, "set_at": set_at, "why": why or ""}
        return out
    finally:
        con.close()


def set_acl(source_id: str, owner: str | None, audience: str, *, by: str | None = None, why: str = "",
            keep: bool = False) -> None:
    """Record an item's owner and audience. keep=True leaves an existing row (a share) alone."""
    with _lock:
        con = _connect(True)
        try:
            verb = "INSERT OR IGNORE" if keep else "INSERT OR REPLACE"
            con.execute(f"{verb} INTO acl (source_id, owner, audience, set_by, set_at, why) VALUES (?,?,?,?,?,?)",
                        (source_id, owner, audience, by, time.time(), why))
            con.commit()
        finally:
            con.close()


def drop_acl(source_id: str) -> None:
    con = _connect(False)
    if con is None:
        return
    with _lock:
        try:
            con.execute("DELETE FROM acl WHERE source_id=?", (source_id,))
            con.commit()
        finally:
            con.close()


# ── the rule ──────────────────────────────────────────────────────────────────────────────────

def stamp(metadata: dict[str, Any] | None, owner: str | None, audience: Any) -> dict[str, Any]:
    """Metadata for a write, with its owner and audience."""
    md = dict(metadata or {})
    md["owner"] = owner
    md["audience"] = _aud.normalize(audience, default="private")
    return md


def acl_of(meta: dict[str, Any] | None, source_id: str | None = None,
           rows: dict[str, tuple[str | None, str]] | None = None) -> tuple[str | None, str]:
    """(owner, audience) of an item: its sharing-table row, else its metadata, else Zach's private: the admin only
    (Zach, 10-04, #833: a missing row means admin only; kb_acl_backfill.py gives every item an explicit row).
    Pass `rows` (acl_rows) when checking many items; without it the table is read for source_id."""
    if source_id:
        row = (rows if rows is not None else acl_rows([source_id])).get(source_id)
        if row:
            return row[0], _aud.normalize(row[1], default=LEGACY_AUDIENCE)
    md = meta or {}
    if "owner" not in md and not md.get("audience"):
        return LEGACY_OWNER, LEGACY_AUDIENCE
    owner = (str(md.get("owner") or "").strip().lower() or None)
    return owner, _aud.normalize(md.get("audience"), default=LEGACY_AUDIENCE)


def visible_acl(owner: str | None, audience: str, person: str | None, role: str | None) -> bool:
    """audience.visible, except that kids and household devices see household items only.
    person and role both None: titan itself (sees everything)."""
    if person is None and role is None:
        return True
    if role == ENGINEERING_ROLE:
        return False                    # decided by kb_visible from the item's metadata (system notes only)
    if role in HOUSEHOLD_ONLY_ROLES:
        return audience == "household"
    return _aud.visible(audience, person, role, owner=owner)


def system_meta(meta: dict[str, Any] | None) -> bool:
    """titan's own notes: the seeded runbook and decisions (kb_seed_*), titan and Jellybean docs, knowledge
    records. What an engineering agent may read, and only when nobody owns the item."""
    md = meta or {}
    return (str(md.get("origin") or "").startswith("kb_seed") or str(md.get("domain") or "") in ("titan", "jellybean")
            or str(md.get("source_type") or "") == "knowledge_record")


def kb_visible(meta: dict[str, Any] | None, person: str | None, role: str | None, *,
               source_id: str | None = None, rows: dict | None = None, text: str | None = None) -> bool:
    """Whether this person may see a KB item (its metadata, and its id for the sharing table). With
    its text (or title), fences apply too (fences.py, #356): every fence it touches must admit them."""
    owner, aud = acl_of(meta, source_id, rows)
    if role == ENGINEERING_ROLE:
        # #355: system notes only, and only while they're titan's (no owner, the admins' audience): a person's
        # note that mentions titan is still theirs
        return owner is None and aud == "admins" and system_meta(meta)
    import zones   # the zone first: kids and household devices never see sync, web or system items (#833)
    if not zones.allowed(zones.zone_of(meta, source_id), role, person):
        return False
    if not visible_acl(owner, aud, person, role):
        return False
    if text is None:
        return True
    import fences
    return fences.get().admits(person, role, owner, text=text, item=source_id)


def can_edit(owner: str | None, audience: str, person: str | None, role: str | None) -> bool:
    """Edit, delete, share: the owner, or the admin (for items the admin can see)."""
    if person is None and role is None:
        return True
    if not visible_acl(owner, audience, person, role):
        return False
    return role == "admin" or bool(owner and person and owner.lower() == str(person).lower())


def access(meta: dict[str, Any] | None, source_id: str, person: str | None, role: str | None) -> str:
    """'edit', 'read' or 'none' for this person on this item."""
    owner, aud = acl_of(meta, source_id)
    if not visible_acl(owner, aud, person, role):
        return "none"
    return "edit" if can_edit(owner, aud, person, role) else "read"


def scoped_id(prefix: str, rest: str, person: str | None) -> str:
    """An id for a person's own note: Zach's keep their old form (note-<slug>); anyone else's carry
    their id (note-<person>-<slug>), so Robin saving "Wifi" never replaces Zach's "Wifi"."""
    p = re.sub(r"[^a-z0-9_]+", "-", str(person or "").lower()).strip("-")
    return f"{prefix}{rest}" if not p or p == LEGACY_OWNER else f"{prefix}{p}-{rest}"


# ── read paths ────────────────────────────────────────────────────────────────────────────────

def filter_hits(hits: list[dict[str, Any]], person: str | None, role: str | None,
                k: int | None = None) -> list[dict[str, Any]]:
    """The query results this person may see, in order, at most k."""
    rows = acl_rows(h.get("source_id") for h in hits)
    out = [h for h in hits if kb_visible(h.get("metadata"), person, role,
                                          source_id=str(h.get("source_id") or ""), rows=rows,
                                          text=f"{h.get('title') or ''}\n{h.get('chunk_text') or h.get('text') or ''}")]
    return out[:k] if k else out


# #371 (10-06, live 95-case bank, hybrid): system notes' likely-question chunks (extra_chunks) lift hybrid 80→85 hit@3
# but cost one calendar case; scoring the hybrid's lexical part on the note's own text keeps every zone at or above
# the no-extras baseline (82/84/0.834). include_extras:false is the kill switch.
QUERY_OPTS: dict[str, Any] = {"extra_lexical": "body"}


def query(call: Runtime, text: str, k: int, person: str | None, role: str | None,
          timeout: int = 30) -> list[dict[str, Any]]:
    """The top k retrieval results this person may see. Over-fetches (the runtime can't filter by
    owner-or-audience), and widens once more if filtering left fewer than k and more matched."""
    fetch = min(max(k * OVERFETCH, k), MAX_FETCH)
    while True:
        r = call("retrieval", "/v1/retrieval/query", "POST", {"query": text, "top_k": fetch, **QUERY_OPTS}, timeout=timeout)
        raw = r.get("results") or []
        hits = filter_hits(raw, person, role, k)
        total = int(r.get("total_matches") or len(raw))
        if len(hits) >= k or total <= len(raw) or fetch >= MAX_FETCH * 2:
            return hits
        fetch = MAX_FETCH * 2


def saved_words(h: dict[str, Any], now: Any = None) -> str | None:
    """When a note was saved ("today, Fri Oct 2", "yesterday, Thu Oct 1", "Tue Sep 29", "Mon Mar 3, 2025") from
    its metadata's "saved" (YYYY-MM-DD, the household's date), so a passage says how old it is (10-01: time and
    order right across knowledge too). None when it doesn't say."""
    from datetime import date
    md = h.get("metadata") if isinstance(h.get("metadata"), dict) else {}
    raw = str(md.get("saved") or "")[:10]
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        return None
    today = (now.date() if now is not None else local_time.today())
    return _day_words(d, today)


def _day_words(d: Any, today: Any) -> str:
    """A day as the model should repeat it: always the weekday and date, with "today"/"yesterday" in front when
    it's that recent (#627: titan's own model read a bare "today" as the day before)."""
    day = f"{d:%a} {d:%b} {d.day}" + ("" if d.year == today.year else f", {d.year}")
    days = (today - d).days
    if days == 0:
        return f"today, {day}"
    if days == 1:
        return f"yesterday, {day}"
    return day


def date_label(h: dict[str, Any], now: Any = None) -> str | None:
    """How a passage says its age: "saved Tue Sep 29" for a note saved through titan, else "dated Tue Sep 29" from
    a synced item's observed_at (mail, calendar, downloads: #396, so "when was that mail" answers from the
    passage). None when neither says."""
    saved = saved_words(h, now)
    if saved:
        return f"saved {saved}"
    from datetime import datetime
    md = h.get("metadata") if isinstance(h.get("metadata"), dict) else {}
    prov = md.get("provenance") if isinstance(md.get("provenance"), dict) else {}
    raw = str(md.get("observed_at") or prov.get("observed_at") or "").strip()
    if not raw:
        return None
    import local_time
    when = local_time.parse_iso(raw)
    if when is None:
        return None
    try:
        import local_time
        tz = local_time.zone()
        d = (when.astimezone(tz) if (when.tzinfo and tz) else when).date()
    except Exception:  # noqa: BLE001
        d = when.date()
    today = (now.date() if now is not None else local_time.today())
    return f"dated {_day_words(d, today)}"


def bundle(hits: list[dict[str, Any]], now: Any = None) -> str:
    """The runtime's context-bundle format, built from filtered hits only (the runtime's own bundle
    covers everything it fetched), each saying when it was saved when the note knows."""
    def ref(h: dict[str, Any]) -> str:
        r = str(h.get("citation_ref") or h.get("source_id"))
        label = date_label(h, now)
        return f"{r} · {label}" if label else r
    return "\n\n".join(f"[{ref(h)}] {h.get('chunk_text') or ''}" for h in hits).strip()


_META_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}
_TEXT_CACHE: dict[str, str] = {}


def _fetch(call: Runtime, source_id: str, updated_at: str = "") -> tuple[dict[str, Any], str] | None:
    """(metadata, text) of a source from its chunks, cached per (id, updated_at). None when unreadable."""
    hit = _META_CACHE.get(source_id)
    if hit and hit[0] == updated_at and updated_at and source_id in _TEXT_CACHE:
        return hit[1], _TEXT_CACHE[source_id]
    import urllib.parse
    try:
        r = call("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(source_id, safe='')}/chunks", "GET")
        chunks = sorted(r.get("chunks") or [], key=lambda c: c.get("chunk_index", 0))
        md = dict((chunks[0].get("metadata") or {}) if chunks else {})
        text = "\n".join(str(c.get("chunk_text") or "") for c in chunks)
    except Exception:  # noqa: BLE001
        return None
    _META_CACHE[source_id] = (updated_at, md)
    _TEXT_CACHE[source_id] = text
    return md, text


def source_meta(call: Runtime, source_id: str, updated_at: str = "") -> dict[str, Any]:
    """A source's metadata (the runtime's source list has none; its chunks do). Cached per
    (id, updated_at). Unreadable: {} (the legacy default: Zach's, private)."""
    got = _fetch(call, source_id, updated_at)
    return got[0] if got else {}


def source_text(call: Runtime, source_id: str, updated_at: str = "") -> str | None:
    """A source's text (cached with its metadata). None when unreadable."""
    got = _fetch(call, source_id, updated_at)
    return got[1] if got else None


def visible_sources(call: Runtime, sources: list[dict[str, Any]], person: str | None,
                    role: str | None) -> list[dict[str, Any]]:
    """The runtime's source list, cut to what this person may see. Each kept source gets `owner`
    and `audience`. A source with a sharing-table row needs no metadata fetch."""
    rows = acl_rows(s.get("source_id") for s in sources)
    need = [s for s in sources if s.get("source_id") not in rows and "metadata" not in s]
    if need:
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=8) as pool:
            metas = list(pool.map(lambda s: source_meta(call, s["source_id"], str(s.get("updated_at") or "")), need))
        for s, md in zip(need, metas):
            s["metadata"] = md
    out = []
    import fences
    fs = fences.get()
    on = fences.enabled() and not (person is None and role is None)
    if on:   # read the notes this viewer could see but doesn't own, in parallel (cached by updated_at)
        todo = []
        for s in sources:
            o, a_ = acl_of(s.get("metadata"), s.get("source_id"), rows)
            if visible_acl(o, a_, person, role) and not (o and person and str(o).lower() == str(person).lower()):
                todo.append(s)
        if todo:
            import concurrent.futures as cf
            with cf.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda s: source_text(call, str(s.get("source_id") or ""), str(s.get("updated_at") or "")),
                              todo))
    for s in sources:
        owner, aud = acl_of(s.get("metadata"), s.get("source_id"), rows)
        if not visible_acl(owner, aud, person, role):
            continue
        if on and not (owner and person and str(owner).lower() == str(person).lower()):
            # Fences read the note itself, never just its title (10-01: "Hale Therapist", whose title
            # names no one, reached a non-parent adult while its text is about Ava). Unreadable text and
            # a title that touches a fence: held back (fail closed).
            title = str(s.get("title") or "")
            text = source_text(call, str(s.get("source_id") or ""), str(s.get("updated_at") or ""))
            if text is None and fs.touches(title):
                continue
            if not fs.admits(person, role, owner, text=f"{title}\n{text or ''}", item=s.get("source_id")):
                continue
        out.append({**s, "owner": owner, "audience": aud})
    return out


# ── sharing ───────────────────────────────────────────────────────────────────────────────────

def people() -> list[dict[str, Any]]:
    try:
        import household_identity as hh
        return hh.get_store().people()
    except Exception:  # noqa: BLE001
        return []


def _name(pid: str | None, folks: list[dict[str, Any]]) -> str:
    hit = next((p for p in folks if p["id"] == pid), None)
    return hit["name"] if hit else (pid or "titan")


def targets(text: Any, folks: list[dict[str, Any]] | None = None) -> tuple[Any, str | None]:
    """'with Robin' / 'household' / 'adults' / 'Robin and Ava' → (audience to add, refusal)."""
    folks = people() if folks is None else folks
    t = re.sub(r"^\s*(with|to)\s+", "", str(text or "").strip(), flags=re.I).strip().rstrip(".")
    low = t.lower()
    if not low:
        return None, "Say who to share it with: a name, the adults, or the household."
    if low in ("household", "the household", "everyone", "everybody", "family", "the family", "all"):
        return "household", None
    if low in ("adults", "the adults", "grown-ups", "the grown-ups", "parents"):
        return "adults", None
    ids, unknown, kids = [], [], []
    for part in [x.strip() for x in re.split(r",|\band\b|&", t) if x.strip()]:
        pl = part.lower()
        hit = next((p for p in folks if pl in (p["id"].lower(), p["name"].lower())
                    or p["name"].lower().split()[0] == pl), None)
        if not hit:
            unknown.append(part)
        elif hit.get("role") in HOUSEHOLD_ONLY_ROLES:
            kids.append(hit["name"])
        else:
            ids.append(hit["id"])
    if unknown:
        known = ", ".join(p["name"] for p in folks) or "nobody yet"
        return None, f"No one in the household called {', '.join(unknown)}. The people are: {known}."
    if kids:
        return None, (f"{' and '.join(kids)} can only read household notes. Share it with the household "
                      "instead (everyone, kids and shared devices too), or keep it with the adults.")
    return (ids or None), (None if ids else "Say who to share it with.")


def who_sees(owner: str | None, audience: str, viewer: str | None = None,
             folks: list[dict[str, Any]] | None = None) -> str:
    """Plain words for who can see an item."""
    folks = people() if folks is None else folks
    me = lambda pid: "you" if viewer and pid == viewer else _name(pid, folks)  # noqa: E731
    if audience == "household":
        return "everyone in the household (kids and shared devices too)"
    if audience == "adults":
        grown = [me(p["id"]) for p in folks if p.get("role") in ("admin", "adult")]
        return "the adults" + (f" ({', '.join(grown)})" if grown else "")
    if audience == "admins":
        admins = [me(p["id"]) for p in folks if p.get("role") == "admin"] or ["the admin"]
        return "only " + " and ".join(admins)
    ids = json.loads(audience) if audience.startswith("[") else []
    names = ([me(owner)] if owner else []) + [me(i) for i in ids if i != owner]
    if not names:
        return "nobody"
    return "only " + names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def share(source_id: str, meta: dict[str, Any] | None, add: Any, person: str | None, role: str | None,
          folks: list[dict[str, Any]] | None = None, text: str | None = None) -> tuple[str | None, str]:
    """Widen an item's audience. → (new audience or None, the sentence to say). With its text, an item
    behind someone else's fence isn't shared (#356); behind the sharer's own fence, it goes past that
    fence for this item only (an exception, logged)."""
    owner, aud = acl_of(meta, source_id)
    if not visible_acl(owner, aud, person, role):
        return None, "not found"
    if not can_edit(owner, aud, person, role):
        who = _name(owner, people() if folks is None else folks) if owner else "the admin"
        return None, f"Not shared: only {who} or the admin can share it."
    own_fences = []
    if text:
        import fences
        if fences.enabled():
            fs = fences.get()
            blocking = fs.blocking_share(text, person)
            if blocking:
                f = blocking[0]
                whose = _name(f["owner"], people() if folks is None else folks) if f["owner"] else "the household"
                names = json.loads(f["names"] or "[]")
                return None, (f"Not shared: it's about {names[0] if names else 'something'}, which {whose} keeps private.")
            own_fences = [f for f in fs.touches(text) if f["kind"] == "entity" and f["owner"] == person]
    new = _aud.widen(aud, add)
    if new.startswith("[") and owner is None and person:
        # titan's own notes have no owner, and a list replaces "admins": keep the person sharing it.
        new = _aud.widen(new, [person])
    set_acl(source_id, owner, new, by=person, why="share")
    _refile_note(source_id, owner, new)
    for f in own_fences:
        import fences
        fences.get().except_item(source_id, f["id"], new, by=person)
    return new, f"Shared. Now {who_sees(owner, new, person, folks)} can see it."


def _refile_note(source_id: str, owner: str | None, audience: str) -> None:
    """A note's world facts follow its new audience (note_claims.refile). Best effort; only where note claims are on
    (AGENT_NOTE_CLAIMS, titan's agent.env), so a test's share never writes into a live world."""
    if not str(source_id).startswith("note-") or not owner:
        return
    if os.environ.get("AGENT_NOTE_CLAIMS", "").strip().lower() not in ("1", "on", "true", "yes"):
        return
    try:
        import note_claims
        note_claims.refile(source_id, owner, audience)
    except Exception as exc:  # noqa: BLE001 - the share stands whatever this does
        print(f"[kb_privacy] note facts not refiled: {type(exc).__name__}", file=sys.stderr)


def unshare(source_id: str, meta: dict[str, Any] | None, remove: Any, person: str | None, role: str | None,
            folks: list[dict[str, Any]] | None = None) -> tuple[str | None, str]:
    """Narrow an item back: drop named people, or (no names) back to its default (private for a
    person's item, admins for titan's own)."""
    owner, aud = acl_of(meta, source_id)
    if not visible_acl(owner, aud, person, role):
        return None, "not found"
    if not can_edit(owner, aud, person, role):
        return None, "Not changed: only its owner or the admin can change who sees it."
    default = "private" if owner else "admins"
    new = default
    drop = _aud.normalize(remove, default="") if remove else ""
    if drop.startswith("[") and aud.startswith("["):
        left = sorted(set(json.loads(aud)) - set(json.loads(drop)))
        new = json.dumps(left) if left else default
    set_acl(source_id, owner, new, by=person, why="unshare")
    _refile_note(source_id, owner, new)
    return new, f"Done. Now {who_sees(owner, new, person, folks)} can see it."
