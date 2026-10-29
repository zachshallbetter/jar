#!/usr/bin/env python3
"""Household connectors: write items from live sources into titan with provenance.

Design: docs/household-connectors.md. A connector (for example Jellybean's calendar, mail,
downloads and browsing collectors on the Mac) sends items. This runner is the only code that
writes them. Each item becomes at most:

  - a KB note        (retrieval /v1/retrieval/index, upsert by id)   when it has `text`
  - a record node    (record /v1/nodes, create or PATCH)             when it has `fields`
  - a timeline event (event /v1/events, stable event_id)             when it has `timeline`

It carries `{source, source_id, observed_at, account, owner, audience}` provenance in all three:
the owner is the person whose device pushed the batch (Zach when none is given) and the audience
"private" (scripts/audience.py); the timeline event also names them as payload.person. A SQLite ledger
(one row per item) makes a re-sync with no change a no-op, and lets `forget(source)` remove
exactly what that source wrote. The event runtime is append-only, so removals append
household.retracted events that the timeline readers apply.

Item fields: source_id (required), observed_at, kind, title, text, fields (dict), links
([relation, entity name] pairs), timeline (one line), account, deleted (bool).

Owner: the person whose device sent the batch (the connectors API's identity). Their KB notes are
private to them (kb_privacy.py; `share` widens one). Zach's items keep their ids; anyone else's are
keyed by "<person>:<source_id>", so two people's copies of one shared calendar event stay apart.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_time  # noqa: E402

LEDGER_PATH = Path(os.environ.get("CONNECTORS_LEDGER", "/ai/datasets/connectors/ledger.db"))
SOURCES = {"calendar", "mail", "downloads", "browsing", "daily", "scans", "fixture",   # no "datasets": nothing produced it (#1626)
           "github_stars",   # titan's own daily sync of the admin's GitHub stars (github_stars.py, #707)
           "youtube"}        # a person's liked videos and named playlists, read-only (youtube_sync.py, #470)
MAX_ITEMS = 500
MAX_TEXT = 20000
# "account" alone is a mail/calendar account name (iCloud, a Gmail address), not a secret.
SECRET_KEYS = re.compile(r"^(code|password|passcode|passphrase|pin|combination|account_number|"
                         r"routing_number|card(_number)?|cvv|ssn|\w+_(code|password|pin|token|secret))$", re.I)

_lock = threading.Lock()
Runtime = Callable[[str, str, str, "dict[str, Any] | None"], dict]


def _default_runtime(service: str, path: str, method: str, payload: dict | None) -> dict:
    import agent_runtime_lib as lib
    return lib._runtime_request(service, path, method, payload)


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = path or LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("""CREATE TABLE IF NOT EXISTS items (
        source TEXT NOT NULL, source_id TEXT NOT NULL, content_hash TEXT NOT NULL,
        item_id TEXT NOT NULL, kb INTEGER NOT NULL DEFAULT 0, node INTEGER NOT NULL DEFAULT 0,
        event_id TEXT, account TEXT, observed_at TEXT, synced_at REAL NOT NULL, deleted_at REAL,
        PRIMARY KEY (source, source_id))""")
    # Every version of an item's structured fields (never its body text), so changes.py can say what changed
    # since a time (#394; design docs/envelopes-and-scenarios.md §5 F2). Kept VERSIONS_DAYS.
    con.execute("""CREATE TABLE IF NOT EXISTS versions (
        source TEXT NOT NULL, source_id TEXT NOT NULL, item_id TEXT NOT NULL, change TEXT NOT NULL,
        digest TEXT, title TEXT, fields TEXT, observed_at TEXT, owner TEXT, audience TEXT, at REAL NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS versions_item ON versions (item_id, at)")
    con.execute("CREATE INDEX IF NOT EXISTS versions_at ON versions (at)")
    # Stays forgotten (Zach, 10-02, jellybean#233/#305): a forgotten item never comes back from a device, and a
    # forgotten source is off for that person until they turn it back on. Per owner; upstream ids (the device's).
    con.execute("CREATE TABLE IF NOT EXISTS tombstones (owner TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL, "
                "at REAL NOT NULL, PRIMARY KEY (owner, source, source_id))")
    con.execute("CREATE TABLE IF NOT EXISTS sources_off (owner TEXT NOT NULL, source TEXT NOT NULL, at REAL NOT NULL, "
                "PRIMARY KEY (owner, source))")
    # A repeating calendar event's one KB item (10-02 audit: Ballet ×28 crowded "When is Ava's ballet?" out of the
    # top 3): its latest occurrence's text and provenance, so the series note keeps its notes and place.
    con.execute("CREATE TABLE IF NOT EXISTS series (source TEXT NOT NULL, series_key TEXT NOT NULL, owner TEXT, "
                "title TEXT, text TEXT, prov TEXT, at REAL NOT NULL, PRIMARY KEY (source, series_key))")
    # The ledger's epoch (10-09 reset): a random id a device compares with the one it last saw; when it changes, titan's
    # ledger was wiped and the device sends everything again. Bumped only by household_reset.py --apply.
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    # Per-item ingest errors by class (the text before the first ':', never a title), counted per day, source and owner
    con.execute("CREATE TABLE IF NOT EXISTS ingest_errors (day TEXT NOT NULL, owner TEXT NOT NULL, source TEXT NOT NULL, "
                "class TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (day, owner, source, class))")
    # #1612 transition: a device that has sent a calendar batch with a "sync" object proves its deletions from then on
    # (strict), whatever it sends later, so a downgraded app can't quietly relax it. Per owner and device.
    con.execute("CREATE TABLE IF NOT EXISTS proof_devices (owner TEXT NOT NULL, device TEXT NOT NULL, source TEXT NOT NULL, "
                "at REAL NOT NULL, PRIMARY KEY (owner, device, source))")
    # Deletions applied the old way (a batch with no proof fields from a device never strict), counted per day
    con.execute("CREATE TABLE IF NOT EXISTS legacy_deletes (day TEXT NOT NULL, owner TEXT NOT NULL, source TEXT NOT NULL, "
                "n INTEGER NOT NULL, PRIMARY KEY (day, owner, source))")
    if "owner" not in {r[1] for r in con.execute("PRAGMA table_info(items)")}:
        con.execute("ALTER TABLE items ADD COLUMN owner TEXT")
        con.execute("ALTER TABLE items ADD COLUMN upstream_id TEXT")
    # backfill: a non-default owner's key is "<owner>:<id>"; the default owner's is the bare id. Every open, not only
    # the first: 10-04, rows left without an owner or upstream id made tombstone_item and forget(owner=) skip them
    # silently (299 of 326 tombstones in the KB cleanup).
    legacy = con.execute("SELECT source, source_id, owner FROM items WHERE owner IS NULL OR upstream_id IS NULL").fetchall()
    for src, key, have in legacy:
        owner = None if have else con.execute("SELECT owner FROM versions WHERE source=? AND source_id=? AND owner IS NOT "
                                              "NULL ORDER BY at DESC LIMIT 1", (src, key)).fetchone()
        o = have or (owner[0] if owner else OWNER_DEFAULT)
        up = key[len(o) + 1:] if (o != OWNER_DEFAULT and key.startswith(f"{o}:")) else key
        con.execute("UPDATE items SET owner=?, upstream_id=? WHERE source=? AND source_id=?", (o, up, src, key))
    if legacy:
        con.commit()
    return con


VERSIONS_DAYS = 90


def _version(con: sqlite3.Connection, source: str, key: str, iid: str, change: str, digest: str | None,
             title: str | None, fields: dict[str, Any] | None, observed_at: Any, owner: str) -> None:
    con.execute("INSERT INTO versions (source, source_id, item_id, change, digest, title, fields, observed_at, owner, "
                "audience, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (source, key, iid, change, digest, title, json.dumps(fields, sort_keys=True, default=str) if fields else None,
                 observed_at, owner, "private", time.time()))


