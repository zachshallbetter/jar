#!/usr/bin/env python3
"""The mail cut-over's one destructive step (titan-node#955 step 6, gap 4; the checklist is on #955): once a connected
account's server mail is in the knowledge base, the Mac's copies of the same mails (connector:mail, cx-mail-*) go, so
the KB holds each mail once.

Nothing is retired automatically. It runs only at cut-over, one account at a time, after Zach's explicit OK
(Coordination asks him):
  dry run (default)   counts only: the Mac's KB mails for the account, how many have a server twin in the KB
                      (matched by Message-ID), how many don't (those stay), the bill records that point at them
  --apply             also needs MAIL_SERVER_RETIRE=1 (a second key). First a 0600 backup of every retired item
                      (its title, text and metadata) in the backups folder as kb-cleanup-mail-<stamp>.jsonl, shredded
                      at 30 days (backup_retention); then each twin-backed Mac item leaves the KB, its ledger row is
                      kept with no hash (so turning the Mac's sync back on writes it again), and bill records filed
                      from it point at the server item instead (the old id kept as provenance.was).
Only Mac mails WITH a server twin in the KB are retired; nothing else is touched.

  python3 scripts/mail_cutover.py retire --owner zach --account icloud [--apply]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))


class CutoverError(RuntimeError):
    pass


def _norm(mid: Any) -> str:
    return str(mid or "").strip().strip("<>").strip().lower()


def plan(owner: str, account: str, *, ledger: Path | None = None, store: Path | None = None,
         directory: Path | None = None, role_of: Callable | None = None) -> dict[str, Any]:
    """Which of the Mac's KB mails for this account have a server twin in the KB. Read-only."""
    import connectors_runner as R
    import mail_server as M
    acct = next((a for a in M.accounts(directory, role_of or M._role_of) if a["name"] == account and a["owner"] == owner),
                None)
    if not acct or not acct.get("mac_account"):
        raise CutoverError("that account isn't connected here with a mac_account")
    mac = " ".join(acct["mac_account"].split()).lower()
    L = sqlite3.connect(f"file:{ledger or R.LEDGER_PATH}?mode=ro", uri=True)
    try:
        macs = [(iid, up) for iid, up, a in L.execute(
            "SELECT item_id, upstream_id, account FROM items WHERE source='mail' AND owner=? AND kb=1 AND deleted_at IS NULL",
            (owner,)) if " ".join(str(a or "").split()).lower() == mac]
    finally:
        L.close()
    S = sqlite3.connect(f"file:{store or M.DB}?mode=ro", uri=True)
    try:
        twins = {_norm(mid): iid for mid, iid in S.execute(
            "SELECT message_id, item_id FROM messages WHERE owner=? AND account=? AND kb=1", (owner, account))}
    finally:
        S.close()
    pairs = [(iid, twins[_norm(up)]) for iid, up in macs if _norm(up) in twins]
    return {"owner": owner, "account": account, "mac_in_kb": len(macs), "with_twin": len(pairs),
            "without_twin": len(macs) - len(pairs), "pairs": pairs}


def retire(owner: str, account: str, *, apply: bool = False, ledger: Path | None = None, store: Path | None = None,
           directory: Path | None = None, role_of: Callable | None = None, backup_dir: Path | None = None,
           kb_source: Callable[[str], dict[str, Any] | None] | None = None, kb_delete: Callable[[str], Any] | None = None,
           records: Callable[[str], list[dict]] | None = None, patch: Callable[[str, dict], Any] | None = None,
           now: float | None = None) -> dict[str, Any]:
    """The dry run, or (apply, with MAIL_SERVER_RETIRE=1) the retirement. → counts only (and the backup's name)."""
    import connectors_runner as R
    import mail_bills
    p = plan(owner, account, ledger=ledger, store=store, directory=directory, role_of=role_of)
    pairs = dict(p.pop("pairs"))
    if records is None:
        import agent_runtime_lib as lib
        records = lambda t: lib.record_nodes(t)[0]   # noqa: E731
    pointing = [(n, t) for t in mail_bills.TYPES for n in records(t)
                if ((n.get("data") or {}).get("provenance") or {}).get("source_id") in pairs]
    out = {**p, "records_to_repoint": len(pointing), "apply": bool(apply)}
    if not apply:
        return out
    if os.environ.get("MAIL_SERVER_RETIRE", "0") != "1":
        raise CutoverError("retiring needs MAIL_SERVER_RETIRE=1 as well (Zach's OK, at cut-over only)")
    if kb_source is None or kb_delete is None or patch is None:
        import agent_runtime_lib as lib
        import kb_hygiene
        import urllib.parse
        meta = {s["source_id"]: s for s in kb_hygiene.list_sources()}
        kb_source = kb_source or (lambda sid: {**meta[sid], "text": kb_hygiene.source_text(sid)} if sid in meta else None)
        kb_delete = kb_delete or (lambda sid: lib._runtime_request(
            "retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}", "DELETE"))
        patch = patch or (lambda nid, data: lib._runtime_request(
            "record", f"/v1/nodes/{urllib.parse.quote(nid, safe='')}", "PATCH", {"data": data}))
    import kb_lifecycle
    folder = Path(backup_dir or kb_lifecycle.BACKUP_DIR)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = folder / f"kb-cleanup-mail-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now or time.time()))}.jsonl"
    fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:     # the backup is whole before anything leaves the KB
        for mac_id in pairs:
            s = kb_source(mac_id)
            if s:
                fh.write(json.dumps({"source_id": mac_id, "title": s.get("title"), "text": s.get("text"),
                                     "source_type": s.get("source_type"), "metadata": s.get("metadata")}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    retired = 0
    L = sqlite3.connect(str(ledger or R.LEDGER_PATH))
    try:
        for mac_id in pairs:
            kb_delete(mac_id)
            # kept, with no hash: if the Mac's sync for this account comes back, its next send writes the mail again
            L.execute("UPDATE items SET kb=0, content_hash='' WHERE item_id=?", (mac_id,))
            retired += 1
        L.commit()
    finally:
        L.close()
    repointed = 0
    for n, _t in pointing:
        data = dict(n.get("data") or {})
        prov = dict(data.get("provenance") or {})
        twin = pairs[prov["source_id"]]
        data["provenance"] = {**prov, "source_id": twin, "item_id": twin, "was": prov["source_id"]}
        patch(n["node_id"], data)
        repointed += 1
    return {**out, "retired": retired, "repointed": repointed, "backup": dest.name}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("retire")
    r.add_argument("--owner", required=True)
    r.add_argument("--account", required=True)
    r.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    try:
        print(json.dumps(retire(a.owner, a.account, apply=a.apply)))
    except CutoverError as exc:
        print(f"mail_cutover: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
