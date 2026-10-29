#!/usr/bin/env python3
# gate: hermetic
"""#951 step 3: a person's word on a bill is a claim with an owner, an audience and a source, never the chat text, and
never widened except by the speaker's own yes (decision (b), 10-09): asked once, only for a bill already the household's
or the adults', a yes shares the fact with the adults, a kid's word never, no answer stays private, and it can be taken
back. The views × viewers matrix: the speaker (Robin), the other adult (Alex), a kid (Ava), the shared screen.
Fictional people; temporary stores. Run: python3 scripts/test_obligation_claims.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-obligation-claims-"))
os.environ["MAIL_BILLS_DB"] = str(_TMP / "bills.db")
sys.path.insert(0, str(Path(__file__).parent))
import bill_due as B  # noqa: E402
import obligations as O  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def bill(nid, vendor, amount, due, mailed, owner, audience):
    return {"node_id": nid, "node_type": "bill", "data": {"doc_type": "bill", "vendor": vendor, "amount": amount,
            "due_date": due, "mail_date": mailed, "payment_status": "due",
            "provenance": {"source": "mail", "owner": owner, "audience": audience}}}


TODAY = date(2026, 10, 9)
NAMES = {"alex": "Alex Quarry", "robin": "Robin Quarry", "ava": "Ava Quarry"}
WATER = bill("b-water", "Hale Water", 48.20, "2026-10-12", "2026-09-21", "alex", "household")   # a household bill
PHONE = bill("b-phone", "Harbor Mobile", 61.00, "2026-10-14", "2026-09-24", "robin", "private")  # Robin's own
RECORDS = [WATER, PHONE]
VIEWERS = {"robin": ("robin", "adult"), "alex": ("alex", "admin"), "ava": ("ava", "kid"), "screen": (None, "household")}


def state(viewer, vendor):
    pid, role = VIEWERS[viewer]
    ts = [t for t in O.threads(RECORDS, TODAY, B.statements_for(pid, role, NAMES)) if t.vendor == vendor]
    return ts[0].state if ts else None


def chain(viewer, vendor):
    pid, role = VIEWERS[viewer]
    t = next(t for t in O.threads(RECORDS, TODAY, B.statements_for(pid, role, NAMES)) if t.vendor == vendor)
    return t.chain()


def threads_for(viewer):
    pid, role = VIEWERS[viewer]
    return O.threads(RECORDS, TODAY, B.statements_for(pid, role, NAMES))


print("── before anyone says anything ──")
check("the water bill is open for both adults", (state("robin", "Hale Water") in O.OPEN, state("alex", "Hale Water") in O.OPEN),
      (True, True))

print("── Robin says in Robin's own chat: the water's paid ──")
water_t = next(t for t in threads_for("robin") if t.vendor == "Hale Water")
check("the water thread carries the household audience its bill has", water_t.audience, "household")
got = B.mark_said_paid("robin", threads_for("robin"), names=["Hale Water"], today=TODAY)
check("Robin (an adult) can give their word on a household bill they see; the share question is raised once",
      (got["marked"], got["share_ask"]), (["Hale Water"], ["Hale Water"]))
check("speaker: resolved by their own word", state("robin", "Hale Water") in O.OPEN, False)
check("other adult: unchanged until Robin answers (no answer means private)", state("alex", "Hale Water") in O.OPEN, True)
for v in ("ava", "screen"):
    check(f"{v}: sees no one's word", B.statements_for(*VIEWERS[v], NAMES), [])

print("── Robin answers no ──")
check("a no keeps it Robin's", B.share("robin", "adult", water_t.account, False, account_audience="household"), "kept")
check("other adult: still open", state("alex", "Hale Water") in O.OPEN, True)

print("── Robin changes their mind: yes ──")
check("a yes shares the fact with the adults", B.share("robin", "adult", water_t.account, True, account_audience="household"),
      "shared")
check("other adult: resolved, and the chain says whose word it is", (state("alex", "Hale Water") in O.OPEN,
                                                                      "Robin said it's done" in chain("alex", "Hale Water")),
      (False, True))
check("speaker: still 'you said'", "you said it's done" in chain("robin", "Hale Water"), True)
for v in ("ava", "screen"):
    check(f"{v}: still sees no one's word (adults only)", B.statements_for(*VIEWERS[v], NAMES), [])
con = sqlite3.connect(os.environ["MAIL_BILLS_DB"])
cols = [r[1] for r in con.execute("PRAGMA table_info(obligation_claims)")]
check("a claim is the structured fact only: no column for any chat text (about is a person id)", sorted(cols),
      sorted(["id", "owner", "audience", "account", "kind", "date", "source", "turn_id", "at", "superseded_by", "asked",
              "about"]))
check("…recorded from the chat, as a source", con.execute(
    "SELECT source, audience, asked FROM obligation_claims WHERE owner='robin' AND superseded_by IS NULL").fetchall(),
    [("chat", "adults", "yes")])

print("── Robin takes it back ──")
check("taken back", B.take_back("robin", water_t.account), True)
check("other adult: open again", state("alex", "Hale Water") in O.OPEN, True)
check("speaker: still resolved by their word", state("robin", "Hale Water") in O.OPEN, False)
check("taking back again: nothing shared", B.take_back("robin", water_t.account), False)

print("── Robin's own private account: never asked ──")
phone_t = next(t for t in threads_for("robin") if t.vendor == "Harbor Mobile")
got = B.mark_said_paid("robin", threads_for("robin"), names=["Harbor Mobile"], today=TODAY)
check("marked, but the share question is never raised", (got["marked"], got["share_ask"]), (["Harbor Mobile"], []))
check("even an explicit yes is refused: it stays Robin's",
      B.share("robin", "adult", phone_t.account, True, account_audience=phone_t.audience), "not_asked")
check("Robin's word on their own private bill never resolves anything in Alex's view", [t.vendor for t in threads_for("alex")
                                                                     if t.vendor == "Harbor Mobile" and t.state not in O.OPEN],
      [])

print("── a kid's word is never widened ──")
check("a kid can't share", B.share("ava", "kid", water_t.account, True, account_audience="household"), "not_asked")
check("a shared screen can't share", B.share("household", "household", water_t.account, True,
                                             account_audience="household"), "not_asked")

print("── superseding, never overwriting ──")
B.say("alex", water_t.account, TODAY, False, source="chat")
B.say("alex", water_t.account, TODAY, True, source="chat")
rows = con.execute("SELECT kind, superseded_by IS NULL FROM obligation_claims WHERE owner='alex' ORDER BY at").fetchall()
check("both words kept; the newer supersedes the older", rows, [("not_paid", 0), ("paid", 1)])
check("…and the reads use the newer", [s["paid"] for s in B.statements("alex")], [True])

print("── migration of the old word ──")
db2 = _TMP / "old.db"
c2 = sqlite3.connect(db2)
c2.execute("CREATE TABLE bill_said (owner TEXT NOT NULL, account TEXT NOT NULL, date TEXT NOT NULL, paid INTEGER NOT NULL, at REAL NOT NULL)")
c2.execute("INSERT INTO bill_said VALUES ('alex','hale water','2026-10-01',1,1.0)")
c2.commit(); c2.close()
check("an old Paid tap becomes a private claim, once", (B.statements("alex", db=db2), B.statements("alex", db=db2)),
      ([{"account": "hale water", "date": "2026-10-01", "paid": True}],) * 2)
check("…private: another adult never sees it", B.statements_for("robin", "adult", NAMES, db=db2), [])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