def item_id(source: str, source_id: str) -> str:
    return f"cx-{source}-{hashlib.sha1(source_id.encode()).hexdigest()[:16]}"


def _clean_fields(fields: Any) -> dict[str, Any]:
    if not isinstance(fields, dict):
        return {}
    out = {}
    for k, v in fields.items():
        if v in (None, "", [], {}):
            continue
        out[str(k)] = "(kept out of titan)" if SECRET_KEYS.match(str(k)) else v
    return out


def _hash(item: dict[str, Any]) -> str:
    keep = {k: item.get(k) for k in ("kind", "title", "text", "fields", "links", "timeline", "account",
                                      "observed_at", "deleted")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()


def _is_conflict(exc: Exception) -> bool:
    return "409" in str(exc) or "already exists" in str(exc).lower()


def _edge_exists(call: Runtime, source: str, target: str, rel: str) -> bool:
    """Whether the record runtime already holds this link."""
    try:
        r = call("record", f"/v1/edges/trace/{source}", "GET", None)
    except Exception:  # noqa: BLE001
        return False
    return any(e.get("source_id") == source and e.get("target_id") == target and e.get("relation_type") == rel
               for e in (r.get("edges") or []))


def hold_items(item_ids: list[str], ledger: Path | None = None) -> list[str]:
    """Delete my data (#1080): tombstone these synced items so their devices don't send them back while the deletion
    can still be cancelled. Ledger only (their KB notes are already stashed and removed). → the ids it held."""
    held = []
    with _lock:
        con = _db(ledger)
        try:
            for i in item_ids:
                if _tombstone_row(con, i):
                    held.append(i)
            con.commit()
        finally:
            con.close()
    return held


def release_items(item_ids: list[str], ledger: Path | None = None) -> int:
    """Cancel of a deletion (#1080): undo hold_items, so the items sync as before."""
    n = 0
    with _lock:
        con = _db(ledger)
        try:
            for i in item_ids:
                r = con.execute("SELECT source, owner, upstream_id FROM items WHERE item_id=?", (i,)).fetchone()
                if not r:
                    continue
                con.execute("DELETE FROM tombstones WHERE owner=? AND source=? AND source_id=?", (r[1], r[0], r[2]))
                con.execute("UPDATE items SET deleted_at=NULL WHERE item_id=?", (i,))
                n += 1
            con.commit()
        finally:
            con.close()
    return n


def tombstone_item(item_id: str, ledger: Path | None = None, runtime: Runtime | None = None) -> dict[str, str] | None:
    """A synced item forgotten on titan (the KB's forget): it never comes back from its device. → the tombstone, or
    None when the ledger has no such item (said on stderr, so a forget that won't stick is never silent).

    A calendar occurrence's series note is rebuilt (gone once nothing is left), and forgetting a series note forgets
    every occurrence behind it (#850, 10-04: 23 series notes outlived their forgotten occurrences)."""
    call = runtime or _default_runtime
    with _lock:
        con = _db(ledger)
        try:
            skey = _series_of_note(con, item_id)
            if skey:
                keys = [k for (k,) in con.execute("SELECT source_id FROM items WHERE source='calendar' AND deleted_at IS NULL "
                                                  "AND source_id LIKE ?", (skey.replace("%", "") + "@%",))
                        if series_key("calendar", k) == skey]
                ids = [r[0] for k in keys for r in con.execute("SELECT item_id FROM items WHERE source='calendar' AND "
                                                                "source_id=?", (k,))]
                stones = [_tombstone_row(con, i) for i in ids]
                con.commit()
                _index_series(con, call, "calendar", skey)
                got = [x for x in stones if x]
                return {**got[0], "source_id": skey, "occurrences": str(len(got))} if got else None
            r = con.execute("SELECT source, owner, upstream_id, source_id FROM items WHERE item_id=?", (item_id,)).fetchone()
            if not r or not r[1] or not r[2]:
                print(f"tombstone skipped: {item_id} is not in the connectors ledger", file=sys.stderr)
                return None
            stone = _tombstone_row(con, item_id)
            con.commit()
            sk = series_key(r[0], r[3])
            if sk:
                _index_series(con, call, r[0], sk)
            return stone
        finally:
            con.close()


def _tombstone_row(con: sqlite3.Connection, item_id: str) -> dict[str, str] | None:
    r = con.execute("SELECT source, owner, upstream_id FROM items WHERE item_id=?", (item_id,)).fetchone()
    if not r or not r[1] or not r[2]:
        return None
    con.execute("INSERT OR REPLACE INTO tombstones VALUES (?,?,?,?)", (r[1], r[0], r[2], time.time()))
    con.execute("UPDATE items SET deleted_at=? WHERE item_id=?", (time.time(), item_id))
    return {"owner": r[1], "source": r[0], "source_id": r[2]}


def _series_of_note(con: sqlite3.Connection, iid: str) -> str | None:
    """The series key whose one KB note has this id, or None."""
    if not str(iid).startswith("cx-calendar-"):
        return None
    for (skey,) in con.execute("SELECT series_key FROM series WHERE source='calendar'"):
        if item_id("calendar", f"series:{skey}") == iid:
            return skey
    return None


def tombstones(owner: str, since: float = 0.0, ledger: Path | None = None) -> dict[str, Any]:
    """What this person's devices should drop: forgotten items since `since`, and sources that are off."""
    con = _db(ledger)
    try:
        items = [{"source": s, "source_id": i, "at": at} for s, i, at in con.execute(
            "SELECT source, source_id, at FROM tombstones WHERE owner=? AND at>? ORDER BY at", (owner, since))]
        off = [s for (s,) in con.execute("SELECT source FROM sources_off WHERE owner=? ORDER BY source", (owner,))]
        return {"tombstones": items, "sources_off": off, "now": time.time(), "ledger_epoch": _epoch(con)}
    finally:
        con.close()


def sources_off(owner: str, ledger: Path | None = None) -> list[str]:
    return tombstones(owner, float("inf"), ledger)["sources_off"]


def set_sources_off(owner: str, sources: list[str], ledger: Path | None = None) -> list[str]:
    """Replace this person's off list (turning a source back on removes it; item tombstones stay)."""
    keep = sorted({s for s in sources if s in SOURCES})
    with _lock:
        con = _db(ledger)
        try:
            con.execute("DELETE FROM sources_off WHERE owner=?", (owner,))
            con.executemany("INSERT INTO sources_off VALUES (?,?,?)", [(owner, s, time.time()) for s in keep])
            con.commit()
        finally:
            con.close()
    return keep


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:48] or "entity"


