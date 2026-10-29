#!/usr/bin/env python3
"""Household knowledge-base hygiene: find duplicate and conflicting notes, back up, then delete.

The KB (retrieval runtime) collected duplicates with conflicting facts — e.g. several "Vet"
notes with different phone numbers — because saves used to create a new random-id note each
time. kb_retrieve then returns whichever ranks first, which is how the agent answered with a
stale number. This finds those, proposes deletions, and only deletes from a plan you approved.

  scan     read-only: writes a plan (JSON) + prints a report. Nothing is deleted.
  apply    backs up every source in the plan to /ai/backups, then deletes them.
  restore  re-indexes sources from a backup file (undo).
  snapshot / rollback   take a full copy of the KB, and later put it back exactly: delete sources
           created since, re-index sources changed since. The household eval wraps its run in these
           so its fake facts never linger — and never overwrite a real note with the same title.
           With --eval-run ID (#346) only what that run's turns wrote (stamped eval_run=ID) is
           deleted or restored; a note or record someone really edits during the run is left alone.

Who decides what:
  code     exact duplicates (same normalised text) and which note is newest
  Jev      for same-subject pairs whose text differs: is this the same fact (duplicate) or a
           conflicting value for the same fact? Every number, code and password-like token is
           replaced by a placeholder first (V1, V2 — the same value gets the same placeholder in
           both notes), so Jev can see *whether* values differ without seeing them.
  you      approve the plan before `apply`.
Keep rule: the newest note wins (it is the latest thing the household said).

  python3 scripts/kb_hygiene.py scan --plan /tmp/kb-plan.json
  python3 scripts/kb_hygiene.py apply --plan /tmp/kb-plan.json --yes
  python3 scripts/kb_hygiene.py restore --backup /ai/backups/kb-hygiene-<ts>.jsonl
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402

BACKUP_DIR = Path(os.environ.get("KB_HYGIENE_BACKUP_DIR", "/ai/backups"))
SURE = 0.75
QUESTIONS = {
    "same_fact": {"type": "noul", "instructions":
                  "`b` states the same fact as `a` about the same thing, possibly in different words, and every "
                  "placeholder value (V1, V2, …) that the fact depends on is the same in both."},
    "conflict": {"type": "noul", "instructions":
                 "`a` and `b` are about the same thing and give a different value for the same fact (for example "
                 "a different placeholder for the same phone number, code, day or name)."},
}
_VALUE_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|\S*\d\S*")   # emails, and any token containing a digit


def norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip().lower()


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def mask_pair(a: str, b: str) -> tuple[str, str]:
    """Replace value-like tokens with placeholders shared across the pair (same value → same V#)."""
    table: dict[str, str] = {}

    def sub(m: re.Match) -> str:
        tok = m.group(0).rstrip(".,;:)")
        tail = m.group(0)[len(tok):]
        if tok not in table:
            table[tok] = f"V{len(table) + 1}"
        return table[tok] + tail
    return _VALUE_RX.sub(sub, a), _VALUE_RX.sub(sub, b)


def list_sources() -> list[dict]:
    return lib.kb_sources()   # paged: the KB is past one page (10-02 audit)


def source_run(sid: str) -> str | None:
    """The eval run a KB source was written by (its metadata's eval_run), or None."""
    r = lib._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}/chunks", "GET")
    chunks = sorted(r.get("chunks", []), key=lambda c: c.get("chunk_index", 0))
    md = (chunks[0].get("metadata") or {}) if chunks else {}
    return str(md.get("eval_run") or "") or None


def _record_run(n: dict) -> str | None:
    return str((n.get("data") or {}).get("eval_run") or "") or None


def source_text(sid: str) -> str:
    r = lib._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}/chunks", "GET")
    return "\n".join(c.get("chunk_text", "") for c in sorted(r.get("chunks", []), key=lambda c: c.get("chunk_index", 0)))


def upsert_by_title(title: str, text: str, source_type: str, metadata: dict, new_id: str,
                    existing: list[dict] | None = None, audience: str = "admins") -> str:
    """Write a note, reusing the id of an existing source with the same title so a re-run updates
    it in place (older notes carry random ids from the removed ingestion API). Returns the id.

    The kb_seed_* writers: titan's own notes, no owner, for the admin (kb_privacy.py). Only a source
    of the same type counts as a match, so a seed never takes over someone's own note."""
    sources = existing if existing is not None else list_sources()
    match = next((s for s in sources if s.get("title") == title
                  and s.get("source_type", source_type) == source_type), None)
    sid = match["source_id"] if match else new_id
    lib._kb_index(sid, title, text, source_type, metadata, owner=None, audience=audience, pin="keep")
    return sid


