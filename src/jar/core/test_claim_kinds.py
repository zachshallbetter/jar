#!/usr/bin/env python3
# gate: hermetic
"""#951 follow-up kinds: disputed (→ contested), not_mine, handled_by. handled_by names a household member, stored as
their person id, never free text. The audience is the speaker's choice under decision (b); naming someone never shows
them a private claim nor widens it to them (they see it only as any adult would, when the speaker shared it with the
adults); a kid named never sees it. disputed and not_mine stay on the speaker's view unless shared, and another adult's
shared "not mine" never closes the bill for anyone else. Matrix: speaker (Robin) × named adult (Jordan) × other adult
(Alex) × kid (Ava, and Leo when named) × the shared screen. Fictional people; temporary stores.
Run: python3 scripts/test_claim_kinds.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS

_TMP = Path(tempfile.mkdtemp(prefix="titan-claim-kinds-"))
for k, v in {"MAIL_BILLS_DB": "bills.db", "FENCES_DB": "fences.db", "HOUSEHOLD_DB": "household.db",
             "HOUSEHOLD_PREFS": "household.json", "LINKS_DB": "links.db", "LINK_SALT_FILE": "salt",
             "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
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
NAMES = {"alex": "Alex Quarry", "robin": "Robin Quarry", "jordan": "Jordan Quarry", "ava": "Ava Quarry",
         "leo": "Leo Quarry"}
WATER = bill("b-water", "Hale Water", 48.20, "2026-10-20", "2026-09-30", "alex", "household")   # a household bill
POWER = bill("b-power", "Hale Power", 90.00, "2026-10-22", "2026-10-01", "alex", "adults")       # the adults' bill
PHONE = bill("b-phone", "Harbor Mobile", 61.00, "2026-10-24", "2026-10-02", "robin", "private")  # Robin's own
RECORDS = [WATER, POWER, PHONE]
VIEWERS = {"robin": ("robin", "adult"), "jordan": ("jordan", "adult"), "alex": ("alex", "admin"),
           "ava": ("ava", "kid"), "leo": ("leo", "kid"), "screen": (None, "household")}
EVERYONE_ELSE = ("jordan", "alex", "ava", "leo", "screen")


def threads_for(viewer):
    pid, role = VIEWERS[viewer]
    return O.threads(RECORDS, TODAY, B.statements_for(pid, role, NAMES))


def thread(viewer, vendor):
    return next(t for t in threads_for(viewer) if t.vendor == vendor)


def said(viewer, vendor):
    """What the viewer's view says about the bill: (state, who handles it, the line)."""
    t = thread(viewer, vendor)
    return t.state, t.handled_by, O.line(t)


def words(viewer):
    return B.statements_for(*VIEWERS[viewer], NAMES)


def acct(vendor):
    return thread("robin", vendor).account


def untouched(viewer, vendor):
    """The viewer's view of the bill as if no one had said anything: due, no one handling it, no one's word."""
    st, by, ln = said(viewer, vendor)
    return st == "due" and by is None and " said " not in ln


print("── Robin, in Robin's own chat: Jordan is paying the water ──")
got = B.mark_said_paid("robin", threads_for("robin"), names=["Hale Water"], today=TODAY, kind="handled_by",
                       about="jordan")
check("marked, and the share question is raised once", (got["marked"], got["share_ask"]), (["Hale Water"], ["Hale Water"]))
st, by, ln = said("robin", "Hale Water")
check("speaker: still open (someone handling it isn't paid), Jordan named", (st in O.OPEN, by), (True, "Jordan"))
check("speaker: the line and chain say so", ("Jordan is handling it" in ln, "you said Jordan is handling it" in ln),
      (True, True))
for v in EVERYONE_ELSE:
    check(f"{v}: naming Jordan shows the claim to no one, Jordan included", (words(v), untouched(v, "Hale Water")),
          ([], True))

con = sqlite3.connect(os.environ["MAIL_BILLS_DB"])
row = con.execute("SELECT about, audience, kind FROM obligation_claims WHERE superseded_by IS NULL AND owner='robin' "
                  "AND account=?", (acct("Hale Water"),)).fetchone()
