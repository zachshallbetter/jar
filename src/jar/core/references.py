#!/usr/bin/env python3
"""References (titan-node#446 step 1, decided 10-02): titan holds a pointer to an item on a person's device, never the
item's body. Mail, scans, files, photos and the rest stay where they are; the hub keeps a small record that says
whose it is, which device has it, what kind it is, the device's own id for it, a hash of it, a title, its dates and
when the device last saw it. Derived facts (a bill's amount, an appointment's time) may cite a reference's id as
their provenance (provenance.make(..., reference=...)); nothing in this step reads that yet.

  record   {id, owner, audience, device_id, kind, native_id, content_hash, title, dates, last_seen, created_at,
            updated_at}; unique per (owner, device_id, kind, native_id), so a device re-sending an item updates it
  who      written only by the owner's own device (a device token: it writes references for its person); read by the
           owner and whoever its audience admits (audience.py), like every item on titan
  never    a body: a write carrying one (body, text, content, html, data, image…) is refused

Stored in its own SQLite file (REFERENCES_DB, 0600), like places and custody: per-viewer filtering and the
(owner, device, kind, native_id) key are titan's rules, which the record runtime's nodes don't enforce.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DB = Path(os.environ.get("REFERENCES_DB", "/ai/datasets/connectors/references.db"))
KINDS = ("mail", "scan", "file", "photo", "note", "calendar", "reminder", "contact", "message", "health", "other")
BODY_KEYS = frozenset({"body", "text", "content", "html", "data", "image", "bytes", "raw", "attachment", "preview",
                       "snippet", "excerpt", "pixels", "audio", "video"})
REF_ID_RX = re.compile(r"^rf-[0-9a-f]{16}$")
_HASH_RX = re.compile(r"^(?:sha256:)?[0-9a-f]{16,64}$")
_DATE_KEYS = ("created", "modified", "sent", "received", "start", "end", "due", "taken")
_lock = threading.Lock()


class RefError(ValueError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS refs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, audience TEXT NOT NULL, "
                "device_id TEXT NOT NULL, kind TEXT NOT NULL, native_id TEXT NOT NULL, content_hash TEXT, title TEXT, "
                "dates TEXT, last_seen REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "UNIQUE (owner, device_id, kind, native_id))")
    con.execute("CREATE INDEX IF NOT EXISTS refs_owner ON refs (owner, kind)")
    return con


def ref_id(owner: str, device_id: str, kind: str, native_id: str) -> str:
    return "rf-" + hashlib.sha256(f"{owner}\0{device_id}\0{kind}\0{native_id}".encode()).hexdigest()[:16]


# #1135: a file reference's native_id is a path under the owner's home, relative ("Documents/Lease.pdf"). Never
# absolute, never outside home, never a secret store or a credentials file.
_SECRET_DIRS = frozenset({".ssh", ".gnupg", ".aws", ".kube", ".docker", ".password-store", ".1password", ".azure",
                          ".gcloud", "keychains"})
_SECRET_FILES = frozenset({".netrc", ".git-credentials", ".pgpass", ".npmrc", ".pypirc", "credentials", "id_rsa",
                           "id_ed25519", "id_ecdsa", "id_dsa"})
_SECRET_EXT = (".pem", ".key", ".p12", ".pfx", ".keychain", ".keychain-db", ".kdbx")


def clean_file_path(nid: str) -> str:
    """A home-relative path, or RefError 400."""
    p = str(nid or "").strip()
    if not p or p.startswith(("/", "~", "\\")) or "\\" in p or ":" in p.split("/")[0] or "\x00" in p:
        raise RefError(400, "a file's native_id is a path relative to its owner's home (Documents/Lease.pdf)")
    parts = p.split("/")
    if any(x in ("", ".", "..") for x in parts):
        raise RefError(400, "a file's path has no empty, '.' or '..' parts")
    low = [x.lower() for x in parts]
    name = low[-1]
    if (set(low[:-1]) & _SECRET_DIRS or low[:2] == ["library", "keychains"] or name in _SECRET_FILES
            or name.startswith(("id_rsa", "id_ed25519", ".env")) or name.endswith(_SECRET_EXT)):
        raise RefError(400, "that file can't be referenced (a key, a credential or a secret store)")
    return p


def _clean(body: dict[str, Any], creating: bool) -> dict[str, Any]:
    import audience as aud
    if not isinstance(body, dict):
        raise RefError(400, "expected a JSON object")
    carried = sorted(k for k in body if str(k).lower() in BODY_KEYS)
    if carried:
        raise RefError(400, f"a reference never holds the item itself: {', '.join(carried)} not accepted")
    out: dict[str, Any] = {}
    if creating:
        kind = str(body.get("kind") or "").strip().lower()
        if kind not in KINDS:
            raise RefError(400, f"kind must be one of {', '.join(KINDS)}")
        nid = " ".join(str(body.get("native_id") or "").split())
        if not nid or len(nid) > 300:
            raise RefError(400, "native_id is the device's own id for the item (1-300 characters)")
        if kind == "file":
            nid = clean_file_path(nid)
        out.update(kind=kind, native_id=nid)
    if "content_hash" in body:
        h = str(body["content_hash"] or "").strip().lower()
        if h and not _HASH_RX.match(h):
            raise RefError(400, "content_hash is a hex digest (sha256:…)")
        out["content_hash"] = h or None
    if "title" in body:   # paths and titles carry names: cleaned like any untrusted name (#1135)
        import untrusted
        out["title"] = untrusted.clean_name(str(body["title"] or ""), limit=200) or None
    if "dates" in body:
        d = body["dates"] if isinstance(body["dates"], dict) else {}
        out["dates"] = json.dumps({k: str(v)[:40] for k, v in d.items() if k in _DATE_KEYS and v}, sort_keys=True)
    if "audience" in body:
        out["audience"] = aud.normalize(body["audience"], default="private")
    if "last_seen" in body:
        try:
            out["last_seen"] = float(body["last_seen"])
        except (TypeError, ValueError):
            raise RefError(400, "last_seen is epoch seconds") from None
    return out


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["dates"] = json.loads(d["dates"]) if d.get("dates") else {}
    return d


def put(person: str, role: str, device_id: str | None, body: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """POST: a device writes (or re-sends) a reference for its own person. → the record."""
    if not device_id:
        raise RefError(403, "references come from the owner's own device")
    if role not in ("admin", "adult", "kid"):
        raise RefError(403, "a shared device has no personal items to point to")
    f = _clean(body, creating=True)
    rid = ref_id(person, device_id, f["kind"], f["native_id"])
    now = time.time()
    with _lock:
        con = _db(path)
        try:
            cur = con.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
            if cur is None:
                con.execute("INSERT INTO refs (id, owner, audience, device_id, kind, native_id, content_hash, title, dates, "
                            "last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (rid, person, f.get("audience", "private"), device_id, f["kind"], f["native_id"],
                             f.get("content_hash"), f.get("title"), f.get("dates", "{}"), f.get("last_seen", now), now, now))
            else:
                sets = {k: v for k, v in f.items() if k not in ("kind", "native_id")}
                sets.setdefault("last_seen", now)
                con.execute(f"UPDATE refs SET {', '.join(k + '=?' for k in sets)}, updated_at=? WHERE id=?",
                            (*sets.values(), now, rid))
            con.commit()
            return _row(con.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone())
        finally:
            con.close()


def patch(rid: str, person: str, role: str, device_id: str | None, body: dict[str, Any],
          path: Path | None = None) -> dict[str, Any]:
    """PATCH: the owner's own device changes its title, dates, hash, audience or last_seen; never what it points to."""
    if not REF_ID_RX.match(str(rid)):
        raise RefError(404, "no such reference")
    if not device_id or role not in ("admin", "adult", "kid"):
        raise RefError(403, "references come from the owner's own device")
    if any(k in body for k in ("kind", "native_id", "device_id", "owner")):
        raise RefError(400, "what a reference points to doesn't change: send a new one")
    f = _clean(body, creating=False)
    with _lock:
        con = _db(path)
        try:
            cur = con.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
            if cur is None or cur["owner"] != person:
                raise RefError(404, "no such reference")       # someone else's doesn't exist for this caller
            if f:
                con.execute(f"UPDATE refs SET {', '.join(k + '=?' for k in f)}, updated_at=? WHERE id=?",
                            (*f.values(), time.time(), rid))
                con.commit()
            return _row(con.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone())
        finally:
            con.close()


