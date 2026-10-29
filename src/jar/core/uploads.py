#!/usr/bin/env python3
"""Files a person sends titan for a turn (POST /v1/files, titan-node#713): a Mac's chat attachments over HTTP
instead of scp, the last SSH use on the Mac (jellybean#397).

  where      UPLOADS_DIR/<id>/<name> (default /ai/tmp/uploads): the id is 24 random hex characters, so a path is
             never guessed, and one person's files never share a name with another's
  record     UPLOADS_DB (sqlite): id, owner, audience, name, type, size, when. The owner is whoever sent it;
             the audience is the people they named (none by default: private to them)
  limits     UPLOADS_MAX_MB (default 25) and an allow-list of types: images, PDF, plain text and office
             documents, by extension and by their first bytes (a .png that isn't a PNG is refused)
  served     GET /v1/files/uploads/{id}: to its owner only (anyone else, and any other id, gets 404)
  read       by the owner's turns, and the audience's, through the agent's file tools (may_read, checked by the
             tools on every path under UPLOADS_DIR); never anyone else's
  kept       UPLOADS_TTL_HOURS (default 72): after that it's never served or read again, and the bytes go with its
             record at the next sweep (on every upload and when the API starts; `python3 scripts/uploads.py sweep`
             by hand). Never put into the knowledge base: saving one there is the person's own act.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

UPLOADS_DIR = Path(os.environ.get("UPLOADS_DIR", "/ai/tmp/uploads"))
UPLOADS_DB = Path(os.environ.get("UPLOADS_DB", "/ai/datasets/connectors/uploads.db"))
MAX_BYTES = int(float(os.environ.get("UPLOADS_MAX_MB", "25")) * 1024 * 1024)
TTL_SECONDS = int(float(os.environ.get("UPLOADS_TTL_HOURS", "72")) * 3600)
# A voice note (jellybean#313): audio only, kept a week, at most VOICE_MAX_BYTES (a minute of speech is well under it),
# and never read by the agent's tools (no model hears it).
AUDIO_TYPES = frozenset({"audio/mp4", "audio/aac"})
VOICE_TTL_SECONDS = int(float(os.environ.get("VOICE_NOTE_TTL_DAYS", "7")) * 86400)
VOICE_MAX_BYTES = 2 * 1024 * 1024


def ttl(mime: str | None) -> int:
    """How long a file of this type is kept: a voice note a week, anything else UPLOADS_TTL_HOURS."""
    return VOICE_TTL_SECONDS if mime in AUDIO_TYPES else TTL_SECONDS


AUDIENCE_MAX = 10

ID_RX = re.compile(r"^[0-9a-f]{24}$")
PERSON_RX = re.compile(r"^[a-z0-9_.-]{1,64}$")

# Extension → (media type, how its first bytes must look). Office formats that are zip containers start with PK;
# the old binary ones with the OLE header. Text must be UTF-8 without NULs.
_ZIP, _OLE = (b"PK\x03\x04",), (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)
TYPES: dict[str, tuple[str, tuple[bytes, ...] | str]] = {
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "gif": ("image/gif", (b"GIF87a", b"GIF89a")),
    "webp": ("image/webp", "webp"),
    "heic": ("image/heic", "heic"),
    "heif": ("image/heif", "heic"),
    "tif": ("image/tiff", (b"II*\x00", b"MM\x00*")),
    "tiff": ("image/tiff", (b"II*\x00", b"MM\x00*")),
    "pdf": ("application/pdf", (b"%PDF-",)),
    "txt": ("text/plain", "text"),
    "md": ("text/markdown", "text"),
    "csv": ("text/csv", "text"),
    "json": ("application/json", "text"),
    "rtf": ("application/rtf", (b"{\\rtf",)),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", _ZIP),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _ZIP),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", _ZIP),
    "odt": ("application/vnd.oasis.opendocument.text", _ZIP),
    # A voice note (jellybean#313): AAC in an MP4 container, as the iPhone records it.
    "m4a": ("audio/mp4", "m4a"),
    "aac": ("audio/aac", (b"\xff\xf1", b"\xff\xf9")),   # ADTS
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", _ZIP),
    "pages": ("application/vnd.apple.pages", _ZIP),
    "numbers": ("application/vnd.apple.numbers", _ZIP),
    "key": ("application/vnd.apple.keynote", _ZIP),
    "doc": ("application/msword", _OLE),
    "xls": ("application/vnd.ms-excel", _OLE),
    "ppt": ("application/vnd.ms-powerpoint", _OLE),
}


def safe_name(name: str) -> str:
    """The file's own name, made safe: its last path part, letters, digits, space, dot, dash, underscore, parentheses
    (anything else becomes _), no leading dots, at most 100 characters keeping the extension. "" when nothing is left."""
    base = re.split(r"[\\/]", str(name or ""))[-1].strip()
    base = re.sub(r"[^\w .()\-]", "_", base, flags=re.ASCII).lstrip(". ")
    if len(base) > 100:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: 99 - len(ext)] + "." + ext) if dot and len(ext) <= 10 else base[:100]
    return base


def kind_of(name: str, head: bytes) -> str | None:
    """The media type when the name's extension is allowed and the first bytes match it, else None."""
    ext = name.rpartition(".")[2].lower() if "." in name else ""
    if ext not in TYPES:
        return None
    mime, sig = TYPES[ext]
    if sig == "text":
        if b"\x00" in head:
            return None
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as e:   # a multi-byte character cut at the end of the head is fine
            if e.start < len(head) - 3:
                return None
        return mime
    if sig == "webp":
        return mime if head[:4] == b"RIFF" and head[8:12] == b"WEBP" else None
    if sig == "m4a":
        return mime if head[4:8] == b"ftyp" and head[8:12] in (b"M4A ", b"mp42", b"isom", b"M4B ") else None
    if sig == "heic":
        return mime if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heis", b"hevc") else None
    return mime if any(head.startswith(s) for s in sig) else None


