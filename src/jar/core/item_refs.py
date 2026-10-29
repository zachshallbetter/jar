#!/usr/bin/env python3
"""Mail and scans as references plus derived facts, not knowledge-base bodies (titan-node#446 step 2; decided 10-02,
recorded on #389). Behind ITEM_REFERENCES (a comma list of sources: "mail,scans"; unset or 0 = off).

With it on, an item a person's device syncs that titan hasn't seen before is kept as:
  - a reference (references.py: whose, which device, the device's id, a hash, a title, dates): never its body;
  - its derived records: the connector's fields node and timeline line (as today), the bill or receipt mail_bills
    files, the suggestions mail_suggestions makes, and the world model's observations (kept here, so they outlive
    the text, as an item envelope keeps them);
  - its redacted text, held in `pending` (0600) only until those passes have read it once, then deleted; at most
    PENDING_TTL_H (48) hours either way. Nothing else of the body stays on titan.
An item already in the knowledge base stays on its old path (an item envelope, #390) until the migration (#446
step 4): one item is either an envelope or a reference with derived facts, never both (docs/envelopes-and-scenarios.md).

  python3 scripts/item_refs.py dry-run [--days 7]    per source: what would become references and what derived facts
                                                     would be kept, from the recent ledger; reads only
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

DB = Path(os.environ.get("ITEM_REFS_DB", "/ai/datasets/connectors/item_refs.db"))
SOURCES = ("mail", "scans")
KIND = {"mail": "mail", "scans": "scan"}
PENDING_TTL_H = float(os.environ.get("ITEM_REFS_PENDING_TTL_H", "48"))
_lock = threading.Lock()


def enabled(source: str) -> bool:
    """ITEM_REFERENCES lists the sources kept as references ("mail,scans"); unset, empty or 0: none (off)."""
    on = {s.strip().lower() for s in os.environ.get("ITEM_REFERENCES", "").split(",") if s.strip()}
    return source in SOURCES and source in on


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("ITEM_REFS_DB") or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    # pending: the redacted text, only until the derivation passes have read it (done: the passes that have)
    con.execute("CREATE TABLE IF NOT EXISTS pending (item_id TEXT PRIMARY KEY, source TEXT NOT NULL, owner TEXT NOT NULL, "
                "ref_id TEXT NOT NULL, title TEXT, text TEXT NOT NULL, metadata TEXT NOT NULL, done TEXT NOT NULL DEFAULT '[]', "
                "created_at REAL NOT NULL)")
    # derived: what's kept for good, per item (the reference, and the world model's observations)
    con.execute("CREATE TABLE IF NOT EXISTS derived (item_id TEXT PRIMARY KEY, source TEXT NOT NULL, owner TEXT NOT NULL, "
                "ref_id TEXT NOT NULL, obs TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    return con


def keep(source: str, item_id: str, owner: str, role: str, device_id: str | None, native_id: str, digest: str,
         title: str, observed_at: Any, text: str, metadata: dict[str, Any], path: Path | None = None,
         refs_path: Path | None = None) -> str:
    """At ingest: the reference, and the text pending its derivations. → the reference id."""
    import references
    dates = {"received" if source == "mail" else "created": str(observed_at)} if observed_at else {}
    ref = references.put(owner, role, device_id or f"connector:{source}",
                         {"kind": KIND[source], "native_id": native_id, "content_hash": f"sha256:{digest}",
                          "title": title, "dates": dates}, path=refs_path)
    now = time.time()
    with _lock:
        con = _db(path)
        try:
            con.execute("INSERT INTO derived (item_id, source, owner, ref_id, created_at, updated_at) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(item_id) DO UPDATE SET ref_id=excluded.ref_id, updated_at=excluded.updated_at",
                        (item_id, source, owner, ref["id"], now, now))
            if text:
                con.execute("INSERT OR REPLACE INTO pending (item_id, source, owner, ref_id, title, text, metadata, done, "
                            "created_at) VALUES (?,?,?,?,?,?,?,'[]',?)",
                            (item_id, source, owner, ref["id"], title, text,
                             json.dumps({**metadata, "reference": ref["id"]}, default=str), now))
            con.commit()
        finally:
            con.close()
    return ref["id"]


def refs_of(item_ids: list[str], path: Path | None = None) -> dict[str, str]:
    """Each Mac-held item's device reference id (#1497: Open on the device that has the original). Ids only."""
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        return {}
    con = _db(path)
    try:
        out: dict[str, str] = {}
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            out.update({r[0]: r[1] for r in con.execute(
                f"SELECT item_id, ref_id FROM derived WHERE item_id IN ({','.join('?' * len(part))})", part)})
        return out
    finally:
        con.close()