def scan(plan_path: Path, use_jev: bool) -> int:
    sources = list_sources()
    for s in sources:
        s["text"] = source_text(s["source_id"])
    deletions: dict[str, dict] = {}

    # 1. exact duplicates — code decides; keep the newest
    by_text: dict[str, list[dict]] = {}
    for s in sources:
        if s["text"].strip():
            by_text.setdefault(norm_text(s["text"]), []).append(s)
    for group in by_text.values():
        if len(group) > 1:
            group.sort(key=lambda s: s["updated_at"], reverse=True)
            for old in group[1:]:
                deletions[old["source_id"]] = {"source_id": old["source_id"], "title": old["title"],
                                               "reason": "exact duplicate", "keep": group[0]["source_id"],
                                               "decided_by": "code"}

    # 2. same title, different text — Jev judges duplicate vs conflict vs complementary (masked)
    by_title: dict[str, list[dict]] = {}
    for s in sources:
        if s["source_id"] not in deletions and norm_title(s["title"]):
            by_title.setdefault(norm_title(s["title"]), []).append(s)
    pairs = [(a, b) for g in by_title.values() if len(g) > 1
             for a, b in itertools.combinations(sorted(g, key=lambda s: s["updated_at"], reverse=True), 2)]
    judged, unsure = [], []
    jev = lib._jev_client() if use_jev else None
    for newer, older in pairs:
        if older["source_id"] in deletions:
            continue
        ma, mb = mask_pair(older["text"][:1500], newer["text"][:1500])
        verdict = {"pair": [older["source_id"], newer["source_id"]], "title": newer["title"]}
        if jev is None:
            unsure.append({**verdict, "why": "Jev not configured — review by hand"})
            continue
        r = jev.decide({"a": ma, "b": mb}, QUESTIONS, timeout=15, retries=2, source="kb-hygiene",
                       origin={"source": "kb-hygiene"})
        same, conf = r["answers"]["same_fact"]["noul"], r["answers"]["conflict"]["noul"]
        verdict.update(same_fact=round(same, 2), conflict=round(conf, 2))
        judged.append(verdict)
        if conf >= SURE or same >= SURE:
            deletions[older["source_id"]] = {"source_id": older["source_id"], "title": older["title"],
                                             "reason": ("conflicting value (older)" if conf >= same else "same fact reworded"),
                                             "keep": newer["source_id"], "decided_by": "jev",
                                             "sure": round(max(conf, same), 2)}
        elif max(same, 1 - same) < SURE or max(conf, 1 - conf) < SURE:
            unsure.append({**verdict, "why": "Jev unsure — review by hand"})

    for d in deletions.values():   # point "keep" at the note that actually survives
        seen = set()
        while d["keep"] in deletions and d["keep"] not in seen:
            seen.add(d["keep"])
            d["keep"] = deletions[d["keep"]]["keep"]
    plan = {"created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sources": len(sources),
            "delete": sorted(deletions.values(), key=lambda d: d["title"].lower()),
            "unsure": unsure, "judged_pairs": judged}
    plan_path.write_text(json.dumps(plan, indent=1))
    print(f"{len(sources)} sources · {len(plan['delete'])} proposed deletions · {len(unsure)} pair(s) to review by hand")
    for d in plan["delete"]:
        print(f"  DELETE {d['source_id']:40} {d['title'][:28]:28} {d['reason']} (keep {d['keep']}; {d['decided_by']}"
              + (f" {d['sure']}" if "sure" in d else "") + ")")
    for u in unsure:
        print(f"  REVIEW {' vs '.join(u['pair'])} {u['title'][:28]} — {u['why']}"
              + (f" (same {u['same_fact']}, conflict {u['conflict']})" if "conflict" in u else ""))
    print(f"plan: {plan_path}  — nothing deleted. Apply with: kb_hygiene.py apply --plan {plan_path} --yes")
    return 0


def apply(plan_path: Path, yes: bool, only: set[str] | None) -> int:
    plan = json.loads(plan_path.read_text())
    todo = [d for d in plan["delete"] if not only or d["source_id"] in only]
    if not yes:
        print(f"would back up and delete {len(todo)} source(s); re-run with --yes")
        return 2
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    bk = BACKUP_DIR / f"kb-hygiene-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    current = {s["source_id"]: s for s in list_sources()}
    with open(bk, "w") as fh:
        for d in todo:
            s = current.get(d["source_id"])
            if s:
                fh.write(json.dumps({**s, "text": source_text(d["source_id"]), "plan_reason": d["reason"]}) + "\n")
    os.chmod(bk, 0o640)
    deleted = 0
    for d in todo:
        if d["source_id"] not in current:
            continue
        lib._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(d['source_id'], safe='')}", "DELETE")
        deleted += 1
    print(f"backed up {deleted} source(s) to {bk}; deleted {deleted}. Undo: kb_hygiene.py restore --backup {bk}")
    return 0


