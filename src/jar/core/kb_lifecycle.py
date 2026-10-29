#!/usr/bin/env python3
"""The KB's lifecycle (docs/kb-partitions.md §3, #833; Zach's retention, 10-04): sync and web items expire, facts and
system items never do.

  mail, scans        180 days after its envelope (first seen + 1 day, envelope_pass's age)
  calendar event     30 days after it ends (a one-off); a series while it has an occurrence in the last 30 days, and
                     at once when every occurrence was deleted or forgotten (an orphaned series note)
  saved web page     90 days after it was saved
  facts, system      never

Never expires: an item someone keeps (`keep <id>`), or one a live world-model claim cites as evidence (the claim's
fact outlives the passage only while the claim stands). An item whose age titan can't tell is kept and counted.

Expiring is not forgetting: only the KB copy goes (and its sharing row); no tombstone, and the connectors ledger says
kb=0, so an unchanged re-sync is skipped and a changed item comes back under its zone's rules. --apply first writes
every expiring item (text and metadata) to a 0600 backup; backups are kept 30 days, then shredded (Zach, 10-04, #905):
each run prunes them (a dry run only lists what is due).

  python3 scripts/kb_lifecycle.py                 dry run: counts per zone and rule (the default)
  python3 scripts/kb_lifecycle.py --apply         back up, then expire
  python3 scripts/kb_lifecycle.py --nightly       the timer: applies only with KB_LIFECYCLE=apply, else a dry run
  python3 scripts/kb_lifecycle.py keep <id> | unkeep <id>
Each run appends one JSON line (counts only) to $KB_LIFECYCLE_LOG for the scorecard.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

DAY = 86400.0
MAIL_DAYS = 180
EVENT_DAYS = 30
WEB_DAYS = 90
LOG = Path(os.environ.get("KB_LIFECYCLE_LOG", "/ai/logs/kb-lifecycle.jsonl"))
BACKUP_DIR = Path(os.environ.get("KB_LIFECYCLE_BACKUP_DIR", "/ai/datasets/backups"))
BACKUP_GLOB = "kb-lifecycle-*.jsonl"   # kept 30 days, then shredded (#905; backup_retention)


# ── pins ──────────────────────────────────────────────────────────────────────────────────────

def _keep_db() -> sqlite3.Connection:
    import kb_privacy as P
    con = sqlite3.connect(str(P._db_path()))
    con.execute("CREATE TABLE IF NOT EXISTS keep (source_id TEXT PRIMARY KEY, by TEXT, at REAL NOT NULL)")
    return con


def keep(source_id: str, by: str | None = None, on: bool = True) -> None:
    con = _keep_db()
    try:
        if on:
            con.execute("INSERT OR REPLACE INTO keep VALUES (?,?,?)", (source_id, by, time.time()))
        else:
            con.execute("DELETE FROM keep WHERE source_id=?", (source_id,))
        con.commit()
    finally:
        con.close()


def kept() -> set[str]:
    import kb_privacy as P
    if not P._db_path().exists():
        return set()
    con = _keep_db()
    try:
        return {r[0] for r in con.execute("SELECT source_id FROM keep")}
    finally:
        con.close()


def cited(world_files: list[Path] | None = None) -> set[str]:
    """Every KB id a live (current) world claim cites, across the world model's files."""
    if world_files is None:
        root = Path(os.environ.get("WORLD_DIR", "/ai/datasets/connectors/world"))
        world_files = sorted(root.glob("*.db")) + ([Path(os.environ["WORLD_DB"])] if os.environ.get("WORLD_DB") else [])
    refs: set[str] = set()
    for f in world_files:
        if not Path(f).exists():
            continue
        try:
            con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
            refs |= {r[0] for r in con.execute("SELECT DISTINCT e.ref FROM evidence e JOIN claims c ON c.id = e.claim_id "
                                               "WHERE c.status = 'current'")}
            con.close()
        except sqlite3.Error:
            continue
    return refs


# ── ages ──────────────────────────────────────────────────────────────────────────────────────

