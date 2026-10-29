#!/usr/bin/env python3
"""Unsubscribing from a sender's mailing list, on the owner's tap (#1642): the sender's own one-click unsubscribe
(RFC 8058), the same floor as mail moves.

  preview   (read) the asker's own INBOX mail from one sender: does its newest mail offer one-click unsubscribe? The
            mail must carry List-Unsubscribe with an https address and List-Unsubscribe-Post: List-Unsubscribe=One-Click,
            and the mailbox's own server must have checked it as signed by the sender's domain: the topmost
            Authentication-Results says dkim=pass for a domain aligned with From (RFC 8058 asks for a DKIM signature;
            a forged From with someone's unsubscribe address gets no card). Its address is checked here too, so a card
            is offered only for one the tap could send to.
            A mailto-only list, or one with no one-click, is said plainly (titan doesn't send mail or open pages for it).
  request   the plan becomes a card to its owner, "Unsubscribe from …?" with Unsubscribe / Not now. Only a tap on the
            owner's own iPhone or Mac sends anything, every time.
  answer    the tap (proactive.card_action, source "mail-unsub"): one POST of "List-Unsubscribe=One-Click" to that
            address, and nothing else: no cookies, no redirects followed, nothing from the mailbox in it.

Where it may go: the address is the sender's text, so it's checked like fetch_url's (fetch_checks.public_url): https
only, a public name (never an IP literal, a .local name or one that resolves to a private, loopback, link-local or
CGNAT address: the house's network or titan itself), port 443.
The connection goes to the address that check resolved, so a name can't move between the check and the request.

An unsubscribe can't be undone from here (the sender keeps the list); the card says so. Mail already in the mailbox
stays. Plans keep owner, account, sender, the address's host, when, who confirmed on which device and the answer's
status; the full address is cleared once it's used, declined or expired, and plans go after 30 days.

On while mail moves are on for that person (governance capability "mail_moves", the same Settings switch); off for the
whole house with MAIL_SERVER_WRITES=0 or MAIL_UNSUBSCRIBE=0.
"""
from __future__ import annotations

import email
import email.policy
import http.client
import ipaddress
import re
import secrets
import socket
import ssl
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_server as M  # noqa: E402

PLAN_TTL = 900.0
KEEP_S = 30 * 86400.0
LOOK_BACK = 50          # the newest mails from the sender whose headers are read
TIMEOUT_S = 10.0
BODY = b"List-Unsubscribe=One-Click"
FIELDS = "MESSAGE-ID FROM DATE LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST AUTHENTICATION-RESULTS"
_DKIM_RX = re.compile(r"\bdkim\s*=\s*pass\b[^;]*?\bheader\.(?:d|i)\s*=\s*([A-Za-z0-9._+@-]+)", re.I)
OFF = "Unsubscribing from mail is switched off on titan for now."
_URL_RX = re.compile(r"<(https://[^<>\s]{1,2000})>", re.I)
_lock = threading.Lock()


class UnsubError(ValueError):
    pass


def _off(person: str | None) -> str | None:
    import os
    import mail_moves
    if os.environ.get("MAIL_UNSUBSCRIBE", "1") == "0":
        return OFF
    return mail_moves._off(person)


def _db(db: Path | None = None):
    con = M._db(db)
    con.execute("""CREATE TABLE IF NOT EXISTS mail_unsub_plans (id TEXT PRIMARY KEY, owner TEXT NOT NULL,
        account TEXT NOT NULL, sender TEXT NOT NULL, host TEXT NOT NULL, url TEXT NOT NULL, created_at REAL NOT NULL,
        expires_at REAL NOT NULL, state TEXT NOT NULL, card_id TEXT, confirmed_by TEXT, confirmed_device TEXT,
        done_at REAL, status INTEGER)""")
    return con