def restore(backup: Path) -> int:
    n = 0
    for line in backup.read_text().splitlines():
        s = json.loads(line)
        lib._kb_index(s["source_id"], s.get("title", ""), s["text"], s.get("source_type", "note"),
                      {"restored_from": backup.name})
        n += 1
    print(f"restored {n} source(s) from {backup}")
    return 0


# The node types a chat turn writes (contacts, hubs, documents, appointments). record-runtime lists at
# most 500 nodes in node_id order with no paging, and the Mac's sync alone has 3,000+ (page_visit),
# so one unfiltered list sees an arbitrary slice. Each of these types, listed on its own, is small.
REC_TYPES = ("contact", "entity", "role", "bill", "invoice", "receipt", "statement", "appointment")
_REC_PAGE = 500


def list_records() -> list[dict]:
    """The record nodes of the types a chat turn writes, each type read in full (paged, #345). A type titan couldn't
    read in full (a runtime without paging) is marked `_partial`, and rollback never acts on what it can't see."""
    out: list[dict] = []
    for t in REC_TYPES:
        nodes, complete = lib.record_nodes(t, page=_REC_PAGE)
        if not complete:
            print(f"records: {t} couldn't be read in full; left untouched", file=sys.stderr)
            nodes = [{**n, "_partial": True} for n in nodes]
        out += nodes
    return out


def _chat_made(n: dict) -> bool:
    """Written by a chat turn (the eval's are), or legacy with no provenance. Nodes the sync made
    (provenance.source = a connector) are real and never rolled back."""
    src = lib._node_prov(n).get("source")
    return not n.get("_partial") and (src in (None, "", "chat"))


def snapshot(out: Path) -> int:
    if out.exists():
        # A run that was killed (SIGKILL, OOM) or left something behind kept its snapshot. Rolling it
        # back now could delete real notes written since, so a person decides.
        print(f"REFUSED: an earlier eval's snapshot is still at {out}. Check what it left, then "
              f"`kb_hygiene.py rollback --snapshot {out}` and `verify --snapshot {out} --remove`, or delete it.")
        return 1
    srcs = list_sources()
    recs = list_records()
    snap = {"taken": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sources": [{**s, "text": source_text(s["source_id"])} for s in srcs],
            "records": recs}
    out.write_text(json.dumps(snap))
    os.chmod(out, 0o600)
    print(f"snapshot of {len(srcs)} sources and {len(recs)} records → {out}")
    return 0


def _node_exists(nid: str) -> bool:
    try:
        lib._runtime_request("record", f"/v1/nodes/{urllib.parse.quote(nid, safe='')}", "GET")
        return True
    except Exception as exc:  # noqa: BLE001
        if "404" in str(exc) or getattr(exc, "code", None) == 404:
            return False
        raise


def rollback_records(snap: dict, run: str | None = None) -> str:
    """Undo record changes since the snapshot: delete new nodes (their edges go with them),
    merge the old values back into changed ones, re-create removed ones. With a run, only nodes that
    run wrote (data.eval_run) count as created or changed."""
    if "records" not in snap:            # snapshot from before records were covered
        return "records not in snapshot"
    before = {n["node_id"]: n for n in snap["records"]}
    now = {n["node_id"]: n for n in list_records()}
    mine = (lambda n: _record_run(n) == run) if run else (lambda n: True)   # noqa: E731
    created = [nid for nid in now if nid not in before and _chat_made(now[nid]) and mine(now[nid])]
    changed = [nid for nid in now if nid in before and _chat_made(before[nid])
               and now[nid].get("data") != before[nid].get("data") and mine(now[nid])]
    # Absent from a list isn't proof it's gone: ask for the node itself before re-creating it.
    removed = [nid for nid in before if nid not in now and _chat_made(before[nid]) and not _node_exists(nid)]
    for nid in created:
        lib._runtime_request("record", f"/v1/nodes/{urllib.parse.quote(nid, safe='')}", "DELETE")
    for nid in changed:
        # PATCH merges, so put the old values back and null any field added since (record-runtime
        # has no replace; a null is better than leaving a fixture value in a real record).
        old_data, new_data = before[nid].get("data") or {}, now[nid].get("data") or {}
        restore = {**{k: None for k in new_data if k not in old_data}, **old_data}
        lib._runtime_request("record", f"/v1/nodes/{urllib.parse.quote(nid, safe='')}", "PATCH", {"data": restore})
    for nid in removed:
        n = before[nid]
        lib._runtime_request("record", "/v1/nodes", "POST",
                             {"node_id": nid, "node_type": n["node_type"], "hash": n.get("hash"), "data": n.get("data") or {}})
    return f"records: deleted {len(created)} new, restored {len(changed)} changed and {len(removed)} removed"