OWNER_DEFAULT = "zach"   # household_identity.ADMIN_PERSON_ID: every connector so far is Zach's Mac


def _epoch(con: sqlite3.Connection) -> str:
    row = con.execute("SELECT value FROM meta WHERE key='ledger_epoch'").fetchone()
    if row:
        return row[0]
    import secrets
    e = secrets.token_hex(8)
    con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('ledger_epoch', ?)", (e,))
    con.commit()
    return con.execute("SELECT value FROM meta WHERE key='ledger_epoch'").fetchone()[0]


def ledger_epoch(ledger: Path | None = None) -> str:
    """The ledger's epoch: devices resend everything when it differs from the one they last saw."""
    with _lock:
        con = _db(ledger)
        try:
            return _epoch(con)
        finally:
            con.close()


def bump_epoch(ledger: Path | None = None) -> str:
    """A new epoch (household_reset.py --apply only): every device's next sync is a full resend. → the new epoch."""
    import secrets
    e = secrets.token_hex(8)
    with _lock:
        con = _db(ledger)
        try:
            con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('ledger_epoch', ?)", (e,))
            con.commit()
        finally:
            con.close()
    return e


_CLASS_RX = re.compile(r"[^A-Za-z0-9 _.()-]")


def error_class(err: Any) -> str:
    """An error's class for counting: the text before its first ':', letters, digits and a little punctuation only,
    at most 60 characters ("HTTP Error 500", "source_id is required"). Never a title or an id."""
    head = str(err or "").split(":", 1)[0]
    return (_CLASS_RX.sub("", head).strip() or "unknown")[:60]


def _count_error(con: sqlite3.Connection, owner: str, source: str, err: Any) -> None:
    con.execute("INSERT INTO ingest_errors (day, owner, source, class, n) VALUES (?,?,?,?,1) ON CONFLICT(day, owner, "
                "source, class) DO UPDATE SET n=n+1", (local_time.day(), owner, source,
                                                       error_class(err)))


def state(owner: str, days: int = 7, ledger: Path | None = None) -> dict[str, Any]:
    """GET /v1/connectors/state: the ledger epoch, and this person's ingest errors by day, source and class (counts)."""
    with _lock:
        con = _db(ledger)
        try:
            since = local_time.day(time.time() - days * 86400)
            errs = [{"day": d, "source": s, "class": c, "count": n} for d, s, c, n in con.execute(
                "SELECT day, source, class, n FROM ingest_errors WHERE owner=? AND day>=? ORDER BY day, source, class",
                (owner, since))]
            legacy = [{"day": d, "source": s_, "count": n} for d, s_, n in con.execute(
                "SELECT day, source, n FROM legacy_deletes WHERE owner=? AND day>=? ORDER BY day, source", (owner, since))]
            return {"ledger_epoch": _epoch(con), "errors": errs, "legacy_calendar_deletes": legacy}
        finally:
            con.close()


# #1612: a calendar event missing from a device's read is not a deleted event. 10-12 the Mac slept mid-sync and 10-13
# "what's on my agenda" said "no events"; a partial or failed read must never remove anything on titan. A calendar
# item's `deleted` is applied only with proof:
#   - a positive delete signal: the item's "deleted_reason" is one the device saw happen (DELETE_SIGNALS: the event
#     was deleted or cancelled on the device, or by Jellybean's own delete), or
#   - a complete sync window: the batch's "sync" says the device read every calendar it syncs, successfully, over
#     [window_start, window_end), and the item's occurrence (its source_id "<uid>@<start>") starts inside it.
# Anything else (a bare `deleted: true` from a window diff, a partial read, a window that doesn't cover it) is kept
# and counted as "kept_absent" (its source_id in "kept_absent_ids"); the device can send it again with proof. Other
# sources are unchanged.
# Transition (until every Mac runs jellybean#1286): a batch with no proof fields at all (no "sync" object, no
# deleted_reason on any item), from a device that has never sent a "sync" object, is today's Jellybean: its deletions
# apply as before and are counted as legacy_calendar_deletes (the batch's stats, the log, /v1/connectors/state). The
# first batch with a "sync" object makes that device strict for good (proof_devices).
DELETE_SIGNALS = frozenset({"deleted", "cancelled", "canceled", "user_deleted", "jellybean_deleted"})
PROOF_SOURCES = frozenset({"calendar"})


