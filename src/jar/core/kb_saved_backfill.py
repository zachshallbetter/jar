#!/usr/bin/env python3
"""One-off (#627 follow-up, 10-03): give KB notes whose "saved" shows a later re-save their first date back.

Before _first_saved, re-saving a note with new text re-stamped "saved". The runtime's created_at is never
overwritten on an upsert, so it is the note's first save: "saved" becomes the household date of created_at,
and "updated" keeps the later date the note shows now.

  --dry-run (default)  list the notes it would change: id, saved now, first date. Ids and dates only.
  --apply              back up every touched source (metadata, text, row) to /ai/datasets/backups/ (0600)
                       first, then re-index each one with the corrected dates. The runtime ignores new
                       metadata when the text is unchanged, so each is deleted and indexed again; a note
                       stored in more than one chunk is skipped (its text can't be rebuilt exactly).

Run on titan with the agent env:
  set -a; . /ai/projects/titan-local-agent/agent.env; set +a
  python3 scripts/kb_saved_backfill.py [--apply]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_runtime_lib as A  # noqa: E402
import kb_privacy as P  # noqa: E402
import local_time  # noqa: E402

BACKUP_DIR = Path(os.environ.get("KB_BACKUP_DIR", "/ai/datasets/backups"))


def _when(value: str) -> datetime:
    d = local_time.parse_iso(value)   # titan's Python 3.10 takes only 3 or 6 fraction digits (#766 rollback)
    if d is None:
        raise ValueError(f"not a timestamp: {value!r}")
    return d


def household_date(created_at: str) -> str:
    """The household's date of a runtime timestamp (UTC, nanoseconds and all)."""
    return _when(created_at).astimezone(local_time.zone()).date().isoformat()


def sources() -> list[dict]:
    rows, off = [], 0
    while True:
        r = A._runtime_request("retrieval", f"/v1/retrieval/sources?limit=500&offset={off}", "GET", None, 30)
        items = r.get("sources") or r.get("items") or (r if isinstance(r, list) else [])
        if not items:
            return rows
        rows += items
        off += len(items)


def chunks(sid: str) -> list[dict]:
    r = A._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}/chunks", "GET")
    return sorted(r.get("chunks") or [], key=lambda c: c.get("chunk_index", 0))


def plan(rows: list[dict], meta: "callable") -> list[dict]:
    """The notes to change: a "saved" later than the household date of their first save."""
    out = []
    for s in rows:
        sid = str(s.get("source_id") or "")
        if not sid or not s.get("created_at"):
            continue
        md = meta(sid)
        saved = str(md.get("saved") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", saved):
            continue
        first = household_date(s["created_at"])
        if saved > first:
            later = max(saved, str(md.get("updated") or "")[:10])
            out.append({"source_id": sid, "saved_now": saved, "first": first, "updated": later})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="back up, then re-index with the first date")
    a = ap.parse_args(argv)
    rows = sources()
    by_id = {str(s.get("source_id")): s for s in rows}
    todo = plan(rows, lambda sid: P.source_meta(A._runtime_request, sid))
    print(f"sources {len(rows)}, to change {len(todo)}{'' if a.apply else ' (dry run)'}")
    for t in todo:
        print(f"  {t['source_id']}: saved {t['saved_now']} -> {t['first']} (updated {t['updated']})")
    if not a.apply or not todo:
        return 0

    backup = []
    for t in todo:
        ch = chunks(t["source_id"])
        backup.append({"row": by_id[t["source_id"]], "chunks": ch, "plan": t})
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKUP_DIR / f"kb-saved-backfill-{time.strftime('%Y%m%d-%H%M%SZ', time.gmtime())}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(backup, f)
    print(f"backup {path} ({len(backup)} sources)")

    done = skipped = 0
    for b in backup:
        t, ch, row = b["plan"], b["chunks"], b["row"]
        if len(ch) != 1:
            print(f"  skipped {t['source_id']}: {len(ch)} chunks")
            skipped += 1
            continue
        md = {**(ch[0].get("metadata") or {}), "saved": t["first"], "updated": t["updated"]}
        sid = t["source_id"]
        A._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}", "DELETE")
        A._kb_index(sid, str(row.get("title") or ""), str(ch[0].get("chunk_text") or ""),
                    str(row.get("source_type") or "note"), md)
        got = P.source_meta(A._runtime_request, sid, str(time.time()))
        ok = (got.get("saved"), got.get("updated")) == (t["first"], t["updated"])
        print(f"  {'ok' if ok else 'CHECK'} {sid}: saved {got.get('saved')}, updated {got.get('updated')}")
        done += ok
    print(f"changed {done}, skipped {skipped}, of {len(backup)}; restore from {path} if needed")
    return 0 if done + skipped == len(backup) else 1


if __name__ == "__main__":
    sys.exit(main())