def one_click(raw: bytes) -> dict[str, Any]:
    """From one mail's header block: {sender, url (https, one-click) or None, mailto (bool), any (bool)}."""
    try:
        h = email.message_from_bytes(raw, policy=email.policy.compat32)
    except Exception:  # noqa: BLE001
        h = email.message.Message()
    lu = " ".join(M._header(h, "List-Unsubscribe").split())
    post = " ".join(M._header(h, "List-Unsubscribe-Post").split()).lower()
    m = _URL_RX.search(lu)
    sender = M._redact(" ".join(M._header(h, "From", True).split()))[:300]
    return {"sender": sender, "url": m.group(1) if m and post == "list-unsubscribe=one-click" else None,
            "mailto": "<mailto:" in lu.lower(), "any": bool(lu),
            "signed": signed_by_sender(M._header(h, "Authentication-Results"), _addr(sender))}


def _domain_of(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].strip().strip(".").lower() if "@" in addr else ""


def signed_by_sender(auth_results: str, from_addr: str) -> bool:
    """The mailbox's own server's verdict (the topmost Authentication-Results, which it adds and a sender can't put
    above): a passing DKIM signature for a domain aligned with From's (the same, or one is a subdomain of the other)."""
    want = _domain_of(from_addr)
    if not want:
        return False
    for d in _DKIM_RX.findall(" ".join(str(auth_results or "").split())):
        d = d.rsplit("@", 1)[-1].strip(".").lower()   # header.i is an address (or @domain): its domain
        if d and (d == want or want.endswith("." + d) or d.endswith("." + want)) and "." in d:
            return True
    return False


def address_ok(url: str, resolve: Callable | None = None) -> tuple[str | None, str]:
    """(the public address to connect to, "") or (None, why). https on 443, a public name, every address it resolves to
    public (fetch_checks' test); never an IP literal or a name with userinfo."""
    import fetch_checks
    try:
        u = urlsplit(url)
    except ValueError:
        return None, "its unsubscribe address isn't a web address"
    if u.scheme.lower() != "https" or not u.hostname or u.username or u.password or (u.port not in (None, 443)):
        return None, "its unsubscribe address isn't a plain https one"
    host = u.hostname.rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
        return None, "its unsubscribe address is a bare IP address, not a name"
    except ValueError:
        pass
    if fetch_checks.public_url(url, resolve=resolve):
        return None, "its unsubscribe address isn't on the public internet"
    try:
        infos = (resolve or socket.getaddrinfo)(host, 443)
    except (OSError, UnicodeError):
        return None, "its unsubscribe address doesn't resolve"
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            continue
        if fetch_checks._private_ip(ip):
            return None, "its unsubscribe address isn't on the public internet"
        return str(ip), ""
    return None, "its unsubscribe address doesn't resolve"