def _sync_window(sync: Any) -> tuple[datetime, datetime] | None:
    """A batch's complete sync window, or None when it doesn't prove absence: it must say complete, ok (no error), and
    give a start before its end."""
    if not isinstance(sync, dict) or sync.get("complete") is not True or sync.get("error"):
        return None
    if sync.get("ok", True) is not True or sync.get("partial"):
        return None
    lo, hi = local_time.parse_iso(sync.get("window_start")), local_time.parse_iso(sync.get("window_end"))
    if lo is None or hi is None:
        return None
    lo = lo if lo.tzinfo else lo.replace(tzinfo=local_time.zone())
    hi = hi if hi.tzinfo else hi.replace(tzinfo=local_time.zone())
    return (lo, hi) if lo < hi else None


def deletion_proven(source: str, raw: dict[str, Any], window: tuple[datetime, datetime] | None) -> bool:
    """Whether this item's `deleted` may remove it on titan (#1612). Only calendar items need proof."""
    if source not in PROOF_SOURCES:
        return True
    if str(raw.get("deleted_reason") or "").strip().lower() in DELETE_SIGNALS:
        return True
    if window is None:
        return False
    sid = str(raw.get("source_id") or "")
    _, sep, when = sid.rpartition("@")
    start = local_time.parse_iso(when) if sep else None
    if start is None:
        return False   # not an occurrence titan can place in the window: unproven
    start = start if start.tzinfo else start.replace(tzinfo=local_time.zone())
    return window[0] <= start < window[1]


def ingest(source: str, items: list[dict[str, Any]], runtime: Runtime | None = None,
           ledger: Path | None = None, owner: str | None = None, device_id: str | None = None,
           sync: Any = None) -> dict[str, Any]:
    """Write a batch from one source for one person (owner; default Zach), from one of their devices (device_id:
    the device a reference points to, #446). Returns counts plus per-item errors (by source_id). sync: the batch's
    own account of its read (#1612; a calendar deletion needs it or a delete signal: deletion_proven)."""
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}; expected one of {sorted(SOURCES)}")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} items per batch")
    call = runtime or _default_runtime
    stats = {"written": 0, "unchanged": 0, "removed": 0, "forgotten": 0, "errors": []}
    if source in PROOF_SOURCES:
        stats["kept_absent"], stats["kept_absent_ids"] = 0, []   # the ids: the device keeps them to prove later
    window = _sync_window(sync) if source in PROOF_SOURCES else None
    who = owner or OWNER_DEFAULT
    with _lock:
        con = _db(ledger)
        try:
            legacy = False
            if source in PROOF_SOURCES:
                dev = str(device_id or "")
                if isinstance(sync, dict):
                    con.execute("INSERT OR IGNORE INTO proof_devices VALUES (?,?,?,?)", (who, dev, source, time.time()))
                proof = isinstance(sync, dict) or any(isinstance(i, dict) and "deleted_reason" in i for i in items)
                strict = con.execute("SELECT 1 FROM proof_devices WHERE owner=? AND device=? AND source=?",
                                     (who, dev, source)).fetchone()
                legacy = not proof and not strict
                stats["legacy_calendar_deletes"] = 0
            off = con.execute("SELECT 1 FROM sources_off WHERE owner=? AND source=?", (who, source)).fetchone()
            gone = {r[0] for r in con.execute("SELECT source_id FROM tombstones WHERE owner=? AND source=?", (who, source))}
            replaced: set[str] = set()
            if source == "mail":
                # an account titan now reads from the server itself (#955 step 6): the Mac's copy is skipped, not stored
                try:
                    import mail_server
                    replaced = mail_server.mac_accounts(who)
                except Exception:  # noqa: BLE001 - no server connector here: nothing is replaced
                    replaced = set()
            for raw in items:
                sid = str(raw.get("source_id") or "").strip()
                if not sid:
                    stats["errors"].append({"source_id": None, "error": "source_id is required"})
                    _count_error(con, who, source, "source_id is required")
                    continue
                if replaced and " ".join(str(raw.get("account") or "").split()).lower() in replaced:
                    stats["replaced"] = stats.get("replaced", 0) + 1
                    continue
                if off or sid in gone:
                    stats["forgotten"] += 1      # forgotten stays forgotten: never written back, changed or not
                    continue
                if raw.get("deleted") and legacy:
                    stats["legacy_calendar_deletes"] += 1   # today's Jellybean (no proof fields): as before
                elif raw.get("deleted") and not deletion_proven(source, raw, window):
                    stats["kept_absent"] += 1    # missing from a read is not deleted (#1612): nothing removed
                    stats["kept_absent_ids"].append(sid)
                    continue
                try:
                    outcome = _ingest_one(con, call, source, sid, raw, owner or OWNER_DEFAULT, device_id)
                    stats[outcome] += 1
                except Exception as exc:  # noqa: BLE001 - one bad item must not sink the batch
                    stats["errors"].append({"source_id": sid, "error": str(exc)[:300]})
                    _count_error(con, who, source, exc)
                    print(f"[connectors] {source}: an item refused ({error_class(exc)})", file=sys.stderr)
            if stats.get("legacy_calendar_deletes"):
                con.execute("INSERT INTO legacy_deletes (day, owner, source, n) VALUES (?,?,?,?) ON CONFLICT(day, owner, "
                            "source) DO UPDATE SET n=n+excluded.n", (local_time.day(), who, source,
                                                                     stats["legacy_calendar_deletes"]))
                print(f"[connectors] {source}: {stats['legacy_calendar_deletes']} legacy deletions (no proof fields)",
                      file=sys.stderr)
            con.execute("DELETE FROM versions WHERE at < ?", (time.time() - VERSIONS_DAYS * 86400,))
            con.execute("DELETE FROM ingest_errors WHERE day < ?",
                        (local_time.day(time.time() - 90 * 86400),))
            stats["ledger_epoch"] = _epoch(con)
            con.commit()
        finally:
            con.close()
    return stats


def live_upstream_ids(owner: str, source: str, prefix: str, ledger: Path | None = None) -> list[str]:
    """The owner's own items of a source that are on titan now (not deleted) whose device id starts with `prefix`:
    read-only, for the trip push's "where did this event go" (jellybean#753). Only that person's own synced items."""
    esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    con = _db(ledger)
    try:
        return [r[0] for r in con.execute("SELECT upstream_id FROM items WHERE source=? AND owner=? AND deleted_at IS NULL "
                                          "AND upstream_id LIKE ? ESCAPE '\\'", (source, owner, esc + "%"))]
    finally:
        con.close()


