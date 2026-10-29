#!/usr/bin/env python3
# gate: hermetic
"""The weekly "what got filtered" digest (#861): counts by what triage decided, the most borderline dropped mail with
"Keep this kind" / "Right call", private to its owner; a tap is decided in code, only by the owner, once; "keep" puts
the mail back in memory; every answer becomes a triage case the model is scored on. Throwaway stores, made-up mail.
Run: python3 scripts/test_kb_digest.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-kbdigest-"))
for k, v in {"MAIL_TRIAGE_DB": "triage.db", "MAIL_TRIAGE_CASES": "cases.jsonl", "PROACTIVE_DB": "proactive.db",
             "KB_LIFECYCLE_LOG": "life.jsonl", "CONNECTORS_LEDGER": "ledger.db", "HOUSEHOLD_DB": "household.db",
             "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl", "DAILY_LOG_DIR": "daily",
             "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import kb_digest as D  # noqa: E402
import mail_triage as mt  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")


MAIL = {  # id: (owner, subject, first lines, what the model says)
    "m1": ("alex", "Dinner Friday?", "From: Robin Hale <robin@example.org>\nDinner at ours?", "personal"),
    "m2": ("alex", "Your card payment failed", "From: Hale Gym <billing@halegym.example>\nUpdate your card.", "action"),
    "m3": ("alex", "Receipt for your order", "From: Shop <noreply@shop.example>\nPaid $12.", "record"),
    "m4": ("alex", "Fall sale: 40% off", "From: Shop <marketing@shop.example>\nSale ends Sunday.", "marketing"),
    "m5": ("alex", "Re: school picnic volunteers", "From: Jordan Ava <jordan@example.org>\nCan you bring chairs?",
           "notification"),
    "m6": ("alex", "Your account security digest", "From: Cloud <no-reply@cloud.example>\nNew sign-in from Chrome.",
           "notification"),
    "m7": ("alex", "The Garden Weekly", "From: Garden <news@garden.example>\nBulbs for fall.", "newsletter"),
    "m8": ("robin", "Re: dentist appointment", "From: Hale Dental <frontdesk@haledental.example>\nSee you Tuesday.",
           "notification"),
}
for iid, (owner, title, text, _) in MAIL.items():
    mt.enqueue(iid, owner, title, text, {"source": "mail", "owner": owner})
by_title = {t: c for _, t, _, c in MAIL.values()}
mt.run(True, llm=lambda p: json.dumps({"category": next(c for t, c in by_title.items() if f"Subject: {t}" in p)}),
       index=lambda *a: True)

print("── the week's summary, per owner ──")
d = D.build("alex")
check("counts by what triage decided", d["counts"], {"personal": 1, "action": 1, "record": 1, "marketing": 1,
                                                     "notification": 2, "newsletter": 1})
check("the summary says it plainly", d["summary"].splitlines()[:3],
      ["Kept in your memory: 2 (1 personal, 1 needing action)", "Read for bills and receipts, then let go: 1",
       "Filtered out: 1 newsletters, 1 marketing, 2 notifications"])
check("the most borderline first: a person's Re:, then a security notice; a sale and a newsletter aren't close calls",
      [i["item_id"] for i in d["items"]], ["m5", "m6"])
check("another person's mail never appears", "m8" in json.dumps(d), False)
check("nobody with nothing triaged: no card", D.build("leo"), None)

print("── cards go to the owner only ──")
sent = []
n = D.send("alex", add_card=lambda *a, **k: sent.append((a, k)))
check("a summary and one card per close call", n, 3)
check("every card is for alex alone", {json.dumps(k["to"]) for _, k in sent}, {'["alex"]'})
first = sent[1]
check("a close call shows subject and sender, and two answers", (first[0][0], first[0][1],
      [x["value"] for x in first[1]["actions"]]),
      ("Filtered: Re: school picnic volunteers", "From Jordan Ava <jordan@example.org> · sorted as notification",
       ["keep:m5", "right:m5"]))

print("── answering ──")
indexed = []
check("someone else can't answer it", D.answer("keep:m5", "robin", index=lambda *a: indexed.append(a)),
      (False, "Only the person whose mail it is can answer that."))
said = D.answer("keep:m5", "alex", index=lambda *a: indexed.append(a))
check("keep: back in memory, noted", (said[0], [x[0] for x in indexed]), (True, ["m5"]))
check("…and it says that sender's mail is kept from now on (true now: #1644)", "kept for you from now on" in said[1], True)
check("…with its text", "bring chairs" in indexed[0][2], True)
check("once only", D.answer("keep:m5", "alex", index=lambda *a: indexed.append(a)), (True, "Already answered."))
check("right call: nothing comes back", (D.answer("right:m6", "alex", index=lambda *a: indexed.append(a))[0], len(indexed)),
      (True, 1))
cases = [json.loads(x) for x in Path(os.environ["MAIL_TRIAGE_CASES"]).read_text().splitlines()]
check("each answer is a triage case (subject, sender, what triage said, the answer)",
      [(c["title"], c["triaged"], c["label"]) for c in cases],
      [("Re: school picnic volunteers", "notification", "keep"), ("Your account security digest", "notification", "drop")])
check("the cases file is private", oct(Path(os.environ["MAIL_TRIAGE_CASES"]).stat().st_mode & 0o777), "0o600")
check("answered items don't come back next week", [i["item_id"] for i in D.build("alex")["items"]], [])
check("a card for mail past its hold says so", D.answer("keep:nope", "alex"),
      (False, "That mail isn't held any more: the week's hold ran out."))

print("── triage learns it, for that person only (#1644) ──")
check("the keep taught a rule for that sender's address, the right call counted",
      {r["sender"]: (r["rule"], r["keeps"], r["drops"]) for r in mt.rules("alex")},
      {"jordan@example.org": ("keep", 1, 0), "no-reply@cloud.example": ("drop", 0, 1)})
mt.enqueue("m9", "alex", "Re: picnic chairs", "From: Jordan Ava <JORDAN@example.org>\nThanks for the chairs!", {"owner": "alex"})
mt.enqueue("m10", "robin", "Picnic?", "From: Jordan Ava <jordan@example.org>\nAre you coming?", {"owner": "robin"})
asked, kept = [], []
st = mt.run(True, llm=lambda p: (asked.append(p), json.dumps({"category": "notification"}))[1],
            index=lambda iid, *a: kept.append(iid) or True)
check("Alex's next mail from Jordan (any case): kept, with no model call", ("m9" in kept, mt.category_of("m9"), st["learned"]),
      (True, "personal", 1))
check("…Robin's mail from the same address is decided by the model, as before (Alex's rule is Alex's)",
      (mt.category_of("m10"), len(asked)), ("notification", 1))
check("a drop answer never overrides the model, it only counts", mt.rule_for("alex", "Cloud <no-reply@cloud.example>"), "drop")
check("a sender with no address learns nothing", (mt.learn("alex", "Jordan Ava", "keep"), mt.sender_key("Jordan Ava")), (None, ""))
mt.learn("alex", "jordan@example.org", "drop")
mt.learn("alex", "jordan@example.org", "drop")
check("saying it was right to drop more often than to keep ends the keep rule", mt.rule_for("alex", "jordan@example.org"), "drop")
import me_delete  # noqa: E402
stores = [(p_, t, c) for p_, t, _o, c in me_delete.Parts(None)._stores()]
check("delete-my-data covers what they taught triage", ("triage", "sender_rules", "owner") in stores, True)

print("── the card's tap reaches the digest (proactive.card_action) ──")
import proactive  # noqa: E402
card = proactive.add_card("Filtered: x", "y", source="kb-digest", to=["robin"], key="kb-digest-item:m8",
                          actions=[{"id": "right", "label": "Right call", "kind": "reply", "value": "right:m8"}])
out = proactive.card_action(card["id"], "right", person="robin", role="adult")
check("answered in code, the card acked", (out["ok"], out.get("said")), (True, "Noted: dropping that kind was right."))

print("── the model, scored on the people's answers ──")
r = mt.eval_cases(llm=lambda p: json.dumps({"category": "personal" if "picnic" in p else "notification"}))
check("keep → personal/action, drop → anything else", (r["cases"], r["right"]), (3, 3))
r = mt.eval_cases(llm=lambda p: json.dumps({"category": "notification"}))
check("a model that drops the picnic again misses it", (r["right"], r["misses"]), (2, [{"label": "keep", "now": "notification"}]))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
