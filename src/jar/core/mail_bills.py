#!/usr/bin/env python3
"""Bills and receipts from synced mail become records, with no upload (titan-node#293, Zach 10-01).

Each mail the Mac syncs (connector:mail in the knowledge base) that looks like money owed or paid is
read once by titan's LOCAL model ("Decision: household data stays on titan") and filed as a record:
  bill       owed: vendor, amount, due date (a failed or declined payment is a bill, payment_status=failed)
  receipt    paid: vendor, amount, paid date (a credit note or refund is a receipt with a negative amount)
  statement  informational: vendor, period, balance
so find_records answers "which bills are due this week" or "what did Apple charge me in September".

Facts are checked in code, never taken from the model on trust:
  - the amount must appear in the mail as written ($21.80); otherwise it's left out;
  - dates must be real ISO dates;
  - the vendor and summary are redacted (no account or card numbers, #268).
The record id comes from the mail item, so a re-sync updates and never duplicates. A bill already
filed by hand (extract_bill: same vendor, amount and due date) isn't filed twice. Each record is
private to the person whose mail it came from, like the mail itself; sharing it is theirs to do.

A failed or declined payment also gets a card (Zach, through the owner session, 10-01): to the
mailbox's owner only, once per payment (keyed by the record), urgency 2, held through quiet hours
(22:00-07:00), with "I've handled it". When a later receipt from the same vendor for the same amount
shows it went through, the card updates to resolved. A failure older than FAILED_CARD_DAYS, or one
already resolved by the time it's seen, gets no card.

Scanned bills (#554, MAIL_BILLS_SCANS=1; off by default): the same pass reads the scans connector's text, the OCR the
owner's Mac already did. The image never leaves the Mac (#446: references, never bodies) and no vision model runs. A
scan's record is private to the scan's owner, as a mail's is to the mailbox's, with provenance source "scans". A scan
with no text on titan (a #446 reference whose text already went) is skipped and counted, never an error. The flag is
on only when it reads exactly "1" (#882: an EnvironmentFile's trailing "# comment" becomes part of the value).

  python3 scripts/mail_bills.py            dry run: what would be filed
  python3 scripts/mail_bills.py --apply    file them (titan-mail-bills.timer runs this)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

SEEN_DB = Path(os.environ.get("MAIL_BILLS_DB", "/ai/datasets/connectors/mail_bills.db"))
LOCAL_URL = os.environ.get("AGENT_ENDPOINT") or "http://127.0.0.1:8010"
TYPES = ("bill", "receipt", "statement")
MAIL, SCANS = "connector:mail", "connector:scans"


def scans_on() -> bool:
    """MAIL_BILLS_SCANS: exactly "1" is on; anything else is off, and a value that isn't "0" or empty says so (#882)."""
    v = os.environ.get("MAIL_BILLS_SCANS", "")
    if v not in ("", "0", "1"):
        print(f"mail bills: MAIL_BILLS_SCANS={v!r} isn't \"1\" or \"0\": scans stay off", file=sys.stderr)
    return v == "1"


def source_types() -> tuple[str, ...]:
    return (MAIL, SCANS) if scans_on() else (MAIL,)
FAILED_CARD_DAYS = 14
QUIET = (22, 7)

# A mail is a candidate when it speaks of money owed or paid AND shows an amount. Calibrated 10-01 on
# the real mailbox: 41 of 327 (Apple, Vercel, Anthropic, GitHub, Warp, T-Mobile, a utility, a mortgage,
# failed and declined payments); the model then says what each one is, or "none".
MONEY_WORDS = re.compile(r"\b(bill|statement|invoice|receipt|payment|paid|amount due|balance due|autopay|auto pay|"
                         r"order (?:confirm\w*|#|number)|your order|ordered|charged?|renewal|subscription|refund|"
                         r"credit note)\b", re.I)
