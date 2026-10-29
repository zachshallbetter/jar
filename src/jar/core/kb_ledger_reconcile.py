#!/usr/bin/env python3
"""Reconcile the connectors ledger with the knowledge base (#1275).

The 10-08 audit found 34 ledger rows (33 mail, 1 daily) saying kb=1, not deleted upstream, for items no longer in the
KB: the 10-03/04 KB cleanup removed them without telling the ledger. Then forget and the ledger's KB counts are wrong,
and an upstream change to one would be treated as a live item rather than one entering the KB. This sets kb=0 on
those rows (nothing else: the record node, the timeline line and the upstream bookkeeping stay).

  python3 scripts/kb_ledger_reconcile.py            dry run: counts by source only
  python3 scripts/kb_ledger_reconcile.py --apply    backs ledger.db up (0600), then sets kb=0 on those rows

Refuses when the KB's source list is empty or fails (a down runtime must never read as "nothing is in the KB"). Skips
rows synced in the last hour (an item being written right now). Counts only. Run on titan with the agent's env."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

RECENT_S = 3600


def plan(sources: list[dict[str, Any]], ledger: Path | None = None, now: float | None = None) -> dict[str, Any]:
    import connectors_runner as C
    if not sources:
        raise SystemExit("refusing: the KB's source list is empty (is the retrieval runtime up?)")
    now = time.time() if now is None else now
    live = {str(s.get("source_id")) for s in sources}
    con = sqlite3.connect(str(ledger or C.LEDGER_PATH))
    try:
        rows = con.execute("SELECT item_id, source, synced_at FROM items WHERE kb=1 AND deleted_at IS NULL").fetchall()
    finally:
        con.close()
    drift = [(i, s) for i, s, at in rows if i not in live and now - float(at or 0) >= RECENT_S]
    recent = sum(1 for i, s, at in rows if i not in live and now - float(at or 0) < RECENT_S)
    by_source: dict[str, int] = {}
    for _, s in drift:
        by_source[s] = by_source.get(s, 0) + 1
    return {"ledger_kb_rows": len(rows), "kb_sources": len(live), "drift": len(drift), "drift_by_source": by_source,
            "skipped_recent": recent, "_ids": [i for i, _ in drift]}


def apply(ids: list[str], ledger: Path | None = None) -> dict[str, Any]:
    import connectors_runner as C
    db = Path(ledger or C.LEDGER_PATH)
    backup = db.with_name(f"{db.name}.bak-{int(time.time())}")
    shutil.copy2(db, backup)
    os.chmod(backup, 0o600)
    with C._lock:
        con = sqlite3.connect(str(db))
        try:
            with con:
                for i in range(0, len(ids), 500):
                    part = ids[i:i + 500]
                    con.execute(f"UPDATE items SET kb=0 WHERE kb=1 AND deleted_at IS NULL AND item_id IN "
                                f"({','.join('?' * len(part))})", part)
            left = con.execute("SELECT COUNT(*) FROM items WHERE kb=1 AND deleted_at IS NULL").fetchone()[0]
        finally:
            con.close()
    return {"reconciled": len(ids), "ledger_kb_rows_now": left, "backup": backup.name}


def main(argv: list[str] | None = None, sources_fn: Callable[[], list[dict[str, Any]]] | None = None,
         ledger: Path | None = None) -> int:
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
    p = plan(sources, ledger)
    ids = p.pop("_ids")
    out: dict[str, Any] = {"mode": "apply" if a.apply else "dry-run", **p}
    if a.apply and ids:
        out |= apply(ids, ledger)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
