#!/usr/bin/env python3
"""titan-node#293: bills and receipts from synced mail become records. Hermetic: the local model and the
record runtime are faked; the cases are the 10-01 dry run's real shapes."""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mail_bills as mb  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


def llm_says(**f):
    return lambda prompt: json.dumps(f)


# The prefilter: money words AND an amount; never a sale.
check("a receipt with an amount is a candidate", mb.is_candidate("Your receipt from Apple.", "Total $9.99"))
check("no amount: not a candidate", not mb.is_candidate("Your bill is ready", "Sign in to view it."))
check("a sale isn't a bill", not mb.is_candidate("20% off: sale ends tonight", "Order now, $19.99"))

# Code checks the model's facts.
f = mb.extract("Your receipt from Apple.", "Total: $30.30 charged Sep 16", llm_says(type="receipt", vendor="Apple", amount=-30.3,
               paid_date="2026-09-16", payment_status="paid"))
check("the sign is the mail's, not the model's (an ordinary receipt is positive)", f and f["amount"] == 30.3, str(f))
f = mb.extract("Credit note from Anthropic, PBC for invoice #II4", "Credit $218.00", llm_says(type="receipt", vendor="Anthropic, PBC", amount=218))
check("a credit note is negative", f and f["amount"] == -218.0, str(f))
f = mb.extract("Your receipt", "Total $12.26", llm_says(type="receipt", vendor="Vercel Inc.", amount=99.0))
check("an amount the mail doesn't show is dropped", f and "amount" not in f, str(f))
f = mb.extract("Payment FAILED", "Amount: $0.00", llm_says(type="bill", vendor="Water District", amount=0, payment_status="failed"))
check("$0.00 is never an amount", f and "amount" not in f and f["payment_status"] == "failed", str(f))
f = mb.extract("$21.80 payment to Vercel Inc. was unsuccessful", "$21.80", llm_says(type="receipt", vendor="Vercel Inc.", amount=21.8))
check("a failed payment is a bill, failed, even if the model says receipt",
      f and f["doc_type"] == "bill" and f["payment_status"] == "failed", str(f))
f = mb.extract("Order", "$17.43", llm_says(type="bill", vendor="Amazon", amount=17.43, payment_status="paid"))
check("a 'bill' that's paid is a receipt", f and f["doc_type"] == "receipt", str(f))
check("a statement with nothing to file is dropped",
      mb.extract("Ordered 1 item", "Arriving Friday $5.00", llm_says(type="statement", vendor="Amazon.com")) is None)
check("marketing is 'none'", mb.extract("Deals", "$5.00", llm_says(type="none")) is None)
# 10-10 (Zach): Apple's renewals were eleven bills "due Jan 1": a date the model gave that the mail never showed
f = mb.extract("Your subscription is renewing", "Harbor Plus renews for $4.99/month.",
               llm_says(type="bill", vendor="Apple", amount=4.99, due_date="2026-01-01", payment_status="due"))
check("a renewal notice is never a bill to pay: a statement, no status", f is not None and f["doc_type"] == "statement"
      and "payment_status" not in f, str(f))
check("…and a due date the mail doesn't show (a defaulted Jan 1) is dropped", f is not None and "due_date" not in f, str(f))
f = mb.extract("Your subscription renewal failed", "We couldn't charge $4.99 for Harbor Plus.",
               llm_says(type="bill", vendor="Apple", amount=4.99, payment_status="failed"))
check("…but a renewal that FAILED is a bill (it needs the person)", f is not None and f["doc_type"] == "bill"
      and f.get("payment_status") == "failed", str(f))
for shown, iso, keep in (("Due Oct 15", "2026-10-15", True), ("due October 15th", "2026-10-15", True),
                         ("due 15 October", "2026-10-15", True), ("due 10/15/2026", "2026-10-15", True),
                         ("due 2026-10-15", "2026-10-15", True), ("due soon", "2026-10-15", False),
                         ("due Oct 15", "2026-01-01", False)):
    f = mb.extract("Your bill is ready", f"Amount $40.00 {shown}.",
                   llm_says(type="bill", vendor="Tidewater Power", amount=40.0, due_date=iso, payment_status="due"))
    check(f"a due date the mail {'shows' if keep else 'does not show'} ({shown!r}, model {iso}) is {'kept' if keep else 'dropped'}",
          f is not None and (f.get("due_date") == iso) == keep, str(f))
