#!/usr/bin/env python3
"""Settings › You (titan-node#1079): what titan holds of yours, and a copy of it. Everything here is the caller's own.

  counts   GET /v1/me/data → {memories, chats, notes, standing_shares}
  export   POST /v1/me/export builds one archive (zip: data.json + Markdown) of the caller's own chats, memories and KB
           notes; GET /v1/me/export → its state, and the file once ready. One per person, kept EXPORT_KEEP_S (24 h), then
           deleted. Never another person's item: one shared with the caller is listed by title only. Kids and shared
           screens: 403 (a parent exports for a kid, later).
Store: EXPORT_DIR (default /ai/datasets/exports, 0700 per person).
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

EXPORT_DIR = Path(os.environ.get("EXPORT_DIR", "/ai/datasets/exports"))
EXPORT_KEEP_S = 24 * 3600
_lock = threading.Lock()


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _root() -> Path:
    return Path(os.environ.get("EXPORT_DIR") or EXPORT_DIR)


def counts(person: str | None, role: str | None, *, facts: int, chats: int, sharing: dict[str, Any],
           notes: int | None = None) -> dict[str, int]:
    """notes: the caller's notes still in the KB, counted as the export finds them (an access row alone isn't a note)."""
    if not person or role == "household":
        raise Refused(403, "a shared screen has nothing of its own")
    if notes is None:
        notes = sum(sharing.get("notes", {}).values())
    shared = sum(v for k, v in sharing.get("notes", {}).items() if k != "private") + \
        sum(v for k, v in sharing.get("facts", {}).items() if k != "private") + len(sharing.get("grants") or [])
    return {"memories": int(facts), "chats": int(chats), "notes": int(notes), "standing_shares": int(shared)}


def _md_chat(c: dict[str, Any], msgs: list[dict[str, Any]]) -> str:
    out = [f"# {c.get('title') or 'Conversation'}", f"_{c.get('started_at', '')} – {c.get('last_at', '')}_", ""]
    for m in msgs:
        out.append(f"**{'You' if m.get('role') == 'user' else 'titan'}** ({m.get('at', '')}): {m.get('text', '')}")
        out.append("")
    return "\n".join(out)


def _sweep(person_dir: Path, now: float) -> None:
    for f in person_dir.glob("export-*.zip"):
        try:
            if now - f.stat().st_mtime > EXPORT_KEEP_S:
                f.unlink()
        except OSError:
            pass


def state(person: str | None, role: str | None, now: float | None = None) -> dict[str, Any]:
    _check(person, role)
    now = time.time() if now is None else now
    d = _root() / _safe(person)
    if not d.exists():
        return {"state": "none"}
    _sweep(d, now)
    if (d / ".building").exists():
        return {"state": "building"}
    files = sorted(d.glob("export-*.zip"))
    if not files:
        return {"state": "none"}
    f = files[-1]
    try:
        meta = json.loads((d / "export.json").read_text())
    except (OSError, ValueError):
        meta = {}
    return {"state": "ready", "file": f.name, "bytes": f.stat().st_size, "truncated": bool(meta.get("truncated", False)),
            "counts": meta.get("counts"),
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(f.stat().st_mtime + EXPORT_KEEP_S))}


def path_of(person: str | None, role: str | None) -> Path | None:
    st = state(person, role)
    return (_root() / _safe(person) / st["file"]) if st.get("state") == "ready" else None


def _check(person: str | None, role: str | None) -> None:
    if not person or role == "household":
        raise Refused(403, "a shared screen has nothing of its own")
    if role == "kid":
        raise Refused(403, "ask a parent")


def _safe(person: str | None) -> str:
    return "".join(ch for ch in str(person) if ch.isalnum() or ch in "_-")[:64]


def build(person: str, role: str | None, *, chats: Callable[[], Any], memories: Callable[[], list[dict]],
          notes: Callable[[], list[dict]], shared_titles: Callable[[], list[str]],
          forgotten: Callable[[], list[dict]] | None = None, dev_notes: Callable[[], list[dict]] | None = None,
          now: float | None = None) -> dict[str, Any]:
    """The archive, everything: each chat as chats/<id>.json and .md (streamed one at a time, so memory stays flat), every
    memory (and the forgotten ones, restorable until purged, and their dev notes, #1605) in memories.json/.md, each note, and data.json with the
    counts and `truncated: false` (nothing is capped; if a cap is ever added, it must say so here and in the README).
    Only the caller's own rows; others' shared items are titles only. Replaces any earlier archive of theirs."""
    _check(person, role)
    now = time.time() if now is None else now
    d = _root() / _safe(person)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    flag = d / ".building"
    with _lock:
        if flag.exists():
            return {"state": "building"}
        flag.write_text(str(now))
    tmp = d / f".export-{int(now)}.zip.part"
    try:
        n_chats = n_msgs = 0
        fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as raw, zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as z:
            for c, m in chats():
                z.writestr(f"chats/{_safe(c.get('id'))}.json", json.dumps({**c, "messages": m}, indent=1))
                z.writestr(f"chats/{_safe(c.get('id'))}.md", _md_chat(c, m))
                n_chats += 1
                n_msgs += len(m)
            ms, ns, st = memories(), notes(), shared_titles()
            fs = forgotten() if forgotten else []
            dn = dev_notes() if dev_notes else []
            z.writestr("memories.json", json.dumps({"memories": ms, "forgotten": fs, **({"dev_notes": dn} if dn else {})},
                                                   indent=1))
            z.writestr("memories.md", "# What titan remembers about you\n\n" + "\n".join(f"- {f.get('text', '')}" for f in ms)
                       + ("\n\n## Forgotten (restorable until purged)\n\n" + "\n".join(f"- {f.get('text', '')}" for f in fs)
                          if fs else "")
                       + ("\n\n## Your notes about titan (never used as facts about you)\n\n"
                          + "\n".join(f"- {f.get('text', '')}" for f in dn) if dn else ""))
            for n in ns:
                z.writestr(f"notes/{_safe(n.get('id'))}.md", f"# {n.get('title', '')}\n\n{n.get('text', '')}")
            counts = {"chats": n_chats, "messages": n_msgs, "memories": len(ms), "forgotten_memories": len(fs),
                      "notes": len(ns)}
            z.writestr("data.json", json.dumps({"person": person, "made_at": now, "truncated": False, "counts": counts,
                                                "notes": ns, "shared_with_you": [{"title": t} for t in st]}, indent=1))
            z.writestr("README.md", "Your titan data, all of it (nothing was left out: truncated = false in data.json):\n"
                                    "chats/ (one .json and .md each), memories.json/.md (with forgotten ones), notes/, "
                                    "data.json. Items other people shared with you are listed by title only.\n")
        for old in d.glob("export-*.zip"):
            old.unlink()
        f = d / f"export-{int(now)}.zip"
        tmp.rename(f)
        meta = d / "export.json"
        meta.write_text(json.dumps({"file": f.name, "truncated": False, "counts": counts}))
        os.chmod(meta, 0o600)
        return {"state": "ready", "file": f.name, "truncated": False, "counts": counts}
    finally:
        flag.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