def preview(person: str, *, sender: str, account: str | None = None, connect: Callable = M._connect_tls,
            directory: Path | None = None, role_of: Callable = M._role_of, db: Path | None = None,
            now: float | None = None, resolve: Callable | None = None) -> dict[str, Any]:
    """A plan to unsubscribe from one sender, from the newest of their mails in the asker's own INBOX that offers
    one-click. Read-only. Raises UnsubError, NoMailbox."""
    import mail_moves
    if _off(person):
        raise UnsubError(_off(person))
    now = time.time() if now is None else now
    keys = M.criteria("", sender, None, now)
    if not keys:
        raise UnsubError("say who the mail is from")
    try:
        acct = mail_moves._account(person, account, directory, role_of)
    except mail_moves.MoveError as exc:
        raise UnsubError(str(exc).replace("archiving", "unsubscribing from")) from None
    conn = M.open_account(acct, connect)
    try:
        conn.examine("INBOX")
        uids = conn.uids(*keys)[-LOOK_BACK:]
        heads = conn.fetch(uids, f"BODY.PEEK[HEADER.FIELDS ({FIELDS})]") if uids else {}
    finally:
        conn.logout()
    if not uids:
        return {"plan_id": None, "count": 0}
    seen = [one_click(heads.get(u) or b"") for u in reversed(uids)]   # newest first
    senders = sorted({s["sender"] for s in seen if s["sender"]})
    if len({_addr(s) for s in senders}) > 1:
        raise UnsubError("that matches more than one sender; say which: " + "; ".join(senders[:5]))
    offered = [s for s in seen if s["url"]]
    best = next((s for s in offered if s["signed"]), None)
    if offered and best is None:
        raise UnsubError("this sender's mail isn't signed as coming from them (DKIM), so titan won't use its unsubscribe")
    if best is None:
        if any(s["mailto"] for s in seen):
            raise UnsubError("this sender only offers unsubscribing by email, which titan doesn't send")
        if any(s["any"] for s in seen):
            raise UnsubError("this sender's unsubscribe needs its web page opened; titan only uses one-click unsubscribe")
        raise UnsubError("this sender's mail has no unsubscribe")
    ip, why = address_ok(best["url"], resolve)
    if not ip:
        raise UnsubError(f"titan can't send it: {why}")
    host = (urlsplit(best["url"]).hostname or "").lower()
    con = _db(db)
    try:
        _expire(con, now)
        pid = secrets.token_hex(8)
        con.execute("INSERT INTO mail_unsub_plans (id, owner, account, sender, host, url, created_at, expires_at, state) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (pid, person, acct["name"], best["sender"], host, best["url"], now,
                                                   now + PLAN_TTL, "pending"))
        con.commit()
    finally:
        con.close()
    return {"plan_id": pid, "count": len(uids), "sender": best["sender"], "host": host, "expires_at": now + PLAN_TTL}


def _expire(con, now: float) -> None:
    """Plans nobody answered in time: expired, their address (often a per-recipient token) cleared."""
    con.execute("UPDATE mail_unsub_plans SET state='expired', url='' WHERE state='pending' AND expires_at < ?", (now,))
    con.commit()


def _addr(sender: str) -> str:
    m = re.search(r"<([^<>]+)>", sender)
    return (m.group(1) if m else sender).strip().lower()


def request(plan_id: str, person: str, *, add_card: Callable | None = None, db: Path | None = None,
            now: float | None = None) -> dict[str, Any]:
    """The plan becomes a card to its owner; nothing is sent until they tap Unsubscribe on their own iPhone or Mac."""
    if _off(person):
        raise UnsubError(_off(person))
    now = time.time() if now is None else now
    con = _db(db)
    try:
        p = con.execute("SELECT * FROM mail_unsub_plans WHERE id=?", (plan_id,)).fetchone()
        if not p or p["owner"] != person or p["state"] != "pending" or now > p["expires_at"]:
            raise UnsubError("that plan isn't there any more: preview again")
        if add_card is None:
            import proactive
            add_card = proactive.add_card
        card = add_card(f"Unsubscribe from {p['sender']}?",
                        f"titan sends the sender's own one-click unsubscribe to {p['host']}. Their mail already here "
                        "stays. This can't be undone from titan.",
                        kind="info", urgency=1, source="mail-unsub", to=[person], key=f"mail-unsub:{plan_id}",
                        expires_in=PLAN_TTL,
                        actions=[{"id": "unsub", "label": "Unsubscribe", "kind": "reply", "value": f"unsub:{plan_id}"},
                                 {"id": "decline", "label": "Not now", "kind": "reply", "value": f"decline:{plan_id}"}],
                        data={"kind": "mail_unsub", "plan_id": plan_id, "confirm_on": "iphone-or-mac"})
        con.execute("UPDATE mail_unsub_plans SET card_id=? WHERE id=?", (str(card.get("id") or ""), plan_id))
        con.commit()
        return {"plan_id": plan_id, "asked": True, "sender": p["sender"]}
    finally:
        con.close()


