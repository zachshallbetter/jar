#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Server mail through the derivation passes (titan-node#955 step 6, gaps 1 and 2): triage, then bills and suggestions,
inside mail_server.derive() while the body is in memory. Only personal and action envelopes may reach the KB; digest
categories are held as the Mac's are; a payment confirmation reaches bills whatever triage said (#955); bills read the
original, masked, never a summary (#954); nothing waits on disk. MAIL_SERVER_PASSES=1, off otherwise.
Fake mailbox, stub model, fictional mail.
Run: python3 scripts/test_mail_server_passes.py
"""
from __future__ import annotations

import json
import time
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mailpasses-"))
os.environ.update({"MAIL_SERVER": "1", "HOUSEHOLD_DB": str(_TMP / "household.db"), "LINKS_DB": str(_TMP / "links.db"),
                   "LINK_SALT_FILE": str(_TMP / "link_salt"), "MAIL_TRIAGE_DB": str(_TMP / "triage.db"),
                   "MAIL_BILLS_DB": str(_TMP / "bills.db"), "MAIL_SUGGESTIONS_DB": str(_TMP / "sugg.db")})
os.environ.pop("MAIL_SERVER_PASSES", None)
sys.path.insert(0, str(Path(__file__).parent))

import mail_server as M  # noqa: E402
import mail_server_passes as P  # noqa: E402
import mail_triage  # noqa: E402

M.PASSES_DAYS = 36500   # the fixtures are dated 10-01: inside the passes' window, whenever this runs (its own test below)

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def mail(mid, frm, subject, body):
    return (f"Message-ID: {mid}\r\nFrom: {frm}\r\nSubject: {subject}\r\nDate: Thu, 01 Oct 2026 09:00:00 -0700\r\n"
            f"Content-Type: text/plain\r\n\r\n{body}\r\n").encode()


MAILS = {
    1: ("<p1@mail.example>", "Robin <robin@mail.example>", "Dinner Friday?", "Want to do dinner Friday at 7?", "personal"),
    2: ("<a1@school.example>", "Maple School <office@school.example>", "Form due Monday",
        "Please return Ava's field trip form by Monday, October 12.", "action"),
    3: ("<r1@halewater.example>", "Hale Water <billing@halewater.example>", "Your receipt",
        "Receipt: payment of $48.20 received. Thank you.", "record"),
    4: ("<n1@carrier.example>", "Hale Wireless <care@carrier.example>", "Account update",
        "Your payment of $120.00 was received on October 1. Device IMEI: 356938035643809.", "notification"),
    5: ("<m1@shop.example>", "Shop <deals@shop.example>", "Weekly deals", "Big savings this week only.", "marketing"),
    6: ("<c1@login.example>", "Login <x@login.example>", "Hello", "Your verification code is 4321.", "code"),
}


class Conn:
    def fetch(self, uids, what):
        return {u: mail(*MAILS[u][:4]) for u in uids if u in MAILS}


def setup(db):
    con = M._db(db)
    for uid, (mid, frm, subj, _, _) in MAILS.items():
        con.execute("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, thread_id, "
                    "sender, subject, sent_at) VALUES ('icloud','INBOX',1,?,'alex',?,?,?,?,?,'2026-10-01T16:00:00Z')",
                    (uid, M.item_id("alex", "icloud", mid), mid, mid, frm, subj))
    con.commit()
    return con


ACCT = {"name": "icloud", "owner": "alex"}


def llm(prompt):
    for _, _, subj, _, cat in MAILS.values():
        if f"{subj}" in prompt:
            return json.dumps({"category": cat})
    return "{}"


def chat(prompt, url):
    return {"summary": "A mail."}, "fake"


class NoFences:
    def touches(self, text):
        return []

    def subject_in(self, text):
        return None

    def carry(self, *a):
        pass


def run(db, *, kb, passes):
    con = setup(db)
    rows = con.execute("SELECT * FROM messages ORDER BY uid").fetchall()
    writes = []
    n = M.derive(con, Conn(), ACCT, "INBOX", rows, kb=kb, chat=chat, fences=NoFences(),
                 write=lambda sid, title, text, st, md: writes.append(title), observe=lambda *a: [], passes=passes)
    out = {r["subject"]: dict(r) for r in con.execute("SELECT * FROM messages")}
    con.close()
    return n, out, writes


print("── off: as before ──")
n, out, writes = run(_TMP / "off.db", kb=True, passes={"llm": llm, "mac_ids": set()})
check("every non-code mail enveloped and (KB on) written; no category", (n, len(writes), {v["category"] for v in out.values()}),
      (5, 5, {None}))

print("\n── on: triage first ──")
os.environ["MAIL_SERVER_PASSES"] = "1"
BILLS, SUGG, REFS = [], [], []
hooks = {"llm": llm, "mac_ids": set(), "bills": lambda items: (BILLS.append([(i["title"], i["category"], i["text"]) for i in items]),
                                             REFS.extend(items)),
         "suggestions": lambda items: SUGG.append([(i["title"], i["category"], "ics" in i and "sender_known" in i)
                                                   for i in items])}
n, out, writes = run(_TMP / "on.db", kb=True, passes=hooks)
check("each mail's category", {k: v["category"] for k, v in out.items() if v["category"]},
      {"Dinner Friday?": "personal", "Form due Monday": "action", "Your receipt": "record", "Account update": "notification",
       "Weekly deals": "marketing"})
check("the code mail (the rule's) keeps nothing", (out["(a sign-in or verification code)"]["envelope"],
                                                   out["(a sign-in or verification code)"]["code"]), (None, 1))
check("only personal and action envelopes reach the KB", sorted(writes), ["Dinner Friday?", "Form due Monday"])
check("…the rest are enveloped locally", sorted(k for k, v in out.items() if v["envelope"] and not v["kb"]),
      ["Account update", "Weekly deals", "Your receipt"])
bills = BILLS[-1]
check("bills get every non-code mail, the notification's payment confirmation included",
      sorted((t, c) for t, c, _ in bills), sorted([("Dinner Friday?", "personal"), ("Form due Monday", "action"),
                                                    ("Your receipt", "record"), ("Account update", "notification"),
                                                    ("Weekly deals", "marketing")]))
pay = next(x for t, _, x in bills if t == "Account update")
check("…read from the original (#954), masked (#979)", ("$120.00 was received" in pay, "356938035643809" in pay), (True, False))
check("…and the batch's texts are emptied once the passes are done", (len(REFS) > 0, {i["text"] for i in REFS}), (True, {""}))
check("suggestions get what a person reads plus notifications, and marketing for the rule's check (10-09)",
      sorted(c for _, c, _f in SUGG[-1]), ["action", "marketing", "notification", "personal", "record"])
check("…each tagged with its invite and known-sender flags for the rule", all(f for *_, f in SUGG[-1]), True)
q = sqlite3.connect(os.environ["MAIL_TRIAGE_DB"])
held = {r[0]: r[1] for r in q.execute("SELECT category, text FROM queue")}
q.close()
check("the digest categories are held as the Mac's are (for the weekly digest, 0600), masked",
      (sorted(held), any("356938035643809" in (t or "") for t in held.values())), (["marketing", "notification"], False))

print("\n── the owner's keep rule beats triage: a kept notification reaches their KB, never held back (#955, 10-10) ──")
q = sqlite3.connect(os.environ["MAIL_TRIAGE_DB"])
q.execute("DELETE FROM queue")
q.commit()
q.close()
kdb = _TMP / "keep.db"
con = setup(kdb)
rule = {"alex": {"words": {"account update"}, "threads": set()}}
threads = M.keep_threads(con, "icloud", "alex", rule)
check("the rule's phrase picks its thread (and only it)", len(threads), 1)
check("…another person's rule never applies to this mailbox", M.keep_threads(con, "icloud", "alex", {"robin": rule["alex"]}), {})
rows = con.execute("SELECT * FROM messages ORDER BY uid").fetchall()
kw = []
M.derive(con, Conn(), ACCT, "INBOX", rows, keep=threads, kb=True, chat=chat, fences=NoFences(),
         write=lambda sid, title, text, st, md: kw.append(title), observe=lambda *a: [], passes=hooks)
out_k = {r["subject"]: dict(r) for r in con.execute("SELECT * FROM messages")}
con.close()
check("the kept notification reaches the owner's KB (its category kept as triage said)",
      (out_k["Account update"]["kb"], out_k["Account update"]["category"], "Account update" in kw), (1, "notification", True))
check("…the unkept marketing mail still doesn't", (out_k["Weekly deals"]["kb"], "Weekly deals" in kw), (0, False))
q = sqlite3.connect(os.environ["MAIL_TRIAGE_DB"])
held_k = sorted(r[0] for r in q.execute("SELECT category FROM queue"))
q.close()
check("…and only the unkept one is held for the digest", held_k, ["marketing"])
MAILS[7] = ("<n2@carrier.example>", "Hale Wireless <no-reply@carrier.example>", "Account update reminder",
            "Your payment of $120.00 is due.", "notification")
q = sqlite3.connect(os.environ["MAIL_TRIAGE_DB"])
q.execute("DELETE FROM queue")
q.commit()
q.close()
con = setup(_TMP / "keep2.db")
threads = M.keep_threads(con, "icloud", "alex", rule)
rows = con.execute("SELECT * FROM messages ORDER BY uid").fetchall()
kw = []
M.derive(con, Conn(), ACCT, "INBOX", rows, keep=threads, kb=True, chat=chat, fences=NoFences(),
         write=lambda sid, title, text, st, md: kw.append(title), observe=lambda *a: [], passes=hooks)
out_m = {r["subject"]: dict(r) for r in con.execute("SELECT * FROM messages")}
con.close()
q = sqlite3.connect(os.environ["MAIL_TRIAGE_DB"])
held_m = sorted(r[0] for r in q.execute("SELECT category FROM queue"))
q.close()
del MAILS[7]
check("a phrase match from a no-reply sender is triaged as before: notification, not the KB, held for the digest",
      (out_m["Account update reminder"]["category"], out_m["Account update reminder"]["kb"],
       "Account update reminder" in kw, held_m.count("notification")), ("notification", 0, False, 1))
check("…while the person's own match in the same run is still kept", out_m["Account update"]["kb"], 1)

print("\n── a kept thread files under its rule's project and facet: filing only, still the owner's alone (#955) ──")
sdb = _TMP / "scope.db"
con = setup(sdb)
scoped = M.keep_threads(con, "icloud", "alex", {"alex": [{"words": {"account update"}, "threads": set(),
                                                          "scope": {"project": "Harbor", "facet": "work"}}]})
check("keep_threads gives each held thread its rule's scope", list(scoped.values()), [{"project": "Harbor", "facet": "work"}])
rows = con.execute("SELECT * FROM messages ORDER BY uid").fetchall()
mds: dict[str, dict] = {}
asked: list[tuple] = []


def resolve(owner, name):
    asked.append((owner, name))
    return "alex-bubble:harbor" if name == "Harbor" else None


M.derive(con, Conn(), ACCT, "INBOX", rows, keep=scoped, kb=True, chat=chat, fences=NoFences(), resolve=resolve,
         write=lambda sid, title, text, st, md: mds.__setitem__(title, md), observe=lambda *a: [], passes=hooks)
con.close()
md = mds.get("Account update") or {}
check("the kept thread's KB entry: facet work, the owner's project entity (resolved by name, as its owner)",
      (md.get("facet"), md.get("project"), asked), ("work", "alex-bubble:harbor", [("alex", "Harbor")]))
check("…still private and its owner's alone", (md.get("audience"), md.get("owner")), ("private", "alex"))
other = mds.get("Dinner Friday?") or {}
check("an unkept thread carries no facet or project", ("facet" in other, "project" in other), (False, False))
con = setup(_TMP / "scope2.db")
rows = con.execute("SELECT * FROM messages ORDER BY uid").fetchall()
mds2: dict[str, dict] = {}
M.derive(con, Conn(), ACCT, "INBOX", rows, keep=scoped and {k: {"project": "Nowhere", "facet": "work"} for k in scoped},
         kb=True, chat=chat, fences=NoFences(), resolve=resolve,
         write=lambda sid, title, text, st, md: mds2.__setitem__(title, md), observe=lambda *a: [], passes=hooks)
con.close()
md2 = mds2.get("Account update") or {}
check("no such project entity yet: the facet still files it, no project is named", (md2.get("facet"), "project" in md2),
      ("work", False))
check("thread_scope: a keep rule's scope first, else the account's (KB's mapping)",
      (M.thread_scope({"project_id": "alex-b:dock", "facet": "work"}, {"t1": {"project": "Harbor"}}, "t1"),
       M.thread_scope({"project_id": "alex-b:dock", "facet": "work"}, {}, "t2"), M.thread_scope({}, {}, "t3")),
      ({"project": "Harbor"}, {"project_id": "alex-b:dock", "facet": "work"}, {}))
print("── an account's mail files by KB's mapping (#1490): its owner's own, by titan's label, never an address ──")
uses = lambda owner: {"alex": {"icloud": {"facet": "Work", "project": "alex-b:dock"}, "other": {"facet": "personal"}}}.get(owner, {})
check("the account's own mapping: its facet and project entity", M.account_scope({"name": "icloud", "owner": "alex"}, uses),
      {"facet": "work", "project_id": "alex-b:dock"})
check("…an unmapped account files nothing", M.account_scope({"name": "gmail", "owner": "alex"}, uses), {})
check("…looked up by the mapping's label form (lowercased, spaces collapsed)",
      M.account_scope({"name": "  ICloud ", "owner": "alex"}, uses), {"facet": "work", "project_id": "alex-b:dock"})
check("…another owner's mapping never applies", M.account_scope({"name": "icloud", "owner": "robin"}, uses), {})
check("…no reader yet (world.mail_uses not shipped): nothing", M.account_scope({"name": "icloud", "owner": "alex"},
                                                                              lambda o: (_ for _ in ()).throw(AttributeError())), {})
con = setup(_TMP / "scope3.db")
rows = con.execute("SELECT * FROM messages ORDER BY uid").fetchall()
mds3: dict[str, dict] = {}
M.derive(con, Conn(), ACCT, "INBOX", rows, kb=True, chat=chat, fences=NoFences(), uses=uses,
         write=lambda sid, title, text, st, md: mds3.__setitem__(title, md), observe=lambda *a: [], passes=hooks)
con.close()
check("a mapped account: each KB thread carries its facet and project, still private and the owner's",
      sorted({(m.get("facet"), m.get("project"), m.get("audience"), m.get("owner")) for m in mds3.values()}),
      [("work", "alex-b:dock", "private", "alex")])

print("\n── a rule covers existing mail: derived before the rule, re-derived under it once (#955, 10-10) ──")
MAILS[8] = ("<n3@carrier.example>", "Hale Wireless <notifications@carrier.example>", "Account update notice",
            "Your plan renews soon.", "notification")
q = sqlite3.connect(os.environ["MAIL_TRIAGE_DB"])
q.execute("DELETE FROM queue")
q.commit()
q.close()
edb = _TMP / "existing.db"
con = setup(edb)
con.execute("INSERT INTO folders VALUES ('icloud','INBOX',1,0)")
con.commit()
rows = con.execute("SELECT * FROM messages ORDER BY uid").fetchall()
M.derive(con, Conn(), ACCT, "INBOX", rows, kb=True, chat=chat, fences=NoFences(), write=lambda *a: None,
         observe=lambda *a: [], passes=hooks)   # before any rule: the notifications are triaged out of the KB
con.commit()
before = {r["subject"]: (r["kb"], r["derived_at"] is not None) for r in con.execute("SELECT * FROM messages")}
check("before the rule: the person's notification and the no-reply one derived, both kb=0",
      (before["Account update"], before["Account update notice"]), ((0, True), (0, True)))
rule = {"alex": [{"words": {"account update"}, "threads": set(), "scope": {"project": "Harbor", "facet": "work"}}]}
held_t = M.keep_threads(con, "icloud", "alex", rule)
sel = M._newest_first(con, "icloud", ["INBOX"], "2026-09-01T00:00:00Z", 50, held_t)
picked = sorted(r["subject"] for rs in sel.values() for r in rs if r["derived_at"] is not None)
check("the rule's next run picks the person's derived kb=0 mail, never the no-reply's", picked, ["Account update"])
mds_e: dict[str, dict] = {}
bills_before = len(BILLS)
M.derive(con, Conn(), ACCT, "INBOX", [r for rs in sel.values() for r in rs], keep=held_t, kb=True, chat=chat,
         fences=NoFences(), resolve=lambda o, n: "alex-b:harbor" if n == "Harbor" else None,
         write=lambda sid, title, text, st, md: mds_e.__setitem__(title, md), observe=lambda *a: [], passes=hooks)
con.commit()
after = {r["subject"]: dict(r) for r in con.execute("SELECT * FROM messages")}
check("after: the person's mail is kept (kb=1, passes keep) and its KB thread files under the rule's project",
      (after["Account update"]["kb"], after["Account update"]["passes"], mds_e.get("Account update", {}).get("project"),
       mds_e.get("Account update", {}).get("facet")), (1, "keep", "alex-b:harbor", "work"))
check("…its bills and cards don't run again (they ran when it was first derived)", len(BILLS), bills_before)
check("…the no-reply mail stays exactly as it was", (after["Account update notice"]["kb"], after["Account update notice"]["passes"]),
      (0, "ran"))
again = M._newest_first(con, "icloud", ["INBOX"], "2026-09-01T00:00:00Z", 50, held_t)
check("the next run picks nothing again: never repeated (a no-reply sender is filtered before any fetch)", sorted(r["subject"] for rs in again.values() for r in rs
                                                                if r["derived_at"] is not None), [])
check("…every kept-thread mail the run touched is marked considered", after["Account update"]["kept_at"] is not None, True)
con.close()
del MAILS[8]

print("\n── the model says code; the model is down ──")
MAILS[7] = ("<c2@bank.example>", "Bank <x@bank.example>", "Sign in", "Tap the button to sign in to your account.", "code")
n, out, writes = run(_TMP / "code.db", kb=True, passes={**hooks, "llm": llm})
check("a mail the model calls code keeps nothing", (out["(a sign-in or verification code)"]["code"], "Sign in" in out), (1, False))
del MAILS[7]


def down(prompt):
    raise OSError("no model")


BILLS.clear()
n, out, writes = run(_TMP / "down.db", kb=True, passes={**hooks, "llm": down})
check("no model: no category, nothing to the KB, envelopes local", ({v["category"] for v in out.values()}, writes), ({None}, []))
check("…a payment still reaches bills", any(t == "Account update" for t, _, _ in BILLS[-1]), True)

print("\n── the real bills pass on server mail ──")
calls = []


def request(rt, path, method="GET", body=None):
    calls.append((method, path, body))
    return {"nodes": []} if method == "GET" else {}


def bill_llm(prompt):
    if "Hale Wireless" in prompt or "$120.00" in prompt:
        return json.dumps({"type": "receipt", "vendor": "Hale Wireless", "amount": -120.0, "paid_date": "2026-10-01",
                           "payment_status": "paid"})
    return json.dumps({"type": "none"})


item = {"item_id": "cx-mailsrv-x1", "owner": "alex", "title": "Account update", "category": "notification",
        "text": "Your payment of $120.00 was received on October 1.", "stamp": "server:2026-10-01T16:00:00Z",
        "md": {"owner": "alex", "audience": "private", "observed_at": "2026-10-01T16:00:00Z", "source": "mail"}}
got = P.run_bills([item], llm=bill_llm, request=request, db=_TMP / "bills2.db")
posts = [b for m, p, b in calls if m == "POST"]
check("filed from the original, the owner's, provenance to the server item",
      (got["candidates"], len(posts), posts[0]["data"].get("vendor") if posts else None,
       posts[0]["data"]["provenance"]["source_id"] if posts else None, posts[0]["data"]["provenance"]["owner"] if posts else None),
      (1, 1, "Hale Wireless", "cx-mailsrv-x1", "alex"))
calls.clear()
P.run_bills([item], llm=bill_llm, request=request, db=_TMP / "bills2.db")
check("the same mail again: never refiled", [b for m, p, b in calls if m == "POST"], [])

print("\n── the cut-over's bounds (#955, 10-09): the Mac's mail, the window, an invite only its calendar part shows ──")
from email.message import EmailMessage  # noqa: E402

ICS = ("BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:7c1e-55@calendar.example\r\n"
       "SUMMARY:Budget review\r\nDTSTART;TZID=America/Los_Angeles:20261012T140000\r\n"
       "DTEND;TZID=America/Los_Angeles:20261012T150000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def cal_mail(mid):
    m = EmailMessage()
    m["Message-ID"], m["From"], m["Subject"] = mid, "Jordan <jordan@work.example>", "Budget review"
    m["Date"] = "Thu, 01 Oct 2026 09:00:00 -0700"
    m.set_content("Looking forward to it.")
    m.add_attachment(ICS.encode(), maintype="text", subtype="calendar", filename="invite.ics")
    return m.as_bytes()


check("calendar_text: the .ics part body_text leaves out", (("BEGIN:VEVENT" in M.calendar_text(cal_mail("<k1@w.example>"))),
      ("BEGIN:VEVENT" in M.body_text(cal_mail("<k1@w.example>")))), (True, False))
check("…a plain mail has none; a broken one never raises", (M.calendar_text(mail("<z@x>", "a", "b", "c")), M.calendar_text(b"\xff\x00")),
      ("", ""))

MAILS[7] = ("<k1@work.example>", "Jordan <jordan@work.example>", "Budget review", "", "action")


class CalConn(Conn):
    def fetch(self, uids, what):
        out = super().fetch([u for u in uids if u != 7], what)
        if 7 in uids:
            out[7] = cal_mail(MAILS[7][0])
        return out


def run_cut(db, mac_ids, *, days=None, sent=None):
    con = setup(db)
    if sent:
        con.execute("UPDATE messages SET sent_at=?", (sent,))
    rows = con.execute("SELECT * FROM messages ORDER BY uid").fetchall()
    seen: list = []
    old = M.PASSES_DAYS
    M.PASSES_DAYS = old if days is None else days
    try:
        M.derive(con, CalConn(), ACCT, "INBOX", rows, kb=False, chat=chat, fences=NoFences(),
                 write=lambda *a: None, observe=lambda *a: [],
                 passes={"llm": llm, "mac_ids": mac_ids, "bills": lambda items: seen.extend(i["title"] for i in items),
                         "suggestions": lambda items: seen.extend(f"S:{i['title']}" for i in items)})
    finally:
        M.PASSES_DAYS = old
    cats = {r["subject"]: r["category"] for r in con.execute("SELECT subject, category FROM messages")}
    con.close()
    return seen, cats


ALL_MAC = {M_ for M_ in (__import__("mail_compare")._norm(v[0]) for v in MAILS.values())}
seen, cats = run_cut(_TMP / "mac.db", ALL_MAC)
check("every mail on the Mac too: the server's passes skip them (no second card or bill), no category kept",
      (sorted(set(seen) - {"Budget review", "S:Budget review"}), cats["Dinner Friday?"], cats["Your receipt"]), ([], None, None))
check("…except an invite only its calendar part shows (the Mac sends plain text): its suggestion runs",
      "S:Budget review" in seen, True)
only = ALL_MAC - {__import__("mail_compare")._norm("<r1@halewater.example>")}
seen, _ = run_cut(_TMP / "srv-only.db", only)
check("a mail the Mac never synced: the server's passes take it (the receipt reaches bills)", "Your receipt" in seen, True)
seen, cats = run_cut(_TMP / "window.db", set(), days=14, sent="2026-08-01T16:00:00Z")
check("mail older than MAIL_SERVER_PASSES_DAYS: derived as before, no pass, no card", (seen, cats["Dinner Friday?"]), ([], None))
seen, _ = run_cut(_TMP / "window2.db", set(), days=14,
                  sent=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2 * 86400)))
check("…two days old: inside the window, the passes run", "Your receipt" in seen, True)
seen, _ = run_cut(_TMP / "nole.db", None)
check("the Mac's ledger unreadable (None): no server pass this run, never a duplicate", seen, [])
del MAILS[7]

print("\n── why the passes ran or not, per mail (passes-status reads it) ──")


def whys(name, mac_ids, *, on=True, days=None, sent=None):
    """derive() over the fixtures, then each mail's recorded reason (the harness above doesn't commit, so read here)."""
    prev = os.environ.get("MAIL_SERVER_PASSES")
    os.environ["MAIL_SERVER_PASSES"] = "1" if on else "0"
    con = setup(_TMP / name)
    if sent:
        con.execute("UPDATE messages SET sent_at=?", (sent,))
    rows = con.execute("SELECT * FROM messages ORDER BY uid").fetchall()
    old = M.PASSES_DAYS
    M.PASSES_DAYS = old if days is None else days
    try:
        M.derive(con, CalConn(), ACCT, "INBOX", rows, kb=False, chat=chat, fences=NoFences(), write=lambda *a: None,
                 observe=lambda *a: [], passes={"llm": llm, "mac_ids": mac_ids, "bills": lambda items: None,
                                                "suggestions": lambda items: None})
        return {r["uid"]: r["passes"] for r in con.execute("SELECT uid, passes FROM messages")}
    finally:
        M.PASSES_DAYS = old
        con.close()
        if prev is None:
            os.environ.pop("MAIL_SERVER_PASSES", None)
        else:
            os.environ["MAIL_SERVER_PASSES"] = prev


CODE_UID = next(u for u, v in MAILS.items() if v[4] == "code")   # the rule's sign-in code mail
w = whys("why-off.db", set(), on=False)
check("off: every mail 'off', the rule's code mail 'code'", ({v for k, v in w.items() if k != CODE_UID}, w[CODE_UID]),
      ({"off"}, "code"))
w = whys("why-on.db", set())
check("on: every mail 'ran'", {v for k, v in w.items() if k != CODE_UID}, {"ran"})
w = whys("why-mac.db", ALL_MAC)
check("every mail on the Mac too: each 'mac'", {v for k, v in w.items() if k != CODE_UID}, {"mac"})
check("older than the window: 'old'", set(whys("why-old.db", set(), days=14, sent="2026-08-01T16:00:00Z").values()) - {"code"},
      {"old"})
check("the Mac's ledger unreadable: 'ledger'", set(whys("why-ledger.db", None).values()) - {"code"}, {"ledger"})

print("\n── a mail the Mac's passes have never goes to the KB from here (10-09) ──")
prev = os.environ.get("MAIL_SERVER_PASSES")
os.environ["MAIL_SERVER_PASSES"] = "1"
noop = {"bills": lambda items: None, "suggestions": lambda items: None}
_, out_m, writes_m = run(_TMP / "kb-mac.db", kb=True, passes={"llm": llm, "mac_ids": ALL_MAC, **noop})
check("KB on, every mail on the Mac: nothing written to the KB, untriaged or twice", writes_m, [])
check("…each recorded as the Mac's", {v["passes"] for v in out_m.values() if v["code"] == 0}, {"mac"})
_, _, writes_s = run(_TMP / "kb-srv.db", kb=True, passes={"llm": llm, "mac_ids": set(), **noop})
check("…while mail only the server has still reaches the KB by triage (personal and action)", sorted(writes_s),
      ["Dinner Friday?", "Form due Monday"])
if prev is None:
    os.environ.pop("MAIL_SERVER_PASSES", None)
else:
    os.environ["MAIL_SERVER_PASSES"] = prev

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