def series_key(source: str, key: str) -> str | None:
    """A calendar occurrence's series ("<uid>@<start>" → "<uid>", with its owner prefix), else None."""
    if source != "calendar" or "@" not in key:
        return None
    return key.rsplit("@", 1)[0]


def _when(start: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    try:
        import local_time
        zone = local_time.zone()
    except Exception:  # noqa: BLE001
        from datetime import timezone
        zone = timezone.utc
    return d.astimezone(zone) if d.tzinfo else d.replace(tzinfo=zone)   # naive: the household's time


def _clock(d: datetime) -> str:
    return d.strftime("%I:%M %p").lstrip("0")


def series_text(title: str, starts: list[datetime], now: datetime, latest: str) -> str:
    """One note for a repeating event: when it repeats, the next one (or the last), how many are synced, and the
    latest occurrence's own lines (place, calendar, notes) without its date."""
    days = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"]
    starts = sorted(starts)
    by_day: dict[int, set[str]] = {}
    for d in starts:
        by_day.setdefault(d.weekday(), set()).add(_clock(d))
    times = {t for ts in by_day.values() for t in ts}
    names = [days[i] for i in sorted(by_day)]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    repeats = f"{joined} at {next(iter(times))}" if len(times) == 1 else \
        "; ".join(f"{days[i]} at {', '.join(sorted(by_day[i]))}" for i in sorted(by_day))
    up = [d for d in starts if d >= now]
    when = (f"Next: {up[0]:%a, %b} {up[0].day}, {up[0].year}, {_clock(up[0])}" if up else
            f"Last: {starts[-1]:%a, %b} {starts[-1].day}, {starts[-1].year}, {_clock(starts[-1])}")
    body = [ln for ln in str(latest or "").splitlines()[1:] if not re.match(r"^\w{3}, \w{3} \d{1,2}, \d{4}", ln)]
    if len(starts) == 1:
        # one occurrence synced says nothing about a rhythm (10-02: "Repeats: Wednesdays at 8:45 AM (1 synced)" for a
        # one-time visit): just when it is or was
        d = starts[0]
        return "\n".join([title, f"{'When' if d >= now else 'Was'}: {d:%a, %b} {d.day}, {d.year}, {_clock(d)}"]
                         + body).strip()
    return "\n".join([title, f"Repeats: {repeats} ({len(starts)} synced, {starts[0]:%b} {starts[0].day} to "
                              f"{starts[-1]:%b} {starts[-1].day})", when] + body).strip()


def _starts(con: sqlite3.Connection, source: str, skey: str) -> list[datetime]:
    """A series' live occurrences' starts, from the ledger."""
    starts = []
    for (key,) in con.execute("SELECT source_id FROM items WHERE source=? AND deleted_at IS NULL AND source_id LIKE ?",
                              (source, skey.replace("%", "") + "@%")):
        if series_key(source, key) != skey:
            continue
        f = con.execute("SELECT fields FROM versions WHERE source=? AND source_id=? AND fields IS NOT NULL "
                        "ORDER BY at DESC LIMIT 1", (source, key)).fetchone()
        d = _when((json.loads(f[0]) if f else {}).get("start") or key.rsplit("@", 1)[1])
        if d is not None:
            starts.append(d)
    return starts


def _same_title(con: sqlite3.Connection, source: str, skey: str, owner: Any, title: str) -> list[str]:
    return [r[0] for r in con.execute("SELECT series_key FROM series WHERE source=? AND series_key<>? AND owner IS ? "
                                      "AND lower(trim(title))=lower(trim(?))", (source, skey, owner, title))]


def _index_series(con: sqlite3.Connection, call: Runtime, source: str, skey: str, _peers: bool = True) -> None:
    """Write (or remove) a series' one KB item from its live occurrences in the ledger. Twins, the same event on two
    calendars (a series of the same person with the same title on a shared day; 10-02 audit: every holiday twice,
    from two holiday calendars), get one item: the first by key. When it goes, the next one is written."""
    sid_ = item_id(source, f"series:{skey}")
    row = con.execute("SELECT owner, title, text, prov FROM series WHERE source=? AND series_key=?", (source, skey)).fetchone()
    starts = _starts(con, source, skey)
    days = {d.date() for d in starts}
    peers = _same_title(con, source, skey, row[0], row[1]) if row else []
    twins = [t for t in peers if days & {d.date() for d in _starts(con, source, t)}] if starts else []
    if not starts or not row or any(t < skey for t in twins):
        try:
            _kb_delete(call, sid_)
        except Exception:  # noqa: BLE001 - nothing there
            pass
        if _peers:   # gone, or deferring: a later twin may be the one to write now
            for t in (twins if starts else peers):
                if t > skey or not starts:
                    _index_series(con, call, source, t, _peers=False)
        return
    for t in twins:   # this one is kept: a later twin written earlier (the migration's order) goes
        try:
            _kb_delete(call, item_id(source, f'series:{t}'))
        except Exception:  # noqa: BLE001 - nothing there
            pass
    owner, title, latest, prov = row
    prov = json.loads(prov or "{}")
    # A ledger row from before items had an owner reads NULL: it's the default owner's (the backfill's rule). A KB item
    # with no owner is private to nobody, so nobody saw it (10-02: 52 series hidden, "When is Ava's ballet?" missed).
    owner = owner or prov.get("owner") or OWNER_DEFAULT
    now = _when(time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())) or datetime.now()
    md = {**prov, "kind": "series", "series": skey, "occurrences": len(starts), "owner": owner, "audience": "private"}
    call("retrieval", "/v1/retrieval/index", "POST", {
        "source_id": sid_, "parent_record_id": sid_, "source_type": f"connector:{source}", "title": title,
        "text": series_text(title, starts, now, latest), "metadata": md})
    _acl_row(sid_, md)