AMOUNT = re.compile(r"(-?)\$\s?(\d{1,3}(?:,\d{3})*|\d+)\.(\d{2})\b")
CREDIT = re.compile(r"\b(refund(?:ed)?|credit note|credited|credit memo|reversal)\b", re.I)
# A subscription's confirmation or renewal notice: it charges itself (its receipt follows), so it is never a bill to
# pay unless it says a payment failed (Zach 10-10: Apple's renewals were listed as eleven bills "due Jan 1").
RENEWAL = re.compile(r"\b(subscription (?:confirm\w*|renew\w*)|renews?\b|renewal (?:notice|reminder|confirm\w*)|"
                     r"will (?:automatically )?renew|auto-?renew\w*|your subscription)\b", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
NOT_MONEY = re.compile(r"\b(\d+% off|sale ends|deals? of the|unsubscribe from (?:our )?promotions|newsletter)\b", re.I)


def is_candidate(title: str, text: str) -> bool:
    head = f"{title}\n{text[:3000]}"
    return bool(MONEY_WORDS.search(head) and AMOUNT.search(text or title) and not NOT_MONEY.search(title))


def amounts_in(text: str) -> set[float]:
    return {round(float(f"{m.group(2).replace(',', '')}.{m.group(3)}"), 2) for m in AMOUNT.finditer(text)}


def days_in(text: str) -> set[tuple[int, int]]:
    """Every (month, day) a mail shows: "Jan 1", "January 1st", "1 January", "1/1", "01/01/2026", "2026-01-01". A date
    the model gives that the mail doesn't show is never kept (the amounts rule, for dates: a defaulted Jan 1)."""
    out: set[tuple[int, int]] = set()
    low = text.lower()
    for m in re.finditer(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", low):
        out.add((int(m.group(2)), int(m.group(3))))
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/\d{2,4})?\b", low):
        out.add((int(m.group(1)), int(m.group(2))))
    mon = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    for m in re.finditer(rf"\b{mon}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", low):
        out.add((_MONTHS.index(m.group(1)) + 1, int(m.group(2))))
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{mon}", low):
        out.add((_MONTHS.index(m.group(2)) + 1, int(m.group(1))))
    return out


def _iso(v: Any) -> str | None:
    try:
        return date.fromisoformat(str(v).strip()[:10]).isoformat()
    except (ValueError, TypeError):
        return None


PROMPT = """{now}
Below is one email. Say whether it is about money the reader owes or paid, and extract the facts.
Return JSON only, with these keys (omit any that are absent):
  "type": "bill" (owed, or a payment that failed or was declined), "receipt" (paid, a refund or credit),
          "statement" (informational), or "none" (anything else: marketing, shipping, a newsletter)
  "vendor": who is paid or who sent it (a company or person, short)
  "amount": the total owed or paid, a number (negative for a refund or credit)
  "currency": e.g. "USD"
  "due_date": "YYYY-MM-DD" when payment is due
  "paid_date": "YYYY-MM-DD" when it was paid or charged
  "payment_status": "failed" when a payment failed or was declined, else "due" or "paid"
  "summary": one plain line, no account or card numbers

SUBJECT: {title}
EMAIL:
{text}"""


def local_llm(prompt: str) -> str:
    """Titan's local model (llama-server). Mail never leaves titan. The answer's shape is a schema (#941): only
    "type" must appear, so absent facts stay absent; extract() still checks every field in code."""
    import typed_local as tl
    s, n = {"type": "string"}, {"type": "number"}
    schema = tl.obj({"type": tl.enum(TYPES + ("none",)), "vendor": s, "amount": n, "currency": s, "due_date": s,
                     "paid_date": s, "payment_status": tl.enum(("failed", "due", "paid")), "summary": s},
                    required=("type",))
    got, _model = tl.ask(prompt, schema, LOCAL_URL, name="money_mail", max_tokens=400, temperature=0.1)
    return json.dumps(got) if got else ""


def extract(title: str, text: str, llm: Callable[[str], str], now_line: str = "") -> dict | None:
    """The checked fields, or None when it isn't a bill, receipt or statement."""
    import redact
    raw = llm(PROMPT.format(now=now_line, title=title[:300], text=text[:4000]))
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        f = json.loads(m.group(0)) if m else {}
    except ValueError:
        return None
    kind = str(f.get("type") or "").strip().lower()
    if kind not in TYPES:
        return None
    vendor = redact.redact_personal(str(f.get("vendor") or "").strip())[:60]
    if not vendor:
        return None
    out: dict[str, Any] = {"doc_type": kind, "vendor": vendor}
    try:
        amt = abs(round(float(f.get("amount")), 2))
    except (TypeError, ValueError):
        amt = None
    # Only an amount the mail shows, and never $0.00 (a placeholder, not a fact). The sign is the mail's
    # words, not the model's (10-01 dry run: the local model made ordinary receipts negative).
    if amt and amt in amounts_in(f"{title}\n{text}"):
        out["amount"] = -amt if CREDIT.search(title) else amt   # the subject: bodies mention refund policies
        out["currency"] = str(f.get("currency") or "USD").upper()[:3]
    shown = days_in(f"{title}\n{text}")
    for k in ("due_date", "paid_date"):
        iso = _iso(f.get(k))
        if iso and (int(iso[5:7]), int(iso[8:10])) in shown:   # only a date the mail shows
            out[k] = iso
    status = str(f.get("payment_status") or "").lower()
    if status in ("failed", "due", "paid"):
        out["payment_status"] = status
    elif re.search(r"\b(unsuccessful|failed|declined)\b", title, re.I):
        out["payment_status"] = "failed"
    # The status decides the type when they disagree: failed or due is owed, paid is a receipt.
    if out.get("payment_status") in ("failed", "due"):
        out["doc_type"] = "bill"
    elif out.get("payment_status") == "paid":
        out["doc_type"] = "receipt"
    if out["doc_type"] == "bill" and out.get("payment_status") == "paid":
        out["doc_type"] = "receipt"
    if out["doc_type"] == "bill" and out.get("payment_status") != "failed" and RENEWAL.search(title):
        out["doc_type"] = "statement"     # a renewal notice charges itself: informational, never a bill to pay
        out.pop("payment_status", None)
    if "amount" not in out and out["doc_type"] == "statement":
        return None                       # nothing to file: an order notice, a "your statement is ready"
    summary = redact.redact_personal(str(f.get("summary") or "").strip())[:200]
    if summary:
        out["summary"] = summary
    return out


def node_id_for(mail_id: str, doc_type: str) -> str:
    return f"{doc_type}-mail-{hashlib.sha1(mail_id.encode()).hexdigest()[:12]}"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _owner_of(data: dict) -> str:
    """Whose a record is: its provenance owner, else the connector default, the admin (with_meta's rule), so a record
    filed before owners were kept still has one."""
    prov = data.get("provenance") or {}
    o = prov.get("owner") if isinstance(prov, dict) else None
    if o:
        return str(o)
    import household_identity as hh
    return hh.ADMIN_PERSON_ID


def duplicate_of(fields: dict, existing: list[dict], owner: str | None = None) -> str | None:
    """A bill or receipt filed another way (extract_bill from a PDF): same vendor, amount and date, and the same
    person's (#1263, JEV: Robin's scan of a bill the admin had filed by hand left Robin with no record of her own)."""
    key = (_slug(fields["vendor"]), fields.get("amount"), fields.get("due_date") or fields.get("paid_date"))
    if key[1] is None or key[2] is None:
        return None
    mine = owner or _owner_of({})
    for n in existing:
        d = n.get("data") or {}
        if (_slug(str(d.get("vendor") or "")), d.get("amount"), d.get("due_date") or d.get("paid_date")) == key \
                and not str(n.get("node_id", "")).startswith(f"{fields['doc_type']}-mail-") and _owner_of(d) == mine:
            return n["node_id"]
    return None


def same_bill_in_mail(fields: dict, filed: list[dict], owner: str | None = None) -> str | None:
    """Two mails about one payment (10-01: rent $2,950 due Oct 1 came from TurboTenant and from the
    landlord by name). Same type, amount and date, and either the same vendor or $100 or more (two
    different large charges on one day for the identical amount are vanishingly rare; two $9.99 app
    subscriptions from Apple on one day are not, so small ones also need the vendor). Only the same person's: two
    people's mail (or a scan and someone else's mail) about one bill each file their own (#1263, JEV)."""
    when = fields.get("due_date") or fields.get("paid_date")
    if fields.get("amount") is None or when is None:
        return None
    mine = owner or _owner_of({})
    for n in filed:
        d = n["data"]
        if (d["doc_type"], d.get("amount"), d.get("due_date") or d.get("paid_date")) == (fields["doc_type"], fields["amount"], when) \
                and (_slug(d["vendor"]) == _slug(fields["vendor"]) or abs(fields["amount"]) >= 100) and _owner_of(d) == mine:
            return n["node_id"]
    return None


def _seen(con: sqlite3.Connection) -> dict[str, str]:
    con.execute("CREATE TABLE IF NOT EXISTS seen (mail_id TEXT PRIMARY KEY, updated_at TEXT, outcome TEXT, "
                "node_id TEXT, ts REAL)")
    return {r[0]: r[1] for r in con.execute("SELECT mail_id, updated_at FROM seen")}


def enveloped(source_id: str) -> bool:
    """Whether the KB copy of this mail is now its envelope (#390): a summary, not the mail."""
    import agent_runtime_lib as lib
    import kb_privacy
    try:
        return bool((kb_privacy.source_meta(lib._runtime_request, source_id) or {}).get("envelope"))
    except Exception:  # noqa: BLE001 - can't tell: treat it as the mail, as before
        return False


def keep_first_reading(con: sqlite3.Connection, table_cols: int, sid: str, stamp: str) -> None:
    """An enveloped mail was already read whole before its envelope (the pass waits 24 h, after this job): its new
    stamp is recorded and its outcome kept, so its bill or suggestion keeps the first extraction."""
    if con.execute("UPDATE seen SET updated_at=? WHERE mail_id=?", (stamp, sid)).rowcount == 0:
        vals = (sid, stamp, "enveloped") + (None,) * (table_cols - 4) + (time.time(),)
        con.execute(f"INSERT INTO seen VALUES ({','.join('?' * table_cols)})", vals)


def run(apply: bool, sources: list[dict], text_of: Callable[[str], str], llm: Callable[[str], str],
        request: Callable[..., Any], con: sqlite3.Connection, now_line: str = "", limit: int = 200,
        is_envelope: Callable[[str], bool] | None = None, types: tuple[str, ...] = (MAIL,)) -> dict:
    """sources: the KB's sources; text_of(source_id) its text; request(runtime, path, method, body) the
    record runtime. Returns counts and what was (or would be) filed.

    A mail whose KB copy became its envelope (is_envelope) is never read again (10-05: the migration re-indexed 57
    mails, this saw them as changed and re-filed six bills from the summaries, overwriting vendor, amount and
    status with what a summary says). Its bill keeps the first extraction, made from the mail itself."""
    seen = _seen(con)
    stats: dict[str, Any] = {"mails": 0, "scans": 0, "no_text": 0, "candidates": 0, "filed": [], "none": 0,
                             "duplicates": 0, "unchanged": 0}
    existing: list[dict] = []
    filed_now: list[dict] = []
    stats["partial_types"] = []
    import agent_runtime_lib as lib
    for t in TYPES:
        # every record of the type, paged (#345); only a runtime that can't page leaves it partial, and then the
        # check against hand-filed ones is skipped for it (said, not silent). Mail ids still never refile.
        nodes, complete = lib.record_nodes(t, call=lambda svc, path, method: request(svc, path, method, None))
        if not complete:
            print(f"mail bills: {t} couldn't be read in full; the check against hand-filed ones is skipped for it",
                  file=sys.stderr)
            stats["partial_types"].append(t)
            continue
        existing += nodes
    for s in sources:
        st = s.get("source_type")
        if st not in types:
            continue
        stats["mails" if st == MAIL else "scans"] += 1
        sid, stamp = s["source_id"], str(s.get("updated_at") or "")
        if seen.get(sid) == stamp:
            stats["unchanged"] += 1
            continue
        if is_envelope is not None and is_envelope(sid):
            stats["unchanged"] += 1
            if apply:
                keep_first_reading(con, 5, sid, stamp)
            continue
        title = str(s.get("title") or "")
        try:
            text = text_of(sid) or ""
        except Exception:  # noqa: BLE001 - a reference whose body isn't on titan (#446): skipped, never an error
            text = ""
        if not text.strip():
            stats["no_text"] += 1
            if apply:
                con.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?,?,?)", (sid, stamp, "no-text", None, time.time()))
            continue
        if not is_candidate(title, text):
            if apply:
                con.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?,?,?)", (sid, stamp, "not-money", None, time.time()))
            continue
        stats["candidates"] += 1
        if stats["candidates"] > limit:
            break
        fields = extract(title, text, llm, now_line)
        outcome, nid = "none", None
        if fields is None:
            stats["none"] += 1
        else:
            owner = (s.get("metadata") or {}).get("owner")
            dup = duplicate_of(fields, existing, owner) or same_bill_in_mail(fields, filed_now, owner)
            if dup:
                stats["duplicates"] += 1
                outcome, nid = "duplicate", dup
            else:
                meta = s.get("metadata") or {}
                nid = node_id_for(sid, fields["doc_type"])
                if _iso(meta.get("observed_at")):
                    fields["mail_date"] = _iso(meta.get("observed_at"))
                data = {**fields, "title": title[:200],
                        "provenance": {"source": "mail" if st == MAIL else "scans", "source_id": sid, "item_id": sid,
                                       "kind": "derived",
                                       "owner": meta.get("owner"),
                                       "audience": "private", "extracted_by": "local"}}
                stats["filed"].append({"node_id": nid, **{k: v for k, v in fields.items() if k != "summary"}})
                filed_now.append({"node_id": nid, "data": data})
                outcome = "filed"
                if apply:
                    try:
                        request("record", "/v1/nodes", "POST", {"node_id": nid, "node_type": fields["doc_type"], "data": data})
                    except Exception as exc:  # noqa: BLE001
                        if "409" not in str(exc) and "onflict" not in str(exc):
                            raise
                        request("record", f"/v1/nodes/{urllib.parse.quote(nid, safe='')}", "PATCH", {"data": data})
        if apply:
            con.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?,?,?)", (sid, stamp, outcome, nid, time.time()))
            con.commit()
    stats["records"] = existing + filed_now
    return stats


