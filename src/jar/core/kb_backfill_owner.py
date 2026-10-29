#!/usr/bin/env python3
"""Give every existing knowledge-base source an owner and an audience (kb_privacy.py).

Before 09-30 nothing in the KB had an owner, so every person (kids and shared devices too) could
read all of it. This sets, for each source without one:

  connector items (connector:*)               zach, private   (his Mac pushed all of them)
  system notes (knowledge_record: kb_seed_*)  no owner, admins
  household.yaml (household, hh-*)            no owner, household
  saved notes and pages (note, web, other)    zach, private

Why a table, not the metadata: the retrieval runtime has no metadata update, and re-indexing a
source with unchanged text returns early on its checksum and keeps the old metadata
(retrieval-runtime/src/storage/db.rs, upsert_source_and_chunks). Deleting and re-indexing all of
them would re-embed everything and reset their dates. So this writes kb_privacy's sharing table
(KB_ACL_DB, default /ai/datasets/connectors/kb_acl.db), which every read path checks first.
Without this run the read paths still treat a source with no owner as Zach's and private (the
legacy default); the run adds the admins / household audiences and makes the listing cheaper.

Never replaces a row that exists (a share made since), and skips sources whose metadata already
carries an owner or an audience (written after the change), unless --explicit: then those get a row too, copied
from their metadata, so every item has one and a missing row decides nothing (#833, Zach 10-04: a missing row means
admin only). The report also counts every source by zone (zones.py). --apply backs the table up first (0600).

Run on titan with the agent env (read-only unless --apply):
    set -a; . /ai/projects/titan-local-agent/agent.env; set +a
    python3 scripts/kb_backfill_owner.py --dry-run     # counts only, writes nothing
    python3 scripts/kb_backfill_owner.py --apply
    python3 scripts/kb_backfill_owner.py --explicit [--apply]   # rows for every source, from its metadata too
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import os
import sys
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb_privacy as P  # noqa: E402


def classify(src: dict[str, Any]) -> tuple[str, str | None, str]:
    """(class, owner, audience) for a source with no owner yet."""
    st = str(src.get("source_type") or "")
    sid = str(src.get("source_id") or "")
    if st.startswith("connector:"):
        return "connector", P.LEGACY_OWNER, "private"
    if st == "knowledge_record":
        return "system", None, "admins"
    if st == "household" or sid.startswith("hh-"):
        return "household.yaml", None, "household"
    return "saved", P.LEGACY_OWNER, "private"


def plan(call: Callable[..., dict], explicit: bool = False) -> tuple[list[tuple[str, str | None, str, str]],
                                                                    collections.Counter]:
    """[(source_id, owner, audience, class)] to write, and counts per class, outcome and zone."""
    import zones
    import agent_runtime_lib as lib
    sources = lib.kb_sources(call)   # paged
    rows = P.acl_rows(s["source_id"] for s in sources)
    todo = [s for s in sources if s["source_id"] not in rows]
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        metas = list(pool.map(lambda s: P.source_meta(call, s["source_id"]), todo))
    counts: collections.Counter = collections.Counter()
    counts["sources"] = len(sources)
    counts["already in the sharing table"] = len(sources) - len(todo)
    for s in sources:
        counts[f"zone {zones.zone_of(None, s['source_id'], s.get('source_type'))}"] += 1
    out = []
    for s, md in zip(todo, metas):
        kind, owner, aud = classify(s)
        if "owner" in md or md.get("audience"):
            if explicit:
                owner, aud = P.acl_of(md)
                counts[f"{kind}: explicit from metadata -> {owner or '(no owner)'}/{aud}"] += 1
                out.append((s["source_id"], owner, aud, "explicit"))
            else:
                counts[f"{kind}: metadata already says"] += 1
            continue
        counts[f"{kind} -> {owner or '(no owner)'}/{aud}"] += 1
        out.append((s["source_id"], owner, aud, kind))
    counts["to write"] = len(out)
    return out, counts


def main(argv: list[str] | None = None, call: Callable[..., dict] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="count what would be written (the default)")
    g.add_argument("--apply", action="store_true", help="write the sharing-table rows")
    ap.add_argument("--explicit", action="store_true", help="also rows for sources whose metadata says (from it)")
    args = ap.parse_args(argv)
    if call is None:
        import agent_runtime_lib as lib
        call = lib._runtime_request
    todo, counts = plan(call, explicit=args.explicit)
    if args.apply and todo and P._db_path().exists():
        import shutil
        import time as _t
        bak = P._db_path().with_name(f"kb_acl.before-backfill-{int(_t.time())}.db")
        shutil.copy2(P._db_path(), bak)
        os.chmod(bak, 0o600)
        counts["backup"] = 1
        print(f"backup: {bak}", file=sys.stderr)
    if args.apply:
        for sid, owner, aud, kind in todo:
            P.set_acl(sid, owner, aud, by="kb_backfill_owner", why=f"backfill:{kind}", keep=True)
        counts["written"] = len(todo)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "table": str(P._db_path()),
                      "counts": dict(sorted(counts.items()))}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