def _ingest_one(con: sqlite3.Connection, call: Runtime, source: str, sid: str,
                raw: dict[str, Any], owner: str = OWNER_DEFAULT, device_id: str | None = None) -> str:
    # The ledger and the ids key one person's copy of an item apart from anyone else's.
    key = sid if owner == OWNER_DEFAULT else f"{owner}:{sid}"
    iid = item_id(source, key)
    row = con.execute("SELECT content_hash, kb, node, event_id, deleted_at FROM items "
                      "WHERE source=? AND source_id=?", (source, key)).fetchone()
    if raw.get("deleted"):
        if not row or row[4]:
            return "unchanged"
        _remove(call, iid, kb=bool(row[1]), node=bool(row[2]), event_id=row[3])
        _forget_reference(iid, owner)
        con.execute("UPDATE items SET deleted_at=?, content_hash=? WHERE source=? AND source_id=?",
                    (time.time(), _hash(raw), source, key))
        _version(con, source, key, iid, "removed", None, None, None, raw.get("observed_at"), owner)
        if series_key(source, key):
            _index_series(con, call, source, series_key(source, key))
        return "removed"

    digest = _hash(raw)
    if row and row[0] == digest and not row[4]:
        return "unchanged"
    if row and row[4]:
        # a revive (deleted on the device, then sent again): its delete already removed its KB copy and record node, so
        # nothing of it is there to update or remove; it's written anew (10-09: 24 calendar revives failed with a 404 on
        # the KB delete of a copy that was already gone)
        row = (row[0], 0, 0, row[3], row[4])

    title = str(raw.get("title") or sid)[:300]
    # Codes, passcodes, account and card numbers never reach the KB (09-30: 7 mails held them).
    import redact
    text = redact.redact_personal(str(raw.get("text") or ""))[:MAX_TEXT]
    kind = str(raw.get("kind") or source)
    account = str(raw.get("account") or "") or None
    prov = {"source": source, "source_id": sid, "observed_at": raw.get("observed_at"),
            "account": account, "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "owner": owner, "audience": "private",
            # The one shape (provenance.py, #393), alongside the old keys their readers still use.
            "upstream_id": sid, "item_id": iid, "kind": "observed", "digest": "sha256:" + digest}
    fields = _clean_fields(raw.get("fields"))
    triage = source == "mail" and _mail_triage_on()
    code = False
    if triage:
        import mail_triage
        code = mail_triage.is_code(title, str(raw.get("text") or ""))
    if code:
        # a sign-in or verification code (10-03 cleanup): kept nowhere, not even its subject; whatever an earlier
        # sync of it left goes
        title, text, fields, raw = mail_triage.MASKED_TITLE, "", {}, {**raw, "links": [], "timeline": ""}
        if row and not row[4]:
            _remove(call, iid, kb=bool(row[1]), node=bool(row[2]), event_id=row[3])
        mail_triage.forget(iid)

    kb = False
    skey = series_key(source, key) if text else None
    # #446 step 2: a new mail or scan, with ITEM_REFERENCES on for its source, is a reference plus derived facts; its
    # text waits only for the derivation passes (item_refs.py). One already in the KB stays an envelope until step 4.
    if code:
        pass
    elif _as_reference(source, iid, row):
        # the reference's hash is the synced text's, as the device sent it (#446 step 3: the Mac rebuilds that text and
        # compares; the item hash above is Python's JSON, which Swift can't reproduce)
        import hashlib
        text_digest = hashlib.sha256(str(raw.get("text") or "").encode()).hexdigest()
        prov["reference"] = _keep_reference(source, iid, owner, device_id, sid, text_digest, title, raw.get("observed_at"),
                                            text, {**prov, "kind": kind, "owner": owner, "audience": "private"})
    elif not _kb_wanted(con, source, key, iid, title, fields, series=bool(skey)):
        # kept out of the KB (10-03 cleanup, Zach: "we've pulled in a ton of garbage"): the ledger, the record node and
        # the timeline line stay; a copy already in the KB goes
        skey = None
        if row and row[1]:
            _kb_delete(call, iid)
    elif skey:
        # one KB item per repeating event, written below once the ledger has this occurrence; the occurrence's own
        # copy from before goes
        if row and row[1]:
            _kb_delete(call, iid)
        con.execute("INSERT OR REPLACE INTO series (source, series_key, owner, title, text, prov, at) VALUES (?,?,?,?,?,?,?)",
                    (source, skey, owner, title, text, json.dumps(prov, default=str), time.time()))
    elif triage and text:
        # waits for the triage pass (mail_triage.py, MAIL_TRIAGE=1): only personal and action mail reach the KB
        mail_triage.enqueue(iid, owner, title, text, {**prov, "kind": kind, "owner": owner, "audience": "private"})
        if row and row[1]:
            _kb_delete(call, iid)
    elif text:
        _index_text(call, iid, source, title, text, {**prov, "kind": kind, "owner": owner, "audience": "private"},
                    fresh=not (row and row[1]))     # not in the KB until now: a fresh sharing row (#1274)
        kb = True
    elif row and row[1]:
        _kb_delete(call, iid)   # text went away

    node = False
    if fields or raw.get("links"):
        data = {**fields, "title": title, "provenance": prov}
        try:
            call("record", "/v1/nodes", "POST", {"node_id": iid, "node_type": kind, "data": data})
        except Exception as exc:  # noqa: BLE001
            if not _is_conflict(exc):
                raise
            call("record", f"/v1/nodes/{iid}", "PATCH", {"data": data})
        for rel, entity in (raw.get("links") or []):
            eid = _slug(entity)
            try:
                call("record", "/v1/nodes", "POST", {"node_id": eid, "node_type": "entity",
                                                    "data": {"name": str(entity), "provenance": prov}})
            except Exception as exc:  # noqa: BLE001
                if not _is_conflict(exc):
                    raise
            try:
                call("record", "/v1/edges", "POST", {"edge_id": f"e-{iid}-{_slug(rel)}-{eid}"[:120],
                                                    "source_id": iid, "target_id": eid,
                                                    "relation_type": str(rel), "properties": {}})
            except Exception as exc:  # noqa: BLE001
                # The record runtime derives an edge's id from (source, target, relation) and answers a
                # repeat with HTTP 500 (a unique-key violation), not 409. A changed item re-sends its links,
                # so every update of an already-synced item failed and was retried for a day (10-02: "titan
                # rejected 10 items: HTTP Error 500"). The link being there already is success.
                if not (_is_conflict(exc) or _edge_exists(call, iid, eid, str(rel))):
                    raise
        node = True

    event_id = row[3] if row and not code else None
    # A calendar item isn't something that happened (#636): the calendar and the KB hold it, and on the timeline
    # its stamp was the sync time ("7:58 AM: you picked up Leo" from a 09-30 event re-synced on 10-02).
    line = "" if source == "calendar" else str(raw.get("timeline") or "").strip()
    if line and not event_id:
        event_id = f"{iid}-t"
        _post_event(call, event_id, "household.note", {"text": line[:500], **prov, "person": owner})
    elif line and event_id and row and row[0] != digest:
        _post_event(call, f"{event_id}-c{digest[:8]}", "household.corrected",
                    {"corrects": event_id, "text": line[:500], **prov, "person": owner})

    _version(con, source, key, iid, "created" if not row or row[4] else "changed", "sha256:" + digest, title,
             fields, raw.get("observed_at"), owner)
    con.execute("""INSERT INTO items (source, source_id, content_hash, item_id, kb, node, event_id,
                   account, observed_at, synced_at, deleted_at, owner, upstream_id) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?)
                   ON CONFLICT(source, source_id) DO UPDATE SET content_hash=excluded.content_hash,
                   kb=excluded.kb, node=excluded.node, event_id=excluded.event_id,
                   account=excluded.account, observed_at=excluded.observed_at,
                   synced_at=excluded.synced_at, deleted_at=NULL, owner=excluded.owner,
                   upstream_id=excluded.upstream_id""",
                (source, key, digest, iid, int(kb), int(node), event_id, account,
                 raw.get("observed_at"), time.time(), owner, sid))
    if skey:
        _index_series(con, call, source, skey)
    return "written"