check("stored: the named person as their id, never a name or text; private until Robin says yes", row,
      ("jordan", "private", "handled_by"))

print("── Robin says yes: tell the other adults ──")
check("shared", B.share("robin", "adult", acct("Hale Water"), True, account_audience="household"), "shared")
st, by, ln = said("jordan", "Hale Water")
check("Jordan (an adult, so as any adult would): open, 'you're handling it', on Robin's word",
      (st in O.OPEN, by, "you're handling it" in ln, "Robin said you're handling it" in ln), (True, "you", True, True))
st, by, ln = said("alex", "Hale Water")
check("Alex: open, Jordan handling it, on Robin's word", (st in O.OPEN, by, "Robin said Jordan is handling it" in ln),
      (True, "Jordan", True))
for v in ("ava", "leo", "screen"):
    check(f"{v}: still nothing, even shared (adults only)", (words(v), untouched(v, "Hale Water")), ([], True))

print("── Robin changes it: Leo (a kid) is paying the water ──")
B.mark_said_paid("robin", threads_for("robin"), names=["Hale Water"], today=TODAY, kind="handled_by", about="leo")
check("a new word is private again until asked: Alex no longer sees Robin's earlier shared word",
      (words("alex"), untouched("alex", "Hale Water")), ([], True))
check("shared again", B.share("robin", "adult", acct("Hale Water"), True, account_audience="household"), "shared")
check("Alex: Leo handling it", said("alex", "Hale Water")[1], "Leo")
check("Leo, the kid named: never sees it via the naming", (words("leo"), untouched("leo", "Hale Water")), ([], True))
check("Jordan: now sees Leo handling it, no longer named", said("jordan", "Hale Water")[1], "Leo")
for v in ("ava", "screen"):
    check(f"{v}: still nothing", words(v), [])
check("taken back: theirs alone again", B.take_back("robin", acct("Hale Water")), True)
for v in EVERYONE_ELSE:
    check(f"{v}: after taking back, untouched", untouched(v, "Hale Water"), True)

print("── Robin's own account: Jordan is paying the phone ──")
got = B.mark_said_paid("robin", threads_for("robin"), names=["Harbor Mobile"], today=TODAY, kind="handled_by",
                       about="jordan")
check("marked, never asked to share (Robin's own)", (got["marked"], got["share_ask"]), (["Harbor Mobile"], []))
check("a yes anyway is refused", B.share("robin", "adult", acct("Harbor Mobile"), True, account_audience="private"),
      "not_asked")
check("speaker: Jordan handling it", said("robin", "Harbor Mobile")[1], "Jordan")
# (the bill itself is fenced upstream by _node_visible; here: the word on it never reaches Jordan)
check("Jordan never gets the word on Robin's own bill", (words("jordan"), said("jordan", "Harbor Mobile")[1]), ([], None))

print("── Robin disputes the power bill ──")
B.mark_said_paid("robin", threads_for("robin"), names=["Hale Power"], today=TODAY, kind="disputed")
st, by, ln = said("robin", "Hale Power")
check("speaker: contested (one question, never a guess), chain says it", (st, "you said it's disputed" in ln),
      ("contested", True))
for v in EVERYONE_ELSE:
    check(f"{v}: unshared, the dispute stays on Robin's view", untouched(v, "Hale Power") if v not in ("ava", "leo", "screen")
          else words(v) == [], True)
B.share("robin", "adult", acct("Hale Power"), True, account_audience="adults")
check("shared: the other adults see it contested, on Robin's word",
      [(said(v, "Hale Power")[0], "Robin said it's disputed" in said(v, "Hale Power")[2]) for v in ("alex", "jordan")],
      [("contested", True)] * 2)
for v in ("ava", "leo", "screen"):
    check(f"{v}: nothing", words(v), [])
B.take_back("robin", acct("Hale Power"))

