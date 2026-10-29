#!/usr/bin/env python3
# gate: hermetic
"""kb_ledger_reconcile (#1275): ledger rows saying kb=1 for items gone from the KB get kb=0; rows for live items,
rows deleted upstream, and rows synced in the last hour stay as they are; a dry run changes nothing; --apply backs the
ledger up (0600) first; an empty or failed source list refuses; counts only. Temp ledger, fake source list.
Run: python3 scripts/test_kb_ledger_reconcile.py"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-ledrec-"))
os.environ["CONNECTORS_LEDGER"] = str(_TMP / "ledger.db")
os.environ["KB_ACL_DB"] = str(_TMP / "kb_acl.db")
sys.path.insert(0, str(Path(__file__).parent))
import connectors_runner as C  # noqa: E402
import kb_ledger_reconcile as R  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


LED = _TMP / "ledger.db"
con = C._db(LED)
old = time.time() - 7200
for iid, src, kb, deleted, at in (("cx-mail-live", "mail", 1, None, old), ("cx-mail-gone", "mail", 1, None, old),
                                  ("cx-daily-gone", "daily", 1, None, old), ("cx-mail-deleted", "mail", 1, old, old),
                                  ("cx-mail-new", "mail", 1, None, time.time()), ("cx-mail-notkb", "mail", 0, None, old)):
    con.execute("INSERT INTO items (source, source_id, content_hash, item_id, kb, synced_at, deleted_at, owner, upstream_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (src, iid, "h", iid, kb, at, deleted, "zach", iid))
con.commit()
con.close()
live = [{"source_id": "cx-mail-live"}]


def run(argv, sources):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        R.main(argv, sources_fn=lambda: sources, ledger=LED)
    return json.loads(buf.getvalue())


def kb_of(i):
    c = sqlite3.connect(str(LED))
    try:
        return c.execute("SELECT kb FROM items WHERE item_id=?", (i,)).fetchone()[0]
    finally:
        c.close()


d = run([], live)
check("dry run: 2 drift (gone from the KB), 1 recent skipped; deleted-upstream and kb=0 rows aren't counted",
      (d["mode"], d["drift"], d["drift_by_source"], d["skipped_recent"]), ("dry-run", 2, {"mail": 1, "daily": 1}, 1))
check("…counts only, and nothing changed", ("gone" in json.dumps(d), kb_of("cx-mail-gone")), (False, 1))
for label, fn in (("empty", lambda: []), ("failed", lambda: (_ for _ in ()).throw(RuntimeError("down")))):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            R.main([], sources_fn=fn, ledger=LED)
        check(f"{label}: refused", "ran", "refused")
    except SystemExit as e:
        check(f"{label} source list: refused, nothing changed", ("refusing" in str(e), kb_of("cx-mail-gone")), (True, 1))
a = run(["--apply"], live)
check("apply: the 2 drift rows get kb=0; live, deleted-upstream and recent rows are untouched",
      (a["reconciled"], [kb_of(i) for i in ("cx-mail-gone", "cx-daily-gone", "cx-mail-live", "cx-mail-deleted", "cx-mail-new")]),
      (2, [0, 0, 1, 1, 1]))
b = _TMP / a["backup"]
check("backed up first (0600)", (b.exists(), oct(b.stat().st_mode & 0o777)), (True, "0o600"))
check("a second dry run finds none", run([], live)["drift"], 0)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
