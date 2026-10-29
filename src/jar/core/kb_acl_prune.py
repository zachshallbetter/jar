#!/usr/bin/env python3
"""Prune sharing-table rows whose item is no longer in the knowledge base (#1274).

The 10-08 audit found 671 of 865 rows for items no longer in the KB: connector removals deleted the KB copy and left
the row (fixed in connectors_runner._kb_delete). KB ids are deterministic (source plus upstream id), so an item that
came back would have inherited its old row, possibly broader than its audience now; since #1274 an item entering the
KB writes a fresh row anyway, and this clears the backlog.

  python3 scripts/kb_acl_prune.py            dry run: counts only (by kind, and how many are broader than private)
  python3 scripts/kb_acl_prune.py --apply    backs the table up (0600), then deletes those rows in one transaction

Refuses when the KB's source list is empty or fails (a down runtime must never read as "every row is stale"). Skips
rows written in the last hour (an item being indexed right now). Counts only, never ids or titles. Run on titan with
the agent's env."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

RECENT_S = 3600


def _kind(sid: str) -> str:
    m = re.match(r"^(cx-[a-z]+|note-runbook|note-decision|note|web|hh)", sid)
    return m.group(1) if m else "other"


def plan(sources: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    import kb_privacy as P
    if not sources:
        raise SystemExit("refusing: the KB's source list is empty (is the retrieval runtime up?)")
    now = time.time() if now is None else now
    live = {str(s.get("source_id")) for s in sources}
    con = P._connect(False)
    rows = con.execute("SELECT source_id, owner, audience, set_at FROM acl").fetchall() if con is not None else []
    if con is not None:
        con.close()
    stale, recent = [], 0
    for sid, owner, aud, at in rows:
        if sid in live:
            continue
        if at is not None and now - float(at) < RECENT_S:
            recent += 1
            continue
        stale.append((sid, owner, aud))
    by_kind: dict[str, int] = {}
    for sid, _, _ in stale:
        by_kind[_kind(sid)] = by_kind.get(_kind(sid), 0) + 1
    broader = sum(1 for _, _, a in stale if str(P._aud.normalize(a, default="private")) != "private")
    return {"rows": len(rows), "kb_sources": len(live), "rows_in_kb": len(rows) - len(stale) - recent,
            "stale": len(stale), "stale_by_kind": by_kind, "stale_broader_than_private": broader,
            "skipped_recent": recent, "_ids": [s for s, _, _ in stale]}


def apply(ids: list[str]) -> dict[str, Any]:
    import kb_privacy as P
    db = Path(P._db_path())
    backup = db.with_name(f"{db.name}.bak-{int(time.time())}")
    shutil.copy2(db, backup)
    os.chmod(backup, 0o600)
    con = P._connect(True)
    try:
        with con:
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                con.execute(f"DELETE FROM acl WHERE source_id IN ({','.join('?' * len(part))})", part)
        left = con.execute("SELECT COUNT(*) FROM acl").fetchone()[0]
    finally:
        con.close()
    return {"deleted": len(ids), "rows_now": left, "backup": backup.name}


def main(argv: list[str] | None = None, sources_fn: Callable[[], list[dict[str, Any]]] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if sources_fn is None:
        import agent_runtime_lib as lib
        sources_fn = lib.kb_sources
    try:
        sources = sources_fn()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"refusing: the KB's source list failed ({type(exc).__name__})")
    p = plan(sources)
    ids = p.pop("_ids")
    out: dict[str, Any] = {"mode": "apply" if a.apply else "dry-run", **p}
    if a.apply and ids:
        out |= apply(ids)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
