#!/usr/bin/env python3
"""The household's documents for the iPhone's read-only File Provider (jellybean#320; contract on that issue, 10-06):

  GET /v1/documents?audience=household        {documents: [{id, title, kind, content_type, size, modified, source,
                                               original, available}], generated_at}
  GET /v1/documents/{id}/content              a KB item's text (text/markdown), or {available: false, reason} for an
                                               original that lives on its owner's device; 404 for anything else

Household-audience items only, for every caller, the admin included: a private, adults-only or named-people item is
never listed or served here, and asking for one's content is the same 404 as an id that doesn't exist. A household
device (the shared screen) may read; every role may.

titan keeps no original files (#446: references, never bodies), so:
  source "kb"      a KB note or document at household audience; its content is the KB's text (original: false)
  source "device"  a household-audience reference (a scan, a file, a photo) whose original is on its owner's device;
                   not served (available: false, "not shared as a file"): household audience covers the KB's redacted
                   text, never the unredacted original, unless its owner shared the file (#1081, file_share.py):
                   then file_shared: true, available: true, and content proxies the original from the owner's device
                   (audited, the owner carded), never on the household screen. file_shared turning false tells a
                   File Provider to evict its cached copy on the next sync.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

DOC_TYPES = frozenset({"note", "document", "scan", "file", "pdf"})
REF_KINDS = ("scan", "file", "photo")
ROLES = frozenset({"admin", "adult", "kid", "household"})
_CT = {"scan": "application/pdf", "file": None, "photo": "image/jpeg"}
_EXT = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".heic": "image/heic",
        ".txt": "text/plain", ".md": "text/markdown", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kb_docs(sources: list[dict[str, Any]], acl: Callable[[str, dict[str, Any] | None], tuple[str | None, str]]
             ) -> list[dict[str, Any]]:
    out = []
    for s in sources:
        sid = str(s.get("source_id") or "")
        st = str(s.get("source_type") or "").lower()
        if not sid or st not in DOC_TYPES:
            continue
        _owner, aud = acl(sid, s.get("metadata"))
        if aud != "household":
            continue
        out.append({"id": "kb:" + sid, "title": str(s.get("title") or sid)[:200], "kind": "note" if st == "note" else st,
                    "content_type": "text/markdown", "size": None, "modified": s.get("updated_at") or s.get("created_at"),
                    "source": "kb", "original": False, "available": True})
    return out


def _ref_docs(refs_db: Any) -> list[dict[str, Any]]:
    import file_share
    import references
    out = []
    try:
        shared = file_share.shared_ids(refs_db)
    except Exception:  # noqa: BLE001 - no share table yet: nothing shared
        shared = set()
    try:
        con = references._db(refs_db)
    except Exception:  # noqa: BLE001 - no references store: none
        return out
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(f"SELECT * FROM refs WHERE audience='household' AND kind IN ({','.join('?' * len(REF_KINDS))})",
                           REF_KINDS).fetchall()
    finally:
        con.close()
    for r in rows:
        d = dict(r)
        dates = json.loads(d.get("dates") or "{}") if isinstance(d.get("dates"), str) else (d.get("dates") or {})
        ext = "." + str(d.get("native_id") or "").rsplit(".", 1)[-1].lower() if "." in str(d.get("native_id") or "") else ""
        out.append({"id": d["id"], "title": str(d.get("title") or d["kind"])[:200], "kind": d["kind"],
                    "content_type": _EXT.get(ext) or _CT.get(d["kind"]), "size": None,
                    "modified": dates.get("modified") or dates.get("created") or d.get("updated_at"),
                    "source": "device", "original": True, "available": d["id"] in shared,
                    "file_shared": d["id"] in shared})
    return out


def _check(role: str | None) -> None:
    if role not in ROLES:
        raise Refused(403, "documents are for the household's own people and screens")


def listing(role: str | None, sources: list[dict[str, Any]] | None = None,
            acl: Callable[[str, dict[str, Any] | None], tuple[str | None, str]] | None = None, refs_db: Any = None
            ) -> dict[str, Any]:
    """The household's documents: KB notes and documents at household audience, and household references' originals."""
    _check(role)
    if sources is None:
        import agent_runtime_lib as lib
        sources = lib.kb_sources()
    if acl is None:
        import kb_privacy
        rows = kb_privacy.acl_rows(str(s.get("source_id")) for s in sources)
        acl = lambda sid, md: kb_privacy.acl_of(md, sid, rows)  # noqa: E731
    docs = _kb_docs(sources, acl) + _ref_docs(refs_db)
    docs.sort(key=lambda d: str(d.get("modified") or ""), reverse=True)
    return {"documents": docs, "generated_at": _now()}


def content(doc_id: str, role: str | None, sources: list[dict[str, Any]] | None = None,
            acl: Callable[[str, dict[str, Any] | None], tuple[str | None, str]] | None = None, refs_db: Any = None,
            text_of: Callable[[str], str | None] | None = None, names: dict[str, str] | None = None,
            reader: str | None = None, device: str | None = None,
            fetch: Callable[[dict[str, Any]], dict[str, Any]] | None = None, card: Any = None
            ) -> tuple[int, Any, str, str | None]:
    """→ (status, body, content type, etag). A KB document's text; a device original's {available: false, reason};
    404 for anything not household-visible (the same answer whether it exists or not)."""
    _check(role)
    doc_id = str(doc_id or "")
    docs = {d["id"]: d for d in listing(role, sources, acl, refs_db)["documents"]}
    hit = docs.get(doc_id)
    if hit is None:
        raise Refused(404, "no such document")
    if hit["source"] == "kb":
        if text_of is None:
            import agent_runtime_lib as lib
            import kb_privacy
            text_of = lambda s: kb_privacy.source_text(lib._runtime_request, s)  # noqa: E731
        text = text_of(doc_id[3:])
        if text is None:
            raise Refused(404, "no such document")
        body = text.encode("utf-8")
        return 200, body, "text/markdown; charset=utf-8", '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    import file_share
    import references
    con = references._db(refs_db)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM refs WHERE id=?", (doc_id,)).fetchone()
    finally:
        con.close()
    who = (((names or {}).get(row["owner"]) if row else None) or "its owner").split(" ")[0]
    if row is None or not hit.get("file_shared") or fetch is None:
        return 200, {"available": False, "reason": f"on {who}'s device; not shared as a file"}, "application/json", None
    status, body, ctype = file_share.read(dict(row), reader, role, device, fetch,
                                          reader_name=((names or {}).get(reader or "") or "Someone").split(" ")[0],
                                          owner_name=who, names=names, card=card, path=refs_db)
    etag = None if isinstance(body, dict) else '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    return status, body, ctype, etag
