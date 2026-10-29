#!/usr/bin/env python3
"""A bill coming due with no payment seen: a card to its owner (titan-node#567, approved by Coordination 10-05).

  when      the mail-bills timer, after the bills pass. Each person's own bill and receipt records become their
            obligation threads (obligations.py, with their own word: "Paid" taps and statements). A thread still
            `due` whose due date is today or in the next 3 days gets one card; one `overdue_unconfirmed` the day after
            its due date gets one too. Never contested or stale ones, never a thread with a payment seen.
  who       the records' owner only, an adult or the admin; never a kid, never a shared screen.
  how often once per thread and due date. Not during quiet hours (it waits for the next run).
  the card  "Hale Water $48.20 is due Thu Oct 8, and I haven't seen a payment." Paid → their word that it's done
            (bill_said, which open_obligations reads too) and the thread resolves; Not yet → asked once more on the
            due day. Nothing here pays, moves or touches money.
  modes     BILL_DUE_CARDS=0 (off, default), dry (log what would be sent: the bill's name, due date and state, never
            an amount; nothing reaches anyone), 1 (send).
Kept in the mail-bills store (MAIL_BILLS_DB): the person's word (account, date, paid) and which cards went out.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

AHEAD_DAYS = 3
OWNERS = frozenset({"admin", "adult"})


def mode() -> str:
    m = os.environ.get("BILL_DUE_CARDS", "0").strip().lower()
    return m if m in ("0", "dry", "1") else "0"


def _db(db: Path | None = None) -> sqlite3.Connection:
    import mail_bills
    p = Path(db or mail_bills.SEEN_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS bill_said (owner TEXT NOT NULL, account TEXT NOT NULL, date TEXT NOT NULL,
            paid INTEGER NOT NULL, at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS bill_due_cards (key TEXT PRIMARY KEY, owner TEXT NOT NULL, account TEXT NOT NULL,
            due TEXT NOT NULL, state TEXT NOT NULL, at REAL NOT NULL);
        -- #951 step 3: a person's word as a claim with its owner, audience and source; never the chat text
        CREATE TABLE IF NOT EXISTS obligation_claims (id TEXT PRIMARY KEY, owner TEXT NOT NULL, audience TEXT NOT NULL,
            account TEXT NOT NULL, kind TEXT NOT NULL, date TEXT NOT NULL, source TEXT NOT NULL, turn_id TEXT,
            at REAL NOT NULL, superseded_by TEXT, asked TEXT);
    """)
    if "about" not in {r[1] for r in con.execute("PRAGMA table_info(obligation_claims)")}:
        con.execute("ALTER TABLE obligation_claims ADD COLUMN about TEXT")   # handled_by: a person id, never free text
    if not con.execute("SELECT 1 FROM obligation_claims WHERE source='migrated' LIMIT 1").fetchone():
        for r in con.execute("SELECT * FROM bill_said").fetchall():   # the old rows: private claims, once
            cid = "oc-" + hashlib.sha256(f"{r['owner']}|{r['account']}|{r['date']}|{r['at']}".encode()).hexdigest()[:16]
            con.execute("INSERT OR IGNORE INTO obligation_claims (id, owner, audience, account, kind, date, source, "
                        "turn_id, at, superseded_by, asked) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, r["owner"], "private", r["account"], "paid" if r["paid"] else "not_paid", r["date"],
                         "migrated", None, r["at"], None, None))
        con.execute("INSERT OR IGNORE INTO obligation_claims (id, owner, audience, account, kind, date, source, turn_id, "
                    "at, superseded_by, asked) VALUES ('oc-migration-mark','-','private','-','-','-','migrated',NULL,0,'-',NULL)")
        con.commit()
    return con


SHARE_AUDIENCES = ("household", "adults")   # a bill whose own audience is one of these may have its word shared


KINDS = ("paid", "not_paid", "disputed", "not_mine", "handled_by")


def _live(con: sqlite3.Connection, where: str, args: tuple) -> list[sqlite3.Row]:
    return con.execute(f"SELECT * FROM obligation_claims WHERE superseded_by IS NULL AND kind IN "
                       f"({','.join('?' * len(KINDS))}) AND {where} ORDER BY at", KINDS + args).fetchall()