def is_reference(item_id: str, path: Path | None = None) -> bool:
    con = _db(path)
    try:
        return con.execute("SELECT 1 FROM derived WHERE item_id=?", (item_id,)).fetchone() is not None
    finally:
        con.close()


def pending_sources(source: str, path: Path | None = None) -> list[dict[str, Any]]:
    """Pending items shaped like the knowledge base's source list (source_id, source_type, title, updated_at,
    metadata), so a derivation pass reads them beside the KB's."""
    con = _db(path)
    try:
        rows = con.execute("SELECT * FROM pending WHERE source=? ORDER BY created_at", (source,)).fetchall()
    finally:
        con.close()
    return [{"source_id": r["item_id"], "source_type": f"connector:{source}", "title": r["title"] or "",
             "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["created_at"])),
             "metadata": json.loads(r["metadata"] or "{}"), "pending": True} for r in rows]


def pending_text(item_id: str, path: Path | None = None) -> str | None:
    con = _db(path)
    try:
        r = con.execute("SELECT text FROM pending WHERE item_id=?", (item_id,)).fetchone()
        return r["text"] if r else None
    finally:
        con.close()


def text_of(fallback: Callable[[str], str], path: Path | None = None) -> Callable[[str], str]:
    """A pass's text reader: a pending item's text, else the knowledge base's."""
    def read(sid: str) -> str:
        t = pending_text(sid, path)
        return t if t is not None else fallback(sid)
    return read


def required(source: str) -> set[str]:
    """The passes that read a pending item's text before it goes."""
    if source == "mail":
        return {"bills", "world"} | ({"suggestions"} if os.environ.get("MAIL_SUGGESTIONS", "1") == "1" else set())
    if source == "scans" and os.environ.get("MAIL_BILLS_SCANS", "") == "1":
        return {"bills", "world"}   # #554: a scan's text waits for the bills pass too
    return {"world"}


def mark(pass_name: str, item_ids: list[str], path: Path | None = None) -> int:
    """A pass read these pending items; each one every pass has read goes (and anything past its TTL)."""
    with _lock:
        con = _db(path)
        try:
            for iid in item_ids:
                r = con.execute("SELECT done FROM pending WHERE item_id=?", (iid,)).fetchone()
                if r is not None:
                    done = sorted(set(json.loads(r["done"] or "[]")) | {pass_name})
                    con.execute("UPDATE pending SET done=? WHERE item_id=?", (json.dumps(done), iid))
            con.commit()
        finally:
            con.close()
    return sweep(path)


def sweep(path: Path | None = None, now: float | None = None) -> int:
    """Delete pending text every pass has read, or older than PENDING_TTL_H. → rows removed."""
    now = now or time.time()
    with _lock:
        con = _db(path)
        try:
            gone = [r["item_id"] for r in con.execute("SELECT item_id, source, done, created_at FROM pending")
                    if required(r["source"]) <= set(json.loads(r["done"] or "[]"))
                    or now - r["created_at"] > PENDING_TTL_H * 3600]
            for iid in gone:
                con.execute("DELETE FROM pending WHERE item_id=?", (iid,))
            con.commit()
            return len(gone)
        finally:
            con.close()


