#!/usr/bin/env python3
# gate: hermetic
"""What people did with each source's cards (#1039): shown / Done / snoozed / muted / expired untouched / acted, for
the weekly turn report, counts only. The admin's own counts beside the totals; nobody else's by name. A noisy source:
expired untouched + muted over 60% of shown, with at least 10 shown. Fictional cards; a temporary proactive.db.
Run: python3 scripts/test_card_outcomes.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-card-outcomes-test-"))
os.environ["PROACTIVE_DB"] = str(_TMP / "proactive.db")
sys.path.insert(0, str(Path(__file__).parent))
import household_identity as hh  # noqa: E402
import proactive as P  # noqa: E402
import agent_turn_report as R  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


DB = Path(os.environ["PROACTIVE_DB"])
ADMIN, OTHER = hh.ADMIN_PERSON_ID, "p_robin"
BTN = [{"id": "open", "label": "Open the bill", "kind": "prompt", "value": "Show me the Harbor Gas bill"},
       {"id": "dismiss", "label": "Done", "kind": "dismiss"}]
# the bills source: 4 cards, each one handled a different way
bills = [P.add_card(f"Harbor Gas bill {i}", source="bills", kind="info", actions=BTN, to="household", db=DB) for i in range(4)]
P.ack_card(bills[0]["id"], db=DB, person=ADMIN, signal="done")                 # Done (the admin)
P.card_action(bills[1]["id"], "open", db=DB, person=OTHER, role="adult")       # a source button (someone else)
P.card_action(bills[2]["id"], "dismiss", db=DB, person=ADMIN, role="admin")    # the Done button (the admin)
P.snooze(bills[3]["id"], time.time() + 3600, person=OTHER, role="adult", db=DB)
# the promos source: 10 cards nobody touched, all expired this week, and one mute
promos = [P.add_card(f"Spring sale {i}", source="promos", kind="info", to="household", db=DB) for i in range(10)]
P.mute(promos[0]["id"], person=ADMIN, role="admin", db=DB)
con = sqlite3.connect(DB)
con.execute("UPDATE cards SET expires_at=? WHERE source='promos'", (time.time() - 60,))
con.execute("UPDATE cards SET expires_at=? WHERE source='bills'", (time.time() - 60,))
con.commit()
now = time.time() + 1
o = P.outcomes(now - 7 * 86400, now, ADMIN, DB)

print("── per source ──")
b = o["bills"]
check("bills: shown, Done (both kinds of Done), acted, snoozed",
      (b["shown"], b["done"], b["acted"], b["snoozed"], b["muted"]), (4, 2, 1, 1, 0))
check("…every bill card was handled: none expired untouched", b["expired_untouched"], 0)
check("…not noisy (only 4 shown)", b["noisy"], False)
p = o["promos"]
check("promos: 10 shown, 1 muted, the other 9 expired untouched (the muted one was dismissed by its mute)",
      (p["shown"], p["muted"], p["expired_untouched"]), (10, 1, 9))
check("…a noisy source: (9 + 1) / 10 > 60%, with 10 shown", p["noisy"], True)

print("── the admin's own, nobody else's by name ──")
check("the admin's own counts", b["mine"], {"done": 2, "snoozed": 0, "muted": 0, "acted": 0})
check("someone else's only in the totals", (b["acted"], b["snoozed"], b["mine"]["acted"], b["mine"]["snoozed"]), (1, 1, 0, 0))
lines = R.card_lines(7, now, DB)
text = "\n".join(lines)
check("the report's lines: counts per source, the noisy one flagged", ("promos: 10/0/0/1/9/0" in text, "← noisy source" in text,
                                                                      "bills: 4/2/1/0/0/1 [2/0/0/0]" in text), (True, True, True))
check("no titles and no person ids in the report", [w for w in ("Harbor", "Spring sale", OTHER, ADMIN) if w in text], [])

print("── the window and repeats ──")
check("outside the window: nothing", P.outcomes(now - 30 * 86400, now - 8 * 86400, ADMIN, DB), {})
P.ack_card(bills[0]["id"], db=DB, person=ADMIN, signal="done")
check("Done twice on one card counts once", P.outcomes(now - 7 * 86400, now + 5, ADMIN, DB)["bills"]["done"], 2)
check("an ack without a signal (a button already counted) adds nothing",
      (P.ack_card(bills[1]["id"], db=DB, person=ADMIN), P.outcomes(now - 7 * 86400, now + 5, ADMIN, DB)["bills"]["done"]),
      (True, 2))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
