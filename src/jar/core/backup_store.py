#!/usr/bin/env python3
"""Copy titan's SQLite stores, safely, without the sqlite3 CLI (titan has none; titan-node#386).

    backup_store.py                       every store in the connectors folder (and world/), as the deploy does
    backup_store.py <db> [<db> ...]       just these
    --dest DIR   where the copies go (default /ai/datasets/backups/stores); each run is DIR/<stamp>/
    --keep N     keep the newest N runs in DIR, removing older ones (default 10)

Each copy is SQLite's online backup (consistent while the agent writes), checked with quick_check, and
mode 600 in a 700 folder. Prints one line; exits 1 if any store couldn't be copied. The deploy runs it
before it moves the host checkout, so a schema change in the incoming code can always be undone:
    sudo systemctl stop titan-agent-api && cp <dest>/<stamp>/<name>.db <store> && sudo systemctl start titan-agent-api
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

CONNECTORS = Path(os.environ.get("CONNECTORS_DIR", "/ai/datasets/connectors"))
DEST = Path(os.environ.get("STORE_BACKUP_DIR", "/ai/datasets/backups/stores"))
STAMP_RX = re.compile(r"^\d{8}-\d{6}Z?$")


def stores(root: Path = CONNECTORS) -> list[Path]:
    return sorted(Path(p) for p in glob.glob(str(root / "*.db")) + glob.glob(str(root / "world" / "*.db")))


def copy(src: Path, out: Path) -> None:
    """SQLite's online backup of src into out (600), then quick_check on the copy."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        fd = os.open(str(out), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.close(fd)
        d = sqlite3.connect(str(out))
        try:
            s.backup(d)
            ok = d.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            d.close()
    finally:
        s.close()
    if ok != "ok":
        raise sqlite3.DatabaseError(f"the copy failed quick_check: {ok}")


def prune(dest: Path, keep: int) -> list[Path]:
    runs = sorted(p for p in dest.iterdir() if p.is_dir() and STAMP_RX.match(p.name))
    gone = runs[:-keep] if keep > 0 else []
    for p in gone:
        shutil.rmtree(p, ignore_errors=True)
    return gone


def run(paths: list[Path], dest: Path = DEST, keep: int = 10, stamp: str | None = None) -> tuple[Path, list[str], list[str]]:
    """→ (the run's folder, names copied, failures)."""
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = dest / (stamp or time.strftime("%Y%m%d-%H%M%SZ", time.gmtime()))
    folder.mkdir(mode=0o700, exist_ok=True)
    done, bad = [], []
    for src in paths:
        # world/<bubble>.db -> world-<bubble>.db, so bubbles don't collide with same-named stores
        name = (f"world-{src.name}" if src.parent.name == "world" else src.name)
        try:
            copy(src, folder / name)
            done.append(name)
        except Exception as exc:  # noqa: BLE001
            (folder / name).unlink(missing_ok=True)   # never leave a half copy that looks like a backup
            bad.append(f"{src}: {exc}")
    prune(dest, keep)
    return folder, done, bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--dest", type=Path, default=DEST)
    ap.add_argument("--keep", type=int, default=10)
    a = ap.parse_args(argv)
    paths = a.paths or stores()
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print(f"no such store: {', '.join(missing)}", file=sys.stderr)
        return 1
    folder, done, bad = run(paths, a.dest, a.keep)
    print(f"backed up {len(done)} store{'s' if len(done) != 1 else ''} to {folder}")
    for b in bad:
        print(f"FAILED {b}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
