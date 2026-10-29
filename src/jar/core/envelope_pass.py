#!/usr/bin/env python3
"""Synced mail and scans become item envelopes in the knowledge base (titan-node#390; Zach, 10-02: "Yes, for new
items"). The 200-item eval: retrieval level with full text, at −56% of the text; private wording, codes and account
numbers no longer kept on titan.

A pass, not ingest: an item is turned into its envelope once it's MIN_AGE_H old, after the jobs that read a mail's
own words have run (mail_bills and mail_suggestions every 30 minutes; the world model each night). For each item:
  - envelopes.build: the summary (titan's own model), the original's digest, its fences, its provenance;
  - the world model's observations, read from the ORIGINAL text, kept on the envelope, so tomorrow's sync still has
    a business's phone number that only the full mail held (world.carried_obs);
  - its fences recorded with the fences store (fences.carry), so a summary that dropped the matched word is still
    held back from people the fence keeps out;
  - the KB item re-indexed in place with the envelope's text, its metadata unchanged plus `envelope`;
  - the original text appended to a backup (mode 600) first, so `--restore` can undo a run. Backups are kept 30 days,
    then shredded (Zach, 10-04, #905): every applying pass prunes them.

Runs with ITEM_ENVELOPES=1 only (titan-mail-bills.service's last step); off, it does nothing. New items: synced after
ENVELOPE_SINCE (the epoch envelopes were turned on; unset, none) and within MAX_AGE_H. Existing items (the migration)
only with --migrate: dry run by default, --apply to write.

  python3 scripts/envelope_pass.py                     dry run: what it would do, counts only
  python3 scripts/envelope_pass.py --apply [--limit 40]
  python3 scripts/envelope_pass.py --migrate [--apply]  every older item too
  python3 scripts/envelope_pass.py --restore BACKUP     put the backed-up originals back
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import envelopes  # noqa: E402

TYPES = ("connector:mail", "connector:scans")
MIN_AGE_H = float(os.environ.get("ENVELOPE_MIN_AGE_H", "24"))
MAX_AGE_H = float(os.environ.get("ENVELOPE_MAX_AGE_H", str(7 * 24)))


def since() -> float | None:
    """ENVELOPE_SINCE: when envelopes were turned on (epoch seconds). Items synced before it are the existing copies,
    which only --migrate turns (Zach, 10-02: new items now; existing ones after a dry run and a backup). Unset: none
    are new (10-02: every mail was under a week old, so "new" by age alone took all 325)."""
    try:
        v = float(os.environ.get("ENVELOPE_SINCE", "") or "nan")
    except ValueError:
        return None
    return None if v != v else v
BACKUP_DIR = Path(os.environ.get("ENVELOPE_BACKUP_DIR", "/ai/backups/envelopes"))
BACKUP_GLOB = "envelopes-*.jsonl"   # kept 30 days, then shredded (#905; backup_retention)


def _age_h(s: dict[str, Any], md: dict[str, Any], now: float) -> float | None:
    """Hours since the item was synced (its provenance's synced_at, else the KB's updated_at)."""
    import local_time
    for v in (md.get("synced_at"), s.get("updated_at")):
        try:
            t = local_time.parse_iso(v).timestamp()   # the KB's updated_at has nanoseconds (Python 3.10 on titan)
            return (now - t) / 3600
        except (AttributeError, TypeError, ValueError):
            continue
    return None


def first_seen_from_ledger(ledger: Path | None = None) -> Callable[[str], float | None]:
    """item id → when titan first had it (10-03 live check): the earlier of its first ledger version and the item's own
    date (observed_at: a mail's received time), since synced_at and the KB's updated_at restart on every re-sync, and
    old versions are pruned. None for an item the ledger doesn't know."""
    import sqlite3
    path = ledger or Path(os.environ.get("CONNECTORS_LEDGER", "/ai/datasets/connectors/ledger.db"))
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        first = {r[0]: r[1] for r in con.execute("SELECT item_id, MIN(at) FROM versions GROUP BY item_id")}
        observed = {r[0]: r[1] for r in con.execute("SELECT item_id, observed_at FROM items")}
        con.close()
    except sqlite3.Error:
        return lambda sid: None
    import local_time

    def get(sid: str) -> float | None:
        ts = [t for t in (first.get(sid),) if isinstance(t, (int, float))]
        try:
            ts.append(local_time.parse_iso(observed.get(sid)).timestamp())
        except (AttributeError, TypeError, ValueError):
            pass
        return min(ts) if ts else None
    return get


def candidates(sources: list[dict[str, Any]], read: Callable[[str], tuple[str, dict]], now: float,
               migrate: bool = False, turned_on: float | None = None,
               first_seen: Callable[[str], float | None] | None = None) -> list[tuple[dict, str, dict]]:
    """(source, text, metadata) of each mail or scan that isn't an envelope yet and is old enough; unless migrating,
    only those titan first had after envelopes were turned on (`turned_on`, ENVELOPE_SINCE; none without it). Age is
    from when titan first had the item (first_seen, the connectors ledger), not its latest sync: a re-synced mail
    kept getting younger, and an existing copy re-synced after the turn-on looked new (10-03 live check)."""
    if not migrate and turned_on is None:
        return []
    out = []
    for s in sources:
        if s.get("source_type") not in TYPES:
            continue
        text, md = read(str(s.get("source_id") or ""))
        if not text or (md or {}).get("envelope") or (md or {}).get("eval_run"):
            continue
        fs = first_seen(str(s.get("source_id") or "")) if first_seen else None
        age = (now - fs) / 3600 if fs is not None else _age_h(s, md or {}, now)
        if age is None or age < MIN_AGE_H or (not migrate and age > MAX_AGE_H):
            continue
        if not migrate and turned_on is not None and now - age * 3600 < turned_on:
            continue   # synced before envelopes were on: an existing copy, for --migrate
        out.append((s, text, md or {}))
    return out


def envelope_one(s: dict[str, Any], text: str, md: dict[str, Any], *, write: Callable[..., Any],
                 observe: Callable[..., list] | None = None, fences: Any = None, url: str | None = None,
                 chat: Any = None) -> dict[str, Any]:
    """Build one item's envelope, carry its fences and observations, and re-index it. Returns the envelope."""
    import world
    sid = str(s.get("source_id") or "")
    title = str(s.get("title") or "")
    import hooks   # #951 (a): link hooks from the original, before the summary replaces it (#954's rule)
    hooks_n = hooks.index_original(sid, "scan" if "scan" in str(s.get("source_type") or "") else "mail", title, text, md)
    env = envelopes.build({"source_id": sid, "source_type": s.get("source_type"), "title": title, "text": text,
                           "metadata": md}, url=url, chat=chat, fences=fences)
    obs = []
    if observe is not None:
        try:
            obs = [world.obs_to_dict(o) for o in observe(sid, title, text, md)]
        except Exception as exc:  # noqa: BLE001 - no observations is a smaller envelope, not a failed one
            print(f"[envelopes] {sid}: observations not kept ({type(exc).__name__})", file=sys.stderr)
    if env["fences"]["touched"]:
        fs = fences
        if fs is None:
            import fences as _f
            fs = _f.get()
        fs.carry(sid, env["fences"]["touched"], env["fences"]["subject"])
    keep = {"digest": env["record"]["digest"], "fences": env["fences"], "interpretation": env["interpretation"],
            "obs": obs, "version": envelopes.VERSION, "hooks": hooks_n}
    write(sid, title, envelopes.index_text(env), str(s.get("source_type")), {**md, "envelope": keep})
    return {**env, "obs_n": len(obs)}


def _backup(path: Path, s: dict[str, Any], text: str, md: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps({"source_id": s.get("source_id"), "title": s.get("title"),
                             "source_type": s.get("source_type"), "text": text, "metadata": md}) + "\n")


def run(*, apply: bool, migrate: bool, limit: int, now: float | None = None, sources: list[dict] | None = None,
        read: Callable | None = None, write: Callable | None = None, observe: Callable | None = None,
        fences: Any = None, url: str | None = None, chat: Any = None, backup: Path | None = None,
        turned_on: float | None = None, first_seen: Callable[[str], float | None] | None = None) -> dict[str, Any]:
    now = now or time.time()
    if sources is None or read is None or write is None:
        import agent_runtime_lib as lib
        import kb_hygiene
        import kb_query
        sources = sources if sources is not None else kb_hygiene.list_sources()
        read = read or kb_query._chunks
        write = write or (lambda sid, title, text, st, md: lib._kb_index(sid, title, text, st, md))
    if observe is None:
        observe = _world_observer()
    if first_seen is None:
        first_seen = first_seen_from_ledger()
    todo = candidates(sources, read, now, migrate=migrate, turned_on=since() if turned_on is None else turned_on,
                      first_seen=first_seen)
    report = {"apply": apply, "migrate": migrate, "candidates": len(todo), "done": 0, "from_model": 0,
              "title_only": 0, "fenced": 0, "obs_kept": 0}
    if not apply:
        return report
    import backup_retention   # the real clock, never a pass's `now`; only the folder this pass writes to
    report["backups_shredded"] = len(backup_retention.prune(backup.parent if backup else BACKUP_DIR, BACKUP_GLOB))
    backup = backup or BACKUP_DIR / f"envelopes-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))}.jsonl"
    for s, text, md in todo[:limit]:
        _backup(backup, s, text, md)
        env = envelope_one(s, text, md, write=write, observe=observe, fences=fences, url=url, chat=chat)
        report["done"] += 1
        report["from_model" if env["interpretation"]["how"] == "model" else "title_only"] += 1
        report["fenced"] += bool(env["fences"]["touched"])
        report["obs_kept"] += env["obs_n"]
    report["backup"] = str(backup) if report["done"] else None
    return report


def _world_observer() -> Callable[[str, str, str, dict], list]:
    """What tonight's world sync would read from the original (mail: a business's phone and address; a scan: a
    note's facts), with the household's roles."""
    def observe(sid: str, title: str, text: str, md: dict) -> list:
        import kb_privacy
        import world
        import household_identity as hh
        roles = {str(p["id"]): str(p.get("role") or "") for p in hh.get_store().people()}
        owner, aud = kb_privacy.acl_of(md, sid)
        st = str(md.get("source_type") or "")
        row = (sid, title, text, {**md, "source_type": st}, owner, aud, str(md.get("synced_at") or ""))
        return world.note_observations(world.get(), [row], roles, mail=st == "connector:mail")
    return observe


def restore(path: Path, write: Callable | None = None) -> int:
    """Put a backup's originals back (their metadata without `envelope`)."""
    if write is None:
        import agent_runtime_lib as lib
        write = lambda sid, title, text, st, md: lib._kb_index(sid, title, text, st, md)  # noqa: E731
    n = 0
    for line in path.read_text().splitlines():
        r = json.loads(line)
        md = {k: v for k, v in (r.get("metadata") or {}).items() if k != "envelope"}
        write(r["source_id"], r.get("title") or "", r["text"], r.get("source_type") or "connector:mail", md)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--migrate", action="store_true", help="older items too (the existing copies)")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--restore", type=Path)
    a = ap.parse_args()
    if a.restore:
        print(f"restored {restore(a.restore)} item(s) from {a.restore}")
        return 0
    if a.apply and not envelopes.enabled():
        print("item envelopes are off (ITEM_ENVELOPES=1 turns them on); a dry run works without it")
        return 0
    print(json.dumps(run(apply=a.apply, migrate=a.migrate, limit=a.limit)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