def statements(owner: str | None, db: Path | None = None) -> list[dict[str, Any]]:
    """The person's own word on their bills (their claims, whatever their audience): [{account, date, paid}]."""
    if not owner:
        return []
    con = _db(db)
    try:
        return [{"account": r["account"], "date": r["date"], "paid": r["kind"] == "paid"}
                for r in _live(con, "owner=?", (owner,)) if r["kind"] in ("paid", "not_paid")]
    finally:
        con.close()


def statements_for(viewer: str | None, role: str | None, names: dict[str, str] | None = None,
                   db: Path | None = None) -> list[dict[str, Any]]:
    """The word on bills this viewer may see (#951 step 3): their own claims, and another adult's claims that adult
    chose to share with the adults (audience.py). Never a kid's view or a shared screen's. [{account, date, paid, who}],
    who "you" or the speaker's first name. Never chat text: a claim is only the structured fact."""
    if not viewer or role not in OWNERS:
        return []
    import audience
    con = _db(db)
    try:
        out = []
        for r in _live(con, "1=1", ()):
            mine = r["owner"] == viewer
            if not mine and not audience.visible(r["audience"], viewer, role, owner=r["owner"]):
                continue
            who = "you" if mine else (str((names or {}).get(r["owner"]) or "another adult").split(" ")[0])
            about = r["about"] if r["kind"] == "handled_by" else None
            # the person named is shown by first name to whoever may see this claim; naming them never shows it to them
            # (they see it only as any adult would: the speaker shared it with the adults)
            by = ("you" if about == viewer else str((names or {}).get(about) or "someone").split(" ")[0]) if about else None
            out.append({"account": r["account"], "date": r["date"], "paid": r["kind"] == "paid", "who": who,
                        "kind": r["kind"], **({"handled_by": by} if by else {})})
        return out
    finally:
        con.close()


def say(owner: str, account: str, when: date, paid: bool, db: Path | None = None, *, source: str = "card",
        turn_id: str | None = None) -> str:
    """A person's word that a bill is paid (or isn't): say_kind with paid / not_paid."""
    return say_kind(owner, account, "paid" if paid else "not_paid", when, db, source=source, turn_id=turn_id)


def say_kind(owner: str, account: str, kind: str, when: date, db: Path | None = None, *, about: str | None = None,
             source: str = "chat", turn_id: str | None = None) -> str:
    """A person's word on a bill, as a private claim (owner the speaker, audience private: from their own chat or their
    own card): paid | not_paid | disputed | not_mine | handled_by (about = the household member's person id, never free
    text). It supersedes their earlier word on the same account; nothing is overwritten. → the claim id."""
    if kind not in KINDS:
        raise ValueError(f"kind is one of {KINDS}")
    if kind == "handled_by" and not about:
        raise ValueError("handled_by names a person (their id)")
    con = _db(db)
    try:
        cid = "oc-" + secrets.token_hex(8)
        for r in _live(con, "owner=? AND account=?", (owner, account)):
            con.execute("UPDATE obligation_claims SET superseded_by=? WHERE id=?", (cid, r["id"]))
        con.execute("INSERT INTO obligation_claims (id, owner, audience, account, kind, date, source, turn_id, at, "
                    "superseded_by, asked, about) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, owner, "private", account, kind, when.isoformat(), source, turn_id, time.time(), None, None,
                     about if kind == "handled_by" else None))
        con.commit()
        return cid
    finally:
        con.close()


def share(owner: str, role: str | None, account: str, yes: bool, *, account_audience: str | None,
          db: Path | None = None) -> str:
    """The speaker's answer to "Tell the other adults?" (#951 step 3, decision (b)): asked once, only for a bill that's
    already the household's or the adults'; a yes widens their latest claim on it to `adults` (the fact only), a no
    keeps it theirs. A kid's word is never widened. → shared | kept | not_asked | none."""
    if role not in OWNERS or account_audience not in SHARE_AUDIENCES:
        return "not_asked"
    con = _db(db)
    try:
        live = _live(con, "owner=? AND account=?", (owner, account))
        if not live:
            return "none"
        cid = live[-1]["id"]
        con.execute("UPDATE obligation_claims SET audience=?, asked=? WHERE id=?",
                    ("adults" if yes else "private", "yes" if yes else "no", cid))
        con.commit()
        return "shared" if yes else "kept"
    finally:
        con.close()


