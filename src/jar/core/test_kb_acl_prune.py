#!/usr/bin/env python3
# gate: hermetic
"""kb_acl_prune (#1274): rows for items no longer in the KB go; rows for live items, and rows written in the last
hour, stay; a dry run changes nothing; --apply backs the table up (0600) first; an empty or failed source list
refuses; counts only (never ids). The sharing table is a temp file; the KB's source list is a fake.
Run: python3 scripts/test_kb_acl_prune.py"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-aclprune-"))
os.environ["KB_ACL_DB"] = str(_TMP / "kb_acl.db")
sys.path.insert(0, str(Path(__file__).parent))
import kb_acl_prune as K  # noqa: E402
import kb_privacy as P  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def run(argv, sources):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        K.main(argv, sources_fn=lambda: sources)
    return json.loads(buf.getvalue())


P.set_acl("cx-mail-live", "zach", "private")
P.set_acl("cx-mail-gone", "zach", "private")
P.set_acl("cx-calendar-gone", "zach", "household")          # a stale row broader than private
P.set_acl("note-gone-x", "robin", "private")
P.set_acl("cx-mail-new", "zach", "private")                 # written just now: an item being indexed
con = P._connect(True)
con.execute("UPDATE acl SET set_at=? WHERE source_id<>'cx-mail-new'", (time.time() - 7200,))
con.commit()
con.close()
live = [{"source_id": "cx-mail-live"}]

print("── dry run ──")
d = run([], live)
check("counts: 5 rows, 1 in the KB, 3 stale, 1 recent skipped",
      (d["mode"], d["rows"], d["rows_in_kb"], d["stale"], d["skipped_recent"]), ("dry-run", 5, 1, 3, 1))
check("…by kind, and how many are broader than private", (d["stale_by_kind"], d["stale_broader_than_private"]),
      ({"cx-mail": 1, "cx-calendar": 1, "note": 1}, 1))
check("…counts only: no ids in the report", any("gone" in json.dumps(v) for v in d.values()), False)
check("…and nothing changed", len(P.acl_rows()), 5)

print("── refuses on an empty or failed source list ──")
for label, fn in (("empty", lambda: []), ("failed", lambda: (_ for _ in ()).throw(RuntimeError("down")))):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            K.main([], sources_fn=fn)
        check(f"{label}: refused", "ran", "refused")
    except SystemExit as e:
        check(f"{label}: refused, nothing changed", ("refusing" in str(e), len(P.acl_rows())), (True, 5))

print("── apply ──")
a = run(["--apply"], live)
check("the 3 stale rows go; the live one and the recent one stay", (a["deleted"], sorted(P.acl_rows())),
      (3, ["cx-mail-live", "cx-mail-new"]))
b = _TMP / a["backup"]
check("backed up first (0600), with all 5 rows", (oct(b.stat().st_mode & 0o777), b.exists()), ("0o600", True))
check("a second dry run finds nothing", run([], live)["stale"], 0)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