def _synced(s: dict) -> bool:
    """Mail, calendar, downloads… arriving from the Mac's sync during an eval are real, not the eval's."""
    return str(s.get("source_type") or "").startswith("connector:")


def rollback(snap_path: Path, run: str | None = None) -> int:
    snap = {s["source_id"]: s for s in json.loads(snap_path.read_text())["sources"]}
    now = {s["source_id"]: s for s in list_sources()}
    created = [sid for sid in now if sid not in snap and not _synced(now[sid])]
    changed = [sid for sid in now if sid in snap and not _synced(snap[sid])
               and now[sid]["updated_at"] != snap[sid]["updated_at"]]
    if run:   # #346: only what this run wrote; a real note written or edited meanwhile stays
        kept = [sid for sid in created + changed if source_run(sid) != run]
        created = [sid for sid in created if sid not in kept]
        changed = [sid for sid in changed if sid not in kept]
        if kept:
            print(f"rollback: kept {len(kept)} source(s) written meanwhile by someone else", file=sys.stderr)
    removed = [sid for sid in snap if sid not in now and not _synced(snap[sid])]
    for sid in created:
        lib._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}", "DELETE")
    for sid in changed + removed:
        s = snap[sid]
        lib._kb_index(sid, s.get("title", ""), s["text"], s.get("source_type", "note"), {"restored_from": "snapshot"})
    print(f"rollback: deleted {len(created)} new, restored {len(changed)} changed and {len(removed)} removed source(s); "
          + rollback_records(json.loads(snap_path.read_text()), run))
    return 0


def verify(snap_path: Path, remove: bool = False, run: str | None = None) -> int:
    """After a rollback: every source must have been in the snapshot (with a run: every one that run
    wrote is gone). Prints "OK" or "LEFT <ids>" and removes the snapshot only when clean (a kept
    snapshot lets a person roll back by hand).

    With a run, a new chat-made item that isn't stamped is reported as UNSTAMPED and fails too, but is
    never deleted: it may be a real note written during the run, or an eval write that bypassed the
    stamp (the 10-01 leak again). A person looks."""
    taken = json.loads(snap_path.read_text())
    snap = {s["source_id"] for s in taken["sources"]}
    new: list[tuple[str, str | None]] = [(s["source_id"], source_run(s["source_id"]) if run else None)
                                         for s in list_sources() if s["source_id"] not in snap and not _synced(s)]
    if "records" in taken:
        before = {n["node_id"] for n in taken["records"]}
        new += [(n["node_id"], _record_run(n)) for n in list_records() if n["node_id"] not in before and _chat_made(n)]
    left = [i for i, r in new if not run or r == run]
    unstamped = [i for i, r in new if run and not r]
    if left:
        print(f"LEFT {len(left)} source(s) the eval created: {', '.join(left[:20])} (snapshot kept: {snap_path})")
    if unstamped:
        print(f"UNSTAMPED {len(unstamped)} new item(s) without an eval run, not removed (a real note, or an eval "
              f"write that missed the stamp): {', '.join(unstamped[:20])} (snapshot kept: {snap_path})")
    if left or unstamped:
        return 1
    if remove:
        snap_path.unlink(missing_ok=True)
    print("OK: nothing the eval wrote remains")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="household KB duplicate/conflict cleanup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan"); p.add_argument("--plan", required=True); p.add_argument("--no-jev", action="store_true")
    p = sub.add_parser("apply"); p.add_argument("--plan", required=True); p.add_argument("--yes", action="store_true")
    p.add_argument("--only", help="comma-separated source ids from the plan")
    p = sub.add_parser("restore"); p.add_argument("--backup", required=True)
    p = sub.add_parser("snapshot"); p.add_argument("--out", required=True)
    p = sub.add_parser("rollback"); p.add_argument("--snapshot", required=True)
    p.add_argument("--remove", action="store_true", help="delete the snapshot file after a successful rollback")
    p.add_argument("--eval-run", help="only what this eval run wrote (#346)")
    p = sub.add_parser("verify"); p.add_argument("--snapshot", required=True); p.add_argument("--remove", action="store_true")
    p.add_argument("--eval-run", help="only what this eval run wrote (#346)")
    a = ap.parse_args()
    if a.cmd == "scan":
        return scan(Path(a.plan), not a.no_jev)
    if a.cmd == "apply":
        return apply(Path(a.plan), a.yes, set(a.only.split(",")) if a.only else None)
    if a.cmd == "snapshot":
        return snapshot(Path(a.out))
    if a.cmd == "verify":
        return verify(Path(a.snapshot), a.remove, a.eval_run)
    if a.cmd == "rollback":
        rc = rollback(Path(a.snapshot), a.eval_run)
        if rc == 0 and a.remove:
            Path(a.snapshot).unlink(missing_ok=True)
        return rc
    return restore(Path(a.backup))


if __name__ == "__main__":
    raise SystemExit(main())