f = mb.extract("Your receipt from Apple.", "Charged $9.99 on Sep 16.",
               llm_says(type="receipt", vendor="Apple", amount=9.99, paid_date="2026-09-16"))
check("a receipt's paid date the mail shows is kept", f is not None and f.get("paid_date") == "2026-09-16", str(f))
f = mb.extract("Bill", "Due $38.85 on 2026-10-02", llm_says(type="bill", vendor="Acme", amount=38.85, due_date="Oct 2",
               summary="Account 1234-5678-9012 due"))
check("a non-ISO date is dropped, identifiers redacted",
      f and "due_date" not in f and "1234-5678-9012" not in f.get("summary", ""), str(f))
check("broken JSON is nothing", mb.extract("Bill", "$5.00", lambda p: "sure! {not json") is None)

# The run: ids from the mail, private to its owner, no duplicates.
sources = [
    {"source_id": "cx-mail-1", "source_type": "connector:mail", "title": "Important: Payment due for 12006 E Lenora Dr.",
     "updated_at": "1", "metadata": {"owner": "zach"}},
    {"source_id": "cx-mail-2", "source_type": "connector:mail", "title": "Reminder: Charge from Colton Prettyman",
     "updated_at": "1", "metadata": {"owner": "zach"}},
    {"source_id": "cx-mail-3", "source_type": "connector:mail", "title": "Your receipt from Apple.", "updated_at": "1",
     "metadata": {"owner": "p_robin"}},
    {"source_id": "cx-mail-4", "source_type": "connector:mail", "title": "Your weekly newsletter", "updated_at": "1"},
    {"source_id": "note-1", "source_type": "note", "title": "Bill notes $5.00", "updated_at": "1"},
]
texts = {"cx-mail-1": "Rent payment due $2,950.00 on Oct 1", "cx-mail-2": "Charge of $2,950.00 due Oct 1",
         "cx-mail-3": "Total $9.99", "cx-mail-4": "Read our stories"}
answers = {"Lenora": dict(type="bill", vendor="TurboTenant", amount=2950, due_date="2026-10-01", payment_status="due"),
           "Colton": dict(type="bill", vendor="Colton Prettyman", amount=2950, due_date="2026-10-01", payment_status="due"),
           "Apple": dict(type="receipt", vendor="Apple", amount=9.99, paid_date="2026-09-16", payment_status="paid")}
calls: list[tuple] = []


def llm(prompt: str) -> str:
    return json.dumps(next(v for k, v in answers.items() if k in prompt))


def request(rt, path, method="GET", body=None):
    calls.append((method, path, body))
    if method == "GET":   # a bill filed by hand from a PDF earlier
        return {"nodes": [{"node_id": "bill-xfinity-abc", "data": {"vendor": "Xfinity", "amount": 161.16, "due_date": "2026-10-03"}}]}
    return {}


con = sqlite3.connect(":memory:")
st = mb.run(True, sources, texts.get, llm, request, con)
posts = [b for m, p, b in calls if m == "POST"]
check("the same rent from two mails is filed once", len([b for b in posts if b["data"].get("amount") == 2950]) == 1, str(posts))
check("only mail is read (not notes), and a newsletter isn't money", st["mails"] == 4 and st["candidates"] == 3, str(st))
apple = next(b for b in posts if b["data"]["vendor"] == "Apple")
check("the id comes from the mail item", apple["node_id"] == mb.node_id_for("cx-mail-3", "receipt"))
check("private to the mailbox's owner, provenance back to the mail",
      apple["data"]["provenance"] == {"source": "mail", "source_id": "cx-mail-3", "item_id": "cx-mail-3", "kind": "derived",
                                      "owner": "p_robin", "audience": "private", "extracted_by": "local"},
      str(apple["data"]["provenance"]))
calls.clear()
st = mb.run(True, sources, texts.get, llm, request, con)
check("a second run reads nothing again", not [c for c in calls if c[0] == "POST"] and st["unchanged"] == 4, str(st))
calls.clear()
mb.run(True, [{**sources[2], "updated_at": "2"}], texts.get, llm, request, con)
check("a changed mail is re-read and its record updated in place",
      [b["node_id"] for m, p, b in calls if m == "POST"] == [apple["node_id"]])
calls.clear()
# #390 (10-05): the envelope migration re-indexed mail; this saw them as changed and re-filed bills from the summaries.
mb.run(True, [{**sources[2], "updated_at": "3"}], lambda sid: "Apple sent a receipt.", llm, request, con,
       is_envelope=lambda sid: sid == "cx-mail-3")