def visible(person: str | None, role: str | None, kind: str | None = None, limit: int = 500,
            path: Path | None = None) -> list[dict[str, Any]]:
    """GET: the viewer's own references and what's shared with them (audience.py), newest first; optionally one kind."""
    import audience as aud
    with _lock:
        con = _db(path)
        try:
            q, args = "SELECT * FROM refs", []
            if kind:
                q, args = q + " WHERE kind=?", [kind]
            rows = con.execute(q + " ORDER BY updated_at DESC", args).fetchall()
        finally:
            con.close()
    return [_row(r) for r in rows if aud.visible(r["audience"], person, role, owner=r["owner"])][:limit]


def native_ids(owner: str, device_id: str, kind: str, path: Path | None = None) -> list[str]:
    """The device ids of one owner's references of one kind from one device: what a full listing from that device is
    compared with, so an item it no longer lists is deleted (#1380). Internal: never returned to a client."""
    with _lock:
        con = _db(path)
        try:
            return [r["native_id"] for r in con.execute(
                "SELECT native_id FROM refs WHERE owner=? AND device_id=? AND kind=? ORDER BY native_id",
                (owner, device_id, kind))]
        finally:
            con.close()


def remove(rid: str, owner: str, path: Path | None = None) -> bool:
    """The item is gone from the owner's device (a connector's deletion, #446 step 2): its reference goes too."""
    with _lock:
        con = _db(path)
        try:
            n = con.execute("DELETE FROM refs WHERE id=? AND owner=?", (rid, owner)).rowcount
            con.commit()
            return n > 0
        finally:
            con.close()


