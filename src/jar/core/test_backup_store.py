#!/usr/bin/env python3
"""backup_store.py (titan-node#386): consistent copies of titan's SQLite stores without the sqlite3 CLI."""
from __future__ import annotations

import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup_store as bs  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


tmp = Path(tempfile.mkdtemp())
root, dest = tmp / "connectors", tmp / "backups"
(root / "world").mkdir(parents=True)
for p, rows in ((root / "todos.db", 3), (root / "world" / "zach.db", 2), (root / "world" / "household.db", 1)):
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE t (x)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    c.commit()
    c.close()
(root / "notes.txt").write_text("not a store")

print("── which stores ──")
check("the connectors folder and world/, databases only", [str(p.relative_to(root)) for p in bs.stores(root)],
      ["todos.db", "world/household.db", "world/zach.db"])

print("\n── a run ──")
held = sqlite3.connect(root / "todos.db")   # the agent holds the store open, mid-write
held.execute("BEGIN")
held.execute("INSERT INTO t VALUES (99)")
folder, done, bad = bs.run(bs.stores(root), dest, keep=10, stamp="20261001-120000")
held.rollback()
held.close()
check("every store copied, world bubbles prefixed", (sorted(done), bad),
      (["todos.db", "world-household.db", "world-zach.db"], []))
check("the copy has the committed rows only", sqlite3.connect(folder / "todos.db").execute("SELECT COUNT(*) FROM t").fetchone()[0], 3)
check("files are 600", oct(stat.S_IMODE(os.stat(folder / "world-zach.db").st_mode)), "0o600")
check("the folder is 700", oct(stat.S_IMODE(os.stat(folder).st_mode)), "0o700")

print("\n── failures and pruning ──")
bad_store = root / "broken.db"
bad_store.write_bytes(b"this is not sqlite" * 100)
_, done, bad = bs.run([root / "todos.db", bad_store], dest, keep=10, stamp="20261001-120100")
check("a broken store is reported, the others still copied", (done, len(bad), "broken.db" in bad[0]), (["todos.db"], 1, True))
check("…and leaves no half copy", sorted(p.name for p in (dest / "20261001-120100").iterdir()), ["todos.db"])
(dest / "keep-me").mkdir()
for s in ("20261001-120200", "20261001-120300"):
    bs.run([root / "todos.db"], dest, keep=2, stamp=s)
check("keeps the newest runs, leaves other folders alone", sorted(p.name for p in dest.iterdir()),
      ["20261001-120200", "20261001-120300", "keep-me"])

print("\n── the command ──")
check("a missing path: exit 1, nothing written", bs.main([str(root / "nope.db"), "--dest", str(tmp / "b2")]), 1)
check("…and no folder made", (tmp / "b2").exists(), False)
check("named stores: exit 0", bs.main([str(root / "todos.db"), "--dest", str(tmp / "b3")]), 0)
check("a failure: exit 1", bs.main([str(bad_store), "--dest", str(tmp / "b4")]), 1)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