def _vendor_key(v: str) -> str:
    """"Vercel Inc." and "Vercel", "Amazon.com" and "Amazon": the first word."""
    return (re.findall(r"[a-z0-9]+", str(v).lower()) or [""])[0]


def _when(d: dict) -> str | None:
    return d.get("paid_date") or d.get("due_date") or d.get("mail_date")


def _money(a: Any) -> str:
    return f"${abs(float(a)):,.2f}"


def _short(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%b %-d")


def payment_cards(records: list[dict], con: sqlite3.Connection, today: date, quiet: bool,
                  send: Callable[..., Any] | None) -> list[str]:
    """Cards for failed payments from mail, and their resolution. send=None: report only (dry run)."""
    con.execute("CREATE TABLE IF NOT EXISTS cards (node_id TEXT PRIMARY KEY, state TEXT, ts REAL)")
    carded = {r[0]: r[1] for r in con.execute("SELECT node_id, state FROM cards")}
    receipts = [n for n in records if (n.get("data") or {}).get("doc_type", n.get("node_type")) == "receipt"
                or str(n.get("node_id", "")).startswith("receipt-")]
    out = []
    for n in records:
        d, nid = n.get("data") or {}, n.get("node_id", "")
        prov = d.get("provenance") or {}
        if d.get("payment_status") != "failed" or prov.get("source") != "mail":
            continue
        failed_on = _when(d)
        owner = prov.get("owner") or "zach"
        # only the failed payment's owner's own receipt settles it (#1055: another person's receipt for the same
        # vendor and amount closed the card, hid a real failure and showed their payment date)
        paid = next((r for r in receipts
                     if (((r.get("data") or {}).get("provenance") or {}).get("owner") or "zach") == owner
                     and _vendor_key((r.get("data") or {}).get("vendor", "")) == _vendor_key(d.get("vendor", ""))
                     and (d.get("amount") is None or (r.get("data") or {}).get("amount") == d.get("amount"))
                     and failed_on and (_when(r.get("data") or {}) or "") >= failed_on), None)
        state = carded.get(nid)
        if state == "resolved" or (state is None and paid):
            if state is None and send is not None:
                con.execute("INSERT OR REPLACE INTO cards VALUES (?,?,?)", (nid, "resolved", time.time()))
            continue                          # never carded, and it went through: nothing to say
        if state is None and (not failed_on or (today - date.fromisoformat(failed_on)).days > FAILED_CARD_DAYS):
            continue                          # too old to be news
        if quiet:
            continue                          # it waits for morning
        vendor, amt = d.get("vendor", "A vendor"), d.get("amount")
        what = f"{vendor} payment" + (f" of {_money(amt)}" if amt is not None else "")   # the vendor's name, no "Your"
        key, to = f"payfail-{nid}"[:80], [prov.get("owner") or "zach"]
        if state is None:
            title = f"{what} didn't go through" + (f" ({_short(failed_on)})" if failed_on else "")
            detail = (d.get("summary") or "") + " Pay it, or update the card on file with them."
            if send:
                send(title, detail.strip(), kind="alert", urgency=2, source="mail-bills", to=to, key=key,
                     actions=[{"label": "I've handled it", "kind": "dismiss"}])
                con.execute("INSERT OR REPLACE INTO cards VALUES (?,?,?)", (nid, "failed", time.time()))
            out.append(title)
        elif state == "failed" and paid:
            pd = _when(paid.get("data") or {})
            title = f"Resolved: {what} went through" + (f" ({_short(pd)})" if pd else "")
            if send:
                send(title, "", kind="update", urgency=0, source="mail-bills", to=to, key=key, expires_in=2 * 86400)
                con.execute("INSERT OR REPLACE INTO cards VALUES (?,?,?)", (nid, "resolved", time.time()))
            out.append(title)
    con.commit()
    return out


def _quiet(now: datetime) -> bool:
    return now.hour >= QUIET[0] or now.hour < QUIET[1]


LEDGER = Path(os.environ.get("CONNECTORS_LEDGER", "/ai/datasets/connectors/ledger.db"))


def with_meta(sources: list[dict], acl: dict | None = None, ledger: Path | None = None) -> list[dict]:
    """The KB's source list carries no metadata, so each mail gets its owner from the sharing table
    (kb_privacy: the person whose device synced it; no row is the connector default, the admin) and
    its date from the connectors ledger. Without this every record was the admin's (10-01 review)."""
    mail = [x for x in sources if x.get("source_type") in (MAIL, SCANS)]   # a scan's owner and date the same way
    if acl is None:
        import kb_privacy
        acl = kb_privacy.acl_rows([x["source_id"] for x in mail])
    dates: dict[str, str] = {}
    try:
        lc = sqlite3.connect(f"file:{ledger or LEDGER}?mode=ro", uri=True)
        dates = {r[0]: r[1] for r in lc.execute("SELECT item_id, observed_at FROM items WHERE source IN ('mail','scans')")}
    except sqlite3.Error:
        pass
    import household_identity as hh
    for x in mail:
        # a pending reference (#446) carries its owner; a KB source's comes from the sharing table
        owner = ((acl.get(x["source_id"]) or (None, None))[0] or (x.get("metadata") or {}).get("owner")
                 or hh.ADMIN_PERSON_ID)
        x["metadata"] = {**(x.get("metadata") or {}), "owner": owner, "observed_at": dates.get(x["source_id"])}
    return sources


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args()
    import agent_runtime_lib as lib
    import kb_hygiene
    SEEN_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(SEEN_DB)
    import item_refs
    import mail_triage
    types = source_types()
    pending = item_refs.pending_sources("mail")   # #446: new mail kept as references, its text until this reads it
    if SCANS in types:
        pending += item_refs.pending_sources("scans")
    records = mail_triage.record_sources()        # receipts, orders, statements kept out of the KB (mail_triage.py)
    stats = run(a.apply, with_meta(kb_hygiene.list_sources() + pending + records),
                mail_triage.text_of(item_refs.text_of(kb_hygiene.source_text)), local_llm,
                lambda rt, path, method="GET", body=None: lib._runtime_request(rt, path, method, body),
                con, now_line=lib._lt.now_line(lib._local_now()), limit=a.limit, is_envelope=enveloped, types=types)
    if a.apply and (pending or records):
        done = _seen(con)
        item_refs.mark("bills", [p["source_id"] for p in pending if p["source_id"] in done])
        mail_triage.mark("bills", [r["source_id"] for r in records if r["source_id"] in done])
    now = lib._local_now()
    send = None
    if a.apply:
        import proactive
        send = proactive.add_card
    # A dry run shows every card it would send, whatever the hour.
    for title in payment_cards(stats["records"], con, now.date(), _quiet(now) if a.apply else False, send):
        print(("  card   " if a.apply else "  would card  ") + title)
    if a.apply:
        # a bill coming due with no payment seen: a card to its owner (#567; BILL_DUE_CARDS=dry logs only)
        try:
            import bill_due
            import household_identity as hh
            roles = {str(p["id"]): str(p.get("role") or "") for p in hh.get_store().people()}
            bd = bill_due.run(stats["records"], now.date(), quiet=_quiet(now), roles=roles, send=send)
            if bill_due.mode() != "0":
                print(f"bill due: {bd['due']} due soon, {bd['sent']} carded, {bd['would_send']} would card (dry), "
                      f"{bd['already']} already, {bd['quiet']} waiting for morning")
        except Exception as exc:  # noqa: BLE001 - never breaks the bills
            print(f"bill due failed: {type(exc).__name__}", file=sys.stderr)
    for f in stats["filed"]:
        bits = [f["doc_type"], f["vendor"]] + [f"{k}={f[k]}" for k in ("amount", "due_date", "paid_date", "payment_status") if k in f]
        print(("  filed  " if a.apply else "  would file  ") + " · ".join(str(b) for b in bits))
    print(f"mail bills: {stats['mails']} mails, {stats['scans']} scans, {stats['no_text']} without text on titan, "
          f"{stats['candidates']} candidates, {len(stats['filed'])} "
          f"{'filed' if a.apply else 'to file'}, {stats['none']} not money, {stats['duplicates']} already filed"
          f"{'' if a.apply else ' (dry run)'}")
    if os.environ.get("MAIL_SUGGESTIONS", "1") == "1":
        # calendar and reminder suggestions from the same mail, as cards (#501); never breaks the bills
        try:
            import mail_suggestions
            st = mail_suggestions.main_run(a.apply)
            print(f"mail suggestions: {st['candidates']} candidates, {len(st['suggested'])} "
                  f"{'queued' if a.apply else 'to suggest'}, {st['sent']} sent")
        except Exception as exc:  # noqa: BLE001
            print(f"mail suggestions failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
