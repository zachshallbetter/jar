#!/usr/bin/env python3
# gate: hermetic   (temp stores only)
"""`mail_server.py passes-status` (#955): the server passes' outcomes in counts only. Categories (all time and in the
window), envelopes derived, cards from server mail by state, duplicates (per mail, per event change), invite cards,
seen server mail, days over the card budget. Nothing it prints names a subject, title, sender or id; a missing store
reads as zeros and is never created. Fictional mail only.
Run: python3 scripts/test_mail_passes_status.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-passes-status-"))
os.environ["MAIL_SERVER_DB"] = str(_TMP / "mail_server.db")
os.environ["MAIL_SUGGESTIONS_DB"] = str(_TMP / "mail_suggestions.db")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mail_server as ms  # noqa: E402
import mail_suggestions as sug  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = 1_800_000_000.0
H = 3600.0

print("── no stores yet ──")
empty = ms.passes_status(24, db=_TMP / "none.db", sug_db=_TMP / "none2.db", now=NOW)
check("zeros everywhere", (empty["categorized"], empty["derived_in_window"], empty["cards_in_window"], empty["days_over_budget"]),
      ({}, 0, {}, 0))
check("…and nothing was created", ((_TMP / "none.db").exists(), (_TMP / "none2.db").exists()), (False, False))

print("── a day of mail ──")
con = ms._db(_TMP / "mail_server.db")
rows = [  # (uid, category, derived hours ago, why the passes ran or not)
    (1, "personal", 2, "ran"), (2, "action", 3, "ran"), (3, "bulk", 5, "ran"), (4, "bulk", 30, "ran"), (5, None, 1, "mac"),
    (6, "code", 4, "ran"), (7, None, None, None), (8, None, 6, None)]
for uid, cat, ago, why in rows:
    con.execute("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, thread_id, subject, "
                "sender, derived_at, category, passes) VALUES ('alex-icloud', 'INBOX', 1, ?, 'alex', ?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, f"cx-mailsrv-{uid:016x}", f"<m{uid}@example.invalid>", f"t{uid}", f"Robin's recital {uid}",
                 "robin@example.invalid", NOW - ago * H if ago is not None else None, cat, why))
con.commit(); con.close()
scon = sug._db(_TMP / "mail_suggestions.db")
cards = [  # (key, mail_id, owner, state, day, hours ago, event, target)
    ("mailsug-inv-aaaa", "cx-mailsrv-0000000000000001", "alex", "sent", "2027-01-15", 2, "ev1", "2027-01-20T10:00"),
    ("mailsug-inv-bbbb", "cx-mailsrv-0000000000000001", "alex", "held", None, 1, "ev1", "2027-01-20T10:00"),   # a dup
    ("mailsug-x1", "cx-mailsrv-0000000000000002", "alex", "sent", "2027-01-15", 3, None, None),
    ("mailsug-x2", "cx-mailsrv-0000000000000003", "alex", "withdrawn", "2027-01-15", 4, "ev2", "cancel"),
    ("mailsug-x3", "cx-mailsrv-0000000000000004", "alex", "sent", "2027-01-15", 5, "ev2", "cancel"),   # one live ev2 card
    ("mailsug-old", "cx-mailsrv-0000000000000009", "alex", "sent", "2027-01-14", 40, None, None),      # outside 24 h
    ("mailsug-mac", "mac-item-1", "robin", "sent", "2027-01-15", 1, None, None),                     # from the Mac
]
for key, mid, owner, state, day, ago, ev, tgt in cards:
    scon.execute("INSERT INTO cards (key, mail_id, owner, title, detail, actions, state, day, ts, event, target) "
                 "VALUES (?,?,?,?,?,'[]',?,?,?,?,?)", (key, mid, owner, "Robin's recital moved", "detail", state, day,
                                                        NOW - ago * H, ev, tgt))
for mid, ago in (("cx-mailsrv-0000000000000001", 2), ("cx-mailsrv-0000000000000002", 3), ("mac-item-2", 1),
                 ("cx-mailsrv-0000000000000005", 50)):
    scon.execute("INSERT INTO seen (mail_id, updated_at, outcome, ts) VALUES (?, '', 'none', ?)", (mid, NOW - ago * H))
scon.commit(); scon.close()

st = ms.passes_status(24, now=NOW)
check("categorized, all time", st["categorized"], {"action": 1, "bulk": 2, "code": 1, "personal": 1})
check("categorized in the window", st["categorized_in_window"], {"action": 1, "bulk": 1, "code": 1, "personal": 1})
check("envelopes derived in the window", st["derived_in_window"], 6)
check("why the passes ran or not (derived before the column: unrecorded)", st["passes_in_window"],
      {"mac": 1, "ran": 4, "unrecorded": 1})
check("server-mail cards by state (the Mac's and old ones left out)", st["cards_in_window"],
      {"held": 1, "sent": 3, "withdrawn": 1})
check("two cards for one mail: 1 duplicate", st["duplicate_mail_cards"], 1)
check("two live cards for one event change: 1 (a withdrawn one doesn't count)", st["duplicate_change_cards"], 1)
check("invite cards", st["invite_cards_in_window"], 2)
check("server mail the suggestions pass saw", st["seen_server_in_window"], 2)
check("days over the card budget (4 for alex on 01-15 > PER_DAY 3)", st["days_over_budget"], 1)
wide = ms.passes_status(72, now=NOW)
check("a wider window takes the older card and mail", (wide["derived_in_window"], wide["cards_in_window"].get("sent")), (7, 4))

print("── counts only ──")
text = json.dumps(st)
check("no subject, title, sender, owner or id in it", [w for w in ("recital", "Robin", "robin", "alex", "cx-mailsrv", "mailsug",
      "example.invalid", "ev1") if w in text], [])
r = subprocess.run([sys.executable, str(HERE / "mail_server.py"), "passes-status", "--hours", "24"], capture_output=True,
                   text=True, env={**os.environ}, timeout=60)
out = json.loads(r.stdout or "{}")
check("the CLI prints the same keys", (r.returncode, sorted(out)), (0, sorted(st)))
before = (_TMP / "mail_server.db").stat().st_mtime_ns
ms.passes_status(24, now=NOW)
check("read-only: the store is untouched", (_TMP / "mail_server.db").stat().st_mtime_ns, before)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
