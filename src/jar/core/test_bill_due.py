#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""A bill coming due with no payment seen (titan-node#567): one card to its owner, once per due date, never in quiet
hours; dry mode logs the bill's name, due date and state and never an amount; Paid records the owner's word (never
money) and open_obligations sees it; Not yet asks once more on the due day; kids and other people never.
Fictional records, a pinned date.
Run: python3 scripts/test_bill_due.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-billdue-"))
os.environ["MAIL_BILLS_DB"] = str(_TMP / "bills.db")
os.environ.pop("BILL_DUE_CARDS", None)
sys.path.insert(0, str(Path(__file__).parent))

import bill_due as B  # noqa: E402
import obligations  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


TODAY = date(2026, 10, 5)


def bill(nid, vendor, amount, due, mailed, owner="alex", status="due"):
    return {"node_id": nid, "node_type": "bill", "data": {"doc_type": "bill", "vendor": vendor, "amount": amount,
            "due_date": due, "mail_date": mailed, "payment_status": status,
            "provenance": {"source": "mail", "owner": owner, "audience": "private"}}}


def receipt(nid, vendor, amount, paid, owner="alex"):
    return {"node_id": nid, "node_type": "receipt", "data": {"doc_type": "receipt", "vendor": vendor, "amount": amount,
            "paid_date": paid, "provenance": {"source": "mail", "owner": owner, "audience": "private"}}}


RECORDS = [
    bill("b-water", "Hale Water", 48.20, "2026-10-08", "2026-09-21"),            # due in 3 days, unpaid → card
    bill("b-power", "Birch Power", 90.00, "2026-10-10", "2026-09-25"),           # due in 5 days → not yet
    bill("b-net", "Cedar Net", 60.00, "2026-10-07", "2026-09-20"),               # due in 2 days, but paid → none
    receipt("r-net", "Cedar Net", 60.00, "2026-10-01"),
    bill("b-robin", "Maple Phone", 70.00, "2026-10-06", "2026-09-22", owner="robin"),   # Robin's own
    bill("b-ava", "Corner Club", 20.00, "2026-10-06", "2026-09-22", owner="ava"),       # a kid's: never
]
ROLES = {"alex": "admin", "robin": "adult", "ava": "kid"}
SENT: list[dict] = []
LOGS: list[str] = []


def send(title, detail="", **kw):
    SENT.append({"title": title, "detail": detail, **kw})
    return {"id": f"c{len(SENT)}"}


print("── which threads ──")
got = sorted((o, t.vendor) for o, t in B.due_threads(RECORDS, TODAY, ROLES))
check("due within 3 days and unpaid, per owner; never paid, later, or a kid's",
      got, [("alex", "Hale Water"), ("robin", "Maple Phone")])

print("\n── off and dry ──")
check("off by default: nothing", (B.run(RECORDS, TODAY, quiet=False, roles=ROLES, send=send)["due"], SENT), (0, []))
st = B.run(RECORDS, TODAY, quiet=False, roles=ROLES, send=send, log=LOGS.append, how="dry")
check("dry: counts, a log line each, no card", (st["would_send"], len(LOGS), SENT), (2, 2, []))
check("…the log has the bill's name, due date and state, never an amount",
      (any("Hale Water · due 2026-10-08 · due" in x for x in LOGS), any("$" in x or "48.2" in x for x in LOGS)), (True, False))
st = B.run(RECORDS, TODAY, quiet=False, roles=ROLES, send=send, log=LOGS.append, how="dry")
check("…once per due date in dry too (the volume is real)", (st["would_send"], st["already"], len(LOGS)), (0, 2, 2))

print("\n── sending ──")
st = B.run(RECORDS, TODAY, quiet=True, roles=ROLES, send=send, how="1")
check("quiet hours: waits, nothing sent", (st["quiet"], SENT), (2, []))
st = B.run(RECORDS, TODAY, quiet=False, roles=ROLES, send=send, how="1")
check("then one card each, to its owner only (dry rows don't block the real ones)",
      sorted((c["to"][0], c["title"]) for c in SENT), [("alex", "Hale Water $48.20 is due Thu Oct 8"),
                                                        ("robin", "Maple Phone $70.00 is due Tue Oct 6")])
