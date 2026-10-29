#!/usr/bin/env python3
# gate: hermetic
"""The linker's expectation harness (#951 (b), rule 6): assertions any correct ingestion must reproduce, judged against
a seeded fictional household's own corpus (about 200 items: Harbor Mobile, Harbor Gas, Lakeside Clinic, American
Express, American Water, Alex, Robin). A failure means the measurement is wrong, not that a threshold needs tuning.
Nothing in the linker or the hooks is specific to a trade-in. A temporary links.db and salt; never titan's.
Run: python3 scripts/test_linker_expectations.py"""
from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-linker-test-"))
for k, v in {"LINK_SALT_FILE": "link_salt", "LINKS_DB": "links.db", "WORLD_DIR": "world", "WORLD_DB": "world.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import hooks as H  # noqa: E402
import linker as L  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


con = H.connect()
TITLES: dict[str, str] = {}


def ENTS(text):   # the household's world model, in miniature: one organisation (fictional)
    return ["org:harbor-mobile"] if "harbor mobile" in text.lower() else []


def add(iid, src, item, owner="alex", aud="private"):
    TITLES[iid] = item.get("title") or ""
    H.index(con, iid, src, H.extract(item, ENTS), owner, aud)


def mail(frm, day, body, title=""):
    return {"title": title, "text": f"From: {frm}\nDate: {day}\nSubject: {title}\n\n{body}"}


# ── the background: a household's ordinary mail, so "rare" means rare here ──
rnd = random.Random(7)
DOMAINS = ["lakesideclinic.org", "harborgas.com", "brightlinehost.com", "maplelibrary.org", "riverschool.edu",
           "cedarpharmacy.com", "northwindair.com", "pinegrocer.com"]
for i in range(150):
    d = rnd.choice(DOMAINS)
    day = f"2026-{rnd.randint(7, 10):02d}-{rnd.randint(1, 28):02d}"
    amt = f"${rnd.choice([12, 18, 24, 35, 49, 61, 75, 99])}.{rnd.choice(['00', '99', '50'])}"
    add(f"bg-{i}", "mail", mail(f"News <hello@{d}>", day, f"Your receipt total {amt}. Thanks for visiting.", "Receipt"))
# a support line in 30 mails from many senders: too common to link anything
for i in range(30):
    add(f"support-{i}", "mail", mail(f"Help <help@{DOMAINS[i % 8]}>", f"2026-09-{(i % 28) + 1:02d}",
                                     "Questions? Call (800) 555-0199 any time.", "Your account"))

# ── the trade-in: a text and two mails, nothing trade-in-specific in code ──
add("text-1", "text", {"text": "HarborMobile: we got the device for order hm-2048-7731. Credit in 1-2 cycles.",
                       "at": "2026-09-27", "sender": "45021"})
add("mail-tradein", "mail", mail("Harbor Mobile <orders@mail.harbormobile.com>", "Sep 28, 2026",
                                 "Your trade-in for Order #HM-2048-7731 was received. A credit of $250.00 posts by Oct 15.",
                                 "Your trade-in was received"))
add("mail-credit", "mail", mail("Harbor Mobile <billing@harbormobile.com>", "Oct 14, 2026",
                                "A $250.00 trade-in credit was applied to your account.", "Your trade-in credit was applied"))
add("mail-otherorder", "mail", mail("Harbor Mobile <orders@harbormobile.com>", "Oct 2, 2026",
                                    "Order HM-3001-1150 shipped. Total $59.99.", "Your order shipped"))

# ── a newsletter that only names the company in its body, with the same rare amount ──
add("newsletter", "mail", mail("Tech Daily <news@techdaily.example>", "Oct 13, 2026",
                               "Harbor Mobile launches trade-in deals worth $250.00 through Oct 15.", "This week in phones"))
# ── two notifications from one system address, each mentioning the other's date ──
add("notif-1", "mail", mail("Calendar <noreply@calendar.example>", "Oct 6, 2026", "Dinner was added for Oct 9.",
                            "Event added"))
add("notif-2", "mail", mail("Calendar <noreply@calendar.example>", "Oct 9, 2026", "Movie night was added for Oct 6.",
                            "Event added"))

# ── one bank's unrelated mails, all quoting the same member number ──
for i, (day, title, body) in enumerate((("Sep 3, 2026", "Weekly alerts", "Member #MB-77120 has 2 new alerts."),
                                        ("Sep 18, 2026", "Statement ready", "Member #MB-77120: your statement is ready."),
                                        ("Sep 25, 2026", "Card declined", "Member #MB-77120: a card payment was declined."))):
    add(f"bank-{i}", "mail", mail("Lakeview Bank <alerts@lakeviewbank.example>", day, body, title))
# ── promotions that happen to share a tiny amount ──
add("promo-1", "mail", mail("Shop <deals@pineshop.example>", "Sep 5, 2026", "Save $0.14 per item today.", "Deals"))
add("promo-2", "mail", mail("Shop <deals@pineshop.example>", "Sep 9, 2026", "Only $0.14 more for gift wrap.", "Gift ideas"))

# ── a bill cycle and the next, with the payment line between ──
add("bill-sep", "record", {"title": "Harbor Gas bill", "vendor": "Harbor Gas", "amount": 61.20, "at": "2026-09-05",
                           "due_date": "2026-09-25"})
add("bill-oct", "record", {"title": "Harbor Gas bill", "vendor": "Harbor Gas", "amount": 64.10, "at": "2026-10-18",
                           "due_date": "2026-11-05"})
add("pay-sep", "record", {"title": "Payment received", "vendor": "Harbor Gas Inc.", "amount": 61.20, "at": "2026-09-24"})

# ── two companies with one first word, the same amount ──
add("amex", "record", {"title": "Statement", "vendor": "American Express", "amount": 45.00, "at": "2026-09-10"})
add("amwater", "record", {"title": "Water bill", "vendor": "American Water", "amount": 45.00, "at": "2026-09-11"})

# ── a record filed from its mail; one mail thread across two owners ──
add("mail-clinic", "mail", mail("Lakeside Clinic <billing@lakesideclinic.org>", "Oct 2, 2026",
                                "Message-ID: <a1@lakesideclinic.org>\nYour payment failed.", "Payment failed"))
add("bill-clinic", "record", {"title": "Payment failed", "vendor": "Lakeside Clinic", "amount": 38.85, "at": "2026-10-02",
                              "derived_from": "mail-clinic"})
add("fw-alex", "mail", {"title": "Fwd: plans", "text": "From: Robin <robin@example.net>\nReferences: <t9@example.net>\n\nsee below"})
add("fw-robin", "mail", {"title": "plans", "text": "From: Jordan <jordan@example.net>\nMessage-ID: <t9@example.net>\n\nplans"},
    owner="robin")

# ── a hidden bridge: Alex can see A and B, not H; only H connects them ──
add("bridge-a", "mail", {"title": "Ticket opened", "text": "Your ticket TK-55102 is open.", "at": "2026-09-01"})
add("bridge-h", "mail", {"title": "Escalation", "text": "Ticket TK-55102 is now case CS-77310.", "at": "2026-09-02"},
    owner="robin")
add("bridge-b", "mail", {"title": "Case update", "text": "Case CS-77310 was resolved.", "at": "2026-09-03"})

items = L.load(con)
edges = L.score(items, L.statements(con))
E = {(e.a, e.b): e for e in edges}


def edge(a, b):
    return E.get(L._pair(a, b))


def state(a, b):
    e = edge(a, b)
    return e.state if e else None


ALEX = lambda it: it.owner == "alex"  # noqa: E731 - what Alex may see here (all her own)


def thread_of(iid, ths):
    return next((sorted(t.items) for t in ths if iid in t.items), None)


NAMES = {"org:harbor-mobile": "Harbor Mobile"}
VENDORS = {"harbor gas": "Harbor Gas", "lakeside clinic": "Lakeside Clinic"}
th = L.threads_for(items, edges, ALEX, TITLES, NAMES, VENDORS)
print("── the trade-in (rule 6's first expectation) ──")
check("the text and the mail: the same order number, accepted", state("text-1", "mail-tradein"), "accepted")
check("the credit mail: same sender, a rare amount: accepted", state("mail-tradein", "mail-credit"), "accepted")
check("…one thread, the three of them", thread_of("text-1", th), ["mail-credit", "mail-tradein", "text-1"])
check("another order from the same company stays out (at most a candidate)",
      ("mail-otherorder" in (thread_of("text-1", th) or []), state("mail-tradein", "mail-otherorder") in (None, "candidate")),
      (False, True))
e = edge("mail-tradein", "mail-credit")
check("the why says what links them, typed then measured, never a value it shouldn't",
      (e.why.startswith("same sender (typed)"), "$250.00, rarer than" in e.why, "HM-2048" in e.why), (True, True, False))
check("…and the amount really is rare in this house", e.measured["amount"]["rarer_than"] >= L.AMOUNT_FLOOR, True)

check("a newsletter naming the company in its body, even with the same rare amount, doesn't join",
      ("newsletter" in (thread_of("text-1", th) or []), state("newsletter", "mail-credit"), state("newsletter", "mail-tradein")),
      (False, None, None))
check("…the company named in the senders is a party, in the newsletter only a mention",
      (sorted({h.kind for h in H.extract({"text": "From: Harbor Mobile <x@harbormobile.com>\n\nhi"}, ENTS)} & {"party:entity", "mention:entity"}),
       sorted({h.kind for h in H.extract({"text": "From: News <n@techdaily.example>\n\nHarbor Mobile news"}, ENTS)}
              & {"party:entity", "mention:entity"})), (["party:entity"], ["mention:entity"]))
check("two notifications from one address on coincident dates: a candidate at most", state("notif-1", "notif-2") in (None, "candidate"),
      True)

check("an account-level number (member, account, policy) joins nothing alone: a bank's unrelated mails stay apart",
      ({state("bank-0", "bank-1"), state("bank-1", "bank-2")} <= {None, "candidate"}, thread_of("bank-1", th)), (True, None))
check("…and the why calls it what it is", "same member number (typed)" in (edge("bank-1", "bank-2").why if edge("bank-1", "bank-2") else ""),
      True)
check("a shared amount under a dollar is never evidence", state("promo-1", "promo-2") in (None, "candidate"), True)
import topics as _TP  # noqa: E402
check("a topic is the title and body, never the mail's header lines",
      _TP.topic_text("Trade-in received", "From: A <a@x.example>\nDate: Oct 5\nMessage-ID: <1@x>\n\nThanks, we got it."),
      "Trade-in received\nThanks, we got it.")
check("…mail_server's shape too: References folded over lines, then a blank line",
      _TP.topic_text("Credit applied", "From: B <b@x.example>\nDate: Oct 14\nMessage-ID: <2@x>\nIn-Reply-To: <1@x>\n"
                     "References: <0@x>\n <1@x>\n\nA credit was applied.\nThanks."), "Credit applied\nA credit was applied.\nThanks.")
check("…a body with no headers is left whole", _TP.topic_text("Note", "Plain text\nsecond line"), "Note\nPlain text\nsecond line")

print("── the corpus decides what is common ──")
check("a support line in 30 mails links nothing by that number", [k for k, v in E.items()
      if any(t.startswith("same phone number") for t in v.typed)], [])
check("…two of those mails from one sender close in time are at most a candidate",
      {v.state for k, v in E.items() if k[0].startswith("support") and k[1].startswith("support")} <= {"candidate"}, True)

print("── a cycle, not the next ──")
check("the payment joins its own bill (same vendor, its amount)", state("bill-sep", "pay-sep"), "accepted")
check("…not the next cycle's", state("bill-oct", "pay-sep"), None)
check("…and the two bills aren't one thread", thread_of("bill-oct", th), None)

print("── two companies with one first word ──")
check("American Express and American Water: no edge, same amount or not", state("amex", "amwater"), None)

print("── structure ──")
check("a record filed from its mail: verified", state("mail-clinic", "bill-clinic"), "verified")
check("one mail thread across two owners: at most accepted", state("fw-alex", "fw-robin"), "accepted")

print("── per viewer: a hidden item never bridges ──")
check("Alex: A and B aren't a thread without H", (thread_of("bridge-a", th), thread_of("bridge-b", th)), (None, None))
th_admin = L.threads_for(items, edges, lambda it: True, TITLES)
check("…someone who may see all three gets the thread", thread_of("bridge-a", th_admin), ["bridge-a", "bridge-b", "bridge-h"])

print("── the person's word outranks the linker ──")
L.say(con, "text-1", "mail-tradein", False, "alex")
edges2 = L.score(items, L.statements(con))
th2 = L.threads_for(items, edges2, ALEX, TITLES)
check("'not related' rejects the edge and splits the text off", ({(e.a, e.b): e.state for e in edges2}[
      L._pair("text-1", "mail-tradein")], thread_of("text-1", th2)), ("rejected", None))
L.say(con, "text-1", "mail-tradein", True, "alex")
check("…'related' makes it verified", {(e.a, e.b): e.state for e in L.score(items, L.statements(con))}[
      L._pair("text-1", "mail-tradein")], "verified")

print("── names are earned ──")
t_trade = next(t for t in th if "text-1" in t.items)
check("the trade-in thread is cohesive enough to be named, for the organisation in its senders",
      (t_trade.cohesion >= L.COHESION_FLOOR, t_trade.name), (True, "Harbor Mobile"))
t_gas = next(t for t in th if "bill-sep" in t.items)
check("a vendor's thread is named for the vendor, as filed", t_gas.name, "Harbor Gas")
check("…without labels, one shared word ('trade-in') is a fragment: no name, the description stands",
      next(t for t in L.threads_for(items, edges, ALEX, TITLES) if "text-1" in t.items).name, None)
T2 = {**TITLES, "q1": "Kitchen remodel quote", "q2": "Re: Kitchen remodel quote revised"}
I2 = {**items, "q1": L.Item("q1", "mail", None, "alex", "private"), "q2": L.Item("q2", "mail", None, "alex", "private")}
check("a distinctive subject phrase two titles share names it", L._name(["q1", "q2"], I2, 1.0, T2, {}, {}),
      "Kitchen remodel quote")
check("a generic subject shared by many titles never names a thread ('Your account' is in 30 titles)",
      L._name(["support-1", "support-2"], items, 1.0, TITLES, {}, {}), None)
T3 = {**TITLES, "m1": "Payment declined: update your information so we can ship your order.",
      "r1": "Payment declined: update your information so we can ship your order."}
I3 = {**items, "m1": L.Item("m1", "mail", None, "alex", "private"), "r1": L.Item("r1", "record", None, "alex", "private")}
for k in ("m1", "r1"):
    I3[k].keys["id:source"].add("self-of-m1")   # the record filed from the mail shares the mail's own id
check("one title seen twice (a record and its mail) names nothing", L._name(["m1", "r1"], I3, 1.0, T3, {}, {}), None)
T4 = {**TITLES, "c1": "Re: HOA Garden club spring plans", "c2": "Re: HOA Garden club spring plans", "c3": "HOA Garden club spring plans"}
I4 = {**items, **{k: L.Item(k, "mail", None, "alex", "private") for k in ("c1", "c2", "c3")}}
check("replies in one mail thread are separate messages: their shared subject names it",
      L._name(["c1", "c2", "c3"], I4, 1.0, T4, {}, {}), "HOA Garden club spring plans")
check("a record's vendor names the mail it was filed from too", next(t for t in th if "mail-clinic" in t.items).name,
      "Lakeside Clinic")
check("titles are cleaned: no Re:, tags, ids, greetings or emoji", L._clean("Re: [GitHub] Alex, your card 1234-5678 was declined 🎉"),
      ["your", "card", "was", "declined"])
check("a machine description always", t_trade.description.endswith("3 items · Sep 27–Oct 14"), True)

print("── topic: measured against the house, candidate-only (#951 (e), phase 1) ──")
import hashlib  # noqa: E402
import topics as TP  # noqa: E402


def fake_vec(text):   # bag of words, hashed: wording-similar texts land close, like an embedding would
    v = [0.0] * 256
    for w in re.findall(r"[a-z]{4,}", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 256] += 1.0
    return v


import re  # noqa: E402
add("kitchen-1", "mail", mail("Cedar Builders <office@cedarbuilders.example>", "Sep 15, 2026",
                              "Kitchen remodel estimate: cabinets, countertops, backsplash tile and plumbing rough-in.",
                              "Kitchen remodel estimate"))
add("kitchen-2", "mail", mail("Stone Supply <sales@stonesupply.example>", "Sep 20, 2026",
                              "Countertops for your kitchen remodel: quartz slab, backsplash tile, cabinets delivery window.",
                              "Your countertop order for the kitchen remodel"))
for iid, txt in TITLES.items():
    TP.put(con, iid, fake_vec(txt + " " + " ".join(r[0] for r in con.execute(
        "SELECT kind FROM hooks WHERE item_id=?", (iid,)))))
TP.put(con, "kitchen-1", fake_vec("Kitchen remodel estimate cabinets countertops backsplash tile plumbing"))
TP.put(con, "kitchen-2", fake_vec("Countertops kitchen remodel quartz slab backsplash tile cabinets delivery"))
items_t = L.load(con)
ctx = L.topic_ctx(con)
edges_t = L.score(items_t, L.statements(con), ctx)
Et = {(e.a, e.b): e for e in edges_t}
k = Et.get(L._pair("kitchen-1", "kitchen-2"))
check("two mails about one kitchen remodel from different senders: a topic candidate, said so",
      (k.state if k else None, "similar topic, closer than" in (k.why if k else "")), ("candidate", True))
check("topic never accepts in phase 1: every accepted or verified edge rests on typed evidence",
      all(e.typed or e.state in ("candidate", "rejected") or e.measured.get("amount") for e in edges_t
          if e.state in ("accepted", "verified")), True)
before = sorted(sorted(t.items) for t in L.threads_for(items_t, L.score(items_t, L.statements(con)), ALEX))
after = sorted(sorted(t.items) for t in L.threads_for(items_t, edges_t, ALEX))
check("…and changes no thread (topic-only pairs are fringe)", after == before, True)
check("an edge that exists for other reasons carries its topic score", "topic" in Et[L._pair("mail-tradein", "mail-credit")].measured,
      True)
check("a hidden item still never bridges, with topic on", (thread_of("bridge-a", L.threads_for(items_t, edges_t, ALEX)),), (None,))

print("── saved: states, history ──")
counts = L.save(con, edges)
check("edges saved by state", set(counts) <= set(L.STATES) and counts.get("accepted", 0) >= 4, True)
check("a state change is logged with its why", con.execute("SELECT COUNT(*) FROM link_log").fetchone()[0] == sum(counts.values()),
      True)

print("── the threads tool: what a person reads ──")
for k, v in {"HOUSEHOLD_DB": "household.db", "KB_ACL_DB": "kb_acl.db", "AGENT_MEMORY_DIR": "memory", "FENCES_DB": "fences.db",
             "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl", "CONNECTORS_LEDGER": "ledger.db"}.items():
    os.environ[k] = str(_TMP / v)
import agent_runtime_lib as lib  # noqa: E402
import kb_hygiene  # noqa: E402
real = (lib._viewer, kb_hygiene.list_sources, lib.record_nodes, H.connect)
lib._viewer = lambda: ("alex", "admin")
kb_hygiene.list_sources = lambda: [{"source_id": k, "title": v} for k, v in TITLES.items()]
lib.record_nodes = lambda t, *a, **k: ([], True)
H.connect = lambda path=None: con
real_labels = L.labels
L.labels = lambda its: (TITLES, VENDORS, NAMES)
try:
    out = lib.execute_tool("threads", {"about": "Harbor Mobile trade-in"})
    print("    " + out.replace("\n", "\n    "))
    check("the trade-in thread, its three items, each link's state and why",
          (out.splitlines()[0].startswith("Filters: party = ") and out.splitlines()[1] == "Thread: Harbor Mobile", out.count("· mail ·") + out.count("· text ·") >= 3,
           "linked (verified)" in out or "linked (accepted)" in out), (True, True, True))
    block = out.split("Not linked to anything yet")[0]
    check("another order is only ever 'possibly related' in the thread, never stated, and listed once",
          ([("possibly related (candidate, not confirmed)" in ln) for ln in block.splitlines() if "Your order shipped" in ln]),
          [True])
    check("no identifier value appears", any(v in out for v in ("HM-2048", "2048-7731", "45021", "555-0199")), False)
    held = lib.execute_tool("threads", {"about": "Harbor Mobile trade-in", "hold": "time"})
    import re as _re
    m = _re.search(r"Holding Sep 24–Oct 17 \(time\); (\d+) other items", held)
    check("hold=time: the same weeks around the thread, other things, grouped by their own threads",
          (bool(m) and int(m.group(1)) > 0, "Lakeside Clinic:" in held.split("Holding")[1]), (True, True))
    check("an unknown axis is refused", lib.execute_tool("threads", {"about": "x", "hold": "mood"}).startswith("hold is one of"),
          True)
    lib._viewer = lambda: ("alex", "kid")
    check("a kid gets no threads", lib.execute_tool("threads", {"about": "trade-in"}), "Threads aren't shown here.")
    lib._viewer = lambda: ("alex", "admin")
    every = lib.execute_tool("threads", {"all": "yes"})
    check("everything, unsorted, is always there (only what Alex may see)", (every.startswith("Everything, unsorted ("),
          "Escalation" in every), (True, False))
finally:
    lib._viewer, kb_hygiene.list_sources, lib.record_nodes, H.connect = real
    L.labels = real_labels

print("── a device's hooks: raw values in, keys stored ──")
r = H.put_device("alex", "admin", "mac-1", {"item_id": "msg-guid-123", "source": "text", "at": "2026-09-27",
                                              "hooks": [{"kind": "id:order", "value": "HM-2048-7731"},
                                                        {"kind": "party:phone", "value": "45021"}]}, con)
rows = con.execute("SELECT kind, key FROM hooks WHERE item_id=?", (r["item_id"],)).fetchall()
check("stored as keys; the order key is the mail's", (any(k == "id:order" and v in {h.key for h in H.extract(
    {"text": "Order #HM-2048-7731"}) if h.kind == "id:order"} for k, v in rows),
    # raw values never stored: the code itself (any spelling) and the phone's digits as a value, never a substring
    # of the hex keys (a fresh random salt makes a key containing "2048" about 1 run in 1,000; 10-10 a deploy hit it)
    any(v.upper().replace("-", "") == "HM20487731" or v == "45021" or "guid" in v for _, v in rows)
    or "guid" in r["item_id"]), (True, False))
for bad, code in (({"item_id": "x", "source": "text", "hooks": [], "text": "the message"}, 400),):
    try:
        H.put_device("alex", "admin", "mac-1", bad, con)
        check("a body is refused", None, code)
    except H.HookError as e:
        check("a body is refused", e.status, code)
try:
    H.put_device("alex", "admin", None, {"item_id": "x", "source": "text", "hooks": []}, con)
except H.HookError as e:
    check("only from the person's own device", e.status, 403)

print("── conversations (#1016): their own scorer, never #951's threads ──")
import chat_links as CL  # noqa: E402
_rem = CL.targets("update_reminder", {"reminder_id": "R-12"})[0]
_t = 1_790_000_000.0
_cs = [CL.Conv(CL.item_id("c1"), "alex", _t, _t + 600, {_rem}), CL.Conv(CL.item_id("c2"), "alex", _t + 1500, _t + 1800, {_rem}),
       CL.Conv(CL.item_id("c3"), "alex", _t + 2000, _t + 2100, {("facet:kind", "calendar")}),
       CL.Conv(CL.item_id("c4"), "robin", _t + 700, _t + 800, {_rem})]
_e = {(e.a, e.b): (e.type, e.state) for e in CL.score(_cs)}
check("the next chat within 30 min, the same reminder: continues", _e.get(L._pair(CL.item_id("c1"), CL.item_id("c2"))),
      ("continues", "accepted"))
check("a facet alone links nothing; nor does someone else's chat", sorted(_e), [L._pair(CL.item_id("c1"), CL.item_id("c2"))])
CL.index_items(con, _cs, _t - 1)
check("conversation items never reach #951's linker", any(i.startswith(CL.PREFIX) for i in L.load(con)), False)
con.execute("DELETE FROM hooks WHERE source=?", (CL.SOURCE,))
con.commit()

print("── topic scores stay cheap as the house grows (10-05: 125 ms a pair at 7.5k items) ──")
import time as _time  # noqa: E402
import numpy as _np  # noqa: E402
import topics as _topics  # noqa: E402
_dist = _np.sort(_np.random.default_rng(1).random(4_000_000).astype(_np.float32))
_v = {"x": _np.ones(8, _np.float32), "y": _np.arange(8, dtype=_np.float32)}
_t0 = _time.perf_counter()
for _ in range(200):
    _sc = _topics.pair_score(_v, "x", "y", _dist)
check("200 pair scores against a 4M-pair distribution: well under a second (no float64 copy per call)",
      _time.perf_counter() - _t0 < 0.5, True)
check("…with the same percentile as before", _sc["pctl"] == round(float(_np.searchsorted(_dist, _np.float64(_sc["sim"]),
                                                                                        side="right")) / len(_dist), 4)
      or abs(_sc["pctl"] - float(_np.searchsorted(_dist, _sc["sim"], side="right")) / len(_dist)) < 1e-3, True)

# the whole scoring pass, with topics, over many fixture pairs: a budget, so a per-pair slip can't come back silently
_items = {}
for _i in range(1500):
    _it = L.Item(f"perf-{_i}", "mail", __import__("datetime").date(2026, 9, 1), "alex", "private")
    _it.keys["party:domain"].add(f"vendor{_i // 20}.example")   # 75 vendors, 20 items each: 14,250 pairs
    _items[_it.item_id] = _it
_vecs = {k: _np.random.default_rng(_n).random(16).astype(_np.float32) for _n, k in enumerate(_items)}
_ctx = {"vecs": _vecs, "near": {}, "dist": _dist}
_old_cap, L.DEMOTE = L.DEMOTE, 25
_t0 = _time.perf_counter()
_edges = L.score(_items, {}, _ctx)
check("a scoring pass with topics over 14k pairs stays inside 5 s", (_time.perf_counter() - _t0 < 5.0, len(_edges) > 0), (True, True))
L.DEMOTE = _old_cap

# naming many threads over a big house of titles: the phrase corpus is built once per view, not once per thread
_ni, _ne, _nt = {}, [], {f"t{_k}": f"Weekly digest number {_k} about garden club news" for _k in range(20000)}
for _k in range(300):
    for _s in ("a", "b"):
        _ni[f"n{_k}{_s}"] = L.Item(f"n{_k}{_s}", "mail", __import__("datetime").date(2026, 9, 1), "alex", "private")
        _nt[f"n{_k}{_s}"] = f"Harbor case {_k} follow-up"
    _ne.append(L.Edge(f"n{_k}a", f"n{_k}b", "verified", ["same case number"], {}, "same case number (typed)"))
_t0 = _time.perf_counter()
_ths = L.threads_for(_ni, _ne, lambda it: True, _nt, {}, {})
check("naming 300 threads over 20k titles stays inside 5 s", (_time.perf_counter() - _t0 < 5.0, len(_ths)), (True, 300))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