def _ts(v: Any) -> float | None:
    import local_time
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v)
    if len(s) == 10 and s[4] == "-":                       # "2026-10-02": a saved date, household time
        s += "T12:00:00"
    t = local_time.parse_iso(s)
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=local_time.zone() or timezone.utc)
    return t.timestamp()


class Ledger:
    """What the connectors ledger knows: when a mail was first seen, when an event ends, a series' occurrences."""

    def __init__(self, path: Path | None = None):
        p = path or Path(os.environ.get("CONNECTORS_LEDGER", "/ai/datasets/connectors/ledger.db"))
        self.con = sqlite3.connect(f"file:{p}?mode=ro", uri=True) if Path(p).exists() else None
        import envelope_pass
        self._first = envelope_pass.first_seen_from_ledger(p)

    def first_seen(self, iid: str) -> float | None:
        return self._first(iid)

    def event_end(self, iid: str) -> float | None:
        if self.con is None:
            return None
        r = self.con.execute("SELECT fields FROM versions WHERE item_id=? AND fields IS NOT NULL ORDER BY at DESC LIMIT 1",
                             (iid,)).fetchone()
        f = json.loads(r[0]) if r and r[0] else {}
        return _ts(f.get("end")) or _ts(f.get("start"))

    def series_last(self, skey: str) -> float | None:
        if self.con is None or not skey:
            return None
        import connectors_runner as cx
        starts = cx._starts(self.con, "calendar", skey)
        return max(d.timestamp() for d in starts) if starts else None

    def series_gone(self, skey: str) -> bool:
        """Every occurrence of the series deleted or forgotten in the ledger (10-04: 23 series notes outlived their
        occurrences after the cleanup's tombstones; a forgotten event must not stay findable through its series)."""
        if self.con is None or not skey:
            return False
        rows = self.con.execute("SELECT COUNT(*), SUM(deleted_at IS NULL) FROM items WHERE source='calendar' AND "
                                "source_id LIKE ?", (skey.replace("%", "") + "@%",)).fetchone()
        return bool(rows and rows[0] and not rows[1])


def due(src: dict[str, Any], md: dict[str, Any], ledger: Ledger) -> tuple[str, str, float | None]:
    """(zone, rule, when it expires: None = never or unknown)."""
    import zones
    sid = str(src.get("source_id") or "")
    zone = zones.zone_of(md, sid, src.get("source_type"))
    if zone == "web":
        saved = _ts(md.get("saved"))
        return zone, "web", (saved + WEB_DAYS * DAY) if saved else None
    if zone != "sync":
        return zone, "never", None
    kind = str(md.get("source") or (sid.split("-")[1] if sid.startswith("cx-") else "")).lower()
    if kind in ("mail", "scans"):
        first = ledger.first_seen(sid) or _ts(md.get("observed_at")) or _ts(md.get("synced_at"))
        return zone, kind, (first + DAY + MAIL_DAYS * DAY) if first else None
    if kind == "calendar":
        if md.get("kind") == "series":
            if ledger.series_gone(str(md.get("series") or "")):
                return zone, "calendar series, every occurrence gone", 0.0
            last = ledger.series_last(str(md.get("series") or ""))
            return zone, "calendar series", (last + EVENT_DAYS * DAY) if last else None
        end = ledger.event_end(sid)
        return zone, "calendar event", (end + EVENT_DAYS * DAY) if end else None
    return zone, f"sync:{kind or 'other'}", None


# ── the run ───────────────────────────────────────────────────────────────────────────────────

