#!/usr/bin/env python3
"""Share the file (#1081, decided by Coordination 10-06 on Zach's instruction; follows #1058 option (a)).

By default the household sees a document's redacted KB text only, never the original. An owner may hand the item's
audience the real original, one item at a time: nothing is shared in bulk or by default.

  share(ref, owner, on)   only the reference's owner, an adult or the admin; a kid's originals stay text-only.
                          Unsharing takes effect at once (the listing says file_shared: false, so other devices' File
                          Providers evict their cached copy on the next sync).
  read                    GET /v1/documents/{id}/content proxies the original from the owner's device (the
                          `item_original` device tool, through cross_device) to the item's audience, never to the
                          household screen. The owner's device offline or unable: {available: false, reason}.
  audit                   every read served: who, when, which device (file_reads); the owner gets one quiet card a
                          day ("Robin opened “Lease.pdf”", batched by its key), never for their own reads.

  changed                 (#1170) the owner's device refuses a read when the file no longer matches what was shared
                          ({refused: "changed"}): the owner gets one quiet card per file, updated in place, 7 days:
                          "“<file>” changed since you shared it: share this version?" (Share this version / Stop
                          sharing / Later). The reader is told only that it isn't available right now, never why.

Store: the references DB ($REFERENCES_DB): file_shares, file_reads, file_changes.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

MAX_BYTES = 25 * 1024 * 1024
READS_KEEP_DAYS = 365


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _db(path: Path | None = None):
    import references
    con = references._db(path)
    con.execute("CREATE TABLE IF NOT EXISTS file_shares (ref_id TEXT PRIMARY KEY, owner TEXT NOT NULL, at REAL NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS file_reads (id TEXT PRIMARY KEY, ref_id TEXT NOT NULL, owner TEXT NOT NULL, "
                "reader TEXT NOT NULL, device TEXT, at REAL NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_file_reads_ref ON file_reads(ref_id, at)")
    con.execute("CREATE TABLE IF NOT EXISTS file_changes (ref_id TEXT PRIMARY KEY, owner TEXT NOT NULL, new_hash TEXT, "
                "at REAL NOT NULL)")
    return con


def is_shared(ref_id: str, path: Path | None = None) -> bool:
    con = _db(path)
    try:
        return con.execute("SELECT 1 FROM file_shares WHERE ref_id=?", (ref_id,)).fetchone() is not None
    finally:
        con.close()


def shared_ids(path: Path | None = None) -> set[str]:
    con = _db(path)
    try:
        return {r[0] for r in con.execute("SELECT ref_id FROM file_shares")}
    finally:
        con.close()


def share(ref_id: str, person: str | None, role: str | None, on: bool, path: Path | None = None) -> dict[str, Any]:
    if not person or role == "household":
        raise Refused(403, "a shared screen shares nothing")
    if role == "kid":
        raise Refused(403, "a kid's originals stay text-only")
    if role not in ("admin", "adult"):
        raise Refused(403, "not allowed")
    con = _db(path)
    try:
        r = con.execute("SELECT owner FROM refs WHERE id=?", (str(ref_id),)).fetchone()
        if not r or str(r[0]).lower() != person.lower():
            raise Refused(404, "no such reference")            # someone else's doesn't exist for this caller
        if on:
            con.execute("INSERT OR IGNORE INTO file_shares VALUES (?,?,?)", (ref_id, person.lower(), time.time()))
        else:
            con.execute("DELETE FROM file_shares WHERE ref_id=?", (ref_id,))
        con.commit()
    finally:
        con.close()
    return {"id": ref_id, "file_shared": bool(on)}


def audit(ref_id: str, owner: str, reader: str, device: str | None, *, title: str, reader_name: str,
          names: dict[str, str] | None = None, card: Callable[..., Any] | None = None, now: float | None = None,
          path: Path | None = None) -> None:
    """Record one read; card the owner once a day (one card per day, its detail growing), never for their own."""
    now = time.time() if now is None else now
    con = _db(path)
    try:
        con.execute("INSERT INTO file_reads VALUES (?,?,?,?,?,?)",
                    ("fr-" + uuid.uuid4().hex[:12], ref_id, owner, reader, device, now))
        con.execute("DELETE FROM file_reads WHERE at < ?", (now - READS_KEEP_DAYS * 86400,))
        con.commit()
        if reader.lower() == owner.lower():
            return
        import local_time
        day = local_time.day(now)
        start = datetime.fromtimestamp(now, local_time.zone() or timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = con.execute("SELECT f.reader, r.title FROM file_reads f LEFT JOIN refs r ON r.id=f.ref_id WHERE f.owner=? "
                            "AND f.at>=? AND lower(f.reader)<>lower(?) ORDER BY f.at", (owner, start, owner)).fetchall()
    finally:
        con.close()
    if card is None:
        import proactive
        card = proactive.add_card
    lines = []
    for rd, t in today:
        who = reader_name if rd == reader else ((names or {}).get(rd) or "Someone").split(" ")[0]
        line = f"{who} opened “{t or 'a file'}”"
        if line not in lines:
            lines.append(line)
    card(f"{reader_name} opened “{title}”" if len(lines) == 1 else f"Your shared files were opened {len(lines)} times today",
         "\n".join(lines), kind="info", urgency=0, source="file_share", to=[owner], key=f"file-reads-{owner}-{day}",
         expires_in=2 * 86400, data={"kind": "file_reads", "day": day})


UNAVAILABLE = "isn't available right now"   # all a reader hears when the owner's device refuses: never why
CHANGED_CARD_S = 7 * 86400


def changed(ref: dict[str, Any], new_hash: str | None, *, card: Callable[..., Any] | None = None,
            now: float | None = None, path: Path | None = None) -> None:
    """The owner's device refused a read: the file changed since it was shared. One card for the owner, keyed by the
    file (a repeat updates it in place), quiet, gone in 7 days; the new hash waits here for "Share this version"."""
    now = time.time() if now is None else now
    con = _db(path)
    try:
        con.execute("INSERT OR REPLACE INTO file_changes VALUES (?,?,?,?)",
                    (ref["id"], str(ref["owner"]).lower(), str(new_hash or "") or None, now))
        con.commit()
    finally:
        con.close()
    if card is None:
        import proactive
        card = proactive.add_card
    title = str(ref.get("title") or "a file")
    card(f"“{title}” changed since you shared it: share this version?", "", kind="info", urgency=0,
         source="file-share-changed", to=[ref["owner"]], key=f"file-changed-{ref['id']}", expires_in=CHANGED_CARD_S,
         actions=[{"id": "share", "label": "Share this version", "kind": "reply", "value": f"share:{ref['id']}"},
                  {"id": "stop", "label": "Stop sharing", "kind": "reply", "value": f"stop:{ref['id']}"},
                  {"id": "later", "label": "Later", "kind": "reply", "value": f"later:{ref['id']}"}],
         data={"kind": "file_changed", "ref_id": ref["id"]})


def answer(value: str, person: str | None, role: str | None, path: Path | None = None) -> tuple[bool, str]:
    """A tap on the changed card (proactive.card_action, source "file-share-changed"), the file's owner only:
    share:<id> shares the version the device has now (its new hash); stop:<id> unshares; later:<id> leaves the card."""
    verb, _, ref_id = str(value or "").partition(":")
    if verb not in ("share", "stop", "later") or not ref_id:
        return False, "titan didn't understand that."
    con = _db(path)
    try:
        r = con.execute("SELECT owner FROM refs WHERE id=?", (ref_id,)).fetchone()
        pending = con.execute("SELECT new_hash FROM file_changes WHERE ref_id=?", (ref_id,)).fetchone()
    finally:
        con.close()
    if not r or not person or str(r[0]).lower() != person.lower():
        return False, "That isn't yours to change."
    if verb == "later":
        return False, "Left for later."            # not done: the card stays
    if verb == "stop":
        share(ref_id, person, role, False, path)
        _clear_change(ref_id, path)
        return True, "Stopped sharing it."
    if not pending or not pending[0]:
        return False, "titan doesn't have the new version's fingerprint yet; open it on your Mac and try again."
    con = _db(path)
    try:
        con.execute("UPDATE refs SET content_hash=? WHERE id=?", (pending[0], ref_id))
        con.commit()
    finally:
        con.close()
    share(ref_id, person, role, True, path)
    _clear_change(ref_id, path)
    return True, "Shared this version."


def _clear_change(ref_id: str, path: Path | None = None) -> None:
    con = _db(path)
    try:
        con.execute("DELETE FROM file_changes WHERE ref_id=?", (ref_id,))
        con.commit()
    finally:
        con.close()


KID_REASON = "originals open for adults; you can read the text"


def _refused(role: str | None, why: str) -> None:
    """A refused original, counted by role and why (feature_counts.py): nothing of the item, and no audit."""
    try:
        import feature_counts
        feature_counts.note("original_refused", role, why)
    except Exception:  # noqa: BLE001 - a count never changes the answer
        pass


def read(ref: dict[str, Any], reader: str | None, role: str | None, device: str | None,
         fetch: Callable[[dict[str, Any]], dict[str, Any]], *, reader_name: str, owner_name: str,
         names: dict[str, str] | None = None, card: Callable[..., Any] | None = None,
         path: Path | None = None) -> tuple[int, Any, str]:
    """The original of a shared reference, for a reader who may see the item (the caller has checked its audience).
    Adults only: a shared screen and a kid get {available: false} before any fetch or audit.
    → (status, bytes or {available: false, reason}, content type)."""
    if role == "household" or not reader:
        _refused(role, "screen")
        return 200, {"available": False, "reason": "originals aren't opened on a shared screen"}, "application/json"
    if role != "admin" and role != "adult":
        # Originals open for adults only (the 10-06 design): a kid reads the text. Refused before any fetch, so the
        # owner's Mac is never asked and no read is audited.
        _refused(role, "kid")
        return 200, {"available": False, "reason": KID_REASON}, "application/json"
    if not is_shared(ref["id"], path):
        return 200, {"available": False, "reason": f"on {owner_name}'s device; not shared as a file"}, "application/json"
    got = fetch(ref, requester=reader) or {}      # #1170: the owner's Mac names who asked in its audit (the id only)
    if got.get("changed"):
        changed(ref, got.get("new_hash"), card=card, path=path)
        return 200, {"available": False, "reason": UNAVAILABLE}, "application/json"
    data = got.get("data")
    if not isinstance(data, (bytes, bytearray)) or not data:
        reason = got.get("reason") or f"on {owner_name}'s device, which couldn't send it just now"
        return 200, {"available": False, "reason": reason}, "application/json"
    if len(data) > MAX_BYTES:
        return 200, {"available": False, "reason": "too large to send"}, "application/json"
    audit(ref["id"], ref["owner"], reader, device, title=str(ref.get("title") or "a file"), reader_name=reader_name,
          names=names, card=card, path=path)
    return 200, bytes(data), str(got.get("content_type") or "application/octet-stream")


def reads_of(owner: str, path: Path | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """The owner's own audit: who opened which of their shared files, newest first."""
    con = _db(path)
    try:
        return [{"ref_id": r[0], "reader": r[1], "device": r[2], "at": r[3]} for r in con.execute(
            "SELECT ref_id, reader, device, at FROM file_reads WHERE owner=? ORDER BY at DESC LIMIT ?", (owner, limit))]
    finally:
        con.close()
