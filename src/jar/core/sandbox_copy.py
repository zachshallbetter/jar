#!/usr/bin/env python3
"""The copy step of scripts/sandbox_box.sh: every connectors store and world file and EVERY memory file into a box dir.

sqlite files go through the backup API (a WAL-safe, consistent copy, so -wal/-shm aren't copied); anything else is
copied as a file. That includes memory's persona.md: #770's boxes copied only *.db and ran without the L3 persona,
so their context wasn't live's. Writes manifest.json with what was copied and the box's switches.

Run: python3 scripts/sandbox_copy.py <box dir> <connectors dir> <memory dir> <port> <code rev> <jev on|off> <cloud on|off> [profile]
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time

SKIP = ("-wal", "-shm", "-journal")
# Cards, scheduled actions and pushes start empty: a box never acts on the real ones.
EMPTY_STORES = ("proactive-empty.db", "scheduled-empty.db", "push-empty.db")


def copy_file(src: str, dst: str) -> bool:
    """One file into the box: sqlite by backup, the rest as is. False for a WAL/SHM side file (the backup has it)."""
    if os.path.basename(src).endswith(SKIP):
        return False
    if src.endswith(".db"):
        s, d = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(dst)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
    else:
        shutil.copy2(src, dst)
    return True


def copy_all(box: str, live: str, mem_live: str) -> dict[str, dict[str, int]]:
    copied: dict[str, dict[str, int]] = {"stores": {}, "world": {}, "memory": {}}
    for kind, src_dir, dst_dir in (("stores", live, "stores"), ("world", os.path.join(live, "world"), "world"),
                                   ("memory", mem_live, "mem")):
        if not os.path.isdir(src_dir):
            continue
        os.makedirs(os.path.join(box, dst_dir), exist_ok=True)
        for name in sorted(os.listdir(src_dir)):
            src, dst = os.path.join(src_dir, name), os.path.join(box, dst_dir, name)
            if os.path.isfile(src) and copy_file(src, dst):
                copied[kind][name] = os.path.getsize(dst)
    for name in EMPTY_STORES:
        sqlite3.connect(os.path.join(box, "stores", name)).close()
    return copied


def main(argv: list[str]) -> int:
    box, live, mem_live, port, rev, jev, cloud = argv[:7]
    profile = argv[7] if len(argv) > 7 else "live"
    copied = copy_all(box, live, mem_live)
    manifest = {"port": int(port), "code": rev, "profile": profile, "jev": jev, "cloud": cloud,
                "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "copied": copied,
                "turn_log": os.path.join(box, "turns.jsonl")}
    with open(os.path.join(box, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print("copied:", {k: len(v) for k, v in copied.items()}, "memory files:", sorted(copied["memory"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