def plan(sources: list[dict[str, Any]], meta: Callable[[str], dict[str, Any]], ledger: Ledger, *, now: float,
         pins: set[str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The items to expire now, and counts: per zone, per rule (due / not yet / kept / age unknown)."""
    out, counts = [], {}

    def bump(k: str) -> None:
        counts[k] = counts.get(k, 0) + 1
    for s in sources:
        md = meta(s["source_id"])
        zone, rule, when = due(s, md, ledger)
        bump(f"zone {zone}")
        if rule == "never":
            continue
        if when is None:
            bump(f"{rule}: age unknown, kept")
        elif when > now:
            bump(f"{rule}: not yet")
        elif s["source_id"] in pins:
            bump(f"{rule}: due, but kept (pinned or cited)")
        else:
            bump(f"{rule}: due")
            out.append({**s, "zone": zone, "rule": rule, "due": when, "metadata": md})
    counts["expiring"] = len(out)
    return out, counts


def expire(items: list[dict[str, Any]], call: Callable[..., Any], text: Callable[[str], str | None], *, now: float,
           ledger_path: Path | None = None) -> Path | None:
    """Back every item up (0600), then delete its KB copy and sharing row, and mark the ledger kb=0. → the backup."""
    if not items:
        return None
    import kb_privacy as P
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    bak = BACKUP_DIR / f"kb-lifecycle-{datetime.fromtimestamp(now):%Y%m%d-%H%M%S}.jsonl"
    fd = os.open(str(bak), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for it in items:
            f.write(json.dumps({"source_id": it["source_id"], "source_type": it.get("source_type"), "title": it.get("title"),
                                "text": text(it["source_id"]), "metadata": it["metadata"], "rule": it["rule"]},
                               default=str) + "\n")
    lp = ledger_path or Path(os.environ.get("CONNECTORS_LEDGER", "/ai/datasets/connectors/ledger.db"))
    lcon = sqlite3.connect(str(lp)) if lp.exists() else None
    try:
        for it in items:
            sid = it["source_id"]
            call("retrieval", f"/v1/retrieval/sources/{sid}", "DELETE", None)
            P.drop_acl(sid)
            if lcon is not None:
                lcon.execute("UPDATE items SET kb=0 WHERE item_id=?", (sid,))
        if lcon is not None:
            lcon.commit()
    finally:
        if lcon is not None:
            lcon.close()
    return bak


def run(apply: bool, *, call: Callable[..., Any] | None = None, now: float | None = None,
        world_files: list[Path] | None = None, ledger_path: Path | None = None) -> dict[str, Any]:
    import agent_runtime_lib as lib
    import kb_privacy as P
    call = call or lib._runtime_request
    now = time.time() if now is None else now
    sources = lib.kb_sources(call)
    items, counts = plan(sources, lambda sid: P.source_meta(call, sid), Ledger(ledger_path), now=now,
                         pins=kept() | cited(world_files))
    out: dict[str, Any] = {"ts": now, "mode": "apply" if apply else "dry-run", "sources": len(sources), "counts": counts}
    if apply:
        bak = expire(items, call, lambda sid: P.source_text(call, sid), now=now, ledger_path=ledger_path)
        out["expired"] = len(items)
        out["backup"] = str(bak) if bak else None
    import backup_retention
    # The real clock, never a run's `now`: a test's future clock must not shred real backups.
    out["backups_shredded" if apply else "backups_due"] = sum(len(backup_retention.prune(BACKUP_DIR, g, apply=apply))
                                                              for g in backup_retention.LIFECYCLE_DIR_PATTERNS)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Expire sync and web items past their zone's retention (#833)")
    ap.add_argument("cmd", nargs="?", choices=["keep", "unkeep"])
    ap.add_argument("id", nargs="?")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--nightly", action="store_true", help="apply only when KB_LIFECYCLE=apply")
    ap.add_argument("--no-log", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd:
        if not a.id:
            ap.error("an id is required")
        keep(a.id, by="cli", on=a.cmd == "keep")
        print(f"{a.cmd}: {a.id}")
        return 0
    apply = a.apply or (a.nightly and os.environ.get("KB_LIFECYCLE", "") == "apply")
    r = run(apply)
    print(json.dumps(r, indent=1, default=str))
    if not a.no_log:
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            with LOG.open("a") as f:
                f.write(json.dumps(r, default=str) + "\n")
        except OSError as exc:
            print(f"(couldn't append to {LOG}: {exc})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