def keep_obs(item_id: str, obs: list[dict[str, Any]], path: Path | None = None) -> None:
    """The world model's observations from a pending item's text, kept with its reference."""
    with _lock:
        con = _db(path)
        try:
            con.execute("UPDATE derived SET obs=?, updated_at=? WHERE item_id=?", (json.dumps(obs), time.time(), item_id))
            con.commit()
        finally:
            con.close()


def carried(source: str, path: Path | None = None) -> list[tuple[str, list[dict[str, Any]]]]:
    """(item id, kept observations) of the references from one source whose text is gone."""
    con = _db(path)
    try:
        return [(r["item_id"], json.loads(r["obs"] or "[]")) for r in con.execute(
            "SELECT item_id, obs FROM derived WHERE source=? AND item_id NOT IN (SELECT item_id FROM pending)", (source,))]
    finally:
        con.close()


def forget(item_id: str, path: Path | None = None) -> None:
    """The device deleted the item (or the person forgot the source): its pending text and derived row go."""
    with _lock:
        con = _db(path)
        try:
            con.execute("DELETE FROM pending WHERE item_id=?", (item_id,))
            con.execute("DELETE FROM derived WHERE item_id=?", (item_id,))
            con.commit()
        finally:
            con.close()


# ── the dry run ─────────────────────────────────────────────────────────────────────────────────

def dry_run(days: float = 7, ledger: Path | None = None, kb_text: Callable[[str], str] | None = None,
            bills_seen: Callable[[], dict[str, str]] | None = None) -> dict[str, Any]:
    """What the recent mail and scans would have become with the flag on: per source, how many items, how many
    references, and which derived facts would be kept (fields, timeline, a filed bill), and how many characters of
    body text the knowledge base would no longer hold. Reads the ledger and the KB only."""
    import connectors_runner as cx
    since = time.time() - days * 86400
    con = sqlite3.connect(f"file:{ledger or cx.LEDGER_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    seen = bills_seen() if bills_seen else {}
    out: dict[str, Any] = {}
    try:
        for source in SOURCES:
            rows = con.execute("SELECT item_id, kb, node, event_id, owner FROM items WHERE source=? AND deleted_at IS NULL "
                               "AND synced_at >= ?", (source, since)).fetchall()
            chars = 0
            if kb_text:
                for r in rows:
                    if r["kb"]:
                        try:
                            chars += len(kb_text(r["item_id"]) or "")
                        except Exception:  # noqa: BLE001
                            pass
            out[source] = {"items": len(rows), "references": len(rows),
                           "kept": {"fields node": sum(1 for r in rows if r["node"]),
                                    "timeline line": sum(1 for r in rows if r["event_id"]),
                                    **({"bill or receipt": sum(1 for r in rows if r["item_id"] in seen)}
                                       if source == "mail" else {}),
                                    "world observations": "kept with the reference (from the text, once)"},
                           "kb bodies no longer kept": sum(1 for r in rows if r["kb"]),
                           "kb characters no longer kept": chars if kb_text else None,
                           "owners": sorted({str(r["owner"] or "") for r in rows})}
    finally:
        con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dry-run")
    d.add_argument("--days", type=float, default=7)
    a = ap.parse_args()
    if a.cmd == "dry-run":
        import kb_hygiene
        import mail_bills
        def seen() -> dict[str, str]:
            try:
                c = sqlite3.connect(f"file:{mail_bills.SEEN_DB}?mode=ro", uri=True)
                return {r[0]: r[1] for r in c.execute("SELECT mail_id, node_id FROM seen WHERE node_id IS NOT NULL")}
            except sqlite3.Error:
                return {}
        rep = dry_run(a.days, kb_text=kb_hygiene.source_text, bills_seen=seen)
        print(json.dumps({"flag": os.environ.get("ITEM_REFERENCES", "") or "off", "days": a.days, **rep}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