def _db() -> sqlite3.Connection:
    UPLOADS_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(UPLOADS_DB)
    con.execute("CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, owner TEXT NOT NULL, audience TEXT NOT NULL, "
                "name TEXT NOT NULL, type TEXT NOT NULL, size INTEGER NOT NULL, ts REAL NOT NULL)")
    return con


def save(owner: str | None, filename: str, data: bytes, audience: list[str] | None = None,
         now: float | None = None) -> tuple[int, dict[str, Any]]:
    """One file from `owner`: (HTTP status, body). 201 {id, name, path, type, size, expires_at}; 400 no owner or
    name, 413 too large, 415 a type that isn't allowed (or isn't what its name says)."""
    now = time.time() if now is None else now
    who = str(owner or "").strip().lower()
    if not PERSON_RX.match(who):
        return 400, {"error": "uploads belong to a person; this sign-in isn't one"}
    name = safe_name(filename)
    if not name or "." not in name:
        return 400, {"error": "say the file's name, with its extension (?name=photo.jpg)"}
    if not data:
        return 400, {"error": "the file is empty"}
    if len(data) > MAX_BYTES:
        return 413, {"error": f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB"}
    mime = kind_of(name, data[:64])
    if mime in AUDIO_TYPES and len(data) > VOICE_MAX_BYTES:
        return 413, {"error": "a voice note is at most a minute long"}
    if not mime:
        return 415, {"error": "titan takes images, PDFs, text and office documents (and the file must be what its "
                              "name says)"}
    aud = sorted({a.strip().lower() for a in (audience or []) if PERSON_RX.match(str(a).strip().lower())} - {who})[:AUDIENCE_MAX]
    sweep(now)
    fid = secrets.token_hex(12)
    folder = UPLOADS_DIR / fid
    folder.mkdir(parents=True, mode=0o700)
    path = folder / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    con = _db()
    with con:
        con.execute("INSERT INTO uploads VALUES (?,?,?,?,?,?,?)", (fid, who, ",".join(aud), name, mime, len(data), now))
    con.close()
    return 201, {"id": fid, "name": name, "path": str(path), "type": mime, "size": len(data),
                 "audience": aud, "expires_at": int(now + ttl(mime))}


def record(fid: str) -> dict[str, Any] | None:
    if not ID_RX.match(str(fid or "")):
        return None
    try:
        con = _db()
        try:
            r = con.execute("SELECT id, owner, audience, name, type, size, ts FROM uploads WHERE id=?", (fid,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not r:
        return None
    return {"id": r[0], "owner": r[1], "audience": [a for a in r[2].split(",") if a], "name": r[3], "type": r[4],
            "size": r[5], "ts": r[6], "path": str(UPLOADS_DIR / r[0] / r[3])}


def for_viewer(fid: str, viewer: str | None, now: float | None = None) -> dict[str, Any] | None:
    """The file's record when `viewer` sent it or is in its audience (a voice note's household, jellybean#313), and it
    hasn't expired; None for anyone else."""
    now = time.time() if now is None else now
    r = record(fid)
    v = str(viewer or "").lower()
    if not r or not v or (r["owner"] != v and v not in r["audience"]) or now - r["ts"] > ttl(r["type"]) \
            or not Path(r["path"]).is_file():
        return None
    return r


def set_audience(fid: str, owner: str, audience: list[str]) -> bool:
    """The owner names who else may have the file (a voice note sent to the household): replaces the audience."""
    r = record(fid)
    who = str(owner or "").lower()
    if not r or r["owner"] != who:
        return False
    aud = sorted({a.strip().lower() for a in audience if PERSON_RX.match(str(a).strip().lower())} - {who})
    con = _db()
    with con:
        con.execute("UPDATE uploads SET audience=? WHERE id=?", (",".join(aud), fid))
    con.close()
    return True


def for_owner(fid: str, viewer: str | None, now: float | None = None) -> dict[str, Any] | None:
    """The file's record when `viewer` sent it and it hasn't expired (GET serves its owner only)."""
    now = time.time() if now is None else now
    r = record(fid)
    if not r or r["owner"] != str(viewer or "").lower() or now - r["ts"] > ttl(r["type"]) or not Path(r["path"]).is_file():
        return None
    return r


def _under_uploads(path: str) -> Path | None:
    """The path, resolved, when it's inside UPLOADS_DIR (symlinks and .. followed), else None."""
    try:
        p = Path(os.path.realpath(str(path or "").strip()))
        root = Path(os.path.realpath(UPLOADS_DIR))
    except (OSError, ValueError):
        return None
    return p if p == root or root in p.parents else None


def may_read(path: str, viewer: str | None, now: float | None = None) -> bool:
    """Whether a turn shown to `viewer` may touch `path`. Anything outside UPLOADS_DIR: yes (other rules apply).
    Inside: only a live upload's own file, and only for its owner or its audience. The folder itself, an id
    without a record, or an expired file: no."""
    p = _under_uploads(path)
    if p is None:
        return True
    root = Path(os.path.realpath(UPLOADS_DIR))
    parts = p.relative_to(root).parts
    if len(parts) != 2:
        return False
    r = record(parts[0])
    now = time.time() if now is None else now
    who = str(viewer or "").lower()
    if r and r["type"] in AUDIO_TYPES:
        return False   # a voice note is played by people, never read by a turn's tools (jellybean#313)
    return bool(r and r["name"] == parts[1] and now - r["ts"] <= ttl(r["type"]) and who and (who == r["owner"] or who in r["audience"]))


def sweep(now: float | None = None) -> int:
    """Remove expired uploads and their records, and any folder under UPLOADS_DIR without a record. How many."""
    now = time.time() if now is None else now
    gone = 0
    try:
        con = _db()
        with con:
            old = [r[0] for r in con.execute("SELECT id, type, ts FROM uploads") if r[2] < now - ttl(r[1])]
            con.executemany("DELETE FROM uploads WHERE id=?", [(i,) for i in old])
            live = {r[0] for r in con.execute("SELECT id FROM uploads")}
        con.close()
    except sqlite3.Error:
        return 0
    if UPLOADS_DIR.is_dir():
        for d in UPLOADS_DIR.iterdir():
            if d.name not in live and (ID_RX.match(d.name) or d.name in old):
                shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)
                gone += 1
    return gone


if __name__ == "__main__":   # by hand: python3 scripts/uploads.py sweep
    import sys
    if sys.argv[1:] == ["sweep"]:
        print(f"removed {sweep()} expired uploads")
