#!/usr/bin/env python3
"""Archiving mail on the server, with the always-ask floor and undo (titan-node#955 step 5; the design and
Coordination's decisions are on #955: Archive only, undo for 7 days, 500 a plan, Gmail later).

  preview   (read) the asker's own INBOX mails a search names become a plan: its exact count, senders, date range,
            INBOX → the account's Archive. 15 minutes. More than 500 is refused ("narrow it").
  request   the plan becomes a card to its owner, "Archive 23 mails from …?" with Archive / Not now. The model never
            moves mail: only a tap on the owner's own iPhone or Mac does, every time, even for one mail.
  answer    the tap (proactive.card_action, source "mail-move"). The plan's mails are found again by Message-ID; if
            any are gone, nothing moves and the card asks again with the ones still there. Then one UID MOVE.
  undo      for 7 days the result card offers Undo: a plan back the other way, asked for the same way.
What's kept per plan (the audit): owner, account, folders, the mails' Message-IDs and senders (never a subject),
count, when, who confirmed and on which device. Plans go after 30 days.
On for each person until they turn it off in Settings (#1627: governance capability "mail_moves", checked for the
mailbox's owner at every step); off for the whole house with MAIL_SERVER_WRITES=0 or the switches file.
"""
from __future__ import annotations

import collections
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_server as M  # noqa: E402

PLAN_TTL = 900.0
CAP = 500
UNDO_S = 7 * 86400.0
KEEP_S = 30 * 86400.0
OWNERS = frozenset({"admin", "adult"})
OFF = "Moving mail on the server is switched off on titan for now."
YOU_OFF = "You've turned off moving your mail on the server. You can turn it back on in Settings."
_lock = threading.Lock()
# #1642: marks change a flag where the mail is, nothing moves. Each plan holds only the mails whose flag would change
# (the search adds the opposite state), so Undo puts back exactly those.
MARKS = {"read": "UNSEEN", "unread": "SEEN", "flag": "UNFLAGGED", "unflag": "FLAGGED"}
OPPOSITE = {"read": "unread", "unread": "read", "flag": "unflag", "unflag": "flag"}
_MARK_TITLE = {"read": "Mark {n} read?", "unread": "Mark {n} unread?", "flag": "Flag {n}?", "unflag": "Unflag {n}?"}
_MARK_TAP = {"read": "Mark read", "unread": "Mark unread", "flag": "Flag", "unflag": "Unflag"}
_MARK_DONE = {"read": "Marked {n} read.", "unread": "Marked {n} unread.", "flag": "Flagged {n}.", "unflag": "Unflagged {n}."}


def enabled(person: str | None) -> bool:
    return _off(person) is None


def _off(person: str | None) -> str | None:
    """Why moving this person's mail is off (the house's switch, or their own setting), or None when it's on."""
    import governance
    d = governance.allowed(person, "mail_moves")
    if d.run:
        return None
    return OFF if d.why == "capability_off" else YOU_OFF


class MoveError(ValueError):
    pass


def _db(db: Path | None = None):
    con = M._db(db)
    con.execute("""CREATE TABLE IF NOT EXISTS mail_plans (id TEXT PRIMARY KEY, owner TEXT NOT NULL, account TEXT NOT NULL,
        from_folder TEXT NOT NULL, to_folder TEXT NOT NULL, mids TEXT NOT NULL, count INTEGER NOT NULL,
        senders TEXT NOT NULL, first_at TEXT, last_at TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL,
        state TEXT NOT NULL, card_id TEXT, undo_of TEXT, confirmed_by TEXT, confirmed_device TEXT, done_at REAL,
        moved INTEGER)""")
    # #1644: where each person files mail from each sender (their own taps only), so a later plan can say where they
    # usually put it; never shared, never used to move anything by itself
    con.execute("CREATE TABLE IF NOT EXISTS filing_habits (owner TEXT NOT NULL, sender TEXT NOT NULL, folder TEXT NOT NULL, "
                "n INTEGER NOT NULL DEFAULT 0, last_at REAL NOT NULL, PRIMARY KEY (owner, sender, folder))")
    if "verb" not in {r[1] for r in con.execute("PRAGMA table_info(mail_plans)")}:
        try:
            con.execute("ALTER TABLE mail_plans ADD COLUMN verb TEXT")   # #1646: archive, trash or back; older: archive
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc):
                raise                                                     # another process added it first: fine
    return con


