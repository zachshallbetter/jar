#!/usr/bin/env python3
# gate: hermetic
"""#1181: the bill lens keeps a viewer's threads until the link graph, the fences or the viewer change (a content
key, never a timer). Fictional viewers; the graph and the threads are stand-ins; a temporary fences store.
Run: python3 scripts/test_bill_threads_memo.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

_TMP = Path(tempfile.mkdtemp(prefix="titan-bill-memo-"))
for k, v in {"FENCES_DB": "fences.db", "HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json",
             "LINKS_DB": "links.db", "LINK_SALT_FILE": "salt", "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402
import fences  # noqa: E402
import linker  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.200} expected={expected!r:.200}"))


ITEMS = {"m1": NS(item_id="m1", owner="robin", audience="private"), "m2": NS(item_id="m2", owner="robin", audience="private")}
linker.scored = lambda con: (ITEMS, [])
linker._SCORED["key"] = ("graph", 1)
calls = []
linker.threads_for = lambda items, edges, visible: (calls.append(1), [NS(items=[i for i in items if visible(items[i])])])[1]
lib._said_of = lambda pid, role=None: []
import obligations  # noqa: E402
obligations.from_links = lambda recs, today, groups, adoptable, said, signals=(): sorted(groups)

r1 = lib._obligation_threads([], "2026-10-07", "robin", "adult")
r2 = lib._obligation_threads([], "2026-10-07", "robin", "adult")
check("the same viewer twice: the threads are worked out once", (r1, r2, len(calls)), (["m1", "m2"], ["m1", "m2"], 1))
check("another viewer is their own key (and sees only their own)", (lib._obligation_threads([], "2026-10-07", "jordan", "adult"),
                                                                     len(calls)), ([], 2))
linker._SCORED["key"] = ("graph", 2)
lib._obligation_threads([], "2026-10-07", "robin", "adult")
check("the link graph changes: worked out again", len(calls), 3)
k0 = lib._fences_key(fences.get())
fences.get().con.execute("INSERT INTO exceptions (item, fence_id, audience, by, at) VALUES ('m1', 1, 'household', 'robin', 5)")
fences.get().con.commit()
check("a fence exception changes the fences key", lib._fences_key(fences.get()) != k0, True)
lib._obligation_threads([], "2026-10-07", "robin", "adult")
check("…so the threads are worked out again", len(calls), 4)
check("one entry per viewer: a new graph replaces it, never piles up", len(lib._VIEWER_THREADS), 2)
for g_ in range(3, 9):
    linker._SCORED["key"] = ("graph", g_)
    lib._obligation_threads([], "2026-10-07", "robin", "adult")
check("…six more graph changes, still one entry for Robin", len(lib._VIEWER_THREADS), 2)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