print("── Robin: the water isn't mine ──")
B.mark_said_paid("robin", threads_for("robin"), names=["Hale Water"], today=TODAY, kind="not_mine")
st, by, ln = said("robin", "Hale Water")
check("speaker: not theirs to pay (no longer open for Robin), in the speaker's words",
      (st, st in O.OPEN, "you said it isn't yours" in ln, O.SAID[st]), ("not_mine", False, True, "not yours to pay, you said"))
check("speaker: no earlier handled_by lingers", by, None)
for v in ("jordan", "alex"):
    check(f"{v}: unshared, untouched", untouched(v, "Hale Water"), True)
B.share("robin", "adult", acct("Hale Water"), True, account_audience="household")
for v in ("jordan", "alex"):
    st, by, ln = said(v, "Hale Water")
    check(f"{v}: shared, Robin's 'not mine' is a note, never closes the bill for them",
          (st, "Robin said it isn't theirs" in ln), ("due", True))
for v in ("ava", "leo", "screen"):
    check(f"{v}: nothing", words(v), [])

print("── a kid's word is never widened; kinds are checked ──")
check("a kid's share is never asked", B.share("ava", "kid", acct("Hale Water"), True, account_audience="household"),
      "not_asked")
for bad, kw in (("handled_by", {}), ("maybe", {}), ("handled_by", {"about": ""})):
    try:
        B.say_kind("robin", acct("Hale Water"), bad, TODAY, **kw)
        check(f"say_kind refuses {bad} {kw}", "accepted", "refused")
    except ValueError:
        check(f"say_kind refuses {bad} {kw}", "refused", "refused")
check("about is kept only for handled_by", con.execute(
    "SELECT COUNT(*) FROM obligation_claims WHERE kind<>'handled_by' AND about IS NOT NULL").fetchone()[0], 0)
check("paid-or-not statements() stays the older shape (only paid / not_paid kinds)",
      all(set(s) == {"account", "date", "paid"} for s in B.statements("robin")), True)

print("── the tool: handled_by is a household member, never free text ──")
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402

PEOPLE = {"jordan": {"id": "jordan", "name": "Jordan Quarry"}, "leo": {"id": "leo", "name": "Leo Quarry"}}
hh.get_store = lambda: NS(person_named=lambda n: PEOPLE.get(str(n).strip().split(" ")[0].lower()))
lib._viewer = lambda: ("robin", "adult")
lib.record_nodes = lambda t: ([WATER, POWER, PHONE] if t == "bill" else [], None)
lib._node_visible = lambda n, pid, role: True
lib._obligation_threads = lambda recs, today, pid, role: O.threads(recs, TODAY, B.statements_for(pid, role, NAMES))
out = lib._mark_bills_paid({"bills": "Hale Power", "kind": "handled_by", "handled_by": "jordan"})
check("named by name, stored by id", (("Jordan handling it" in out), con.execute(
    "SELECT about FROM obligation_claims WHERE superseded_by IS NULL AND owner='robin' AND account=?",
    (acct("Hale Power"),)).fetchone()[0]), (True, "jordan"))
check("…and the ask to share names the handling", "handled by Jordan?" in out, True)
before = con.execute("SELECT COUNT(*) FROM obligation_claims").fetchone()[0]
out = lib._mark_bills_paid({"bills": "Hale Power", "kind": "handled_by", "handled_by": "the landlord"})
check("someone outside the household: asked about, nothing stored",
      (out.startswith("Ask them who"), con.execute("SELECT COUNT(*) FROM obligation_claims").fetchone()[0]),
      (True, before))
check("a bad kind is an error", lib._mark_bills_paid({"bills": "Hale Power", "kind": "forgiven"}).startswith("Error"), True)
check("not_mine through the tool", "Noted as not theirs to pay" in lib._mark_bills_paid({"bills": "Hale Power",
                                                                                          "kind": "not mine"}), True)
lib._viewer = lambda: ("ava", "kid")
check("a kid can't record a word through the tool", lib._mark_bills_paid({"bills": "Hale Water", "kind": "disputed"}),
      "Bills are recorded for the person whose they are, from their own device.")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