def own_device(device: dict[str, Any] | None, role: str | None) -> bool:
    """The owner's own iPhone or Mac: never a shared screen, a Watch, an iPad or a TV."""
    return bool(device) and role in OWNERS and str(device.get("platform") or "") in ("ios", "mac")


def _account(person: str, account: str | None, directory: Path | None, role_of: Callable) -> dict[str, Any]:
    mine = [a for a in M.accounts(directory, role_of) if a["owner"] == person]
    if account:
        mine = [a for a in mine if a["name"] == account]
    if not mine:
        raise M.NoMailbox(person)
    if len(mine) > 1:
        raise MoveError("which mailbox: " + ", ".join(a["name"] for a in mine))
    if mine[0].get("provider") == "gmail":
        raise MoveError("archiving Gmail isn't possible yet")
    return mine[0]


def _store(con, *, owner: str, account: str, src: str, dest: str, rows: list[dict[str, Any]], now: float,
           undo_of: str | None = None, verb: str = "archive") -> dict[str, Any]:
    pid = secrets.token_hex(8)
    senders = collections.Counter(r["sender"] for r in rows).most_common()
    dates = sorted(r["date"] for r in rows if r["date"])
    con.execute("INSERT INTO mail_plans (id, owner, account, from_folder, to_folder, mids, count, senders, first_at, "
                "last_at, created_at, expires_at, state, undo_of, verb) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, owner, account, src, dest, json.dumps([[r["mid"], r["sender"], r["date"]] for r in rows]), len(rows),
                 json.dumps(senders),
                 dates[0] if dates else None, dates[-1] if dates else None, now, now + PLAN_TTL, "pending", undo_of, verb))
    con.commit()
    return {"plan_id": pid, "count": len(rows), "senders": senders[:5], "first": dates[0][:10] if dates else None,
            "last": dates[-1][:10] if dates else None, "from": src, "to": dest, "expires_at": now + PLAN_TTL,
            "verb": verb}


