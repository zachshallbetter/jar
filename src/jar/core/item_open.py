#!/usr/bin/env python3
"""Opening an item titan keeps only as a reference (titan-node#446 step 3, contract approved 10-03 on #446): the hub
answers from derived facts when they cover the question, and otherwise asks the owner's device for the body, for
this turn only. Behind ITEM_REFERENCES, like the references themselves (item_refs.py).

  who        the reference must exist for the asker (references.visible: its owner and audience); never on a shared
             household device (a kid's watch is out of this step: no relay channel, #446); a kid opens only their
             own, never asks for another person's (#446 v2 §3)
  asking     item_body to the device the reference names: a Mac (its inbox, Wake-on-LAN) or an iPhone that declared
             it holds the kind (#776: in the owner's own turn on it, a device call; otherwise its inbox and a
             content-available push): {ref_id, kind,
             native_id, content_hash, max_chars, purpose, asker, for_owner}
  consent    the owner asking: the device's own confirmation, as for any Mac tool. Anyone else: the owner is asked
             every time on a card on their devices (iOS's shape: data {kind: item_share, asker_first_name, title,
             item_kind, actions}, buttons allow_once | always | decline), unless they said "Always" for that person and
             kind (body_grants); no answer is no
  asleep     wake if it can; then derived facts, and fixed words that it's on the device, which isn't reachable;
             never a stale copy
  the text   redacted again, capped, wrapped as untrusted text (#473), used for this turn only: written nowhere
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

KINDS = ("mail", "scan")
MAX_CHARS = 8000
CONSENT_TIMEOUT_S = 110.0
_lock = threading.Lock()

# Fixed words, in code: what the reply says when there's no body to give.
HOUSEHOLD = "A shared screen can't open someone's mail or scans."
KID_OTHER = "Only the person it belongs to can open their mail or scans."
NOT_FOUND = "No synced mail or scan like that is on record for you."
DECLINED = "I asked {owner}. They haven't said yes, so I can't read it."
OWNER_DECLINED = "You declined, so I didn't read it."
ASLEEP_OWN = "The {what} itself is on your {device}, which is asleep or away. I can't read it until it's back."
ASLEEP_OTHER = "It's on {owner}'s {device}, which isn't reachable right now."
NO_HOLDER = "The {what} itself is on {whose} {device}, which titan can't reach."
GONE = "It's no longer on {whose} {device}, so I can't read it."
OLD_APP = "{whose} {device}'s Jellybean needs an update to open it."
PHONE_QUIET_OWN = "The {what} itself is on your {device}, which didn't answer. I can't read it until it does."
PHONE_QUIET_OTHER = "It's on {owner}'s {device}, which didn't answer."
MAC_NO_SCANS = "A Mac doesn't hold scans, so I can't open this one."
PHONE_PLATFORMS = ("ios", "ipados")


def _what(kind: str) -> str:
    return "email" if kind == "mail" else "scan"


# ── always-grants (the owner's "Always for Robin") ─────────────────────────────────────────────

def _grants_db(path: Path | None = None) -> sqlite3.Connection:
    import item_refs
    con = item_refs._db(path)
    con.execute("CREATE TABLE IF NOT EXISTS body_grants (owner TEXT NOT NULL, asker TEXT NOT NULL, kind TEXT NOT NULL, "
                "at REAL NOT NULL, PRIMARY KEY (owner, asker, kind))")
    if "used_at" not in {r[1] for r in con.execute("PRAGMA table_info(body_grants)")}:
        con.execute("ALTER TABLE body_grants ADD COLUMN used_at REAL")   # #1060: the last read it let through
    return con


def granted(owner: str, asker: str, kind: str, path: Path | None = None) -> bool:
    con = _grants_db(path)
    try:
        return con.execute("SELECT 1 FROM body_grants WHERE owner=? AND asker=? AND kind=?",
                           (owner, asker, kind)).fetchone() is not None
    finally:
        con.close()


def grant(owner: str, asker: str, kind: str, path: Path | None = None) -> None:
    with _lock:
        con = _grants_db(path)
        try:
            con.execute("INSERT OR REPLACE INTO body_grants (owner, asker, kind, at, used_at) VALUES (?,?,?,?,NULL)",
                        (owner, asker, kind, time.time()))
            con.commit()
        finally:
            con.close()


def revoke(owner: str, asker: str | None = None, kind: str | None = None, path: Path | None = None) -> int:
    with _lock:
        con = _grants_db(path)
        try:
            q, a = "DELETE FROM body_grants WHERE owner=?", [owner]
            if asker:
                q, a = q + " AND asker=?", a + [asker]
            if kind:
                q, a = q + " AND kind=?", a + [kind]
            n = con.execute(q, a).rowcount
            con.commit()
            return n
        finally:
            con.close()


def touch(owner: str, asker: str, kind: str, path: Path | None = None) -> None:
    """A grant just let a read through: its last use, for "unused for 90 days" (#1060). Never fails the read."""
    try:
        with _lock:
            con = _grants_db(path)
            try:
                con.execute("UPDATE body_grants SET used_at=? WHERE owner=? AND asker=? AND kind=?",
                            (time.time(), owner, asker, kind))
                con.commit()
            finally:
                con.close()
    except sqlite3.Error:
        pass


def take(owner: str, asker: str | None = None, kind: str | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    """Take back the owner's grants (all for one asker, or one) and return them as they were, for one undo (#1060)."""
    with _lock:
        con = _grants_db(path)
        try:
            q, a = "SELECT asker, kind, at, used_at FROM body_grants WHERE owner=?", [owner]
            if asker:
                q, a = q + " AND asker=?", a + [asker]
            if kind:
                q, a = q + " AND kind=?", a + [kind]
            rows = [{"asker": r[0], "kind": r[1], "at": r[2], "used_at": r[3]} for r in con.execute(q, a)]
            for r in rows:
                con.execute("DELETE FROM body_grants WHERE owner=? AND asker=? AND kind=?", (owner, r["asker"], r["kind"]))
            con.commit()
            return rows
        finally:
            con.close()


def restore(owner: str, rows: list[dict[str, Any]], path: Path | None = None) -> int:
    """Put back grants exactly as take() returned them (their dates too): the undo of a stop."""
    with _lock:
        con = _grants_db(path)
        try:
            for r in rows:
                con.execute("INSERT OR REPLACE INTO body_grants (owner, asker, kind, at, used_at) VALUES (?,?,?,?,?)",
                            (owner, r["asker"], r["kind"], r["at"], r.get("used_at")))
            con.commit()
            return len(rows)
        finally:
            con.close()


def grant_id(owner: str, asker: str, kind: str) -> str:
    """A grant's id for the apps (GET/DELETE /v1/sharing/grants): stable, opaque, only meaningful to its owner."""
    return "gr-" + hashlib.sha256(f"{owner}\0{asker}\0{kind}".encode()).hexdigest()[:16]


def revoke_id(owner: str, gid: str, path: Path | None = None) -> bool:
    """Take back one of the owner's own grants by its id. False: not one of theirs."""
    hit = next((g for g in grants_of(owner, path) if grant_id(owner, g["asker"], g["kind"]) == gid), None)
    return bool(hit) and revoke(owner, hit["asker"], hit["kind"], path) > 0


def grants_of(owner: str, path: Path | None = None) -> list[dict[str, Any]]:
    con = _grants_db(path)
    try:
        return [{"asker": r[0], "kind": r[1], "at": r[2], "used_at": r[3]} for r in
                con.execute("SELECT asker, kind, at, used_at FROM body_grants WHERE owner=? ORDER BY at", (owner,))]
    finally:
        con.close()


# ── the owner's answer (a card on their devices) ──────────────────────────────────────────────

class _Consent:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.answer: str | None = None


_PENDING: dict[str, _Consent] = {}


def consent_card(owner: str, asker_name: str, kind: str, title: str, cid: str,
                 add_card: Callable[..., Any]) -> None:
    """The prompt on the owner's devices: a card only they see, urgency 2 (pushed), its buttons decided in code."""
    add_card(f"{asker_name} asked titan about your “{title}” {_what(kind)}",
             "Share what it says this once?", source="item-consent", urgency=2, to=[owner], key=f"consent-{cid}",
             expires_in=CONSENT_TIMEOUT_S,
             data={"kind": "item_share", "asker_first_name": asker_name, "title": title, "item_kind": kind,
                   "actions": ["allow_once", "always", "decline"]},
             actions=[{"id": "allow_once", "label": "Allow once", "kind": "reply", "value": f"allow_once:{cid}"},
                      {"id": "always", "label": f"Always for {asker_name}"[:24], "kind": "reply", "value": f"always:{cid}"},
                      {"id": "decline", "label": "No", "kind": "reply", "value": f"decline:{cid}"}])


def answer(value: str, person: str | None) -> tuple[bool, str]:
    """A button on the consent card (proactive.card_action, source item-consent). → (done, words)."""
    m = re.fullmatch(r"(allow_once|always|decline):([0-9a-f]{16})", str(value or ""))
    if not m:
        return False, "That answer doesn't match a request."
    c = _PENDING.get(m.group(2))
    if c is None or c.answer is not None:
        return False, "That request is over: it was answered, or it timed out."
    if person and getattr(c, "owner", None) and str(person).lower() != c.owner:
        return False, "Only the owner can answer that."
    c.answer = m.group(1)
    c.event.set()
    return True, {"allow_once": "Shared this once.", "always": "Shared, and it won't ask again for them.",
                  "decline": "Not shared."}[c.answer]


def ask_owner(owner: str, asker: str, asker_name: str, kind: str, title: str, *,
              add_card: Callable[..., Any], timeout: float = CONSENT_TIMEOUT_S,
              grants_path: Path | None = None) -> bool:
    """Blocking: the owner's yes for this one read. An Always grant answers at once; no answer is no."""
    if granted(owner, asker, kind, grants_path):
        touch(owner, asker, kind, grants_path)
        return True
    cid = uuid.uuid4().hex[:16]
    c = _Consent()
    c.owner = owner  # type: ignore[attr-defined]
    _PENDING[cid] = c
    try:
        consent_card(owner, asker_name, kind, title, cid, add_card)
        c.event.wait(timeout)
        if c.answer == "always":
            grant(owner, asker, kind, grants_path)
        return c.answer in ("allow_once", "always")
    finally:
        _PENDING.pop(cid, None)


# ── picking the reference ─────────────────────────────────────────────────────────────────────

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "my", "our", "your", "email", "mail", "scan", "about", "from", "what", "did", "say", "says",
         "of", "to", "in", "on", "for", "it", "that", "this", "me", "show", "read", "open", "quote"}


def pick(refs: list[dict[str, Any]], ref_id: str | None, what: str | None) -> tuple[dict | None, list[dict]]:
    """(the one reference, or None; the close ones when it's unclear)."""
    if ref_id:
        hit = next((r for r in refs if r["id"] == ref_id), None)
        return hit, []
    q = {w for w in _WORD.findall(str(what or "").lower()) if w not in _STOP}
    if not q:
        return None, refs[:5]
    scored = sorted(((len(q & set(_WORD.findall(str(r.get("title") or "").lower()))), r) for r in refs),
                    key=lambda x: -x[0])
    scored = [(s, r) for s, r in scored if s > 0]
    if not scored:
        return None, []
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None, [r for s, r in scored if s == scored[0][0]][:5]
    return scored[0][1], []


# ── opening it ────────────────────────────────────────────────────────────────────────────────

def open_item(args: dict[str, Any], *, asker: str, role: str, asker_name: str,
              people: dict[str, dict[str, Any]], holders: Callable[[str], list[dict[str, Any]]],
              route: Callable[[dict[str, Any], str, dict[str, Any], Callable[[], bool] | None], dict[str, Any]],
              derived: Callable[[dict[str, Any]], list[str]], wrap: Callable[[str, str, str | None], str],
              add_card: Callable[..., Any] | None = None, refs_path: Path | None = None,
              grants_path: Path | None = None, consent_timeout: float = CONSENT_TIMEOUT_S,
              server_refs: Callable[[str], list[dict[str, Any]]] | None = None,
              server_body: Callable[[str, dict[str, Any], int], dict[str, Any]] | None = None) -> str:
    """The open_item tool. people: id → {name, role}; holders(owner) the owner's devices that can hold an item (their
    Macs, household_identity.macs_for, and iPhones);
    route(req, owner, mac, approve) one item_body call (cross_device.route), approve() answering a confirmation
    for a reader who isn't the owner; derived(ref) the hub's facts about it; wrap(text, source, from) #473."""
    import redact
    import references
    if role == "household":
        return HOUSEHOLD
    refs = [r for r in references.visible(asker, role, path=refs_path) if r["kind"] in KINDS]
    refs += server_refs(asker) if server_refs is not None else []
    ref, close = pick(refs, str(args.get("ref") or args.get("ref_id") or "").strip() or None,
                      str(args.get("what") or args.get("item") or ""))
    if ref is None:
        if close:
            return "Several match; ask which one: " + "; ".join(f"“{r['title']}” ({r['id']})" for r in close)
        return NOT_FOUND
    owner = str(ref["owner"]).lower()
    oname = str((people.get(owner) or {}).get("name") or "someone").split(" ")[0]
    is_owner = owner == str(asker).lower()
    if role == "kid" and not is_owner:
        return KID_OTHER   # #446 v2 §3 (approved 10-09): a kid never reads another person's body, nor asks for it
    whose = "your" if is_owner else f"{oname}'s"
    what = _what(ref["kind"])
    facts = derived(ref)
    head = f"“{ref['title'] or what}” ({what}, {whose})"
    known = ("What titan knows about it: " + "; ".join(facts) + ".") if facts else "titan has no facts from it."
    if ref.get("server"):
        # #446 v2: server mail. Its row is the reference and the server is the store: only its owner ever finds it
        # (server_refs gives the asker's own), and the body is read now, for this answer only
        if not is_owner or server_body is None:
            return NOT_FOUND
        got = server_body(owner, ref, min(int(args.get("max_chars") or MAX_CHARS), MAX_CHARS)) or {}
        if got.get("found") is False:
            return f"{head}. {known} It's no longer on your mail server, so I can't read it."
        if not got.get("found"):
            return f"{head}. {known} Your mail server didn't answer, so I can't read it now."
        raw = redact.redact_personal(str(got.get("text") or ""))[:MAX_CHARS]
        return (f"{head}, read from your mail server just now for this answer only (titan keeps no copy).\n"
                + wrap(raw, "mail", None))
    mac = next((m for m in holders(owner) if m.get("device_id") == ref["device_id"]), None)
    device = (mac or {}).get("name") or "device"
    if mac is None:
        return f"{head}. {known} " + NO_HOLDER.format(what=what, whose=whose, device=device)
    phone = str(mac.get("platform") or "") in PHONE_PLATFORMS
    if phone:   # #776: an iPhone answers the kinds it declared (PATCH /v1/devices/self {holds})
        if ref["kind"] not in (mac.get("holds") or []):
            return f"{head}. {known} " + OLD_APP.format(whose=whose.capitalize(), device=device)
    else:
        if ref["kind"] != "mail":
            return f"{head}. {known} {MAC_NO_SCANS}"
        if mac.get("device_tools") is not None and "item_body" not in (mac.get("device_tools") or []):
            return f"{head}. {known} " + OLD_APP.format(whose=whose.capitalize(), device=device)
    req = {"tool": "item_body", "args": {
        "ref_id": ref["id"], "kind": ref["kind"], "native_id": ref["native_id"], "content_hash": ref["content_hash"],
        "text_hash": ref["content_hash"],   # a connector reference's hash is its synced text's (sha256, UTF-8)
        "max_chars": min(int(args.get("max_chars") or MAX_CHARS), MAX_CHARS),
        "purpose": "quote" if str(args.get("purpose") or "") == "quote" else "answer",
        "asker": {"person_id": asker, "first_name": asker_name, "role": role}, "for_owner": is_owner},
        "risk": "read"}
    approve = None
    if not is_owner:
        if add_card is None:
            return f"{head}. {known} " + DECLINED.format(owner=oname)
        def approve() -> bool:  # noqa: E306 - the owner's card, or their Always grant
            return ask_owner(owner, asker, asker_name, ref["kind"], ref["title"] or what, add_card=add_card,
                             timeout=consent_timeout, grants_path=grants_path)
    res = route(req, owner, mac, approve) or {}
    text = str(res.get("result") or res.get("error") or "")
    import cross_device as xd
    if text == xd.NO_WAKE or text == xd.NO_ANSWER:
        if phone:
            tail = (PHONE_QUIET_OWN.format(what=what, device=device) if is_owner
                    else PHONE_QUIET_OTHER.format(owner=oname, device=device))
        else:
            tail = (ASLEEP_OWN.format(what=what, device=device) if is_owner
                    else ASLEEP_OTHER.format(owner=oname, device=device))
        return f"{head}. {known} {tail}"
    if text == xd.DECLINED or res.get("completed") is False:
        tail = OWNER_DECLINED if is_owner else DECLINED.format(owner=oname)
        return f"{head}. {known} {tail}"
    try:
        body = json.loads(text) if text.strip().startswith("{") else {}
    except ValueError:
        body = {}
    if not body:
        return f"{head}. {known} Your {device} didn't give it back."
    if body.get("found") is False:
        references.mark_gone(ref["id"], path=refs_path)
        return f"{head}. {known} " + GONE.format(whose=whose, device=device)
    raw = redact.redact_personal(str(body.get("text") or ""))[:req["args"]["max_chars"]]
    now_hash = str(body.get("text_hash") or body.get("content_hash") or "").lower()
    changed = bool(now_hash and ref.get("content_hash") and now_hash != str(ref["content_hash"]).lower())
    if now_hash:
        references.seen(ref["id"], now_hash, path=refs_path)
    note = (f" It changed on {whose} {device} since titan last saw it; this is the version there now." if changed else "")
    return (f"{head}, read from {whose} {device} just now for this answer only (titan keeps no copy).{note}\n"
            + wrap(raw, "mail" if ref["kind"] == "mail" else "scan", None if is_owner else oname))


def fixture_route(fixture: dict[str, Any] | None) -> Callable[..., dict[str, Any]]:
    """An eval turn's Mac (eval_mac): "asleep" is a Mac that doesn't wake; otherwise the fixture item, never a device.
    A reader who isn't the owner is still asked (approve()), so the consent path is exercised."""
    import cross_device as xd

    def route(req: dict[str, Any], owner: str, mac: dict[str, Any], approve: Callable[[], bool] | None) -> dict[str, Any]:
        f = fixture or {}
        if f.get("asleep"):
            return {"result": xd.NO_WAKE, "completed": False}
        if approve is not None and not approve():
            return {"result": xd.DECLINED, "completed": False}
        text = str(f.get("text") or "Fixture body.")
        return {"result": json.dumps({"found": f.get("found", True), "title": f.get("title") or "",
                                      "text": text, "content_hash": f.get("content_hash") or req["args"]["content_hash"]})}
    return route


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(str(text).encode()).hexdigest()
