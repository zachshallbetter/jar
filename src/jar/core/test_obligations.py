#!/usr/bin/env python3
# gate: hermetic
"""Obligation threads (#951 slice 1): a bill's notices, its payment and the person's word, settled in code. Fictional
vendors and people only (Harbor Gas, Lakeside Clinic, Alex, Robin). No titan, no model.
Run: python3 scripts/test_obligations.py"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import obligations as O  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def bill(nid, vendor, amount, status, mail, due=None, paid=None, owner="alex"):
    return {"node_id": nid, "node_type": "bill", "data": {"doc_type": "bill", "vendor": vendor, "amount": amount,
            "payment_status": status, "mail_date": mail, "due_date": due, "paid_date": paid,
            "provenance": {"owner": owner, "audience": "private"}}}


def receipt(nid, vendor, amount, paid, owner="alex"):
    return {"node_id": nid, "node_type": "receipt", "data": {"doc_type": "receipt", "vendor": vendor, "amount": amount,
            "payment_status": "paid", "paid_date": paid, "mail_date": paid, "provenance": {"owner": owner}}}


TODAY = date(2026, 10, 5)


def states(recs, said=()):
    return {(t.account, t.cycle.isoformat()): t.state for t in O.threads(recs, TODAY, said)}


print("── one account, normalised ──")
check("suffixes and aliases fold", [O.account_of(v) for v in ("Harbor Gas Inc.", "Harbor Gas", "Lakeside Clinic, LLC",
      "Amazon.com", "Comcast")], ["harbor gas", "harbor gas", "lakeside clinic", "amazon", "xfinity"])

check("whole names, never a shared first word", [O.account_of(v) for v in ("American Express", "American Water",
      "Vercel Inc.", "City of Spokane")], ["american express", "american water", "vercel", "city of spokane"])
M = O.merge_accounts(["american express", "american water", "apple card", "apple services", "apple", "bank of tacoma",
                      "bank of the west", "city of spokane", "city of tacoma", "providence",
                      "providence washington montana", "vercel"])
check("different companies with one first word stay apart",
      [M[n] for n in ("american express", "american water", "apple card", "apple services", "bank of tacoma",
                      "bank of the west", "city of spokane", "city of tacoma")],
      ["american express", "american water", "apple card", "apple services", "bank of tacoma", "bank of the west",
       "city of spokane", "city of tacoma"])
check("a short name beginning two families joins neither", M["apple"], "apple")
check("a name beginning one family joins it", M["providence washington montana"], "providence")
check("one company's receipt never pays another's bill",
      states([bill("b1", "American Water", 61.20, "failed", "2026-10-01"), receipt("r1", "American Express", 61.20, "2026-10-02")]),
      {("american water", "2026-10-01"): "failed"})
check("…while the same company's longer name still pays it",
      states([bill("b1", "Lakeside", 38.85, "failed", "2026-10-02"), receipt("r1", "Lakeside Clinic LLC", 38.85, "2026-10-03")]),
      {("lakeside", "2026-10-02"): "resolved"})

print("── settled in code ──")
check("a failure, then a receipt for the amount: resolved",
      states([bill("b1", "Harbor Gas", 61.20, "failed", "2026-09-20"), receipt("r1", "Harbor Gas Inc.", 61.20, "2026-09-21")]),
      {("harbor gas", "2026-09-20"): "resolved"})
check("a receipt the same day as the decline pays it",
      states([receipt("r1", "Amazon", 17.43, "2026-09-17"), bill("b1", "Amazon.com", 17.43, "failed", "2026-09-17")]),
      {("amazon", "2026-09-17"): "resolved"})
check("a late fee on top still pays it",
      states([bill("b1", "Harbor Gas", 61.20, "failed", "2026-10-01"), receipt("r1", "Harbor Gas", 71.20, "2026-10-03")]),
      {("harbor gas", "2026-10-01"): "resolved"})
check("a smaller payment: partly paid, still open",
      states([bill("b1", "Harbor Gas", 61.20, "due", "2026-09-28", due="2026-10-10"), receipt("r1", "Harbor Gas", 30.00, "2026-10-02")]),
      {("harbor gas", "2026-09-28"): "partly_paid"})
check("a receipt from another month's order doesn't pay this one",
      states([bill("b1", "Amazon.com", 19.61, "failed", "2026-10-01"), receipt("r1", "Amazon", 54.00, "2026-10-02")]),
      {("amazon", "2026-10-01"): "failed"})
check("retries join one thread; still failing",
      [(t.state, len(t.pieces)) for t in O.threads([bill("b1", "Lakeside Clinic", 38.85, "failed", "2026-10-02"),
                                                     bill("b2", "Lakeside Clinic", 38.85, "failed", "2026-10-03")], TODAY)],
      [("failed", 2)])
check("an old failure with nothing since: stale, not waiting",
      states([bill("b1", "Amazon.com", 17.43, "failed", "2026-09-17")]), {("amazon", "2026-09-17"): "stale"})
check("due date passed, no payment seen: overdue_unconfirmed",
      states([bill("b1", "Harbor Gas", 161.16, "due", "2026-09-18", due="2026-10-03")]),
      {("harbor gas", "2026-09-18"): "overdue_unconfirmed"})
check("…said as 'no confirmation seen', never 'overdue'",
      ("no confirmation seen" in O.line(O.threads([bill("b1", "Harbor Gas", 161.16, "due", "2026-09-18", due="2026-10-03")],
                                                  TODAY)[0]), "overdue" in O.SAID["overdue_unconfirmed"]), (True, False))
check("due in the future: due", states([bill("b1", "Harbor Gas", 61.20, "due", "2026-10-01", due="2026-10-12")]),
      {("harbor gas", "2026-10-01"): "due"})

print("── the person's word ──")
F = [bill("b1", "Lakeside Clinic", 38.85, "failed", "2026-10-02")]
check("'fixed' outranks the notice before it", states(F, [{"vendor": "Lakeside Clinic", "date": "2026-10-04", "paid": True}]),
      {("lakeside clinic", "2026-10-02"): "resolved"})
check("…a new failure after it reopens",
      states(F + [bill("b2", "Lakeside Clinic", 38.85, "failed", "2026-10-05")],
             [{"vendor": "Lakeside", "date": "2026-10-04", "paid": True}]), {("lakeside clinic", "2026-10-02"): "failed"})
check("a receipt says paid, the person says not: contested",
      states(F + [receipt("r1", "Lakeside Clinic", 38.85, "2026-10-03")],
             [{"vendor": "Lakeside Clinic", "date": "2026-10-04", "paid": False}]), {("lakeside clinic", "2026-10-02"): "contested"})

print("── the thread key stays abstract (account, cycle, thread_ref) ──")
t = O.threads(F, TODAY)[0]
check("key", t.key, ("lakeside clinic", "2026-10-02", None))
check("the chain cites each step", O.line(O.threads(F + [bill("b2", "Lakeside Clinic", 38.85, "failed", "2026-10-03")],
                                                     TODAY)[0]), "Lakeside Clinic $38.85: payment failed (failed Oct 2 → failed Oct 3)")
check("owner and audience come from the opening notice", (t.owner, t.audience), ("alex", "private"))

print("── the tool: what \"anything waiting on me?\" reads ──")
import os, tempfile  # noqa: E401,E402
_TMP = Path(tempfile.mkdtemp(prefix="titan-oblig-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "CONNECTORS_LEDGER": "ledger.db", "AGENT_MEMORY_DIR": "memory",
             "KB_ACL_DB": "kb_acl.db", "HOUSEHOLD_PREFS": "household.json", "DAILY_LOG_DIR": "daily",
             "PROACTIVE_DB": "proactive.db", "AGENT_TURN_LOG": "turns.jsonl", "LINKS_DB": "links.db",
             "LINK_SALT_FILE": "link_salt", "WORLD_DIR": "world", "FENCES_DB": "fences.db"}.items():
    os.environ[k] = str(_TMP / v)
# these cases index items and read their links back at once: the linker scores a content change on the spot
# (production re-scores in the background, linker.scored)
os.environ["LINKER_SCORE_SYNC"] = "1"
import agent_runtime_lib as lib  # noqa: E402
from datetime import datetime  # noqa: E402
RECS = {"bill": [bill("b1", "Lakeside Clinic", 38.85, "failed", "2026-10-02"),
                 bill("b2", "Lakeside Clinic", 38.85, "failed", "2026-10-03"),
                 bill("b3", "Harbor Gas", 161.16, "due", "2026-09-18", due="2026-10-03"),
                 bill("b4", "Amazon.com", 17.43, "failed", "2026-09-17"),
                 bill("b5", "Brightline Hosting Inc.", 21.80, "failed", "2026-09-20"),
                 bill("b6", "Robin's Gym", 40.00, "failed", "2026-10-04", owner="robin")],
        "receipt": [receipt("r1", "Brightline Hosting", 21.80, "2026-09-21")]}
real = (lib.record_nodes, lib._node_visible, lib._viewer, lib._local_now)
lib.record_nodes = lambda t, *a, **k: (RECS.get(t, []), True)
lib._node_visible = lambda n, pid, role: (n["data"]["provenance"].get("owner") or "alex") == pid
lib._viewer = lambda: ("alex", "admin")
lib._local_now = lambda: datetime(2026, 10, 5, 9, 0)
try:
    out = lib.execute_tool("open_obligations", {})
    print("    " + out.replace("\n", "\n    "))
    waiting = out.split("Old, nothing since")[0]
    check("waiting: the failing one and the unconfirmed one only", (waiting.count("\n- "), "Lakeside Clinic $38.85: payment failed"
          in waiting, "Harbor Gas $161.16: no confirmation seen, due Oct 3" in waiting), (2, True, True))
    check("the old decline is listed as old, not waiting", "Amazon.com $17.43: old, nothing since" in out.split("Old, nothing since")[1], True)
    check("the failure paid the next day is under paid", "Brightline Hosting Inc. $21.80: paid (failed Sep 20 → paid Sep 21)" in out, True)
    check("never 'overdue', and another person's bill is never shown", ("overdue" in out.lower(), "Gym" in out), (False, False))
    check("one vendor", lib.execute_tool("open_obligations", {"vendor": "Harbor Gas"}).count("\n- "), 1)
finally:
    lib.record_nodes, lib._node_visible, lib._viewer, lib._local_now = real

print("── the bill lens reads threads (#951) ──")
import random  # noqa: E402
import hooks as HK  # noqa: E402
import linker as LK  # noqa: E402
LREC = [bill("l1", "Harbor Gas", 61.20, "failed", "2026-09-20"), receipt("l2", "Harbor Gas Inc.", 61.20, "2026-09-21"),
        bill("l3", "Lakeside Clinic", 120.00, "due", "2026-09-25", due="2026-10-10"),
        receipt("l4", "Lakeside Clinic", 50.00, "2026-10-01"),                              # a partial payment
        bill("l5", "Brightline Hosting", 9.99, "failed", "2026-10-01"),
        bill("l6", "Brightline Hosting", 9.99, "failed", "2026-10-03"),                     # a retry, a common amount
        bill("l7", "American Water", 45.00, "failed", "2026-10-02"),
        receipt("l8", "American Express", 45.00, "2026-10-03")]
rnd = random.Random(3)
BG = [receipt(f"bg{i}", f"Shop {i % 9}", rnd.choice([9.99, 9.99, 9.99, 14.00, 22.50, 31.00]), f"2026-08-{(i % 27) + 1:02d}")
      for i in range(60)]
lcon = HK.connect()
for n in LREC + BG:
    d = n["data"]
    HK.index(lcon, n["node_id"], "record", HK.extract({"title": "", "vendor": d["vendor"], "amount": d["amount"],
             "at": d.get("paid_date") or d.get("mail_date"), "due_date": d.get("due_date")}), "alex", "private")
real2 = (lib.record_nodes, lib._node_visible, lib._viewer, lib._local_now)
lib.record_nodes = lambda t, *a, **k: ([n for n in LREC if n["node_type"] == t], True)
lib._node_visible = lambda n, pid, role: True
lib._viewer = lambda: ("alex", "admin")
lib._local_now = lambda: datetime(2026, 10, 5, 9, 0)
try:
    ts = lib._obligation_threads(LREC, date(2026, 10, 5), "alex", "admin")
    S = {tuple(sorted(p.ref for p in t.pieces)): t for t in ts}
    check("a bill and the receipt it's linked to are one obligation, paid", S.get(("l1", "l2")).state if S.get(("l1", "l2")) else None,
          "resolved")
    part = S.get(("l3", "l4"))
    check("a partial payment the linker only offered as a candidate is adopted, and says so",
          (part.state if part else None, "(taken as a payment of this bill)" in (part.chain() if part else "")),
          ("partly_paid", True))
    retry = S.get(("l5", "l6"))
    check("a retry for a common amount is adopted as a retry", (retry.state if retry else None,
          "(taken as a retry)" in (retry.chain() if retry else "")), ("failed", True))
    check("American Water's bill isn't paid by American Express's receipt (a receipt alone isn't an obligation)",
          (("l7",) in S, ("l8",) in S, S[("l7",)].state), (True, False, "failed"))
    items = LK.load(lcon)
    edges = LK.score(items, LK.statements(lcon))
    strong = {i: k for k, t in enumerate(LK.threads_for(items, edges, lambda it: True)) for i in t.items}
    cands = {frozenset((e.a, e.b)) for e in edges if e.state == "candidate"}
    agree = all(len({strong.get(p.ref, p.ref) for p in t.pieces if not p.adopted and p.ref != "you said"}) == 1
                and all(any(frozenset((p.ref, q.ref)) in cands for q in t.pieces if not q.adopted) for p in t.pieces if p.adopted)
                for t in ts)
    check("'waiting on me' agrees with threads: each obligation is one thread plus what it adopted from its candidates",
          agree, True)
    out = lib.execute_tool("open_obligations", {})
    check("…and the tool still says 'no confirmation seen', never 'overdue'", ("overdue" in out.lower()), False)
finally:
    lib.record_nodes, lib._node_visible, lib._viewer, lib._local_now = real2

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
