#!/usr/bin/env python3
"""Facts in a saved note become world claims, with the note as their evidence (#860).

The #814/#705 lesson: a keyed claim that a newer statement overwrites beats a pile of notes ranked at answer time.
So when someone saves (or replaces) a note, each line that states a changeable fact about one thing titan already
knows becomes that person's claim: a phone, an address, a schedule, a pickup, a birthday (world.EXTRACTABLE) or a
business's hours. The note is the evidence (`note:<source id>#<attr>`), so saving the note again re-states the same
evidence and the claim follows it: keyed overwrite, never a second value beside the old one.

Code picks the facts (the same value_in / cue_ok shapes the memory extractor uses); no model writes a value.
Only things titan already knows (world.find): a note never invents an entity. And a fact goes only where the note
itself could be seen: a household note's facts to household things, a private note's to the person's own bubble.
Anything else is left alone, so a private note never becomes a household claim.

  from_note(w, source_id, title, text, person, role, people, audience) -> [(entity id, attr, value, code)]
  python3 scripts/note_claims.py backfill [--apply]   (in the agent's env) the KB's notes, dry run by default
  python3 scripts/note_claims.py refile [--apply]     notes' own-bubble facts set to each note's audience (10-06)

A household note's facts in its saver's own bubble are the household's, as the note is; a private note's stay private;
when a note is shared or unshared (kb_privacy), its facts follow (refile).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import world as W  # noqa: E402

# A business's hours: "Hours: Mon–Fri 8–5", "open weekdays 9 AM to 6 PM". The cue word is required.
_HOURS_RX = re.compile(r"(?i)\b(?:hours?|open)\b\s*(?:are|is|:)?\s*"
                       r"((?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*|weekdays?|weekends?|daily|every day)"
                       r"[^.;\n]{0,60}?\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:-|–|to)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?)")
NOTE_ATTRS = tuple(sorted(W.EXTRACTABLE | {"hours"}))


def value_in(attr: str, line: str) -> str | None:
    if attr == "hours":
        m = _HOURS_RX.search(line)
        return " ".join(m.group(1).split()) if m else None
    return W.value_in(attr, line)


def _lines(title: str, text: str) -> list[str]:
    """Each sentence or line, carrying the title when it names nothing itself ("Hours: Mon–Fri 9–5" under
    "Maple Dental")."""
    out = []
    for part in re.split(r"(?<=[.!?])\s+|\n+", str(text or "")):
        part = " ".join(part.split())
        if part:
            out.append(part)
    return out


def _allowed(cid: str, person: str, audience: Any) -> bool:
    """A fact goes only where its note could be seen: household note → household things; a private note → the
    person's own bubble. Anything else (a note shared with named people, a kid's bubble) is left alone."""
    bubble = cid.split(":", 1)[0] if ":" in cid else "household"
    aud = audience if isinstance(audience, str) else "shared"
    if bubble == "household":
        return aud == "household"
    return bubble == W.bubble_name(person) and aud in ("private", "household")


def from_note(w, source_id: str, title: str, text: str, person: str | None, role: str | None,
              people: list[dict[str, Any]] | None, audience: Any,
              observed_at: str | None = None) -> list[tuple[str, str, str, str]]:
    """The note's facts as the saver's claims, the note as evidence. → [(entity id, attr, value, code)]."""
    out: list[tuple[str, str, str, str]] = []
    if not person or role not in ("admin", "adult", "kid"):
        return out
    title = " ".join(str(title or "").split())
    about = w.find(title, person, role) if title else []
    # the world's own clock and form, so the note's claim orders correctly against what titan already holds
    import local_time
    parsed = local_time.parse_iso(observed_at) if observed_at else None
    when = (parsed or w.now()).isoformat(timespec="seconds")
    seen: set[tuple[str, str]] = set()
    for line in _lines(title, text):
        named = w.find(line, person, role) or about
        for attr in NOTE_ATTRS:
            val = value_in(attr, line)
            if not val:
                continue
            own = [c for c in named if attr in W.ATTRS.get(w.entity(c)["type"], {})
                   and (attr == "hours" or W.cue_ok(attr, w.entity(c)["type"], line))]
            if len(own) != 1 or (own[0], attr) in seen or not _allowed(own[0], person, audience):
                continue
            cid = own[0]
            seen.add((cid, attr))
            ent = w.entity(cid)
            src = {"ref": f"note:{source_id}#{attr}", "label": f"the note “{title or source_id}”", "observed_at": when}
            done, _, code = W.set_fact(w, ent["name"], attr, val, person, role, people=people, cid=cid, source=src,
                                       note_audience=audience if isinstance(audience, str) else None)
            if done:
                out.append((cid, attr, val, code))
    return out


def backfill(apply: bool) -> dict[str, Any]:
    """Every KB note through from_note, as its owner. Dry run: the world is a throwaway copy, and only counts by
    entity kind and attribute are printed, never values."""
    import shutil
    import tempfile
    import agent_runtime_lib as lib
    import household_identity as hh
    import kb_hygiene
    import kb_privacy
    people = hh.get_store().people()
    roles = {p["id"]: p.get("role") for p in people}
    if apply:
        w = W.get()
    else:
        tmp = Path(tempfile.mkdtemp(prefix="note-claims-dry-"))
        shutil.copytree(W.get().root, tmp / "world")
        w = W.Bubbles(tmp / "world")
        W.send_asks = lambda *a, **k: None    # a kid's proposal would card a parent: never from a dry run
    srcs = [s for s in lib.kb_sources() if str(s.get("source_id", "")).startswith("note-")]
    rows = kb_privacy.acl_rows(s["source_id"] for s in srcs)
    counts: dict[str, int] = {}
    notes_with = 0
    for s in srcs:
        sid = s["source_id"]
        owner, aud = kb_privacy.acl_of(s.get("metadata"), sid, rows)
        got = from_note(w, sid, str(s.get("title") or ""), kb_hygiene.source_text(sid) or "", owner,
                        roles.get(owner, "admin" if owner == kb_privacy.LEGACY_OWNER else None), people, aud,
                        observed_at=str(s.get("updated_at") or "") or None)
        notes_with += bool(got)
        for cid, attr, _v, code in got:
            k = f"{w.entity(cid)['type']}.{attr} ({code})"
            counts[k] = counts.get(k, 0) + 1
    return {"notes": len(srcs), "notes with facts": notes_with, "claims by kind": counts, "applied": apply}


def refile(source_id: str, owner: str | None, audience: Any, w: Any = None, apply: bool = True) -> int:
    """The note's audience changed (shared, unshared): its facts in the owner's bubble follow. → rows changed."""
    if not owner:
        return 0
    return (w or W.get()).reaudience_note(source_id, owner, audience if isinstance(audience, str) else "private", apply)


def refile_all(apply: bool) -> dict[str, Any]:
    """Every KB note's own-bubble facts set to the note's audience today (10-06 migration: household notes' facts were
    recorded private). Apply backs the world up first (0600). Counts only."""
    import agent_runtime_lib as lib
    import kb_privacy
    w = W.get()
    srcs = [s for s in lib.kb_sources() if str(s.get("source_id", "")).startswith("note-")]
    rows = kb_privacy.acl_rows(s["source_id"] for s in srcs)
    backup = None
    if apply:
        backup = backup_world(w)
    by: dict[str, int] = {}
    total = 0
    for s in srcs:
        sid = s["source_id"]
        owner, aud = kb_privacy.acl_of(s.get("metadata"), sid, rows)
        n = refile(sid, owner, aud, w, apply)
        if n:
            k = "household" if aud == "household" else "private"
            by[k] = by.get(k, 0) + n
            total += n
    return {"notes": len(srcs), "evidence rows changed" if apply else "evidence rows to change": total,
            "to audience": by, "backup": str(backup) if backup else None, "applied": apply}


def backup_world(w: Any) -> Path:
    """A consistent copy of every bubble file (sqlite's backup API), 0600, under /ai/backups/world-<stamp>."""
    import os
    import sqlite3
    import time
    root = Path(os.environ.get("WORLD_BACKUP_DIR", "/ai/backups")) / f"world-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    for f in sorted(Path(w.root).glob("*.db")):
        src = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        dst = sqlite3.connect(str(root / f.name))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.chmod(root / f.name, 0o600)
    return root


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("--apply", action="store_true", help="write the claims (default: a dry run on a copy)")
    r = sub.add_parser("refile", help="set notes' own-bubble facts to each note's audience (dry run by default)")
    r.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    print(json.dumps(backfill(a.apply) if a.cmd == "backfill" else refile_all(a.apply), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