check("…saying no payment was seen, with Paid and Not yet", (all("haven't seen a payment" in c["detail"] for c in SENT),
      [a["label"] for a in SENT[0]["actions"]]), (True, ["Paid", "Not yet"]))
n = len(SENT)
check("again the same day: once per due date", (B.run(RECORDS, TODAY, quiet=False, roles=ROLES, send=send, how="1")["already"],
                                                len(SENT)), (2, n))

print("\n── the taps ──")
water = next(c for c in SENT if c["to"] == ["alex"])
key = water["data"]["key"]
check("someone else's tap does nothing", B.answer(f"paid:{key}", "robin", "adult", today=TODAY)[0], False)
check("a kid's tap does nothing", B.answer(f"paid:{key}", "alex", "kid", today=TODAY)[0], False)
ok, said = B.answer(f"paid:{key}", "alex", "admin", today=TODAY)
check("Paid: the owner's word, and it says nothing was paid by titan", (ok, "Nothing was paid" in said), (True, True))
ts = {t.vendor: t.state for t in obligations.threads([r for r in RECORDS if r["data"]["provenance"]["owner"] == "alex"],
                                                     TODAY, B.statements("alex"))}
check("…the thread resolves for open_obligations too", ts["Hale Water"], "resolved")
check("…Robin's word list is her own (empty)", B.statements("robin"), [])
robin_card = next(c for c in SENT if c["to"] == ["robin"])
ok, said = B.answer(f"later:{robin_card['data']['key']}", "robin", "adult", today=TODAY)
check("Not yet: asked again on the due day, not before", (ok, B.run(RECORDS, TODAY, quiet=False, roles=ROLES, send=send,
                                                                   how="1")["sent"]), (True, 0))
n = len(SENT)
st = B.run(RECORDS, date(2026, 10, 6), quiet=False, roles=ROLES, send=send, how="1")
check("…on the due day, once more", (st["sent"], SENT[-1]["to"], SENT[-1]["title"]), (1, ["robin"], "Maple Phone $70.00 is due today"))

print("\n── overdue with no confirmation: one card the day after ──")
late = [bill("b-late", "Hale Gas", 33.00, "2026-10-04", "2026-09-18")]
check("the day after the due date", [(o, t.state) for o, t in B.due_threads(late, TODAY, ROLES)], [("alex", "overdue_unconfirmed")])
check("…not two days after", B.due_threads(late, date(2026, 10, 6), ROLES), [])

print("\n── nothing here touches money ──")
src = (Path(__file__).parent / "bill_due.py").read_text()
check("no payment, transfer or record write in the module", [w for w in ("/v1/nodes", "transfer", "charge(", "pay(")
                                                             if w in src], [])


from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402
import local_time  # noqa: E402
import time as _time  # noqa: E402

_LA = ZoneInfo("America/Los_Angeles")
_REAL = (_time.time, local_time.now)


def freeze_household(y, mo, d, h, mi):
    """Pin the household clock (and the epoch clock) to a local time; titan's own UTC day is already the next one at 23:30."""
    at = datetime(y, mo, d, h, mi, tzinfo=_LA)
    _time.time = lambda: at.timestamp()
    local_time.now = lambda name=None: at.astimezone(local_time.zone(name) or _LA)
    return at


def unfreeze_household():
    _time.time, local_time.now = _REAL

print("\n── the household's day, not titan's UTC day (#1567) ──")
import sqlite3  # noqa: E402
for label, hh, mm, day_, want in (("23:30 household time (titan's UTC is already the 10th)", 23, 30, 9, "2026-10-09"),
                                  ("00:30 household time", 0, 30, 10, "2026-10-10")):
    freeze_household(2026, 10, day_, hh, mm)
    ok, _ = B.answer(f"paid:{robin_card['data']['key']}", "robin", "adult")
    unfreeze_household()
    cdb = sqlite3.connect(os.environ["MAIL_BILLS_DB"])
    got = cdb.execute("SELECT date FROM obligation_claims WHERE owner='robin' ORDER BY rowid DESC LIMIT 1").fetchone()
    cdb.close()
    check(f"Paid at {label} is dated the household's day", (ok, got[0] if got else None), (True, want))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