def preview(person: str, *, query: str = "", sender: str | None = None, days: int | None = None,
            account: str | None = None, folder: str | None = None, mark: str | None = None,
            restore_from: str | None = None, connect: Callable = M._connect_tls,
            directory: Path | None = None, role_of: Callable = M._role_of, db: Path | None = None,
            now: float | None = None) -> dict[str, Any]:
    """A plan to clear the asker's own INBOX mails that match: to the Archive, or to the Trash when that's what clearing
    means for them (#1646, clear_prefs); or, with `folder`, to one of their mailbox's own folders by name (filing:
    "move my school mail to School"); or, with `mark` (#1642), read / unread / flag / unflag where they are, only the
    mails that aren't that already; or, with `restore_from` (#1642: unarchive, restore from the Trash), the mails that
    match in that folder ("trash", "archive" or one of their folders' names) back to the INBOX. Read-only. Raises
    MoveError, NoMailbox."""
    if _off(person):
        raise MoveError(_off(person))
    now = time.time() if now is None else now
    keys = M.criteria(query, sender, days, now)
    if not keys or (keys[-2] == "SINCE" and len(keys) == 2):
        raise MoveError("say which mail: words in it, or who it's from")
    mark = str(mark or "").strip().lower() or None
    if mark and mark not in MARKS:
        raise MoveError("a mark is read, unread, flag or unflag")
    if mark and folder and str(folder).strip():
        raise MoveError("mark them or move them, one at a time")
    restore_from = str(restore_from or "").strip() or None
    if restore_from and (mark or (folder and str(folder).strip())):
        raise MoveError("bring them back to the inbox, or move or mark them: one at a time")
    acct = _account(person, account, directory, role_of)
    conn = M.open_account(acct, connect)
    try:
        src = "INBOX"
        if restore_from:
            low = restore_from.lower()
            src = (M.trash_folder(conn) if low in ("trash", "the trash", "deleted", "bin") else
                   M.archive_folder(conn) if low in ("archive", "the archive", "archived") else
                   M.find_folder(conn, restore_from))
            if not src or src == "INBOX":
                have = M.folder_names(conn)
                raise MoveError("there's no folder by that name in this mailbox" +
                                (f"; its folders: {', '.join(have)}" if have else ""))
            verb, dest = "restore", "INBOX"
        elif mark:
            verb, dest = mark, "INBOX"
            keys = [*keys, MARKS[mark]]
        elif folder and str(folder).strip():
            verb, dest = "move", M.find_folder(conn, folder)
            if not dest:
                have = M.folder_names(conn)
                raise MoveError("there's no folder by that name in this mailbox" +
                                (f"; its folders: {', '.join(have)}" if have else ""))
        else:
            import clear_prefs
            verb = clear_prefs.mode(person, "mail")
            dest = M.trash_folder(conn) if verb == "trash" else M.archive_folder(conn)
            if not dest:
                raise MoveError(f"this mailbox has no {'Trash' if verb == 'trash' else 'Archive'} folder")
        conn.examine(src)
        uids = conn.uids(*keys)
        if len(uids) > CAP:
            raise MoveError(f"that's {len(uids)} mails, more than {CAP} at once: narrow it (a sender, or fewer days)")
        rows = []
        heads = conn.fetch(uids, f"BODY.PEEK[HEADER.FIELDS ({M.HEADER_FIELDS})]") if uids else {}
        for uid in uids:
            h = M.parse_headers(heads.get(uid) or b"")
            if h["message_id"]:      # a mail without a Message-ID couldn't be found again to undo: left out
                rows.append({"mid": h["message_id"], "sender": h["sender"], "date": h["sent_at"] or ""})
        usual = None
        if verb not in ("move", "restore", *MARKS) and rows:   # #1644: where they've usually filed these senders' mail, if that folder is still there
            habit = usual_folder(person, sorted({r["sender"] for r in rows if r["sender"]}), db)
            usual = M.find_folder(conn, habit) if habit else None
    finally:
        conn.logout()
    if not rows:
        return {"plan_id": None, "count": 0}
    con = _db(db)
    try:
        plan = _store(con, owner=person, account=acct["name"], src=src, dest=dest, rows=rows, now=now, verb=verb)
        if usual:
            plan["usual_folder"] = usual
        return plan
    finally:
        con.close()


def _card_text(p: Any) -> tuple[str, str]:
    senders = json.loads(p["senders"])
    names = ", ".join(f"{s} ({n})" if n > 1 else s for s, n in senders[:3]) + (" and others" if len(senders) > 3 else "")
    span = f"{(p['first_at'] or '')[:10]} – {(p['last_at'] or '')[:10]}" if p["first_at"] else ""
    back = p["undo_of"] is not None
    trash = _verb(p) == "trash"
    n = f"{p['count']} mail{'s' if p['count'] != 1 else ''}"
    if _verb(p) in MARKS:
        return (_MARK_TITLE[_verb(p)].format(n=n), f"From {names}. {span}. In {p['from_folder']}." +
                ("" if back else " You can undo it for 7 days."))
    where_back = "the inbox" if p["to_folder"] == "INBOX" else p["to_folder"]
    title = (f"Move {n} back to {where_back}?" if back else f"Move {n} from {p['from_folder']} back to the inbox?"
             if _verb(p) == "restore" else f"Move {n} to the Trash?" if trash
             else f"Move {n} to {p['to_folder']}?" if _verb(p) == "move" else f"Archive {n}?")
    return title, f"From {names}. {span}. {p['from_folder']} → {p['to_folder']}." + (
        "" if back else " You can undo it for 7 days, while the server keeps it in the Trash." if trash
        else " You can undo it for 7 days.")