def _index_text(call: Runtime, iid: str, source: str, title: str, text: str, metadata: dict[str, Any],
                fresh: bool = False) -> None:
    call("retrieval", "/v1/retrieval/index", "POST", {
        "source_id": iid, "parent_record_id": iid, "source_type": f"connector:{source}",
        "title": title, "text": text,
        # private to the person whose device sent it (kb_privacy.py); `share` widens it
        "metadata": metadata})
    _acl_row(iid, metadata, fresh=fresh)


def _kb_delete(call: Runtime, sid: str) -> None:
    """Remove a connector item's KB copy and its sharing-table row with it (#1274: 671 rows outlived their items,
    and an item that came back would have inherited one). A copy that's already gone (404) is removed: done."""
    try:
        call("retrieval", f"/v1/retrieval/sources/{sid}", "DELETE", None)
    except Exception as exc:  # noqa: BLE001
        if "404" not in str(exc):
            raise
    try:
        import kb_privacy
        kb_privacy.drop_acl(sid)
    except Exception as exc:  # noqa: BLE001 - the KB copy is gone either way; the prune catches a leftover row
        print(f"[connectors] acl row not dropped: {type(exc).__name__}", file=sys.stderr)


def _acl_row(iid: str, metadata: dict[str, Any], fresh: bool = False) -> None:
    """Every connector item gets its sharing-table row when it's indexed (#1273; #833: every item has one). The row is
    what the read paths already resolve from its metadata (kb_privacy.acl_of: its owner, private unless it says
    otherwise), so writing it changes nobody's view. A live item's existing row (a share made since) is kept; an item
    entering the KB (`fresh`: new, or back after it left) gets a fresh row that replaces any it had before, so it never
    inherits an old row broader than its audience now (#1274). A failed write never blocks the item: without a row the
    reads fall back to the same answer."""
    try:
        import kb_privacy
        owner, audience = kb_privacy.acl_of(metadata)
        kb_privacy.set_acl(iid, owner, audience, by="connector", why="index" if not fresh else "index:fresh", keep=not fresh)
    except Exception as exc:  # noqa: BLE001
        print(f"[connectors] acl row not written: {type(exc).__name__}", file=sys.stderr)


def index_triaged(iid: str, title: str, text: str, metadata: dict[str, Any], runtime: Runtime | None = None,
                  ledger: Path | None = None) -> bool:
    """A mail the triage pass kept (personal or action) goes into the KB, and the ledger says so (a later deletion
    removes it). → False when the mail was deleted meanwhile."""
    with _lock:
        con = _db(ledger)
        try:
            if not con.execute("SELECT 1 FROM items WHERE item_id=? AND deleted_at IS NULL", (iid,)).fetchone():
                return False
            _index_text(runtime or _default_runtime, iid, "mail", title, text, metadata, fresh=True)   # enters the KB
            con.execute("UPDATE items SET kb=1 WHERE item_id=?", (iid,))
            con.commit()
            return True
        finally:
            con.close()


def _mail_triage_on() -> bool:
    try:
        import mail_triage
        return mail_triage.enabled()
    except Exception:  # noqa: BLE001
        return False


# Kept out of the knowledge base at ingest (10-03 KB cleanup): file names and page titles aren't knowledge, and the
# daily log is titan's own activity. Their ledger rows, record nodes and timeline lines stay.
KB_SKIP_SOURCES = frozenset({"downloads", "browsing", "daily"})
# A calendar nobody in the house writes: subscribed ones, and holiday calendars wherever they live. Birthdays stay
# ("when is Robin's birthday?" is a real household question, 10-03).
_SUBSCRIBED_ACCOUNTS = frozenset({"subscribed calendars"})
_HOLIDAY_CAL_RX = re.compile(r"(?i)\b(holidays?|moon phases?|week numbers|siri suggestions|"
                             r"found in (?:mail|natural language))\b")


def _kb_wanted(con: sqlite3.Connection, source: str, key: str, iid: str, title: str, fields: dict[str, Any],
               series: bool = False) -> bool:
    """Whether an item's text goes into the KB. Not: downloads, browsing and the daily log (KB_SKIP_SOURCES); a calendar
    event on a subscribed or holiday calendar; or a second calendar event with the same title on the same
    day (the same holiday on two calendars counted twice, 10-03)."""
    if source in KB_SKIP_SOURCES:
        return False
    if source != "calendar":
        return True
    cal, acct = str(fields.get("calendar") or ""), str(fields.get("account_name") or "").strip().lower()
    if acct in _SUBSCRIBED_ACCOUNTS or _HOLIDAY_CAL_RX.search(cal):
        return False
    day = str(fields.get("start") or "")[:10]
    if series or not day or not title.strip():
        return True                  # a repeating event's copies on two calendars: the series twins handle those
    dkey = f"{' '.join(title.lower().split())}|{day}"
    con.execute("CREATE TABLE IF NOT EXISTS cal_kb (dkey TEXT PRIMARY KEY, item_id TEXT NOT NULL)")
    have = con.execute("SELECT item_id FROM cal_kb WHERE dkey=?", (dkey,)).fetchone()
    if have and have[0] != iid:
        live = con.execute("SELECT 1 FROM items WHERE item_id=? AND deleted_at IS NULL AND kb=1", (have[0],)).fetchone()
        if live:
            return False             # the same event, already in the KB from another calendar
    con.execute("INSERT OR REPLACE INTO cal_kb (dkey, item_id) VALUES (?,?)", (dkey, iid))
    return True