check("an enveloped mail is never read again: its record keeps the first extraction",
      not [c for c in calls if c[0] in ("POST", "PATCH")])
check("…its new stamp is recorded, its outcome and record kept",
      con.execute("SELECT updated_at, outcome, node_id FROM seen WHERE mail_id='cx-mail-3'").fetchone()
      == ("3", "filed", apple["node_id"]))
calls.clear()
st = mb.run(True, [{**sources[2], "updated_at": "3"}], texts.get, llm, request, con, is_envelope=lambda sid: True)
check("…and the next run finds it unchanged", st["unchanged"] == 1 and not [c for c in calls if c[0] != "GET"], str(st))
con_e = sqlite3.connect(":memory:")
mb.run(True, [sources[2]], lambda sid: "Apple sent a receipt.", llm, request, con_e, is_envelope=lambda sid: True)
check("a mail first seen as an envelope is marked, never filed from its summary",
      con_e.execute("SELECT outcome, node_id FROM seen WHERE mail_id='cx-mail-3'").fetchone() == ("enveloped", None)
      and not [c for c in calls if c[0] in ("POST", "PATCH")])
calls.clear()
con2 = sqlite3.connect(":memory:")
mb.run(False, sources, texts.get, llm, request, con2)
check("a dry run writes nothing", not [c for c in calls if c[0] != "GET"])
check("a bill filed by hand isn't filed again",
      mb.duplicate_of({"doc_type": "bill", "vendor": "Xfinity", "amount": 161.16, "due_date": "2026-10-03"},
                      request("record", "/v1/nodes", "GET")["nodes"]) == "bill-xfinity-abc")
con3 = sqlite3.connect(":memory:")
full = [{"node_id": f"bill-x-{i}", "data": {"vendor": "V", "amount": 1.0, "due_date": "2026-01-01"}} for i in range(500)]
st = mb.run(False, [], texts.get, llm, lambda rt, path, method="GET", body=None: {"nodes": full if "bill" in path else []}, con3)
check("a type at the 500 cap is reported, not silently trusted", st["partial_types"] == ["bill"], str(st.get("partial_types")))
# Owner and date come from the sharing table and the ledger (the KB's list has no metadata).
import tempfile  # noqa: E402
with tempfile.TemporaryDirectory() as d:
    led = os.path.join(d, "ledger.db")
    lc = sqlite3.connect(led)
    lc.execute("CREATE TABLE items (source TEXT, source_id TEXT, item_id TEXT, observed_at TEXT)")
    lc.execute("INSERT INTO items VALUES ('mail', 'm1@x', 'cx-mail-a', '2026-09-29T17:30:33Z')")
    lc.commit()
    srcs = mb.with_meta([{"source_id": "cx-mail-a", "source_type": "connector:mail"},
                         {"source_id": "cx-mail-b", "source_type": "connector:mail"},
                         {"source_id": "n", "source_type": "note"}],
                        acl={"cx-mail-a": ("p_robin", "private")}, ledger=led)
check("the owner is the sharing table's (Robin's mail is hers)", srcs[0]["metadata"]["owner"] == "p_robin")
check("no row: the connector default, the admin", srcs[1]["metadata"]["owner"] == "zach")
check("the mail's date comes from the ledger", srcs[0]["metadata"]["observed_at"] == "2026-09-29T17:30:33Z"
      and srcs[1]["metadata"]["observed_at"] is None)
check("notes are left alone", "metadata" not in srcs[2])

# Failed-payment cards.
from datetime import date  # noqa: E402


def rec(nid, doc, vendor, amount=None, status=None, when=None, owner="zach", src="mail"):
    d = {"doc_type": doc, "vendor": vendor, "provenance": {"source": src, "owner": owner}}
    if amount is not None:
        d["amount"] = amount
    if status:
        d["payment_status"] = status
    if when:
        d["paid_date" if doc == "receipt" else "mail_date"] = when
    return {"node_id": nid, "node_type": doc, "data": d}


sent: list[dict] = []


def send(title, detail="", **kw):
    sent.append({"title": title, "detail": detail, **kw})


TODAY = date(2026, 10, 1)
cc = sqlite3.connect(":memory:")
fail = rec("bill-mail-v", "bill", "Vercel Inc.", 21.8, "failed", "2026-09-29")
out = mb.payment_cards([fail], cc, TODAY, False, send)
c = sent[-1] if sent else {}
check("a failed payment gets a card in plain words",
      out == ["Vercel Inc. payment of $21.80 didn't go through (Sep 29)"], str(out))
