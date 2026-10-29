#!/usr/bin/env python3
"""How long the KB's own backups are kept (titan-node#905; Zach, 10-04: "30 days, then shred").

Covers the writers that copy household text off the KB before changing it: kb_lifecycle's expiry backups
(kb-lifecycle-*.jsonl), envelope_pass's originals, new items and the migration alike (envelopes-*.jsonl), and the
one-off KB cleanups' backups in the same folder as kb_lifecycle's (kb-cleanup-*.jsonl, kb-delete-*.json; Zach, 10-04,
#905), and the mail store's copy before a one-off redaction (mail-server-*.db, #979). Each run of kb_lifecycle prunes its folder's patterns and each applying envelope_pass its own, so nothing else
needs a timer.

A backup's age is the later of its file name's stamp and its modification time, so it is never shredded early. Shred
is: overwrite the whole file with random bytes, flush it to disk, then unlink. Only regular files matching the
writer's own pattern are touched; symlinks and anything else in the directory are left alone.

  python3 scripts/backup_retention.py            dry run: what is due, in both directories
  python3 scripts/backup_retention.py --apply    shred what is due
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

KEEP_DAYS = 30
# In kb_lifecycle's folder: its own backups and the one-off cleanups' (no writer of theirs runs again to prune them).
# A cleanup's backup folder (kb-cleanup-<stamp>/) is not a file and isn't matched.
# mail-server-*.db: mail_server.py redact-store's copy of the mail store before it masks it in place (#979).
LIFECYCLE_DIR_PATTERNS = ("kb-lifecycle-*.jsonl", "kb-cleanup-*.jsonl", "kb-delete-*.json", "mail-server-*.db")
DAY = 86400.0
_STAMP_RX = re.compile(r"(\d{8})-(\d{6})(Z?)")


def age_s(path: Path, now: float) -> float:
    """Seconds since the backup was written: the younger of its name's stamp and its mtime."""
    when = path.lstat().st_mtime
    m = _STAMP_RX.search(path.name)
    if m:
        try:   # writers stamp in local time or UTC; either is within a day, and the later one wins
            named = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            if m.group(3):   # a trailing Z is UTC; an old bare stamp is local or UTC
                named = named.replace(tzinfo=timezone.utc)
            when = max(when, named.timestamp())
        except ValueError:
            pass
    return now - when


def shred(path: Path) -> None:
    """Overwrite with random bytes, fsync, unlink."""
    size = path.lstat().st_size
    fd = os.open(str(path), os.O_WRONLY)
    try:
        left = size
        while left > 0:
            n = min(left, 1 << 20)
            os.write(fd, os.urandom(n))
            left -= n
        os.fsync(fd)
    finally:
        os.close(fd)
    path.unlink()


def prune(folder: Path, pattern: str, *, now: float | None = None, apply: bool = True,
          days: float = KEEP_DAYS) -> list[str]:
    """Shred `pattern` backups in `folder` older than `days` (apply) or list them (dry run). → their names."""
    now = time.time() if now is None else now
    if not folder.is_dir():
        return []
    due = []
    for p in sorted(folder.glob(pattern)):
        try:
            if not stat.S_ISREG(p.lstat().st_mode) or age_s(p, now) <= days * DAY:
                continue
            if apply:
                shred(p)
            due.append(p.name)
        except OSError as exc:   # one unreadable file doesn't stop the rest
            print(f"(backup_retention: {p.name}: {exc})", file=sys.stderr)
    return due


def targets() -> list[tuple[Path, str]]:
    import envelope_pass
    import kb_lifecycle
    return ([(kb_lifecycle.BACKUP_DIR, g) for g in LIFECYCLE_DIR_PATTERNS]
            + [(envelope_pass.BACKUP_DIR, envelope_pass.BACKUP_GLOB)])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Shred KB backups older than 30 days (#905)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    now = time.time()
    out = {}
    for d, g in targets():
        if not a.apply:   # the dry run lists each due file with its age, nothing else
            due = prune(d, g, now=now, apply=False)
            out[str(d / g)] = [f"{n} ({age_s(d / n, now) / DAY:.0f} d)" for n in due]
        else:
            out[str(d / g)] = prune(d, g, now=now, apply=True)
    print(json.dumps({"apply": a.apply, "keep_days": KEEP_DAYS, "due" if not a.apply else "shredded": out}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