def _as_reference(source: str, iid: str, row: Any) -> bool:
    """A reference, not a KB body: the source has ITEM_REFERENCES on, and the item isn't already a KB note (an item
    that is stays one, as an envelope, until the migration, #446 step 4)."""
    try:
        import item_refs
    except Exception:  # noqa: BLE001
        return False
    if not item_refs.enabled(source):
        return False
    return not (row and row[1]) or item_refs.is_reference(iid)


def _keep_reference(source: str, iid: str, owner: str, device_id: str | None, sid: str, digest: str, title: str,
                    observed_at: Any, text: str, metadata: dict[str, Any]) -> str:
    import household_identity as hh
    import item_refs
    role = str((hh.get_store().person(owner) or {}).get("role") or "admin")
    return item_refs.keep(source, iid, owner, role, device_id, sid, digest, title, observed_at, text, metadata)


def _forget_reference(iid: str, owner: str) -> None:
    """A deleted item that was a reference: its pending text, derived row and reference go (#446); a mail waiting
    for triage goes from the queue."""
    try:
        import mail_triage
        mail_triage.forget(iid)
    except Exception:  # noqa: BLE001
        pass
    try:
        import item_refs
        import references
        con = item_refs._db()
        try:
            r = con.execute("SELECT ref_id FROM derived WHERE item_id=?", (iid,)).fetchone()
        finally:
            con.close()
        if r:
            references.remove(r["ref_id"], owner)
            item_refs.forget(iid)
    except Exception:  # noqa: BLE001 - the ledger's deletion stands either way
        pass


def _post_event(call: Runtime, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
    # The household timeline binds its stream to one record/session (see log_event).
    try:
        call("event", "/v1/events", "POST", {
            "event_id": event_id, "record_id": "household-timeline", "event_type": event_type,
            "trace_id": event_id, "stream_key": "household-timeline", "session_id": "household-timeline",
            "payload": payload})
    except Exception as exc:  # noqa: BLE001
        # Event ids are content-derived ({id}-c{digest}): an item that flips back to an earlier version
        # re-posts an event that's already on the stream. Already recorded, not a failure (#647: 42 calendar
        # items given up on the Mac after a 409, like #527's edges).
        if not _is_conflict(exc):
            raise


def _remove(call: Runtime, iid: str, kb: bool, node: bool, event_id: str | None) -> None:
    if kb:
        _kb_delete(call, iid)
    if node:
        try:
            call("record", f"/v1/nodes/{iid}", "DELETE", None)
        except Exception as exc:  # noqa: BLE001
            if "404" not in str(exc):
                raise
    if event_id:
        _post_event(call, f"{event_id}-r", "household.retracted", {"retracts": event_id})


def status(ledger: Path | None = None) -> dict[str, Any]:
    con = _db(ledger)
    try:
        rows = con.execute("""SELECT source, COUNT(*) FILTER (WHERE deleted_at IS NULL),
                              COUNT(*) FILTER (WHERE deleted_at IS NOT NULL), MAX(synced_at)
                              FROM items GROUP BY source""").fetchall()
        # The newest item the source had never sent before (#648): re-sends of old items keep last_sync fresh
        # while nothing new arrives (10-02: Mail.app closed for a day, "Mail sync is running again" twice).
        new = dict(con.execute("SELECT source, MAX(at) FROM versions WHERE change='created' GROUP BY source").fetchall())
    finally:
        con.close()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None   # noqa: E731
    return {s: {"items": live, "removed": gone, "last_sync": iso(last), "last_new": iso(new.get(s))}
            for s, live, gone, last in rows}


def forget(source: str, apply: bool = False, runtime: Runtime | None = None,
           ledger: Path | None = None, owner: str | None = None) -> dict[str, Any]:
    """Remove everything `source` wrote for `owner` (everyone's when None: the admin's maintenance path).
    Dry run unless apply=True. Applied for a person, the source is off for them from now on (Zach, 10-02: it
    stays forgotten; their devices read `connectors_off` in /v1/me and stop syncing it)."""
    call = runtime or _default_runtime
    with _lock:
        con = _db(ledger)
        try:
            q, a = ("SELECT source_id, item_id, kb, node, event_id, owner FROM items WHERE source=? AND deleted_at IS NULL",
                    [source])
            if owner:
                q += " AND owner=?"
                a.append(owner)
            rows = con.execute(q, a).fetchall()
            if not apply:
                return {"source": source, "would_remove": len(rows), "applied": False}
            if owner:
                con.execute("INSERT OR REPLACE INTO sources_off VALUES (?,?,?)", (owner, source, time.time()))
            removed, errors = 0, []
            series = set()
            for sid, iid, kb, node, event_id, row_owner in rows:
                try:
                    _remove(call, iid, bool(kb), bool(node), event_id)
                    _forget_reference(iid, row_owner or OWNER_DEFAULT)   # a reference's too (#446)
                    con.execute("UPDATE items SET deleted_at=? WHERE source=? AND source_id=?",
                                (time.time(), source, sid))
                    removed += 1
                    if series_key(source, sid):
                        series.add(series_key(source, sid))
                except Exception as exc:  # noqa: BLE001
                    errors.append({"source_id": sid, "error": str(exc)[:300]})
            con.commit()
            for sk in sorted(series):   # their series notes go too (#850)
                try:
                    _index_series(con, call, source, sk)
                except Exception as exc:  # noqa: BLE001
                    errors.append({"source_id": f"series:{sk}", "error": str(exc)[:300]})
            return {"source": source, "removed": removed, "errors": errors, "applied": True}
        finally:
            con.close()
