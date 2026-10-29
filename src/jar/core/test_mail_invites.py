#!/usr/bin/env python3
# gate: hermetic   (fixtures only; the cards go to a throwaway proactive store)
"""Meeting invites, reschedules and cancels in mail (mail_invites + mail_suggestions): read by rule, never by the
model; a relative day counted from the mail's own Date header; times converted to the household's zone; the owner's
own calendar matched by UID or title; Add, Update or Remove cards to the mail's owner only, one per invite; nothing
when unsure. Fictional people and companies only.

    python3 scripts/test_mail_invites.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mailinv-test-"))
for k, v in {"AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db", "MAIL_TRIAGE_DB": "mail_triage.db",
             "HOUSEHOLD_DB": "household.db", "CONNECTORS_LEDGER": "ledger.db", "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ.setdefault("MEMORY_HYBRID", "0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mail_invites as I  # noqa: E402
import mail_suggestions as M  # noqa: E402
import mail_triage as T  # noqa: E402
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


def mail(sid, subject, body, owner="alex", sent="Mon, Oct 5, 2026, 9:14 AM", sender="Robin Hale <robin@fabrikam.example>",
         extra=""):
    text = f"From: {sender}\nTo: Alex Hale <alex@hale.example>\nSubject: {subject}\nDate: {sent}{extra}\n\n{body}"
    return {"source_id": sid, "source_type": "connector:mail", "title": subject, "updated_at": "v1",
            "metadata": {"owner": owner, "observed_at": "2026-10-05T16:14:00Z"}, "_text": text}


def event(title, start, owner="alex", uid="evt-1"):
    return {"data": {"title": title, "start": start, "provenance": {"owner": owner, "source_id": f"{uid}@{start}"}}}


def no_model(prompt):
    raise AssertionError("an invite is read by rule, never by the model")


ROLES = {"alex": "admin", "robin": "adult", "ava": "kid"}
NOW = datetime(2026, 10, 6, 9, 0)          # household time, outside quiet hours; the mails were sent on Oct 5
PDB = _TMP / "cards.db"


def go(srcs, events, con, now=NOW, llm=no_model):
    texts = {x["source_id"]: x["_text"] for x in srcs}
    sent = []

    def send(title, detail, **kw):
        sent.append({"title": title, "detail": detail, **kw})
        proactive.add_card(title, detail, db=PDB, **kw)

    st = M.run(True, srcs, texts.__getitem__, llm, events, con, now, ROLES, send=send)
    return st, sent


def fresh():
    return M._db(Path(tempfile.mkdtemp(dir=_TMP)) / "s.db")


print("── an Outlook invite forwarded, its When: in Mountain Time ──")
OUTLOOK = mail("m-fw", "FW: Q4 planning",
               "-----Original Appointment-----\nFrom: Jordan Reyes <jordan@contoso.example>\n"
               "Sent: Friday, October 2, 2026 4:12 PM\nTo: Robin Hale\nSubject: Q4 planning\n"
               "When: Thursday, October 15, 2026 11:00 AM-12:00 PM (UTC-07:00) Mountain Time (US & Canada).\n"
               "Where: Microsoft Teams Meeting\n\nPlease review the deck before we meet.")
check("an event with no recorded owner matches no one (fail closed, never assumed to be the admin's)",
      M._event_owner({"title": "Project sync", "provenance": {}}), "")
check("detected by rule", I.form_of(OUTLOOK["title"], OUTLOOK["_text"]), "when")
check("a candidate, whatever triage said", M.is_candidate(OUTLOOK["title"], OUTLOOK["_text"]), True)
con = fresh()
st, sent = go([OUTLOOK], [], con)
check("Denver's 11 AM is the household's 10 AM: an Add card", [(c["title"], c["to"]) for c in sent],
      [("Add “Q4 planning” on Thu Oct 15, 10:00 AM to your calendar?", ["alex"])])
check("…Add and Ignore, the button built from the checked fields",
      [(a["label"], a["kind"]) for a in sent[0]["actions"]], [("Add", "prompt"), ("Ignore", "dismiss")])
check("…the button creates the event at the household's time, as the person's own tap",
      sent[0]["actions"][0]["value"],
      M.CARD_PROMPT_MARK + ' Create a calendar event ⟪Q4 planning⟫ on 2026-10-15 from 10:00 to 11:00.')
check("…the card carries the title and time only: no sender, address, place or subject line",
      any(x in sent[0]["title"] + sent[0]["detail"] for x in ("Jordan", "Robin", "@", "Teams", "FW:", "deck")), False)
check("…a suggestion that expires by the event's start (within a week)",
      (sent[0]["kind"], 0 < sent[0]["expires_in"] <= 7 * 86400), ("suggestion", True))
check("…counts only in the stats", (st["invites"], st["outcomes"]),
      ({"add": 1, "update": 0, "cancel": 0}, {"invite-add": 1}))

print("── an updated invitation moves an existing event: Update, same card ──")
FIRST = mail("m-g1", "Invitation: Project sync @ Thu Oct 15, 2026 11am - 12pm (PDT) (robin@fabrikam.example)",
             "Robin Hale has invited you to Project sync. Join with Google Meet.", sender="Robin Hale <robin@fabrikam.example>")
con = fresh()
st, sent1 = go([FIRST], [], con)
check("not on the calendar yet: Add", [c["title"] for c in sent1],
      ["Add “Project sync” on Thu Oct 15, 11:00 AM to your calendar?"])
UPD = mail("m-g2", "Updated invitation: Project sync @ Thu Oct 15, 2026 12pm - 1pm (PDT) (robin@fabrikam.example)",
           "This event has been changed. Project sync, Thursday Oct 15, 2026 12pm – 1pm Pacific Time - Los Angeles",
           sent="Tue, Oct 6, 2026, 8:02 AM")
ON_CAL = [event("Project sync", "2026-10-15T18:00:00Z")]          # 11 AM PDT: the person tapped Add
st, sent2 = go([UPD], ON_CAL, con, now=datetime(2026, 10, 6, 10, 0))
check("an Update card: move it", [c["title"] for c in sent2], ["Move “Project sync” to Thu Oct 15, 12:00 PM?"])
check("…Update and Ignore", [(a["label"], a["kind"]) for a in sent2[0]["actions"]], [("Update", "prompt"), ("Ignore", "dismiss")])
check("…the button moves the owner's own event, found by its time",
      sent2[0]["actions"][0]["value"], M.CARD_PROMPT_MARK + ' Move my calendar event ⟪Project sync⟫ on 2026-10-15T11:00 '
                                                             "to start 2026-10-15T12:00 and end 2026-10-15T13:00.")
check("…the same key as the invite's first card", sent2[0]["key"], sent1[0]["key"])
cards = [c for c in proactive.list_cards(db=PDB, include_acked=True) if c["key"] == sent1[0]["key"]]
check("…so the person has one card, now the Update", [c["title"] for c in cards],
      ["Move “Project sync” to Thu Oct 15, 12:00 PM?"])
check("…the key carries no title", "sync" in sent1[0]["key"] or "project" in sent1[0]["key"], False)

print("── a cancel ──")
CANCEL = mail("m-g3", "Canceled event: Project sync @ Thu Oct 15, 2026 12pm - 1pm (PDT) (robin@fabrikam.example)",
              "This event has been canceled and removed from your calendar.", sent="Wed, Oct 7, 2026, 7:30 AM")
MOVED = [event("Project sync", "2026-10-15T19:00:00Z")]           # noon PDT
st, sent3 = go([CANCEL], MOVED, con, now=datetime(2026, 10, 7, 9, 0))
check("on the calendar: a Remove card, same key", [(c["title"], c["key"] == sent1[0]["key"]) for c in sent3],
      [("Remove “Project sync” on Thu Oct 15, 12:00 PM?", True)])
check("…Remove and Ignore, deleting that one event only on a tap",
      ([(a["label"], a["kind"]) for a in sent3[0]["actions"]], sent3[0]["actions"][0]["value"]),
      ([("Remove", "prompt"), ("Ignore", "dismiss")],
       M.CARD_PROMPT_MARK + ' Delete my calendar event ⟪Project sync⟫ on 2026-10-15T12:00.'))
OUT_CANCEL = mail("m-o1", "Cancelled: Budget review", "When: Friday, October 16, 2026 9:00 AM-9:30 AM (UTC-08:00) Pacific "
                  "Time (US & Canada).\nThis meeting has been cancelled.", sent="Wed, Oct 7, 2026, 7:30 AM")
check("Outlook's \"Cancelled:\" is a cancel", (I.read(OUT_CANCEL["title"], OUT_CANCEL["_text"]) or {}).get("kind"), "cancel")
st, sent4 = go([OUT_CANCEL], [], fresh(), now=datetime(2026, 10, 7, 9, 0))
check("a cancel of something never on the calendar: silent", (sent4, st["outcomes"]), ([], {"invite-gone": 1}))
con = fresh()
go([mail("m-b1", "Invitation: Budget review @ Fri Oct 16, 2026 9am - 9:30am (PDT) (robin@fabrikam.example)", "Join us.")],
   [], con)
st, sent5 = go([OUT_CANCEL], [], con, now=datetime(2026, 10, 7, 9, 0))
check("…but a sent Add card for it says it was canceled (same key), so nobody adds it",
      [(c["title"], [a["label"] for a in c["actions"]]) for c in sent5], [("“Budget review” was canceled", ["Ignore"])])

print("── a relative weekday, counted from the mail's Date header ──")
REL = mail("m-r1", "Coffee next week?", "Hi Alex, meeting with Jordan Thursday at 3pm works for me.",
           sent="Mon, Oct 5, 2026, 9:14 AM")
st, sent = go([REL], [], fresh(), now=datetime(2026, 10, 7, 9, 0))     # read two days later: still Oct 8
check("Thursday after Monday Oct 5 is Oct 8, whatever today is", [c["title"] for c in sent],
      ["Add “Meeting with Jordan” on Thu Oct 8, 3:00 PM to your calendar?"])
SAME_DAY = mail("m-r2", "Coffee?", "Hi Alex, meeting with Jordan Thursday at 3pm works for me.",
                sent="Thu, Oct 8, 2026, 9:14 AM")
st, sent = go([SAME_DAY], [], fresh(), now=datetime(2026, 10, 8, 10, 0))
check("\"Thursday\" sent on a Thursday (today or next week?): no card", (sent, st["outcomes"]), ([], {"invite-unsure": 1}))
TOMORROW = mail("m-r3", "Meeting with Ava tomorrow 9:30am", "See you then.", sent="Tue, Oct 6, 2026, 6:00 PM")
check("\"tomorrow\" is the day after the Date header", {k: v for k, v in (I.read(TOMORROW["title"], TOMORROW["_text"],
      I.sent_at(TOMORROW["_text"])) or {}).items() if k in ("date", "start")}, {"date": "2026-10-07", "start": "09:30"})
check("a server mail's RFC Date header, in its own zone", I.sent_at("Date: Thu, 8 Oct 2026 01:30:00 +0000\n\nx",
      I.zone_of("America/Los_Angeles")), datetime(2026, 10, 7, 18, 30))

print("── a newsletter that mentions a meeting: nothing ──")
NEWS = mail("m-n1", "Hale Gardens newsletter: our members' meeting",
            "Our members' meeting with Mayor Leo is Thursday at 7pm in the hall. Unsubscribe | View in browser",
            sender="Hale Gardens <news@halegardens.example>", extra="\nList-Unsubscribe: yes")
check("not an invite, not a candidate", (I.detect(NEWS["title"], NEWS["_text"]), M.is_candidate(NEWS["title"], NEWS["_text"])),
      (False, False))
st, sent = go([NEWS], [], fresh(), llm=lambda p: json.dumps({"kind": "event", "title": "Members meeting",
                                                             "date": "2026-10-08", "start": "19:00"}))
check("…so no card, even with a model that would have read one out of it", (sent, st["candidates"]), ([], 0))
WEBINAR = mail("m-n2", "Invitation: Webinar on cloud savings @ Thu Oct 15, 2026 10am (PDT)", "Register now.",
               sender="Contoso Events <events@contoso.example>")
check("a webinar invitation isn't one either", I.detect(WEBINAR["title"], WEBINAR["_text"]), False)
check("a reply to the reader's own invite (Accepted:) isn't one",
      I.detect("Accepted: Q4 planning", "From: Jordan\nSubject: Accepted: Q4 planning\n\nWhen: Thursday, October 15, 2026 11:00 AM"),
      False)

print("── an unparseable time: nothing ──")
VAGUE = mail("m-u1", "FW: Offsite", "When: Thursday, October 15, 2026, sometime in the afternoon\nWhere: TBD")
st, sent = go([VAGUE], [], fresh())
check("a When: line without a time: no card", (sent, st["outcomes"]), ([], {"invite-unsure": 1}))
BARE = mail("m-u2", "Lunch", "Meeting with Jordan Thursday at 3 works.")
check("\"at 3\" isn't a time: not even an invite", I.detect(BARE["title"], BARE["_text"]), False)
WRONG = mail("m-u3", "FW: Offsite", "When: Thursday, October 16, 2026 11:00 AM-12:00 PM (UTC-08:00) Pacific Time")
st, sent = go([WRONG], [], fresh())
check("a weekday that disagrees with the date: no card, never a guess", (sent, st["outcomes"]), ([], {"invite-unsure": 1}))
ZONE = mail("m-u4", "FW: Offsite", "When: Thursday, October 15, 2026 11:00 AM-12:00 PM (UTC+25:00)")
check("a zone that isn't one: no card", M.check(I.read(ZONE["title"], ZONE["_text"]) or {}, ZONE["title"],
                                                 I.body_of(ZONE["_text"]), date(2026, 10, 6)), None)

print("── owner-only ──")
ROBIN = mail("m-p1", "Invitation: Project sync @ Thu Oct 15, 2026 12pm - 1pm (PDT) (jordan@contoso.example)",
             "Jordan Reyes has invited you.", owner="robin")
KID = mail("m-p2", "Invitation: Science fair @ Fri Oct 16, 2026 4pm (PDT) (leo@school.example)", "Come see it.", owner="ava")
st, sent = go([ROBIN, KID], [event("Project sync", "2026-10-15T18:00:00Z", owner="alex")], fresh())
check("Robin's invite: an Add to Robin only (Alex's calendar is never looked at for her)",
      [(c["title"], c["to"]) for c in sent], [("Add “Project sync” on Thu Oct 15, 12:00 PM to your calendar?", ["robin"])])
check("…a kid's mail is never read", st["mails"] == 2 and st["candidates"] == 1, True)
check("…the stored card is hers alone", [c["to"] for c in proactive.list_cards(db=PDB, include_acked=True)
                                          if c["key"] == sent[0]["key"]], [json.dumps(["robin"])])

print("── already there at that time: silent ──")
SAME = mail("m-s1", "Invitation: Project sync @ Thu Oct 15, 2026 11am - 12pm (PDT) (robin@fabrikam.example)", "Join.")
st, sent = go([SAME], [event("Project sync", "2026-10-15T18:00:00Z")], fresh())
check("same day, same time: no card", (sent, st["outcomes"]), ([], {"invite-same": 1}))
ICS = mail("m-i1", "Budget review", "BEGIN:VCALENDAR\nMETHOD:REQUEST\nBEGIN:VEVENT\nUID:4f2a-77@contoso.example\n"
           "SUMMARY:Budget review\nDTSTART;TZID=America/Denver:20261015T110000\n"
           "DTEND;TZID=America/Denver:20261015T120000\nSEQUENCE:2\nEND:VEVENT\nEND:VCALENDAR")
st, sent = go([ICS], [event("Q4 money talk", "2026-10-14T16:00:00Z", uid="4f2a-77@contoso.example")], fresh())
check("a calendar part's UID finds the event even under another title: Update", [c["title"] for c in sent],
      ["Move “Q4 money talk” to Thu Oct 15, 10:00 AM?"])

print("── the old path: a same-day time change is no longer swallowed ──")
APPT = mail("m-l1", "Appointment moved", "Your cleaning with Dr. Ruiz is now Tuesday, Oct 13 at 2:30 PM.",
            sender="Front Desk <frontdesk@brightsmile.example>")
llm = lambda p: json.dumps({"kind": "event", "title": "Dental cleaning", "date": "2026-10-13", "start": "14:30"})  # noqa: E731
st, sent = go([APPT], [event("Dental cleaning", "2026-10-13T16:00:00Z")], fresh(), llm=llm)
check("on the calendar at 9 AM that day: Move it to 2:30", [c["title"] for c in sent],
      ["Move “Dental cleaning” to Tue Oct 13, 2:30 PM?"])
st, sent = go([APPT], [event("Dental cleaning", "2026-10-13T21:30:00Z")], fresh(), llm=llm)
check("…already at 2:30: silent", (sent, st["outcomes"]), ([], {"on-calendar": 1}))
check("a relative day is shown when it's the Date header's", M._date_shown(date(2026, 10, 8), "see you Thursday",
                                                                          date(2026, 10, 5)), True)
check("…and not against another day's header", M._date_shown(date(2026, 10, 8), "see you Thursday", date(2026, 10, 9)),
      False)

print("── triage keeps invites the model calls notifications ──")
T.enqueue("cx-mail-inv", "alex", UPD["title"], UPD["_text"], {"owner": "alex"})
T.enqueue("cx-mail-note", "alex", "Robin commented on your post", "From: noreply@social.example\n\nRobin commented.",
          {"owner": "alex"})
st = T.run(True, index=lambda *a: True, llm=lambda p: '{"category": "notification"}')
check("the invite is kept as a record for suggestions; the ping is dropped",
      (T.category_of("cx-mail-inv"), T.category_of("cx-mail-note"), st["invites"]), ("record", "notification", 1))
check("…and the suggestions pass sees it", [r["source_id"] for r in T.record_sources()], ["cx-mail-inv"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