def my_word(owner: str, account: str, db: Path | None = None) -> dict[str, Any] | None:
    """The speaker's own live word on an account, for their account page (#951 step 5): {kind, audience, asked}, or
    None. Only their own; never another person's."""
    con = _db(db)
    try:
        live = _live(con, "owner=? AND account=?", (owner, account))
        if not live:
            return None
        r = live[-1]
        return {"kind": r["kind"], "audience": r["audience"], "asked": r["asked"]}
    finally:
        con.close()


def take_back(owner: str, account: str, db: Path | None = None) -> bool:
    """The speaker takes a shared word back: their latest claim on the account is theirs alone again."""
    con = _db(db)
    try:
        live = _live(con, "owner=? AND account=?", (owner, account))
        if not live or live[-1]["audience"] == "private":
            return False
        con.execute("UPDATE obligation_claims SET audience='private', asked='taken_back' WHERE id=?", (live[-1]["id"],))
        con.commit()
        return True
    finally:
        con.close()


def _key(owner: str, account: str, due: date) -> str:
    return hashlib.sha1(f"{owner}\0{account}\0{due.isoformat()}".encode()).hexdigest()[:16]


def _owner_of(record: dict[str, Any]) -> str | None:
    return ((record.get("data") or {}).get("provenance") or {}).get("owner")


def due_threads(records: Iterable[dict[str, Any]], today: date, roles: dict[str, str],
                db: Path | None = None) -> list[tuple[str, Any]]:
    """(owner, thread) for each person's own threads that should be carded today."""
    import obligations
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        o = _owner_of(r)
        if o and roles.get(o) in OWNERS:
            by_owner.setdefault(o, []).append(r)
    out = []
    for owner, recs in by_owner.items():
        for t in obligations.threads(recs, today, statements(owner, db)):
            if not t.due:
                continue
            if t.state == "due" and today <= t.due <= today + timedelta(days=AHEAD_DAYS):
                out.append((owner, t))
            elif t.state == "overdue_unconfirmed" and t.due == today - timedelta(days=1):
                out.append((owner, t))
    return out


def run(records: list[dict[str, Any]], today: date, *, quiet: bool, roles: dict[str, str],
        send: Callable[..., Any] | None = None, db: Path | None = None, log: Callable[[str], None] | None = None,
        how: str | None = None) -> dict[str, int]:
    """Card (or, dry, log) each owner's bills coming due. → counts only."""
    how = how or mode()
    log = log or (lambda s: print(s, file=sys.stderr))
    stats = {"due": 0, "sent": 0, "would_send": 0, "already": 0, "quiet": 0}
    if how == "0":
        return stats
    con = _db(db)
    try:
        for owner, t in due_threads(records, today, roles, db):
            stats["due"] += 1
            key = _key(owner, t.account, t.due)
            row = con.execute("SELECT state FROM bill_due_cards WHERE key=?", (key,)).fetchone()
            again = row is not None and row["state"] == "later" and today >= t.due
            if how == "dry":
                if row is not None:
                    stats["already"] += 1
                    continue
                # the bill's name, its due date, its state: never an amount (Coordination, 10-05)
                log(f"[bill-due dry] would card: {t.vendor} · due {t.due.isoformat()} · {t.state}")
                con.execute("INSERT INTO bill_due_cards VALUES (?,?,?,?,?,?)",
                            (key, owner, t.account, t.due.isoformat(), "dry", time.time()))
                stats["would_send"] += 1
                continue
            if row is not None and row["state"] not in ("dry",) and not again:
                stats["already"] += 1
                continue
            if quiet:
                stats["quiet"] += 1
                continue
            if send is None:
                import proactive
                send = proactive.add_card
            amt = f" ${t.amount:,.2f}" if t.amount is not None else ""
            when = "today" if t.due == today else f"{t.due:%a %b %-d}"
            over = t.state == "overdue_unconfirmed"
            title = f"{t.vendor}{amt} " + (f"was due {t.due:%a %b %-d}" if over else f"is due {when}")
            detail = ("I haven't seen a payment for it." if not over else "I haven't seen a payment confirmation.") + \
                f" ({t.chain()})"
            send(title, detail, kind="info", urgency=1, source="bill-due", to=[owner], key=f"bill-due:{key}",
                 expires_in=max(86400.0, (t.due - today).days * 86400.0 + 86400.0),
                 actions=[{"id": "paid", "label": "Paid", "kind": "reply", "value": f"paid:{key}"},
                          {"id": "later", "label": "Not yet", "kind": "reply", "value": f"later:{key}"}],
                 data={"kind": "bill_due", "key": key, "due": t.due.isoformat()})
            con.execute("INSERT OR REPLACE INTO bill_due_cards VALUES (?,?,?,?,?,?)",
                        (key, owner, t.account, t.due.isoformat(), "sent-again" if again else "sent", time.time()))
            stats["sent"] += 1
        con.commit()
    finally:
        con.close()
    return stats


