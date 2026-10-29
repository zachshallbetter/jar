#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""A person saying in their own chat that bills are paid or handled (#567, after the bill-due dry run: "fixed" in chat
never reached bill_said): mark_bills_paid records their word for their own open threads, the same record as the card's
Paid tap; "all except X" never marks X (#952); a name that matches nothing or several is asked about; another person's
bills are never touched; nothing touches money. Fictional vendors.
Run: python3 scripts/test_bills_said.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-billsaid-"))
for k, v in {"MAIL_BILLS_DB": "bills.db", "HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory",
             "AGENT_TURN_LOG": "turns.jsonl", "KB_ACL_DB": "kb_acl.db", "PROACTIVE_DB": "proactive.db",
             "LINKS_DB": "links.db", "LINK_SALT_FILE": "salt"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import bill_due as B  # noqa: E402
import exclusions  # noqa: E402
import obligations  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


TODAY = date(2026, 10, 5)


def bill(nid, vendor, amount, due, mailed, owner="alex"):
    return {"node_id": nid, "node_type": "bill", "data": {"doc_type": "bill", "vendor": vendor, "amount": amount,
            "due_date": due, "mail_date": mailed, "payment_status": "due",
            "provenance": {"source": "mail", "owner": owner, "audience": "private"}}}


RECS = [bill("b1", "Hale Water", 48.20, "2026-10-03", "2026-09-18"), bill("b2", "Birch Power", 90.00, "2026-10-10", "2026-09-25"),
        bill("b3", "Cedar Net", 60.00, "2026-10-09", "2026-09-24"),
        bill("b4", "Maple Phone", 70.00, "2026-10-08", "2026-09-22", owner="robin")]


def threads(person):
    return obligations.threads([r for r in RECS if r["data"]["provenance"]["owner"] == person], TODAY, B.statements(person))


def states(person):
    return {t.vendor: t.state for t in threads(person)}


print("── by name ──")
got = B.mark_said_paid("alex", threads("alex"), names=["Hale Water"], today=TODAY)
check("\"Hale Water's paid\": marked, on their word", (got["marked"], got["ask"]), (["Hale Water"], []))
check("…the thread resolves, as with the Paid tap", states("alex")["Hale Water"], "resolved")

print("\n── all except X (#952) ──")
said = "all of those are fixed except Birch Power"
got = B.mark_said_paid("alex", threads("alex"), everything=True, excluded=exclusions.excluded(said), today=TODAY)
check("\"all … except Birch Power\": the rest marked, Birch Power left", (got["marked"], got["left"]), (["Cedar Net"], ["Birch Power"]))
check("…Birch Power still open", states("alex")["Birch Power"], "due")
got = B.mark_said_paid("alex", threads("alex"), names=["Birch Power"], excluded=["birch power"], today=TODAY)
check("…even named outright while set aside", (got["marked"], got["left"]), ([], ["Birch Power"]))

print("\n── ask, never guess ──")
check("a name with no open bill", B.mark_said_paid("alex", threads("alex"), names=["Oak Gas"], today=TODAY)["ask"],
      ["I don't see an open bill for Oak Gas."])
RECS.append(bill("b5", "Birch Power", 95.00, "2026-11-10", "2026-10-02"))
got = B.mark_said_paid("alex", threads("alex"), names=["Birch Power"], today=TODAY)
check("a name matching two open bills: asks which, marks neither", (got["marked"], len(got["ask"]), "which one" in got["ask"][0]),
      ([], 1, True))
RECS.pop()

print("\n── their own bills only ──")
got = B.mark_said_paid("alex", obligations.threads(RECS, TODAY, B.statements("alex")), names=["Maple Phone"], today=TODAY)
check("Alex saying Robin's bill is paid: not marked", got["marked"], [])
check("…Robin's thread unchanged", states("robin")["Maple Phone"], "due")

print("\n── the tool, in a turn ──")
import agent_runtime_lib as lib  # noqa: E402
lib._viewer = lambda: ("alex", "admin")
lib.record_nodes = lambda t: ([r for r in RECS if r["node_type"] == t], True)
lib._node_visible = lambda n, pid, role: n["data"]["provenance"]["owner"] == pid
lib._obligation_threads = lambda recs, today, pid, role: obligations.threads(recs, today, B.statements(pid))
lib._local_now = lambda: __import__("datetime").datetime(2026, 10, 5, 12)
lib._TURN_IO.excluded = exclusions.excluded("yep, all of them are handled except birch")
out = lib._execute_tool("mark_bills_paid", {"all": "true"})
check("all=true with \"except birch\" in their message: Birch is left, never noted as paid",
      ("Left as they are" in out and "Birch Power" in out.split("Left as they are")[1],
       "Birch" in out.split("Left as they are")[0]), (True, False))
out = lib._execute_tool_checked("mark_bills_paid", {"bills": "Birch Power"})
check("the #952 guard refuses a call naming what they set aside", out.startswith("Not run: they set this aside"), True)
lib._TURN_IO.excluded = None
lib._viewer = lambda: ("ava", "kid")
check("a kid: not theirs to record", lib._execute_tool("mark_bills_paid", {"all": "true"}).startswith("Bills are recorded"), True)

print("\n── nothing here touches money ──")
src = Path(B.__file__).read_text()
check("no record writes or payments in bill_due", [w for w in ("/v1/nodes", "transfer", "pay(") if w in src], [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