HABIT_MIN = 3   # filings of one sender into one folder before titan mentions it


def usual_folder(owner: str, senders: list[str], db: Path | None = None) -> str | None:
    """Where this person has usually filed mail from these senders (their own confirmed filings, #1644): the folder
    that holds most of them, if it's been chosen at least HABIT_MIN times and more than every other one. None otherwise."""
    if not senders:
        return None
    con = _db(db)
    try:
        q = ",".join("?" * len(senders))
        rows = con.execute(f"SELECT folder, SUM(n) AS n FROM filing_habits WHERE owner=? AND sender IN ({q}) GROUP BY folder "
                           "ORDER BY n DESC", (owner, *[s.lower() for s in senders])).fetchall()
    finally:
        con.close()
    if not rows or rows[0]["n"] < HABIT_MIN or (len(rows) > 1 and rows[1]["n"] >= rows[0]["n"]):
        return None
    return rows[0]["folder"]


def _verb_of(con, plan_id: str) -> str:
    r = con.execute("SELECT verb FROM mail_plans WHERE id=?", (plan_id,)).fetchone()
    return (r[0] if r else None) or "archive"


def _verb(p: Any) -> str:
    """archive, trash, move, back, or a mark (#1642); a plan from before #1646 has none and was an archive."""
    try:
        return p["verb"] or "archive"
    except (IndexError, KeyError):
        return "archive"


def request(plan_id: str, person: str, *, add_card: Callable | None = None, db: Path | None = None,
            now: float | None = None) -> dict[str, Any]:
    """The plan becomes a card to its owner; nothing moves until they tap Archive on their own iPhone or Mac."""
    if _off(person):
        raise MoveError(_off(person))
    now = time.time() if now is None else now
    con = _db(db)
    try:
        p = con.execute("SELECT * FROM mail_plans WHERE id=?", (plan_id,)).fetchone()
        if not p or p["owner"] != person or p["state"] != "pending" or now > p["expires_at"]:
            raise MoveError("that plan isn't there any more: preview again")
        if add_card is None:
            import proactive
            add_card = proactive.add_card
        title, detail = _card_text(p)
        card = add_card(title, detail, kind="info", urgency=1, source="mail-move", to=[person], key=f"mail-move:{plan_id}",
                        expires_in=PLAN_TTL,
                        actions=[{"id": "move", "label": _MARK_TAP[_verb(p)] if _verb(p) in MARKS else
                                  "Move back" if p["undo_of"] else "Move to inbox" if _verb(p) == "restore" else
                                  "Move to Trash" if _verb(p) == "trash" else "Move" if _verb(p) == "move" else "Archive",
                                  "kind": "reply",
                                  "value": f"move:{plan_id}"},
                                 {"id": "decline", "label": "Not now", "kind": "reply", "value": f"decline:{plan_id}"}],
                        data={"kind": "mail_move", "plan_id": plan_id, "count": p["count"], "confirm_on": "iphone-or-mac"})
        con.execute("UPDATE mail_plans SET card_id=? WHERE id=?", (str(card.get("id") or ""), plan_id))
        con.commit()
        return {"plan_id": plan_id, "count": p["count"], "asked": True, "verb": _verb(p), "to": p["to_folder"]}
    finally:
        con.close()


