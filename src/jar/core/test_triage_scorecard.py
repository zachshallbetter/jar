#!/usr/bin/env python3
# gate: hermetic
"""Triage measured against what each person did (#1644, triage_scorecard.py): mail they cleared that triage dropped
(agreed) or would have kept (triage_kept); mail triage dropped that they kept by filing it or undoing its clear
(missed); untriaged counted apart; marks aren't a keep-or-clear choice; each person's own; counts only, never a sender
or subject. Fictional people (Alex, Robin); a temporary mail store. Run: python3 scripts/test_triage_scorecard.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-triage-score-"))
os.environ.update({"MAIL_SERVER_DB": str(_TMP / "mail.db"), "SPACES_DB": str(_TMP / "spaces.db"),
                   "HOUSEHOLD_DB": str(_TMP / "household.db"), "CAPABILITY_SWITCHES": str(_TMP / "switches.json")})
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mail_moves as MM  # noqa: E402
import mail_server as M  # noqa: E402
import triage_scorecard as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = time.time()
con = M._db()
CATS = {"<n1@x>": "newsletter", "<n2@x>": "marketing", "<p1@x>": "personal", "<r1@x>": "record", "<u1@x>": None,
        "<n3@x>": "notification", "<n4@x>": "newsletter", "<a1@x>": "action"}
for i, (mid, cat) in enumerate(CATS.items(), 1):
    con.execute("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, thread_id, sender, "
                "subject, category) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("icloud", "INBOX", 1, i, "alex", f"m{i}", mid, mid, "Harbor News <news@harbor.example>", "Weekly", cat))
con.commit()
con.close()


def plan(owner, mids, verb, *, undo_of=None, ago=3600, state="done"):
    c = MM._db()
    try:
        p = MM._store(c, owner=owner, account="icloud", src="INBOX", dest="Archive",
                      rows=[{"mid": m, "sender": "x", "date": ""} for m in mids], now=NOW - ago, undo_of=undo_of, verb=verb)
        c.execute("UPDATE mail_plans SET state=?, done_at=? WHERE id=?", (state, NOW - ago, p["plan_id"]))
        c.commit()
        return p["plan_id"]
    finally:
        c.close()


plan("alex", ["<n1@x>", "<n2@x>", "<p1@x>", "<u1@x>"], "archive")     # 2 agreed, 1 triage would keep, 1 untriaged
plan("alex", ["<r1@x>"], "trash")                                     # triage kept a record they threw out
undone = plan("alex", ["<n3@x>"], "archive")                          # cleared, then taken back
plan("alex", ["<n3@x>"], "back", undo_of=undone)
plan("alex", ["<n4@x>"], "move")                                      # triage dropped it; they filed it: a miss
plan("alex", ["<a1@x>"], "read")                                      # a mark: not a keep-or-clear choice
plan("alex", ["<n1@x>"], "archive", state="declined")                 # not done: not counted
plan("alex", ["<n2@x>"], "archive", ago=40 * 86400)                   # older than the window
plan("robin", ["<n1@x>"], "archive")

s = T.score("alex", since=NOW - 30 * 86400)
check("Alex: cleared 5 (one undone apart), 2 agreed, 2 triage would have kept, 1 untriaged",
      (s["cleared"], s["agreed"], s["triage_kept"], s["untriaged"]), (5, 2, 2, 1))
check("…missed: the undone clear and the filed newsletter", (s["undone"], s["filed"], s["missed"]), (1, 1, 2))
check("…agreement over what triage judged: 2 of 4", s["agreement"], 0.5)
check("Robin's figures are hers", (T.score("robin", since=NOW - 86400)["cleared"], T.score("robin", since=NOW - 86400)["agreed"]),
      (1, 1))
check("nobody's plans: zeros, agreement unknown", T.score("ava", since=NOW - 86400)["agreement"], None)
check("the owners with done plans in the window", T.owners(since=NOW - 30 * 86400), ["alex", "robin"])
out = json.dumps(s)
check("counts only: no sender, subject or Message-ID", any(x in out for x in ("Harbor", "Weekly", "@x")), False)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