def answer(value: str, person: str | None, role: str | None, *, db: Path | None = None,
           today: date | None = None) -> tuple[bool, str]:
    """A tap (proactive.card_action, source "bill-due"): paid:<key> or later:<key>. Only the bill's owner."""
    verb, _, key = str(value or "").partition(":")
    if verb not in ("paid", "later"):
        return False, "titan didn't understand that."
    import local_time
    today = today or local_time.today()
    con = _db(db)
    try:
        row = con.execute("SELECT * FROM bill_due_cards WHERE key=?", (key,)).fetchone()
        if not row or row["owner"] != str(person or "") or role not in OWNERS:
            return False, "That isn't there any more."
        if verb == "later":
            con.execute("UPDATE bill_due_cards SET state='later' WHERE key=?", (key,))
            con.commit()
            return True, "OK, I'll ask again on the day it's due."
        con.execute("UPDATE bill_due_cards SET state='paid' WHERE key=?", (key,))
        con.commit()
    finally:
        con.close()
    say(str(person), row["account"], today, True, db)
    return True, "Got it: marked as paid. (Nothing was paid or moved by titan.)"


def mark_said_paid(person: str, threads: list[Any], *, names: list[str] | None = None, everything: bool = False,
                   excluded: list[str] | None = None, today: date | None = None,
                   db: Path | None = None, kind: str = "paid", about: str | None = None) -> dict[str, Any]:
    """The person said in their own chat that bills are paid or handled ("Xfinity's paid", "all of those are fixed
    except Providence"): their word for the matching open threads, the same record as the card's Paid tap. Only their
    own open threads; whatever they set aside (#952) is never marked; a name that matches no open bill, or more than
    one, is asked about, never guessed. → {marked: [vendor], left: [vendor], ask: [str], share_ask: [vendor]}: share_ask
    names the household or adults' bills to ask about sharing once (#951 step 3); the word stays theirs until a yes."""
    import exclusions
    import obligations
    import local_time
    today = today or local_time.today()
    # their own bills, and the household's or adults' bills they can see (#951 step 3): marking writes only their own
    # private claim, which changes no one else's view unless they choose to share it
    mine = [t for t in threads if t.state in obligations.OPEN
            and (t.owner in (None, person) or t.audience in SHARE_AUDIENCES)]
    out: dict[str, Any] = {"marked": [], "left": [], "ask": [], "share_ask": []}
    picked: list[Any] = []
    if everything:
        picked = list(mine)
    for name in names or []:
        acct = obligations.account_of(name)
        hits = [t for t in mine if acct and obligations.same_account(acct, t.account)]
        if not hits:
            out["ask"].append(f"I don't see an open bill for {name}.")
        elif len({t.account for t in hits}) > 1 or len(hits) > 1:
            when = ", ".join(f"{t.vendor} due {t.due:%b %-d}" if t.due else t.vendor for t in hits)
            out["ask"].append(f"{name} matches more than one open bill ({when}): which one?")
        else:
            picked.append(hits[0])
    seen: set[tuple] = set()
    for t in picked:
        if t.key in seen:
            continue
        seen.add(t.key)
        if exclusions.hit(excluded or [], f"{t.vendor} {t.account}"):
            out["left"].append(t.vendor)
            continue
        say_kind(person, t.account, kind, today, db, about=about, source="chat")
        out["marked"].append(t.vendor)
        if t.audience in SHARE_AUDIENCES:
            out["share_ask"].append(t.vendor)   # a household or adults' bill: ask once whether to tell the other adults
    return out