check("to the mailbox's owner only, urgent, keyed by the record, with a dismiss",
      c.get("to") == ["zach"] and c.get("urgency") == 2 and c.get("key") == "payfail-bill-mail-v"
      and c.get("actions") == [{"label": "I've handled it", "kind": "dismiss"}], str(c))
n = len(sent)
mb.payment_cards([fail], cc, TODAY, False, send)
check("a re-run never repeats it", len(sent) == n)
paid = rec("receipt-mail-v2", "receipt", "Vercel", 21.8, "paid", "2026-09-30")
out = mb.payment_cards([fail, paid], cc, TODAY, False, send)
check("a later receipt for the same amount resolves the same card",
      out == ["Resolved: Vercel Inc. payment of $21.80 went through (Sep 30)"] and sent[-1]["key"] == "payfail-bill-mail-v"
      and sent[-1]["urgency"] == 0, str(out))
n = len(sent)
mb.payment_cards([fail, paid], cc, TODAY, False, send)
check("and says so once", len(sent) == n)

# #1055: only the owner's own receipt settles their failed payment
cc = sqlite3.connect(":memory:")
sent.clear()
zfail = rec("bill-mail-h", "bill", "Hale Rentals", 1250.0, "failed", "2026-09-29")
rpaid = rec("receipt-mail-r", "receipt", "Hale Rentals", 1250.0, "paid", "2026-09-30", owner="p_robin")
mb.payment_cards([zfail, rpaid], cc, TODAY, False, send)
got = [(x["title"].split(" (")[0], x["to"]) for x in sent]
check("another person's receipt (same vendor and amount) doesn't settle it: the failed card still goes, to its owner",
      got == [("Hale Rentals payment of $1,250.00 didn't go through", ["zach"])], str(got))
mb.payment_cards([zfail, rpaid], cc, TODAY, False, send)
check("…and no 'Resolved' card carrying the other person's payment date", not any("Resolved" in x["title"] for x in sent))
zpaid = rec("receipt-mail-z", "receipt", "Hale Rentals", 1250.0, "paid", "2026-10-01")
mb.payment_cards([zfail, rpaid, zpaid], cc, TODAY, False, send)
check("the owner's own receipt does resolve it, with their own date",
      (sent[-1]["title"] if sent else "") == "Resolved: Hale Rentals payment of $1,250.00 went through (Oct 1)",
      sent[-1]["title"] if sent else "no card at all")

cc = sqlite3.connect(":memory:")
sent.clear()
mb.payment_cards([rec("bill-mail-a", "bill", "Amazon.com", 17.43, "failed", "2026-09-28")], cc, TODAY, True, send)
check("quiet hours hold it", not sent)
mb.payment_cards([rec("bill-mail-a", "bill", "Amazon.com", 17.43, "failed", "2026-09-28")], cc, TODAY, False, send)
check("and it goes out after", len(sent) == 1 and sent[0]["to"] == ["zach"])
sent.clear()
mb.payment_cards([rec("bill-mail-m", "bill", "Water District", None, "failed", "2026-09-28", owner="p_robin")],
                 cc, TODAY, False, send)
check("Robin's mail: the card is hers alone, and says no amount it doesn't know",
      sent and sent[0]["to"] == ["p_robin"] and sent[0]["title"] == "Water District payment didn't go through (Sep 28)",
      str(sent))
sent.clear()
mb.payment_cards([rec("bill-mail-o", "bill", "Old Co", 5.0, "failed", "2026-08-01")], cc, TODAY, False, send)
check("a failure older than two weeks isn't news", not sent)
mb.payment_cards([rec("bill-mail-r", "bill", "Warp", 21.82, "failed", "2026-09-20"),
                  rec("receipt-mail-r", "receipt", "Warp", 21.82, "paid", "2026-09-22")], cc, TODAY, False, send)
check("one already resolved when first seen gets no card", not sent)
mb.payment_cards([rec("bill-pdf-x", "bill", "Hand Co", 5.0, "failed", "2026-09-29", src="chat")], cc, TODAY, False, send)
check("only mail-filed failures (a hand-filed bill is the person's own news)", not sent)
mb.payment_cards([rec("bill-mail-d", "bill", "Due Co", 5.0, "due", "2026-09-29")], cc, TODAY, False, send)
check("a bill that's merely due gets no card", not sent)
check("a dry run (send=None) reports without sending or recording",
      mb.payment_cards([rec("bill-mail-z", "bill", "Zed", 3.0, "failed", "2026-09-30")], cc, TODAY, False, None)
      == ["Zed payment of $3.00 didn't go through (Sep 30)"]
      and not cc.execute("SELECT 1 FROM cards WHERE node_id='bill-mail-z'").fetchone())


