#!/usr/bin/env python3
# gate: hermetic   (fixtures only; signals in a throwaway links.db, cards in a throwaway proactive store)
"""Text reschedules and cancels (event_signals + hooks.put_device + mail_suggestions' queue): a strictly shaped,
body-free signal from the poster's own device; a card to the owner only, about their own calendar event, titled from
that event's record; nothing when unsure, someone else's or ownerless; one card per event; never a second card for a
change a mail invite already proposed. Fictional people only. Confidences are the Mac's scale (jellybean#1062):
0.85 clear, 0.7 day only, 0.4 a cancel that fell back to the day, 0.2 vague.

    python3 scripts/test_event_signals.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-evsig-test-"))
for k, v in {"AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_DB": "household.db",
             "CONNECTORS_LEDGER": "ledger.db", "KB_ACL_DB": "kb_acl.db", "LINKS_DB": "links.db",
             "LINK_SALT_FILE": "link_salt", "MAIL_SUGGESTIONS_DB": "mail_suggestions.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ.setdefault("MEMORY_HYBRID", "0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import connectors_runner as CR  # noqa: E402
import event_signals as E  # noqa: E402
import hooks as H  # noqa: E402
import local_time  # noqa: E402
import mail_suggestions as M  # noqa: E402
import proactive  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  MISS {label}: got {got!r}, expected {expected!r}")


ROLES = {"alex": "admin", "robin": "adult", "ava": "kid"}
NOW = datetime(2026, 10, 6, 9, 0)                                   # household time, naive; outside quiet hours
NOW_AWARE = NOW.replace(tzinfo=local_time.zone())
PDB = _TMP / "cards.db"
E._now = lambda: NOW_AWARE                                          # validation's clock, pinned


def sha16(s):
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def mac_ref(source_id):
    """What the Mac sends: "cx-calendar-" + sha1(CalendarMapping.sourceID)[:16]."""
    return "cx-calendar-" + sha16(source_id)


def node(title, start, end, owner, uid):
    """A calendar record as connectors_runner writes it: node id = item_id("calendar", key), key = the source_id for
    the default owner, "<owner>:<source_id>" for anyone else."""
    utc = local_time.parse_iso(start).astimezone(local_time.parse_iso("2026-01-01T00:00:00+00:00").tzinfo)
    sid = f"{uid}@{utc:%Y-%m-%dT%H:%M:%SZ}"
    prov = {"source": "calendar", "source_id": sid}
    if owner:
        prov["owner"] = owner
        iid = CR.item_id("calendar", sid if owner == CR.OWNER_DEFAULT else f"{owner}:{sid}")
        prov["item_id"] = iid
    else:
        iid = CR.item_id("calendar", sid)
    return {"node_id": iid, "node_type": "appointment",
            "data": {"title": title, "start": start, "end": end, "provenance": prov}}


DENTIST = node("Dentist with Dr. Hale", "2026-10-08T15:00:00-07:00", "2026-10-08T16:00:00-07:00", "alex", "evt-dentist")
STUDY = node("Study group at Leo's", "2026-10-09T18:00:00-07:00", "2026-10-09T19:30:00-07:00", "alex", "evt-study")
PIANO = node("Piano lesson", "2026-10-10T10:00:00-07:00", None, "robin", "evt-piano")
ORPHAN = node("Lunch with Jordan", "2026-10-11T12:00:00-07:00", None, None, "evt-lunch")
SOCCER = node("Soccer practice", "2026-10-07T16:00:00-07:00", "2026-10-07T17:00:00-07:00", "ava", "evt-soccer")
SHARED_ALEX = node("Family dinner", "2026-10-12T18:00:00-07:00", None, "alex", "evt-dinner")
SHARED_ROBIN = node("Family dinner", "2026-10-12T18:00:00-07:00", None, "robin", "evt-dinner")
EVENTS = [DENTIST, STUDY, PIANO, ORPHAN, SOCCER, SHARED_ALEX, SHARED_ROBIN]


def ref_of(n):
    return mac_ref(n["data"]["provenance"]["source_id"])


def sig(signal, n, start, conf, sid=None, **extra):
    body = {"kind": "event_signal", "signal": signal, "event_ref": ref_of(n) if isinstance(n, dict) else n,
            "proposed_start": start, "confidence": conf,
            "sig_id": sid or sha16(f"{signal}{n if isinstance(n, str) else n['node_id']}{start}{conf}")}
    body.update(extra)
    return body


def fresh():
    d = Path(tempfile.mkdtemp(dir=_TMP))
    return H.connect(d / "links.db"), M._db(d / "s.db")


def post(links, person, body, role="adult", device="dev-1"):
    try:
        return H.put_device(person, role, device, body, con=links)
    except H.HookError as exc:
        return exc.status


sent: list[dict] = []


def send(title, detail, **kw):
    sent.append({"title": title, "detail": detail, **kw})
    proactive.add_card(title, detail, db=PDB, **kw)


def go(links, cards, now=NOW, events=EVENTS):
    st = E.process(links, events, cards, now, ROLES, True)
    sent.clear()
    M.send_held(cards, now, send)
    return st


def outcomes(st):
    return st["outcomes"]


print("── validation: only the six fields, strictly shaped, never text ──")
links, cards = fresh()
ok = sig("reschedule", DENTIST, "2026-10-15T12:00:00-07:00", 0.85)
check("a well-formed signal is accepted", post(links, "alex", ok), {"kind": "event_signal", "sig_id": ok["sig_id"],
                                                                    "accepted": True})
check("free text rides along: 400", post(links, "alex", {**ok, "text": "can we move to Thursday?"}), 400)
check("a sender rides along: 400", post(links, "alex", {**ok, "sender": "+15555550123"}), 400)
check("an unknown field: 400", post(links, "alex", {**ok, "note": "x"}), 400)
check("the old item_id field: 400", post(links, "alex", {**{k: v for k, v in ok.items() if k != "sig_id"},
                                                         "item_id": "abc"}), 400)
check("a missing field: 400", post(links, "alex", {k: v for k, v in ok.items() if k != "confidence"}), 400)
check("a bad ref (uppercase hex): 400", post(links, "alex", {**ok, "event_ref": ok["event_ref"].upper()}), 400)
check("a bad ref (another source): 400", post(links, "alex", {**ok, "event_ref": "cx-mail-" + "a" * 16}), 400)
check("a bad ref (text in it): 400", post(links, "alex", {**ok, "event_ref": "cx-calendar-dentist moved"}), 400)
check("a bad sig_id: 400", post(links, "alex", {**ok, "sig_id": "not-a-guid-hash!"}), 400)
check("a signal other than the two: 400", post(links, "alex", {**ok, "signal": "move"}), 400)
check("a past date: 400", post(links, "alex", {**ok, "proposed_start": "2026-10-05T12:00:00-07:00"}), 400)
too_far = (NOW_AWARE + timedelta(days=61)).isoformat()
check("more than 60 days out: 400", post(links, "alex", {**ok, "proposed_start": too_far}), 400)
check("no offset: 400", post(links, "alex", {**ok, "proposed_start": "2026-12-15T12:00:00"}), 400)
check("words in the date: 400", post(links, "alex", {**ok, "proposed_start": "Thursday at noon"}), 400)
check("confidence as a string: 400", post(links, "alex", {**ok, "confidence": "0.85"}), 400)
check("confidence above 1: 400", post(links, "alex", {**ok, "confidence": 1.5}), 400)
check("confidence as a bool: 400", post(links, "alex", {**ok, "confidence": True}), 400)
check("a shared household screen: 403", post(links, "alex", ok, role="household"), 403)
check("no paired device: 403", post(links, "alex", ok, device=None), 403)
try:
    E.validate({**ok, "proposed_start": "2026-01-02T12:00:00-08:00"}, NOW_AWARE)
    msg = ""
except E.SignalError as exc:
    msg = str(exc)
check("an error never echoes the value", "2026-01-02" in msg, False)
z = E.validate({**ok, "proposed_start": "2026-10-15T19:00:00Z"}, NOW_AWARE)
check("Z is an offset (UTC)", z["proposed_start"], "2026-10-15T19:00:00+00:00")
check("a null start is accepted (judged later)", E.validate({**ok, "proposed_start": None}, NOW_AWARE)["proposed_start"],
      None)
rows = links.execute("SELECT owner, signal, event_ref FROM event_signals").fetchall()
check("only the accepted one is kept, owned by the poster", rows, [("alex", "reschedule", ok["event_ref"])])
cols = [r[1] for r in links.execute("PRAGMA table_info(event_signals)")]
check("the table has no place for text, a sender or a handle", cols,
      ["owner", "sig_id", "signal", "event_ref", "proposed_start", "confidence", "made_at", "state", "outcome"])
check("a signal is no link hook (the linker never sees it)",
      links.execute("SELECT COUNT(*) FROM hooks").fetchone()[0], 0)

print("── event_ref: the Mac's formula and titan's ids ──")
check("the default owner's record id is exactly the Mac's ref",
      CR.item_id("calendar", "evt-x@2026-10-08T22:00:00Z"), mac_ref("evt-x@2026-10-08T22:00:00Z"))
check("anyone else's record id is keyed by person (so differs)", DENTIST["node_id"] == ref_of(DENTIST), False)
check("…and the Mac's ref still finds their own event", E.find(ref_of(DENTIST), "alex", EVENTS)[1], DENTIST["node_id"])
check("…as does the record id itself", E.find(DENTIST["node_id"], "alex", EVENTS)[1], DENTIST["node_id"])
check("a shared event's ref finds the poster's own copy",
      (E.find(ref_of(SHARED_ALEX), "alex", EVENTS)[1], E.find(ref_of(SHARED_ROBIN), "robin", EVENTS)[1]),
      (SHARED_ALEX["node_id"], SHARED_ROBIN["node_id"]))

print("── a reschedule → an Update card to the owner only, titled from their calendar ──")
st = go(links, cards)
check("one update", outcomes(st), {"update": 1})
c = sent[0] if sent else {}
check("title from the calendar record, household time", c.get("title"), "Move “Dentist with Dr. Hale” to Thu Oct 15, 12:00 PM?")
check("Update / Ignore", [a["label"] for a in c.get("actions") or []], ["Update", "Ignore"])
check("the Update button is the calendar update turn (keeps its hour)", (c.get("actions") or [{}])[0].get("value"),
      f'{M.CARD_PROMPT_MARK} Move my calendar event ⟪Dentist with Dr. Hale⟫ on 2026-10-08T15:00 to start '
      "2026-10-15T12:00 and end 2026-10-15T13:00.")
check("to the owner only", c.get("to"), ["alex"])
check("a stable key per event", c.get("key"), "textsig-" + DENTIST["node_id"])
check("its own source and kind (Stop these mutes text signals only)", (c.get("source"), c.get("kind")),
      ("text-signal", "suggestion"))
check("expires at the event's original start", c.get("expires_in"),
      (datetime(2026, 10, 8, 15, 0) - NOW).total_seconds())
check("the detail is the calendar's own time, nothing from the text", c.get("detail"),
      "A text says it moves. On your calendar now: Thu Oct 8, 3:00 PM.")
seen_by = {p: [x["title"] for x in proactive.list_cards(db=PDB, person=p, role=ROLES[p])] for p in ("alex", "robin")}
check("Alex sees it", c.get("title") in seen_by["alex"], True)
check("Robin doesn't", c.get("title") in seen_by["robin"], False)
check("stats carry counts only", json.dumps(st), json.dumps({"signals": 1, "outcomes": {"update": 1}}))
check("a judged signal isn't judged again", outcomes(go(links, cards)), {})

print("── the same event again: the same key, the card updated ──")
post(links, "alex", sig("reschedule", DENTIST, "2026-10-16T09:30:00-07:00", 0.85))
st = go(links, cards)
check("an update", outcomes(st), {"update": 1})
check("same key", sent[0]["key"] if sent else None, "textsig-" + DENTIST["node_id"])
check("one row for the event in the queue", cards.execute("SELECT COUNT(*) FROM cards WHERE key LIKE 'textsig-%'")
      .fetchone()[0], 1)
alex_cards = [x for x in proactive.list_cards(db=PDB, person="alex", role="admin") if x.get("key") == "textsig-" + DENTIST["node_id"]]
check("one card on Alex's devices, now the new time", [x["title"] for x in alex_cards],
      ["Move “Dentist with Dr. Hale” to Fri Oct 16, 9:30 AM?"])
post(links, "alex", sig("reschedule", DENTIST, "2026-10-08T15:00:00-07:00", 0.85))
check("a signal for the time it already has: nothing", outcomes(go(links, cards)), {"same": 1})

print("── a day-only signal (0.7) keeps the event's own time ──")
post(links, "alex", sig("reschedule", STUDY, "2026-10-13T18:00:00-07:00", 0.7))
go(links, cards)
check("Update card at the event's hour", sent[0]["title"] if sent else None,
      "Move “Study group at Leo's” to Tue Oct 13, 6:00 PM?")

print("── a cancel → a Remove card ──")
links, cards = fresh()
post(links, "alex", sig("cancel", DENTIST, None, 0.85))
st = go(links, cards)
check("one remove", outcomes(st), {"remove": 1})
c = sent[0] if sent else {}
check("Remove “title”?", c.get("title"), "Remove “Dentist with Dr. Hale”?")
check("Remove / Ignore", [a["label"] for a in c.get("actions") or []], ["Remove", "Ignore"])
check("the delete turn names the owner's event", (c.get("actions") or [{}])[0].get("value"),
      f'{M.CARD_PROMPT_MARK} Delete my calendar event ⟪Dentist with Dr. Hale⟫ on 2026-10-08T15:00.')
check("to the owner, same key family", (c.get("to"), c.get("key")), (["alex"], "textsig-" + DENTIST["node_id"]))

print("── nothing when unsure, someone else's, ownerless or gone ──")
links, cards = fresh()
post(links, "alex", sig("cancel", STUDY, None, 0.4))
post(links, "alex", sig("reschedule", STUDY, "2026-10-14T18:00:00-07:00", 0.2))
post(links, "alex", sig("reschedule", STUDY, None, 0.85))
post(links, "robin", sig("reschedule", DENTIST, "2026-10-15T12:00:00-07:00", 0.85))
post(links, "robin", {**sig("cancel", DENTIST, None, 0.85), "event_ref": DENTIST["node_id"]})
post(links, "alex", sig("reschedule", ORPHAN, "2026-10-12T12:00:00-07:00", 0.85))
post(links, "alex", sig("cancel", "cx-calendar-" + "0" * 16, None, 0.85))
st = go(links, cards)
check("low confidence ×2, no start, someone else's ×2, ownerless, unknown",
      outcomes(st), {"low-confidence": 2, "no-start": 1, "not-found": 4})
check("no card at all", sent, [])
check("nothing queued", cards.execute("SELECT COUNT(*) FROM cards").fetchone()[0], 0)
past = node("Coffee with Hale", "2026-10-06T08:00:00-07:00", None, "alex", "evt-coffee")
post(links, "alex", sig("cancel", past, None, 0.85))
check("an event that has started: nothing", outcomes(go(links, cards, events=EVENTS + [past])), {"started": 1})

print("── a kid's signal: their own calendar only ──")
links, cards = fresh()
post(links, "ava", sig("reschedule", SOCCER, "2026-10-08T16:00:00-07:00", 0.85), role="kid")
post(links, "ava", sig("cancel", DENTIST, None, 0.85), role="kid")
st = go(links, cards)
check("Ava's own event: a card; Alex's: nothing", outcomes(st), {"update": 1, "not-found": 1})
check("to Ava only", [(x["to"], x["title"]) for x in sent], [(["ava"], "Move “Soccer practice” to Thu Oct 8, 4:00 PM?")])

print("── a mail invite's card for the same change: no second card ──")
links, cards = fresh()
inv = {"kind": "update", "title": "Dentist with Dr. Hale", "date": "2026-10-15", "start": "12:00", "uid": "evt-dentist"}
check("the mail invite makes its Update card", M._invite(inv, "cx-mail-inv1", "alex", EVENTS, cards, True, NOW), ("invite-update", "update"))
row = cards.execute("SELECT event, target FROM cards WHERE key LIKE 'mailsug-inv-%'").fetchone()
check("…recording the event and the change", row, (DENTIST["node_id"], "2026-10-15T12:00"))
post(links, "alex", sig("reschedule", DENTIST, "2026-10-15T12:00:00-07:00", 0.85))
check("the text's same change: skipped", outcomes(go(links, cards)), {"dup-mail": 1})
check("only the mail's card was sent", [x["key"][:12] for x in sent], ["mailsug-inv-"])
post(links, "alex", sig("reschedule", DENTIST, "2026-10-15T19:00:00Z", 0.85, sid="1" * 16))
check("the same instant in UTC: still the same change", outcomes(go(links, cards)), {"dup-mail": 1})
post(links, "alex", sig("reschedule", DENTIST, "2026-10-16T12:00:00-07:00", 0.85))
check("a different change to that event: its own card", outcomes(go(links, cards)), {"update": 1})
post(links, "alex", sig("cancel", STUDY, None, 0.85))
go(links, cards)
cancel_inv = {"kind": "cancel", "title": "Study group at Leo's", "date": "2026-10-09", "start": "18:00",
              "uid": "evt-study"}
check("…and the other way: a mail cancel after the text's Remove card makes none",
      M._invite(cancel_inv, "cx-mail-inv2", "alex", EVENTS, cards, True, NOW), ("invite-dup-text", None))
check("one Remove card for the study group", cards.execute(
    "SELECT COUNT(*) FROM cards WHERE event=? AND target='cancel'", (STUDY["node_id"],)).fetchone()[0], 1)

print("── the dedup is the owner's own; both paths name the event alike (JEV's review) ──")
links, cards = fresh()
inv = {"kind": "update", "title": "Dentist with Dr. Hale", "date": "2026-10-15", "start": "12:00", "uid": "evt-dentist"}
M._invite(inv, "cx-mail-inv1", "alex", EVENTS, cards, True, NOW)
cards.execute("UPDATE cards SET owner='jordan' WHERE key LIKE 'mailsug-inv-%'")   # someone else's card, same change
post(links, "alex", sig("reschedule", DENTIST, "2026-10-15T12:00:00-07:00", 0.85))
check("another person's mail card for the same change never stands in for this person's", outcomes(go(links, cards)),
      {"update": 1})
links, cards = fresh()
post(links, "alex", sig("cancel", STUDY, None, 0.85))
go(links, cards)
cards.execute("UPDATE cards SET owner='jordan' WHERE key LIKE ?", (E.KEY_PREFIX + "%",))
check("…nor another person's text card for a mail cancel",
      M._invite(cancel_inv, "cx-mail-inv2", "alex", EVENTS, cards, True, NOW)[0] != "invite-dup-text", True)
ev = {"provenance": {"source_id": "UID-77", "owner": "alex"}}
check("an event with provenance but no item_id: the same id with or without its record node",
      (E.event_id(ev, "node-other") == E.event_id(ev), bool(E.event_id(ev))), (True, True))
check("…the node id only when nothing else tells", E.event_id({}, "node-9"), "node-9")

print("── the pass inside mail_suggestions' run: counts only ──")
links, cards = fresh()
post(links, "alex", sig("cancel", DENTIST, None, 0.85))
st = E.process(links, EVENTS, cards, NOW, ROLES, False)
check("a dry run judges but writes nothing", (outcomes(st), cards.execute("SELECT COUNT(*) FROM cards").fetchone()[0],
                                              links.execute("SELECT state FROM event_signals").fetchone()[0]),
      ({"remove": 1}, 0, "new"))
check("a repeated sig_id replaces its row",
      (post(links, "alex", sig("cancel", DENTIST, None, 0.85)) and
       links.execute("SELECT COUNT(*) FROM event_signals").fetchone()[0]), 1)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