def answer(value: str, person: str | None, role: str | None, device: dict[str, Any] | None, *,
           connect: Callable = M._connect_tls, directory: Path | None = None, role_of: Callable = M._role_of,
           add_card: Callable | None = None, db: Path | None = None, now: float | None = None) -> tuple[bool, str]:
    """A tap: move:<plan>, decline:<plan> or undo:<plan>. (done, said). Decided in code."""
    now = time.time() if now is None else now
    verb, _, ref = str(value or "").partition(":")
    if verb not in ("move", "decline", "undo"):
        return False, "titan didn't understand that."
    con = _db(db)
    try:
        p = con.execute("SELECT * FROM mail_plans WHERE id=?", (ref,)).fetchone()
        if not p or p["owner"] != str(person or ""):
            return False, "That isn't there any more."
        if verb == "decline":
            con.execute("UPDATE mail_plans SET state='declined' WHERE id=? AND state='pending'", (ref,))
            con.commit()
            return True, "OK, nothing was moved."
        if _off(person):
            return False, _off(person)
        if not own_device(device, role):
            return False, "Confirm on your own iPhone or Mac."
        if verb == "undo":
            con.close()
            con = None
            return undo(ref, str(person), add_card=add_card, db=db, now=now)
        if p["state"] != "pending":
            return False, "That's been answered already."
        if now > p["expires_at"]:
            con.execute("UPDATE mail_plans SET state='expired' WHERE id=?", (ref,))
            con.commit()
            return False, "That ran out. Ask again."
        with _lock:   # the first tap wins
            if not con.execute("UPDATE mail_plans SET state='moving' WHERE id=? AND state='pending'", (ref,)).rowcount:
                return False, "That's been answered already."
            con.commit()
        return _execute(con, p, person, device, connect=connect, directory=directory, role_of=role_of,
                        add_card=add_card, db=db, now=now)
    finally:
        if con is not None:
            con.close()


def undo(ref: str, person: str, *, add_card: Callable | None = None, db: Path | None = None,
         now: float | None = None) -> tuple[bool, str]:
    """Within 7 days, a done archive becomes a plan back the other way, asked for the same way (a card, a tap)."""
    if _off(person):
        return False, _off(person)
    now = time.time() if now is None else now
    con = _db(db)
    try:
        p = con.execute("SELECT * FROM mail_plans WHERE id=?", (ref,)).fetchone()
        if not p or p["owner"] != person:
            return False, "That isn't there any more."
        if p["state"] != "done" or p["undo_of"] is not None or now - (p["done_at"] or 0) > UNDO_S:
            return False, "That can't be undone any more."
        back = [{"mid": m, "sender": s, "date": d} for m, s, d in json.loads(p["mids"])]
        if _verb(p) in MARKS:   # the same mails, the other way, where they are
            plan = _store(con, owner=p["owner"], account=p["account"], src=p["from_folder"], dest=p["from_folder"],
                          rows=back, now=now, undo_of=ref, verb=OPPOSITE[_verb(p)])
        else:
            plan = _store(con, owner=p["owner"], account=p["account"], src=p["to_folder"], dest=p["from_folder"],
                          rows=back, now=now, undo_of=ref, verb="back")
    finally:
        con.close()
    request(plan["plan_id"], person, add_card=add_card, db=db, now=now)
    if plan["verb"] in MARKS:
        return True, f"Confirm on your iPhone or Mac to put {plan['count']} back as they were."
    return True, f"Confirm on your iPhone or Mac to move {plan['count']} back to the inbox."