# #554: scanned bills, from the OCR text the owner's Mac sent (fictional bills; a fake model).
SCAN_SRC = [
    {"source_id": "cx-scan-1", "source_type": "connector:scans", "title": "Scan: Riverside Water & Power",
     "updated_at": "1", "metadata": {"owner": "p_robin"}},
    {"source_id": "cx-scan-2", "source_type": "connector:scans", "title": "Scan: Riverside Water & Power (copy)",
     "updated_at": "1", "metadata": {"owner": "p_robin"}},
    {"source_id": "cx-scan-3", "source_type": "connector:scans", "title": "Scan: Maple Dental",
     "updated_at": "1", "metadata": {"owner": "zach"}},
    {"source_id": "cx-mail-9", "source_type": "connector:mail", "title": "Your Riverside Water & Power bill",
     "updated_at": "1", "metadata": {"owner": "p_robin"}},
]
SCAN_TEXT = {"cx-scan-1": "Riverside Water & Power. Amount due $142.37. Due 2026-10-21.",
             "cx-scan-2": "Riverside Water & Power. Amount due $142.37. Due 2026-10-21.",
             "cx-mail-9": "Your bill: amount due $142.37 by 2026-10-21."}   # cx-scan-3: a reference whose text went
WATER = dict(type="bill", vendor="Riverside Water & Power", amount=142.37, due_date="2026-10-21", payment_status="due")


def scan_llm(prompt):
    return json.dumps(WATER)


def scan_req(rt, path, method="GET", body=None):
    scan_calls.append((method, path, body))
    return {"nodes": []} if method == "GET" else {}


def gone(sid):
    if sid == "cx-scan-3":
        raise KeyError(sid)   # not on titan (#446)
    return SCAN_TEXT.get(sid)


scan_calls: list[tuple] = []
os.environ.pop("MAIL_BILLS_SCANS", None)
check("scans are off by default", mb.source_types() == (mb.MAIL,))
st = mb.run(True, SCAN_SRC[:1], gone, scan_llm, scan_req, sqlite3.connect(":memory:"))
check("…and off, a scan is never read", st["scans"] == 0 and not [c for c in scan_calls if c[0] == "POST"])
import contextlib  # noqa: E402
import io  # noqa: E402
for v in ("1 # scanned bills", "yes", "true", " 1"):
    os.environ["MAIL_BILLS_SCANS"] = v
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        on = mb.scans_on()
    check(f"MAIL_BILLS_SCANS={v!r} is off, and says so (#882)", not on and "isn't" in err.getvalue())
os.environ["MAIL_BILLS_SCANS"] = "1"
check("exactly '1' is on", mb.source_types() == (mb.MAIL, mb.SCANS))
scan_calls.clear()
st = mb.run(True, SCAN_SRC, gone, scan_llm, scan_req, sqlite3.connect(":memory:"), types=mb.source_types())
posts = [b for m, p, b in scan_calls if m == "POST"]
check("one bill from a scan and its copy and the same bill's mail (not three)", len(posts) == 1 and st["duplicates"] == 2,
      str((len(posts), st["duplicates"])))
prov = posts[0]["data"]["provenance"] if posts else {}
check("the scan's bill is its owner's, private, from scans, read locally",
      (prov.get("owner"), prov.get("audience"), prov.get("source"), prov.get("extracted_by")) == ("p_robin", "private", "scans", "local"),
      str(prov))
check("a scan whose text isn't on titan: skipped and counted, no bill, no error",
      st["no_text"] == 1 and st["scans"] == 3 and not any("cx-scan-3" in str(b) for b in posts), str(st))
led = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mb-scan-ledger.db")
if os.path.exists(led):
    os.remove(led)
lc = sqlite3.connect(led)
lc.execute("CREATE TABLE items (source TEXT, source_id TEXT, item_id TEXT, observed_at TEXT)")
lc.execute("INSERT INTO items VALUES ('scans', 's1', 'cx-scan-a', '2026-10-01T16:00:00Z')")
lc.commit()
srcs = mb.with_meta([{"source_id": "cx-scan-a", "source_type": "connector:scans"}], acl={"cx-scan-a": ("p_robin", "private")},
                    ledger=led)
