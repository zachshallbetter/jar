#!/usr/bin/env python3
"""One KB item per repeating calendar event, for what was synced before connectors_runner wrote them that way
(10-02 audit: 166 calendar occurrences in the KB were 66 series; Ballet ×28 kept "When is Ava's ballet?" out of
the top 3). For each series: its latest occurrence's text and provenance become the series' note, the occurrences'
own KB copies go, and the record runtime's occurrence nodes (the world model's) stay. Counts only.

  python3 scripts/calendar_series_migrate.py            dry run
  python3 scripts/calendar_series_migrate.py --apply    (a fleet write: only with the owner session's go)
  python3 scripts/calendar_series_migrate.py --refresh  rewrite every series' item after its text changed in code
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))


def migrate(call: Callable[..., dict], chunks: Callable[[str], tuple[str, dict]], ledger: Path | None = None,
            apply: bool = False) -> dict[str, Any]:
    """chunks(item_id) → (text, metadata) of a KB item."""
    import connectors_runner as cx
    with cx._lock:
        con = cx._db(ledger)
        try:
            rows = con.execute("SELECT source_id, item_id, owner FROM items WHERE source='calendar' AND kb=1 "
                               "AND deleted_at IS NULL").fetchall()
            groups: dict[str, list[tuple[str, str, str]]] = {}
            for key, iid, owner in rows:
                sk = cx.series_key("calendar", key)
                if sk:
                    groups.setdefault(sk, []).append((key, iid, owner))
            out = {"occurrence_items": sum(len(v) for v in groups.values()), "series": len(groups), "applied": apply}
            if not apply:
                return out
            for sk, occ in groups.items():
                key, iid, owner = max(occ, key=lambda o: o[0].rsplit("@", 1)[1])   # the latest occurrence
                owner = owner or cx.OWNER_DEFAULT   # a row from before the owner column (the backfill's rule)
                text, md = chunks(iid)
                title = (text.splitlines() or [""])[0][:300]
                prov = {k: md.get(k) for k in ("source", "source_id", "observed_at", "account", "synced_at", "owner",
                                               "audience", "upstream_id", "item_id", "kind", "digest") if k in md}
                con.execute("INSERT OR REPLACE INTO series (source, series_key, owner, title, text, prov, at) "
                            "VALUES (?,?,?,?,?,?,?)", ("calendar", sk, owner, title, text, json.dumps(prov), time.time()))
                for _, oid, _ in occ:
                    call("retrieval", f"/v1/retrieval/sources/{oid}", "DELETE", None)
                    con.execute("UPDATE items SET kb=0 WHERE item_id=?", (oid,))
                cx._index_series(con, call, "calendar", sk)
                con.commit()
            return out
        finally:
            con.close()


def refresh(call: Callable[..., dict], ledger: Path | None = None) -> int:
    """Rewrite every series' KB item from the ledger (after its text changed in code). → series rewritten."""
    import connectors_runner as cx
    with cx._lock:
        con = cx._db(ledger)
        try:
            keys = [r[0] for r in con.execute("SELECT series_key FROM series WHERE source='calendar'")]
            for k in keys:
                cx._index_series(con, call, "calendar", k)
            con.commit()
            return len(keys)
        finally:
            con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="rewrite every series' KB item (a fleet write)")
    a = ap.parse_args()
    import connectors_runner as cx
    import kb_query
    if a.refresh:
        print(json.dumps({"refreshed": refresh(cx._default_runtime)}))
        return 0
    print(json.dumps(migrate(cx._default_runtime, kb_query._chunks, apply=a.apply)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
