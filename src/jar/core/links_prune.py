#!/usr/bin/env python3
"""Take items out of the link index (#951, owner call 10-05: only mail triage keeps is indexed). The ids come from a
file, one per line (mail_server's re-judge lists the triaged-out mail). Only mail items are removed; anything else in
the list is reported and left alone.

What goes: each item's hooks and topic vector, and every edge touching it (logged in link_log as "removed from the
index", so its history stays). Statements stay: they're the person's word and cost nothing.
Before anything is written, links.db is copied to LINKS_BACKUP_DIR (0600). Counts before and after; no ids or text
are printed.

  python3 scripts/links_prune.py --ids /ai/tmp/drop.txt            counts only
  python3 scripts/links_prune.py --ids /ai/tmp/drop.txt --apply    backup, then remove
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

BACKUP_DIR = Path(os.environ.get("LINKS_BACKUP_DIR", "/ai/backups/links"))
SOURCES = frozenset({"mail"})


def counts(con: sqlite3.Connection) -> dict[str, Any]:
    import linker
    con.executescript(linker.SCHEMA)
    import topics
    con.executescript(topics.SCHEMA)
    return {"items": dict(con.execute("SELECT source, COUNT(DISTINCT item_id) FROM hooks GROUP BY source").fetchall()),
            "edges": dict(con.execute("SELECT state, COUNT(*) FROM links GROUP BY state").fetchall()),
            "topics": con.execute("SELECT COUNT(*) FROM topics").fetchone()[0]}


def backup(src: Path, dest_dir: Path | None = None) -> Path:
    """A consistent copy (sqlite's backup API, WAL included), 0600."""
    d = Path(dest_dir or BACKUP_DIR)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"links-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.db"
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    t = sqlite3.connect(str(out))
    try:
        s.backup(t)
    finally:
        t.close()
        s.close()
    os.chmod(out, 0o600)
    return out


def prune(con: sqlite3.Connection, ids: set[str], apply: bool) -> dict[str, Any]:
    import linker
    import topics
    con.executescript(linker.SCHEMA)
    con.executescript(topics.SCHEMA)
    src = {}
    for i in ids:
        r = con.execute("SELECT source FROM hooks WHERE item_id=? LIMIT 1", (i,)).fetchone()
        src[i] = r[0] if r else None
    drop = {i for i, s in src.items() if s in SOURCES}
    why = Counter("not in the index" if s is None else ("removed" if s in SOURCES else f"kept: {s} item")
                  for s in src.values())
    edges = Counter()
    for a, b, st in con.execute("SELECT a, b, state FROM links"):
        if a in drop or b in drop:
            edges[st] += 1
    out = {"listed": len(ids), "by_reason": dict(why), "edges_touching": dict(edges), "apply": apply}
    if not apply or not drop:
        return out
    now = time.time()
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _drop (item_id TEXT PRIMARY KEY)")
    con.execute("DELETE FROM _drop")
    con.executemany("INSERT INTO _drop VALUES (?)", [(i,) for i in drop])
    con.execute("INSERT INTO link_log SELECT a, b, state, 'gone', 'removed from the index (mail scope, #951)', ? FROM links "
                "WHERE a IN (SELECT item_id FROM _drop) OR b IN (SELECT item_id FROM _drop)", (now,))
    con.execute("DELETE FROM links WHERE a IN (SELECT item_id FROM _drop) OR b IN (SELECT item_id FROM _drop)")
    con.execute("DELETE FROM topics WHERE item_id IN (SELECT item_id FROM _drop)")
    con.execute("DELETE FROM hooks WHERE item_id IN (SELECT item_id FROM _drop)")
    con.commit()
    return out


def main(argv: list[str] | None = None) -> int:
    import hooks
    ap = argparse.ArgumentParser(description="Take mail items out of the link index (#951); counts only")
    ap.add_argument("--ids", required=True, help="a file of item ids, one per line")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    ids = {ln.strip() for ln in Path(a.ids).read_text().splitlines() if ln.strip()}
    con = hooks.connect()
    rep: dict[str, Any] = {"before": counts(con)}
    if a.apply:
        rep["backup"] = str(backup(Path(hooks.LINKS_DB)))
    rep.update(prune(con, ids, a.apply))
    rep["after"] = counts(con)
    print(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