class _Pinned(http.client.HTTPSConnection):
    """HTTPS to a name, connected to the address the check resolved (never re-resolved), its certificate checked for
    the name."""

    def __init__(self, host: str, ip: str, timeout: float):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _post(url: str, ip: str) -> int:
    u = urlsplit(url)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    c = _Pinned(u.hostname or "", ip, TIMEOUT_S)
    try:
        c.request("POST", path, body=BODY, headers={"Content-Type": "application/x-www-form-urlencoded",
                                                    "User-Agent": "titan-household-agent/1.0"})
        return c.getresponse().status
    finally:
        c.close()


def answer(value: str, person: str | None, role: str | None, device: dict[str, Any] | None, *,
           post: Callable[[str, str], int] = _post, resolve: Callable | None = None, db: Path | None = None,
           now: float | None = None) -> tuple[bool, str]:
    """A tap: unsub:<plan> or decline:<plan>. (done, said). Decided in code."""
    import mail_moves
    now = time.time() if now is None else now
    verb, _, ref = str(value or "").partition(":")
    if verb not in ("unsub", "decline"):
        return False, "titan didn't understand that."
    con = _db(db)
    try:
        p = con.execute("SELECT * FROM mail_unsub_plans WHERE id=?", (ref,)).fetchone()
        if not p or p["owner"] != str(person or ""):
            return False, "That isn't there any more."
        if verb == "decline":
            con.execute("UPDATE mail_unsub_plans SET state='declined', url='' WHERE id=? AND state='pending'", (ref,))
            con.commit()
            return True, "OK, nothing was sent."
        if _off(person):
            return False, _off(person)
        if not mail_moves.own_device(device, role):
            return False, "Confirm on your own iPhone or Mac."
        if p["state"] != "pending":
            return False, "That's been answered already."
        if now > p["expires_at"]:
            con.execute("UPDATE mail_unsub_plans SET state='expired', url='' WHERE id=?", (ref,))
            con.commit()
            return False, "That ran out. Ask again."
        with _lock:   # the first tap wins
            if not con.execute("UPDATE mail_unsub_plans SET state='sending' WHERE id=? AND state='pending'", (ref,)).rowcount:
                return False, "That's been answered already."
            con.commit()
        ip, why = address_ok(p["url"], resolve)
        if not ip:
            con.execute("UPDATE mail_unsub_plans SET state='refused', url='' WHERE id=?", (ref,))
            con.commit()
            return False, f"Not sent: {why}."
        import egress
        egress.note("mail-unsubscribe")   # one request to the sender's site, on the owner's tap (egress.KINDS)
        try:
            status = int(post(p["url"], ip))
        except Exception as exc:  # noqa: BLE001 - a network or TLS failure
            con.execute("UPDATE mail_unsub_plans SET state='failed', url='' WHERE id=?", (ref,))
            con.commit()
            return False, f"The sender's site didn't answer ({type(exc).__name__}); nothing changed here. Try again later."
        ok = 200 <= status < 300
        con.execute("UPDATE mail_unsub_plans SET state=?, url='', status=?, done_at=?, confirmed_by=?, confirmed_device=? "
                    "WHERE id=?", ("done" if ok else "failed", status, now, str(person or ""),
                                   str((device or {}).get("id") or ""), ref))
        con.commit()
        if ok:
            return True, f"Unsubscribed from {p['sender']}: their site accepted it. Their mail already here stays."
        return False, f"The sender's site said no (HTTP {status}); you may still get their mail."
    finally:
        con.close()


def prune(db: Path | None = None, now: float | None = None) -> int:
    """Plans (the audit) go after 30 days."""
    now = time.time() if now is None else now
    con = _db(db)
    try:
        _expire(con, now)
        n = con.execute("DELETE FROM mail_unsub_plans WHERE created_at < ?", (now - KEEP_S,)).rowcount
        con.commit()
        return n
    finally:
        con.close()