os.remove(led)
check("a scan's owner comes from the sharing table and its date from the ledger, like mail",
      srcs[0]["metadata"] == {"owner": "p_robin", "observed_at": "2026-10-01T16:00:00Z"}, str(srcs[0].get("metadata")))
import item_refs  # noqa: E402
check("with references on, a pending scan's text waits for the bills pass too", "bills" in item_refs.required("scans"))
os.environ.pop("MAIL_BILLS_SCANS", None)
check("…only while scans are on", item_refs.required("scans") == {"world"})


# #1263, JEV: a duplicate is only ever the same person's.
ADMIN_PDF = {"node_id": "bill-pdf-zach1", "data": {"doc_type": "bill", "vendor": "Riverside Water", "amount": 84.2,
             "due_date": "2026-10-15", "provenance": {"source": "pdf", "owner": "zach"}}}
LEGACY = {"node_id": "bill-pdf-old", "data": {"doc_type": "bill", "vendor": "Riverside Water", "amount": 84.2, "due_date": "2026-10-15"}}
RIVER = {"doc_type": "bill", "vendor": "Riverside Water", "amount": 84.2, "due_date": "2026-10-15"}
check("Robin's scan of a bill the admin filed by hand isn't its duplicate (JEV's probe)",
      mb.duplicate_of(RIVER, [ADMIN_PDF], "p_robin") is None)
check("…the admin's own copy still is", mb.duplicate_of(RIVER, [ADMIN_PDF], "zach") == "bill-pdf-zach1")
check("a record from before owners were kept is the admin's (with_meta's default)",
      (mb.duplicate_of(RIVER, [LEGACY], "zach"), mb.duplicate_of(RIVER, [LEGACY], "p_robin")) == ("bill-pdf-old", None))
ADMIN_RENT = {"node_id": "bill-mail-zrent", "data": {"doc_type": "bill", "vendor": "TurboTenant", "amount": 2950.0,
              "due_date": "2026-10-01", "provenance": {"source": "mail", "owner": "zach"}}}
RENT = {"doc_type": "bill", "vendor": "Hale Property Co", "amount": 2950.0, "due_date": "2026-10-01"}
check("Robin's $2,950 scan from another vendor isn't the admin's mail bill (the ≥$100 rule, JEV's probe)",
      mb.same_bill_in_mail(RENT, [ADMIN_RENT], "p_robin") is None)
check("…but two of the admin's own mails about it are one bill", mb.same_bill_in_mail(RENT, [ADMIN_RENT], "zach") == "bill-mail-zrent")
os.environ["MAIL_BILLS_SCANS"] = "1"
two = [{"source_id": "cx-mail-z", "source_type": "connector:mail", "title": "Riverside Water bill", "updated_at": "1",
        "metadata": {"owner": "zach"}},
       {"source_id": "cx-mail-r", "source_type": "connector:mail", "title": "Riverside Water bill", "updated_at": "1",
        "metadata": {"owner": "p_robin"}},
       {"source_id": "cx-scan-r", "source_type": "connector:scans", "title": "Scan: Riverside Water", "updated_at": "1",
        "metadata": {"owner": "p_robin"}}]
scan_calls.clear()
st = mb.run(True, two, lambda sid: "Riverside Water. Amount due $84.20. Due 2026-10-15.",
            lambda p: json.dumps(dict(type="bill", vendor="Riverside Water", amount=84.2, due_date="2026-10-15", payment_status="due")),
            lambda rt, path, method="GET", body=None: (scan_calls.append((method, path, body)) or
                                                       ({"nodes": [ADMIN_PDF]} if method == "GET" and "bill" in path else {"nodes": []} if method == "GET" else {})),
            sqlite3.connect(":memory:"), types=mb.source_types())
owners = sorted(b["data"]["provenance"]["owner"] for m, p, b in scan_calls if m == "POST")
check("two mailboxes and a scan of one bill: Robin files her own once; the admin's is the hand-filed one",
      owners == ["p_robin"] and st["duplicates"] == 2, str((owners, st["duplicates"])))
os.environ.pop("MAIL_BILLS_SCANS", None)

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mail_bills.py")).read()
check("mail is read by the local model only", "generativelanguage" not in src and "_gemini" not in src and "openrouter" not in src.lower())

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all mail-bill checks passed'}")
sys.exit(1 if failures else 0)