def seen(rid: str, content_hash: str, path: Path | None = None) -> None:
    """The owner's device just returned the item (#446 step 3): its hash as it is now, and when."""
    h = str(content_hash or "").strip().lower()
    if not _HASH_RX.match(h):
        return
    with _lock:
        con = _db(path)
        try:
            con.execute("UPDATE refs SET content_hash=?, last_seen=?, updated_at=? WHERE id=?",
                        (h, time.time(), time.time(), rid))
            con.commit()
        finally:
            con.close()


def mark_gone(rid: str, path: Path | None = None) -> None:
    """The device says the item is gone: the reference says so (dates.gone), so claims citing it stop counting."""
    with _lock:
        con = _db(path)
        try:
            r = con.execute("SELECT dates FROM refs WHERE id=?", (rid,)).fetchone()
            if r is None:
                return
            d = json.loads(r["dates"] or "{}")
            d["gone"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            con.execute("UPDATE refs SET dates=?, updated_at=? WHERE id=?", (json.dumps(d, sort_keys=True), time.time(), rid))
            con.commit()
        finally:
            con.close()


def mine(person: str | None, role: str | None, path: Path | None = None) -> list[dict[str, Any]]:
    """#1135: the caller's own references only, newest first, for Settings › Share the file: {id, kind, title, native,
    file_shared, audience, last_seen}. Never another person's; never the native id itself (a path holds names)."""
    if not person or role == "household":
        raise RefError(403, "a shared screen has no references of its own")
    import untrusted
    try:
        import file_share
        shared = file_share.shared_ids(path)
    except Exception:  # noqa: BLE001 - no share table yet: nothing shared
        shared = set()
    with _lock:
        con = _db(path)
        try:
            rows = con.execute("SELECT * FROM refs WHERE owner=? ORDER BY updated_at DESC", (person,)).fetchall()
        finally:
            con.close()
    return [{"id": r["id"], "kind": r["kind"], "title": untrusted.clean_name(str(r["title"] or ""), limit=200) or None,
             "native": "path" if r["kind"] == "file" else "id", "file_shared": r["id"] in shared,
             "audience": r["audience"], "last_seen": r["last_seen"]} for r in rows]


# ── the after-check (#446 v2, #776's verification items; rebuild_check calls it in-process) ───────────────────────

LIVE_VIEWERS = ("owner", "other_adult", "kid", "screen")
LIVE_REASONS = ("device_offline", "server_unreachable", "gone", "old_app", "no_holder", "unexpected")


def _classify(res: str) -> tuple[str, str | None]:
    """open_item's reply → (opened | refused | error, reason). Read from its fixed words (item_open), never a body."""
    import item_open as io
    t = str(res or "")
    if "for this answer only" in t:
        return "opened", None
    if any(w in t for w in (io.HOUSEHOLD, io.NOT_FOUND, io.OWNER_DECLINED, io.KID_OTHER)) or "haven't said yes" in t or \
            t.startswith("Several match"):
        return "refused", None
    if "mail server didn't answer" in t:
        return "error", "server_unreachable"
    if "no longer" in t:
        return "error", "gone"
    if "needs an update" in t or "doesn't hold scans" in t:
        return "error", "old_app"
    if "can't reach" in t:
        return "error", "no_holder"
    if "asleep" in t or "isn't reachable" in t or "didn't answer" in t:
        return "error", "device_offline"
    return "error", "unexpected"


def live_check(sample: int = 5, *, people: list[dict[str, Any]] | None = None,
               opener: Any = None, server_refs: Any = None) -> dict[str, dict[str, Any]]:
    """Does a reference open its body for its owner, and for nobody else (another adult: only through the owner's card;
    a kid and a shared screen: never)? Up to `sample` of the newest references (server mail and device references),
    each tried as its owner, another adult, a kid and a screen. Dry: a consent card is counted, never written or pushed
    (and nobody answers it, so it's a no); a body read is discarded unseen, written nowhere. → per viewer {tried,
    opened, refused, card, errors, error_reasons: {reason: n}}: ints and fixed reason keys only."""
    import household_identity as hh
    import item_open as io
    import agent_runtime_lib as lib
    folks = people if people is not None else hh.get_store().people()
    by_id = {str(p["id"]).lower(): p for p in folks}
    srv = server_refs or lib._server_mail_refs
    pool: list[dict[str, Any]] = []
    for p in folks:
        if p.get("role") in ("admin", "adult"):
            pid = str(p["id"]).lower()
            pool += [r for r in srv(pid)][:sample]
            pool += [r for r in visible(pid, p.get("role")) if r["owner"] == pid and r["kind"] in io.KINDS][:sample]
    pool = sorted(pool, key=lambda r: str((r.get("dates") or {}).get("received") or r.get("updated_at") or ""),
                  reverse=True)[:sample]
    out = {v: {"tried": 0, "opened": 0, "refused": 0, "card": 0, "errors": 0, "error_reasons": {}} for v in LIVE_VIEWERS}
    cards: list[int] = []

    def count_card(*a: Any, **k: Any) -> None:   # dry: counted only (no proactive write, no push)
        cards.append(1)

    def route(req: dict[str, Any], owner: str, mac: dict[str, Any], approve: Any) -> dict[str, Any]:
        """No device is reached out of a turn: another reader goes through the owner's consent (the card, counted,
        unanswered, so no); the owner's own device read counts as offline (only server mail reads for real here)."""
        import cross_device as xd
        if approve is not None:
            approve()
            return {"result": xd.DECLINED, "completed": False}
        return {"result": xd.NO_WAKE, "completed": False}

    def open_as(ref: dict[str, Any], asker: str | None, role: str) -> str:
        if opener is not None:
            return opener(ref, asker, role, count_card)
        return io.open_item({"ref": ref["id"]}, asker=asker or "", role=role, asker_name="someone", people=by_id,
                            holders=lib._item_holders, route=route,
                            derived=lambda r: [], wrap=lambda t, s, f: "", add_card=count_card, consent_timeout=0.0,
                            server_refs=lambda a: [r for r in srv(a) if r["id"] == ref["id"]],
                            server_body=lambda o, r, n: lib._server_mail_body(o, r, n))

    for ref in pool:
        owner = str(ref["owner"]).lower()
        others = [p for p in folks if str(p["id"]).lower() != owner]
        askers = {"owner": (owner, str((by_id.get(owner) or {}).get("role") or "adult")),
                  "other_adult": next(((str(p["id"]), p["role"]) for p in others if p.get("role") in ("admin", "adult")), None),
                  "kid": next(((str(p["id"]), "kid") for p in others if p.get("role") == "kid"), None),
                  "screen": (None, "household")}
        for v, who in askers.items():
            if who is None:
                continue
            before = len(cards)
            kind, reason = _classify(open_as(ref, who[0], who[1]))
            row = out[v]
            row["tried"] += 1
            row["card"] += len(cards) - before
            if kind == "opened":
                row["opened"] += 1
            elif kind == "refused":
                row["refused"] += 1
            else:
                row["errors"] += 1
                r = reason if reason in LIVE_REASONS else "unexpected"
                row["error_reasons"][r] = row["error_reasons"].get(r, 0) + 1
    return out