def _execute(con, p: Any, person: str | None, device: dict[str, Any] | None, *, connect: Callable, directory: Path | None,
             role_of: Callable, add_card: Callable | None, db: Path | None, now: float) -> tuple[bool, str]:
    items = json.loads(p["mids"])     # [[Message-ID, sender, date], …]
    mids = [m for m, _, _ in items]
    try:
        acct = next(a for a in M.accounts(directory, role_of) if a["name"] == p["account"] and a["owner"] == p["owner"])
        w = M.open_writer(acct, connect)
    except StopIteration:
        con.execute("UPDATE mail_plans SET state='failed' WHERE id=?", (p["id"],))
        con.commit()
        return False, "That mailbox isn't connected any more."
    except Exception as exc:  # noqa: BLE001 - a refusal (no MOVE), a login or network failure
        con.execute("UPDATE mail_plans SET state='failed' WHERE id=?", (p["id"],))
        con.commit()
        return False, f"The mail server didn't answer ({type(exc).__name__}); nothing was moved."
    try:
        w.select(p["from_folder"])
        found = {m: w.find(m) for m in mids}
        here = [m for m in mids if found[m]]
        if len(here) != len(mids):
            # some went (read elsewhere, moved by hand): never a different set than the one shown; ask again
            con.execute("UPDATE mail_plans SET state='changed' WHERE id=?", (p["id"],))
            con.commit()
            if not here:
                return False, ("Those mails aren't in the Trash any more (the server may have emptied it); nothing was "
                               "moved." if p["undo_of"] is not None and _verb_of(con, p["undo_of"]) == "trash" else
                               "Those mails aren't in the inbox any more; nothing was moved." if p["from_folder"] == "INBOX"
                               else f"Those mails aren't in {p['from_folder']} any more; nothing was moved.")
            rows = [{"mid": m, "sender": s, "date": d} for m, s, d in items if found[m]]
            plan = _store(con, owner=p["owner"], account=p["account"], src=p["from_folder"], dest=p["to_folder"],
                          rows=rows, now=now, undo_of=p["undo_of"], verb=_verb(p))
            request(plan["plan_id"], p["owner"], add_card=add_card, db=db, now=now)
            return False, f"{len(mids) - len(here)} of them aren't there any more; nothing moved. I've asked again for {len(here)}."
        uids = sorted({u for m in here for u in found[m]})
        if _verb(p) in MARKS:
            w.mark(uids, _verb(p))
        else:
            w.move(uids, p["to_folder"])
    except Exception as exc:  # noqa: BLE001
        con.execute("UPDATE mail_plans SET state='failed' WHERE id=?", (p["id"],))
        con.commit()
        return False, f"The move didn't finish ({type(exc).__name__}); check the inbox before trying again."
    finally:
        w.logout()
    con.execute("UPDATE mail_plans SET state='done', done_at=?, moved=?, confirmed_by=?, confirmed_device=? WHERE id=?",
                (now, len(here), str(person or ""), str((device or {}).get("id") or ""), p["id"]))
    if _verb(p) == "move":   # #1644: a filing the owner confirmed: one count per sender for that folder
        for sender in {s for m, s, _d in items if m in here and s}:
            con.execute("INSERT INTO filing_habits (owner, sender, folder, n, last_at) VALUES (?,?,?,1,?) "
                        "ON CONFLICT(owner, sender, folder) DO UPDATE SET n = n + 1, last_at = excluded.last_at",
                        (p["owner"], sender.lower(), p["to_folder"], now))
    con.commit()
    back = p["undo_of"] is not None
    n = f"{len(here)} mail{'s' if len(here) != 1 else ''}"
    said = (_MARK_DONE[_verb(p)].format(n=n) if _verb(p) in MARKS
            else f"Moved {len(here)} back to {'the inbox' if p['to_folder'] == 'INBOX' else p['to_folder']}." if back
            else f"Moved {n} back to the inbox from {p['from_folder']}." if _verb(p) == "restore"
            else f"Moved {n} to the Trash." if _verb(p) == "trash"
            else f"Moved {n} to {p['to_folder']}." if _verb(p) == "move"
            else f"Archived {n}.")
    if not back:
        if add_card is None:
            import proactive
            add_card = proactive.add_card
        undo_note = ("Undo is here for 7 days, while the server keeps it in the Trash." if _verb(p) == "trash"
                     else "Undo is here for 7 days.")
        where = f"In {p['from_folder']}." if _verb(p) in MARKS else f"{p['from_folder']} → {p['to_folder']}."
        add_card(said, f"{where} {undo_note}", kind="info", urgency=0,
                 source="mail-move", to=[p["owner"]], key=f"mail-moved:{p['id']}", expires_in=UNDO_S,
                 actions=[{"id": "undo", "label": "Undo", "kind": "reply", "value": f"undo:{p['id']}"}],
                 data={"kind": "mail_moved", "plan_id": p["id"], "count": len(here)})
    return True, said


def prune(db: Path | None = None, now: float | None = None) -> int:
    """Plans (the audit) go after 30 days."""
    now = time.time() if now is None else now
    con = _db(db)
    try:
        n = con.execute("DELETE FROM mail_plans WHERE created_at < ?", (now - KEEP_S,)).rowcount
        con.commit()
        return n
    finally:
        con.close()
