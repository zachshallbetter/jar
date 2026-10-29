#!/usr/bin/env python3
# gate: hermetic
"""Link hooks (#951 (a)): what an item could be linked by, read in code from its original. Fictional items only
(Harbor Mobile, Lakeside Clinic, Alex, Robin); a temporary world.db and salt, never titan's.
Run: python3 scripts/test_hooks.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-hooks-test-"))
for k, v in {"LINK_SALT_FILE": "salt/link_salt", "WORLD_DB": "world.db", "LINKS_DB": "links.db", "WORLD_DIR": "world", "ENVELOPE_BACKUP_DIR": "envelopes",
             "HOUSEHOLD_DB": "household.db", "CONNECTORS_LEDGER": "ledger.db", "KB_ACL_DB": "kb_acl.db",
             "AGENT_MEMORY_DIR": "memory", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "FENCES_DB": "fences.db", "PLACES_DB": "places.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["LINK_TOPICS"] = "0"   # never the live embeddings model
sys.path.insert(0, str(Path(__file__).parent))
import hooks as H  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def kinds(hs, kind):
    return [h.key for h in hs if h.kind == kind]


MAIL = {"title": "Trade-in update", "text": "From: Harbor Mobile <orders@mail.harbormobile.com>\nTo: alex@example.org\n"
        "Date: Sep 28, 2026\nSubject: Trade-in update\n\nYour trade-in for Order #HM-2048-7731 was received.\n"
        "Questions? Call (509) 555-0142. A credit of $250.00 posts by Oct 15."}
TEXT = {"title": "", "text": "HarborMobile: we got the device for order hm-2048-7731. Credit in 1-2 cycles.",
        "at": "2026-09-27", "sender": "45021"}
CREDIT = {"title": "Your credit was applied", "text": "From: Harbor Mobile <billing@harbormobile.com>\nDate: Oct 14, 2026\n\n"
          "A $250.00 trade-in credit was applied to your account."}
OTHER = {"title": "Order shipped", "text": "From: Harbor Mobile <orders@harbormobile.com>\nDate: Oct 2, 2026\n\n"
         "Order HM-3001-1150 shipped. Tracking 1Z999AA10123456784."}

print("── what an item could be linked by ──")
m, t, c, o = (H.extract(x) for x in (MAIL, TEXT, CREDIT, OTHER))
check("the mail: sender, domain, order, phone, amount, dates",
      sorted({h.kind for h in m}), ["amount", "date:item", "date:mention", "id:order", "party:addr", "party:domain",
                                    "party:phone", "party:to"])
check("the item's own day comes from its Date header", m[0].at, "2026-09-28")
check("the text and the mail share the order number, whatever the label or case", kinds(t, "id:order"), kinds(m, "id:order"))
check("…a different order doesn't", bool(set(kinds(o, "id:order")) & set(kinds(m, "id:order"))), False)
check("the credit mail: same domain, same amount (its own subdomain folds)", (kinds(c, "party:domain"), kinds(c, "amount")),
      (["harbormobile.com"], ["250.00"]))
check("a tracking number", len(kinds(o, "id:tracking")), 1)
check("a shared mailbox provider is not a party", kinds(H.extract({"text": "From: Robin <robin@gmail.com>\n\nHi"}),
                                                        "party:domain"), [])
check("a short code is a party", len(kinds(t, "party:phone")), 1)
check("a mentioned date without a year takes the one nearest the item", kinds(m, "date:mention"), ["2026-10-15"])
check("each hook says where it came from", [h.locator for h in m if h.kind == "id:order"],
      [{"part": "body", "line": 6}])
check("an offer isn't an amount ('$2 off', 'save $5', 'from $9.99', 'only $0.14 more'); a charge is",
      [kinds(H.extract({"text": t}), "amount") for t in ("Get $2.00 off your next order", "Save $5.00 today", "Plans from $9.99",
                                                        "Your order total was $61.20", "A $250.00 credit was applied")],
      [[], [], [], ["61.20"], ["250.00"]])
check("a code with too few digits isn't an identifier", kinds(H.extract({"text": "Your order ABCDE-1 is ready"}), "id:order"), [])

print("── privacy: the salt stays here, values never kept ──")
check("the salt file is created 0600", oct(H.SALT_FILE.stat().st_mode & 0o777), "0o600")
blob = json.dumps([h.__dict__ for h in m + t + c + o])
check("no identifier, address or phone in the hooks", [v for v in ("HM-2048", "2048-7731", "orders@", "555-0142", "5550142",
                                                                    "45021", "1Z999") if v.lower() in blob.lower()], [])

print("── the index ──")
con = sqlite3.connect(str(_TMP / "w.db"))
check("index replaces one item's hooks", (H.index(con, "cx-mail-1", "mail", m, "alex", "private"),
                                          H.index(con, "cx-mail-1", "mail", m, "alex", "private"),
                                          con.execute("SELECT COUNT(*) FROM hooks WHERE item_id='cx-mail-1'").fetchone()[0]),
      (len(m) + 1, len(m) + 1, len(m) + 1))   # + the item's own id (a record filed from it finds it)
H.index(con, "text-1", "text", t, "alex", "private")
check("the order key joins the text and the mail in the index",
      sorted(r[0] for r in con.execute("SELECT item_id FROM hooks WHERE kind='id:order' AND key=?", (kinds(m, "id:order")[0],))),
      ["cx-mail-1", "text-1"])

print("── a record is its mail's manifestation ──")
rec = H.extract({"title": "An auto payment failed", "vendor": "Lakeside Clinic, LLC", "amount": 38.85,
                 "due_date": "2026-10-05", "derived_from": "cx-mail-1", "at": "2026-10-02"})
check("vendor, amount, due date and its source mail", (kinds(rec, "party:vendor"), kinds(rec, "amount"),
      kinds(rec, "date:mention"), len(kinds(rec, "id:source"))), (["lakeside clinic"], ["38.85"], ["2026-10-05"], 1))

print("── entities: a counterparty, never the household's own people ──")
import sqlite3 as _sq  # noqa: E402
import world  # noqa: E402
import household_identity as hh  # noqa: E402


class _Row(dict):
    pass


def _row(d):
    c = _sq.connect(":memory:")
    c.row_factory = _sq.Row
    cols = ", ".join(f"? AS {k}" for k in d)
    return c.execute(f"SELECT {cols}", tuple(d.values())).fetchone()


class _FakeWorld:
    def find(self, text, person, role):
        return [e for e, n in (("zach:alex-1", "alex"), ("zach:harbor-mobile-1", "harbor mobile")) if n in text.lower()]

    def entity(self, eid):
        return _row({"name": "Alex" if "alex" in eid else "Harbor Mobile", "type": "person" if "alex" in eid else "org"})


class _People:
    def people(self):
        return [{"id": "alex", "name": "Alex", "role": "admin"}]


real_w, real_hh = world.get, hh.get_store
world.get, hh.get_store = (lambda: _FakeWorld()), (lambda: _People())
try:
    f = H.entity_finder("alex")
    check("an organisation named is a party; the household's own member isn't (rows read as dicts)",
          f("Hi Alex, your Harbor Mobile trade-in"), ["zach:harbor-mobile-1"])
finally:
    world.get, hh.get_store = real_w, real_hh

print("── the backfill reads originals from the envelope backups ──")
bdir = Path(os.environ["ENVELOPE_BACKUP_DIR"])
bdir.mkdir(parents=True)
(bdir / "envelopes-premigrate-20261005-001418.jsonl").write_text(json.dumps({
    "source_id": "cx-mail-9", "title": "Trade-in update", "source_type": "connector:mail", "text": MAIL["text"],
    "metadata": {"observed_at": "2026-09-28T10:00:00Z"}}) + "\n")
(bdir / "envelopes-20261005-003043.jsonl").write_text(json.dumps({
    "source_id": "cx-mail-9", "title": "Trade-in update", "source_type": "connector:mail", "text": "changed later",
    "metadata": {}}) + "\n")
import kb_hygiene  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
real = (kb_hygiene.list_sources, lib.record_nodes)
kb_hygiene.list_sources = lambda: []
lib.record_nodes = lambda t, *a, **k: ([], True)
try:
    items = H.backfill_items()
    check("one item per source id, from its first (oldest) backup",
          [(i[0], i[1], "HM-2048" in i[2]["text"]) for i in items], [("cx-mail-9", "mail", True)])
    import io  # noqa: E402
    import contextlib  # noqa: E402
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        H.main(["--backfill"])
    out = json.loads(buf.getvalue())
    check("the dry run counts by kind and writes nothing", (out["apply"], out["items"], out["hooks_by_kind"]["id:order"],
          (Path(os.environ["LINKS_DB"])).exists()), (False, 1, 1, False))
    check("…and never prints a value", any(v in buf.getvalue() for v in ("HM-2048", "harbormobile", "555")), False)
    old_umask = os.umask(0o022)   # the file mode under test is the code's, never the caller's umask (a 077 gave 0600)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            H.main(["--backfill", "--apply"])
    finally:
        os.umask(old_umask)
    w = sqlite3.connect(os.environ["LINKS_DB"])
    check("--apply indexes them in links.db (0640), never the world model's files",
          (oct(Path(os.environ["LINKS_DB"]).stat().st_mode & 0o777), Path(os.environ["WORLD_DB"]).exists()), ("0o640", False))
    check("…its rows are there", w.execute("SELECT COUNT(*) FROM hooks WHERE item_id='cx-mail-9'").fetchone()[0] > 0,
          True)
finally:
    kb_hygiene.list_sources, lib.record_nodes = real


print("── recipients: party:to (#1399) ──")
TO = {"title": "Pickup plan", "text": "From: Robin <robin@lakes.example.org>\nTo: Alex <alex@example.org>, jordan@example.net\n"
      "Cc: \"Hale, Ava\" <ava.hale@school.example.edu>,\n  leo@example.com\nDate: Oct 3, 2026\nSubject: Pickup\n\n"
      "Write to robin.alt@lakes.example.org if anything changes."}
th = H.extract(TO)
to = [h for h in th if h.kind == "party:to"]
check("each To and Cc address, a folded Cc line too, and never one from the body", len(to), 4)
check("keyed exactly like a sender's address (so a reply's From matches the original's To)",
      {h.key for h in to} >= {H.keyed("addr", "alex@example.org")}, True)
check("…the key is a salted hash: no raw address, local part or domain in any key or locator",
      [h for h in to if any(w in (h.key + json.dumps(h.locator)) for w in ("@", "alex", "example", "hale", "leo"))], [])
check("each locator names the header line it came from", sorted({h.locator["line"] for h in to}), [2, 3, 4])
check("the sender is not a recipient", H.keyed("addr", "robin@lakes.example.org") in {h.key for h in to}, False)
many = {"title": "Newsletter", "text": "From: News <news@lakes.example.org>\nTo: " +
        ", ".join(f"p{i}@example.org" for i in range(8)) + "\nCc: " + ", ".join(f"c{i}@example.org" for i in range(3)) + "\n\nHi"}
check("more than 10 recipients (a list or a blast): no party:to at all", kinds(H.extract(many), "party:to"), [])
ten = {"title": "Team", "text": "From: Robin <robin@lakes.example.org>\nTo: " + ", ".join(f"p{i}@example.org" for i in range(10))
       + "\n\nHi"}
check("exactly 10: each one", len(kinds(H.extract(ten), "party:to")), 10)
check("a repeated address counts once", len(kinds(H.extract({"text": "From: a@example.org\nTo: b@example.org\nCc: B@example.org\n\nx"}),
                                                  "party:to")), 1)
check("a text (no headers) has none", kinds(H.extract(TEXT), "party:to"), [])
import linker as L  # noqa: E402
check("the linker never links by a recipient (the household's own address is on nearly every mail)",
      "party:to" in L.NOT_LINKING, True)
from datetime import date as _d  # noqa: E402
_its = {}
for n, extra in (("cx-a", {}), ("cx-b", {}), ("cx-c", {"id:order": {"o1"}})):
    it = L.Item(n, "mail", _d(2026, 10, 3), "alex", "private")
    it.keys["party:to"].add("k-alex")
    for k, v in extra.items():
        it.keys[k] |= v
    _its[n] = it
_its["cx-a"].keys["id:order"].add("o1")
check("…in the scorer: two mails sharing only a recipient get no edge; a shared order number still links",
      sorted((e.a, e.b) for e in L.score(_its)), [("cx-a", "cx-c")])
import chat_links as CL  # noqa: E402
check("…nor does a chat continuation count one as evidence",
      CL.Conv.evidence(type("T", (), {"keys": {("party:to", "k"), ("id:order", "o")}})()), {("id:order", "o")})

print("── server mail: recipients read from the raw headers at derive time, never stored (#1399) ──")
os.environ["MAIL_SERVER_DB"] = str(_TMP / "mail_server.db")
import mail_server as MS  # noqa: E402
RAW = (b"From: Robin <robin@lakes.example.org>\r\nTo: \"Hale, Ava\" <ava.hale@school.example.edu>,\r\n"
       b" Alex <ALEX@example.org>\r\nCc: jordan@example.net, alex@example.org\r\nSubject: Pickup\r\n"
       b"Message-ID: <m1@lakes.example.org>\r\n\r\nWrite to leo@example.com instead.")
check("recipients(): To (a quoted name with a comma, a folded line) and Cc, lowercased, once each, never the body's",
      MS.recipients(RAW), ["ava.hale@school.example.edu", "alex@example.org", "jordan@example.net"])
check("…a malformed header block: none, no raise", MS.recipients(b"\xff\xfe not a mail"), [])
row = {"sender": "Robin <robin@lakes.example.org>", "sent_at": "2026-10-03T12:00:00Z", "message_id": "<m1@lakes.example.org>",
       "in_reply_to": None, "refs": "[]"}
txt = MS.with_headers(row, "Body", MS.recipients(RAW))
hh = H.extract({"title": "Pickup", "text": txt})
check("…what the hook index reads: a To line, so the server mail gets its party:to hooks", len(kinds(hh, "party:to")), 3)
check("…and no recipients: no To line (as before)", "To:" in MS.with_headers(row, "Body"), False)
con = MS._db() if hasattr(MS, "_db") else None
cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")} if con else set()
check("the messages table keeps no recipients (no to/cc column)", bool(cols) and not ({"to", "cc", "recipients"} & cols), True)

print("── a device's text hooks: a time, a place, a direction (#1436) ──")
import places  # noqa: E402
pcon = sqlite3.connect(os.environ["PLACES_DB"])
places.ensure_registry(pcon)
pcon.executemany("INSERT INTO places (id, label, kind, owner, audience, aliases) VALUES (?,?,?,?,?,?)",
                 [("pl-pine-park", "Pine Park", "park", None, "household", '["the park"]'),
                  ("pl-willow", "Willow Studio", "candidate", "robin", "private", "[]")])
pcon.commit()
pcon.close()
dcon = sqlite3.connect(str(_TMP / "device-links.db"))   # never LINKS_DB: the backfill above checks it stays absent


def post(hs, person="alex", role="admin", native="guid-1"):
    return H.put_device(person, role, "mac-1", {"item_id": native, "source": "text", "at": "2026-10-09", "hooks": hs}, dcon)


def rows(iid):
    return sorted((k, v, json.loads(loc)) for k, v, loc in dcon.execute(
        "SELECT kind, key, locator FROM hooks WHERE item_id=? AND kind NOT IN ('date:item', 'id:source')", (iid,)))


r = post([{"kind": "date:mention", "value": "2026-10-10T10:00", "line": 2},
          {"kind": "date:mention", "value": "2026-10-12"},
          {"kind": "mention:place", "value": "pl-pine-park", "line": 2},
          {"kind": "party:phone", "value": "+1 (555) 010-0001", "dir": "from"},
          {"kind": "party:phone:to", "value": "555.010.0003"},
          {"kind": "party:addr", "value": "Jordan.Hale@example.org", "dir": "to"}])
got = rows(r["item_id"])
check("a timed date: the key stays its day (the linker's match), the time in the locator",
      [x for x in got if x[0] == "date:mention"],
      [("date:mention", "2026-10-10", {"part": "body", "line": 2, "time": "10:00"}),
       ("date:mention", "2026-10-12", {"part": "body", "line": 0})])
check("a place: its registry id", [x for x in got if x[0] == "mention:place"],
      [("mention:place", "pl-pine-park", {"part": "body", "line": 2})])
check("a direction: in the locator, whether as dir or in the kind; phones and addresses keyed as ever",
      sorted((k, v, loc.get("dir"), loc["part"]) for k, v, loc in got if k.startswith("party:")),
      sorted([("party:phone", H.keyed("phone", "5550100001"), "from", "header"),
              ("party:phone", H.keyed("phone", "5550100003"), "to", "header"),
              ("party:addr", H.keyed("addr", "jordan.hale@example.org"), "to", "header")]))
check("seconds are dropped", rows(post([{"kind": "date:mention", "value": "2026-10-10T18:30:00"}], native="g2")["item_id"]),
      [("date:mention", "2026-10-10", {"part": "body", "line": 0, "time": "18:30"})])
check("an undirected party is as before", rows(post([{"kind": "party:phone", "value": "5550100001"}], native="g3")["item_id"]),
      [("party:phone", H.keyed("phone", "5550100001"), {"part": "body", "line": 0})])
check("Robin may name her own candidate place", rows(post([{"kind": "mention:place", "value": "pl-willow"}],
                                                          person="robin", role="adult", native="g4")["item_id"]),
      [("mention:place", "pl-willow", {"part": "body", "line": 0})])
check("jellybean#1189: a kept plan's thread_ref, kept as it is (already an opaque hash), the poster's own",
      (rows(post([{"kind": "thread:device", "value": "0123456789abcdef"}], native="g6")["item_id"]),
       dcon.execute("SELECT DISTINCT owner, audience FROM hooks WHERE kind='thread:device'").fetchall()),
      ([("thread:device", "0123456789abcdef", {"part": "body", "line": 0})], [("alex", "private")]))
kinds_of = lambda iid: sorted(r[0] for r in dcon.execute("SELECT kind FROM hooks WHERE item_id=?", (iid,)))
own = post([{"kind": "date:mention", "value": "2026-10-12"}, {"kind": "party:phone", "value": "5550100001", "dir": "from"}],
           native="g7")["item_id"]
tied = post([{"kind": "thread:device", "value": "fedcba9876543210"}], native="g7")["item_id"]
check("jellybean#1218 review: a kept plan's thread, posted for a text that has hooks, adds to them (never a replace)",
      (own == tied, kinds_of(tied)), (True, ["date:item", "date:mention", "id:source", "party:phone", "thread:device"]))
post([{"kind": "date:mention", "value": "2026-10-13"}], native="g7")
check("…and the text posted again without it keeps its thread (the device's newer hooks replace its older ones)",
      (kinds_of(tied), [r[0] for r in dcon.execute("SELECT key FROM hooks WHERE item_id=? AND kind='date:mention'", (tied,))]),
      (["date:item", "date:mention", "id:source", "thread:device"], ["2026-10-13"]))
post([{"kind": "thread:device", "value": "0011223344556677"}], native="g7")
check("…a new thread for the same text replaces its thread only",
      ([r[0] for r in dcon.execute("SELECT key FROM hooks WHERE item_id=? AND kind='thread:device'", (tied,))], kinds_of(tied)),
      (["0011223344556677"], ["date:item", "date:mention", "id:source", "thread:device"]))
check("…another device's or person's text is never touched (the item id is per person and device)",
      kinds_of(post([{"kind": "thread:device", "value": "fedcba9876543210"}], native="g7", person="robin", role="adult")["item_id"]),
      ["date:item", "id:source", "thread:device"])
print("── #1650: a text's conversation and its inline reply ──")
conv = "5a6b7c8d9e0f1a2b"
parent = post([{"kind": "thread:device", "value": conv}, {"kind": "party:phone", "value": "5550100001", "dir": "to"}],
              native="guid-p1")["item_id"]
r = post([{"kind": "thread:device", "value": conv}, {"kind": "reply:device", "value": "guid-p1", "line": 0}],
         native="guid-r1")
check("a reply to the poster's own text: kept as that text's item id (minted as its own id), in the header",
      (r["dropped"], [x for x in rows(r["item_id"]) if x[0] == "reply:device"]),
      ({}, [("reply:device", parent, {"part": "header", "line": 0})]))
check("…the conversation is the same thread:device value on both texts",
      sorted(k for (k,) in dcon.execute("SELECT key FROM hooks WHERE kind='thread:device' AND item_id IN (?,?)",
                                       (parent, r["item_id"]))), [conv, conv])
replied = r["item_id"]
for label, kw, hs in (
        ("a text the device never posted (or not yet)", {}, [{"kind": "reply:device", "value": "guid-none"}]),
        ("itself", {"native": "guid-self"}, [{"kind": "reply:device", "value": "guid-self"}]),
        ("Alex's text, named by Robin's device: minted under Robin, so it is never Alex's item",
         {"person": "robin", "role": "adult", "native": "guid-r2"}, [{"kind": "reply:device", "value": "guid-p1"}])):
    got = post(hs, **kw)
    check(f"a reply to {label}: dropped as 'reply', nothing kept",
          (got["dropped"], [x for x in rows(got["item_id"]) if x[0] == "reply:device"]), ({"reply": 1}, []))
other = H.put_device("alex", "admin", "mac-2", {"item_id": "guid-r3", "source": "text", "at": "2026-10-09",
                                                 "hooks": [{"kind": "reply:device", "value": "guid-p1"}]}, dcon)
check("…Alex's other device naming that id: another device's id space, dropped", other["dropped"], {"reply": 1})
two = post([{"kind": "reply:device", "value": "guid-p1"}, {"kind": "reply:device", "value": "guid-r1"}], native="guid-r4")
check("two parents: neither is trusted", (two["dropped"], [x for x in rows(two["item_id"]) if x[0] == "reply:device"]),
      ({"reply": 2}, []))
same = post([{"kind": "reply:device", "value": "guid-p1"}, {"kind": "reply:device", "value": "guid-p1"}],
            native="guid-r5")
check("the same parent twice is one", (same["dropped"], [x[1] for x in rows(same["item_id"]) if x[0] == "reply:device"]),
      ({}, [parent]))
post([{"kind": "thread:device", "value": "0011223344556677"}], native="guid-r1")
check("a thread-only post (a kept plan's) keeps the reply as it keeps every other hook",
      [x[0] for x in rows(replied)], ["reply:device", "thread:device"])
post([{"kind": "party:phone", "value": "5550100001", "dir": "from"}], native="guid-r1")
check("…a full post without it replaces it (the device's latest word), keeping only the thread",
      [x[0] for x in rows(replied)], ["party:phone", "thread:device"])
check("the thread value is never a chat id or handle: anything but 16 lowercase hex is dropped",
      post([{"kind": "thread:device", "value": "chat123456789@example"}], native="guid-r6")["dropped"], {"value": 1})
dump = "\n".join(str(tuple(x)) for x in dcon.execute("SELECT * FROM hooks"))
check("nothing raw stored: no phone, address, place name or device id",
      [v for v in ("5550100001", "5550100003", "010-0001", "jordan", "example.org", "Pine Park", "guid") if v.lower() in dump.lower()], [])
GOOD = {"kind": "id:order", "value": "HM-2048-7731"}
GOOD_ROW = ("id:order", H.keyed("id", "HM20487731"), {"part": "body", "line": 0})
replies = {}
for label, bad, reason, kw in (
        ("an unknown kind", {"kind": "mention:pet", "value": "Biscuit"}, "unknown_kind", {}),
        ("a person's name, not their id", {"kind": "mention:person", "value": "Robin"}, "person", {}),
        ("a directed domain", {"kind": "party:domain:from", "value": "example.org"}, "unknown_kind", {}),
        ("a time that isn't one", {"kind": "date:mention", "value": "2026-10-10T25:00"}, "date", {}),
        ("a day that isn't one", {"kind": "date:mention", "value": "2026-02-30"}, "date", {}),
        ("a date in words", {"kind": "date:mention", "value": "Saturday 10am"}, "date", {}),
        ("a time with an offset (it's household-zone local)", {"kind": "date:mention", "value": "2026-10-10T10:00-07:00"},
         "date", {}),
        ("a place's name, not its id", {"kind": "mention:place", "value": "Pine Park"}, "place", {}),
        ("a place id not in the registry", {"kind": "mention:place", "value": "pl-nowhere"}, "place", {}),
        ("another person's private place", {"kind": "mention:place", "value": "pl-willow"}, "place", {}),
        ("a kid naming another's private place", {"kind": "mention:place", "value": "pl-willow"}, "place",
         {"person": "ava", "role": "kid"}),
        ("a direction that isn't from or to", {"kind": "party:phone", "value": "5550100001", "dir": "cc"}, "direction", {}),
        ("a direction on a non-party hook", {"kind": "amount", "value": "12.00", "dir": "from"}, "direction", {}),
        ("two directions", {"kind": "party:phone:from", "value": "5550100001", "dir": "to"}, "direction", {}),
        ("an unreadable amount", {"kind": "amount", "value": "twelve"}, "value", {}),
        ("a thread ref that isn't 16 lowercase hex", {"kind": "thread:device", "value": "iMessage;-;+15550100001"},
         "value", {}),
        ("a thread ref in capitals", {"kind": "thread:device", "value": "0123456789ABCDEF"}, "value", {})):
    r = post([GOOD, bad], native=f"drop-{len(replies)}", **kw)
    replies[label] = r
    check(f"dropped, that hook only: {label}", (rows(r["item_id"]), r["dropped"]), ([GOOD_ROW], {reason: 1}))
check("an unseen private place reads exactly as an unknown id",
      {k: v for k, v in replies["another person's private place"].items() if k != "item_id"},
      {k: v for k, v in replies["a place id not in the registry"].items() if k != "item_id"})
r = post([GOOD, {"kind": "mention:person", "value": "Robin"}, {"kind": "mention:place", "value": "pl-willow"},
          {"kind": "mention:place", "value": "Pine Park"}, {"kind": "date:mention", "value": "Saturday"},
          {"kind": "party:phone", "value": "x", "dir": "up"}, {"kind": "party:phone", "value": "no digits"},
          {"kind": "mention:place", "value": "pl-pine-park"}, "not a hook"], native="drop-many")
check("several drops in one post: counted per fixed label, the good hooks stored",
      (sorted(x[0] for x in rows(r["item_id"])), r["dropped"]),
      (["id:order", "mention:place"], {"date": 1, "direction": 1, "person": 1, "place": 2, "unknown_kind": 1, "value": 1}))
check("…the reply names no value and no kind", [v for v in ("Robin", "mention:person", "pl-willow", "Pine Park",
                                                           "Saturday", "no digits", "HM-2048") if v in json.dumps(r)], [])
dump = "\n".join(str(tuple(x)) for x in dcon.execute("SELECT * FROM hooks"))
check("…and nothing raw stored from a dropped hook either",
      [v for v in ("mention:person", "Pine Park", "Saturday", "twelve", "5550100001", "example.org", "guid", "drop-")
       if v.lower() in dump.lower()], [])
os.environ["PLACES_DB"] = str(_TMP / "no-places.db")
r = post([GOOD, {"kind": "mention:place", "value": "pl-pine-park"}], native="g5")
check("no registry yet: every place is dropped, the rest stored", (rows(r["item_id"]), r["dropped"]),
      ([GOOD_ROW], {"place": 1}))
check("a clean post says it dropped nothing", post([GOOD], native="g6")["dropped"], {})

print("── mention:person: a household member a text names, by household person id only (#1396) ──")
import household_identity as HHI  # noqa: E402

real_store = HHI.get_store


class _House:
    def people(self):
        return [{"id": "alex", "name": "Alex", "role": "admin"}, {"id": "robin", "name": "Robin", "role": "adult"},
                {"id": "ava", "name": "Ava", "role": "kid"}, {"id": "kitchen", "name": "Kitchen", "role": "household"}]


HHI.get_store = lambda: _House()
try:
    r = post([GOOD, {"kind": "mention:person", "value": "ava", "line": 1}, {"kind": "mention:person", "value": "robin"},
              {"kind": "mention:person", "value": "ava"},                       # twice in one text: one row
              {"kind": "mention:person", "value": "Robin"},                     # a name, not the id
              {"kind": "mention:person", "value": "Hale Dorsey"},               # someone outside the house
              {"kind": "mention:person", "value": "kitchen"},                   # a shared screen is not a member
              {"kind": "mention:person", "value": "jordan"},                    # an id the house doesn't have
              {"kind": "mention:person", "value": ""}], native="people-1")
    check("members by id: one row each, the line kept; a name, an outsider, a screen and an unknown id dropped",
          ([x for x in rows(r["item_id"]) if x[0] == "mention:person"], r["dropped"]),
          ([("mention:person", "ava", {"part": "body", "line": 1}), ("mention:person", "robin", {"part": "body", "line": 0})],
           {"person": 4, "value": 1}))
    check("…the poster's own, private: never a wider audience",
          dcon.execute("SELECT DISTINCT owner, audience FROM hooks WHERE kind='mention:person'").fetchall(), [("alex", "private")])
    check("…the reply names no value", [v for v in ("Robin", "Hale", "kitchen", "jordan") if v in json.dumps(r)], [])
    dump = "\n".join(str(tuple(x)) for x in dcon.execute("SELECT * FROM hooks"))
    check("…and no dropped value is stored anywhere", [v for v in ("Robin", "Hale", "Dorsey", "kitchen", "jordan") if v in dump], [])
    only_bad = post([{"kind": "mention:person", "value": "Hale Dorsey"}, {"kind": "mention:person", "value": "p_nobody"}],
                    native="people-2")
    check("a post naming only outsiders and unknown ids stores no mention at all",
          ([x for x in rows(only_bad["item_id"]) if x[0] == "mention:person"], only_bad["dropped"]), ([], {"person": 2}))
    robin = post([{"kind": "mention:person", "value": "ava"}], person="robin", role="adult", native="people-1")
    check("Robin's device posting the same device id gets her own item, never Alex's; Alex's rows untouched",
          (robin["item_id"] != r["item_id"], [x[1] for x in rows(r["item_id"]) if x[0] == "mention:person"],
           dcon.execute("SELECT DISTINCT owner FROM hooks WHERE item_id=?", (robin["item_id"],)).fetchall()),
          (True, ["ava", "robin"], [("robin",)]))
    try:
        H.put_device("kitchen", "household", "screen-1", {"item_id": "k", "source": "text",
                                                          "hooks": [{"kind": "mention:person", "value": "ava"}]}, dcon)
        check("a shared screen can't post one", None, 403)
    except H.HookError as e:
        check("a shared screen can't post one", e.status, 403)
finally:
    HHI.get_store = real_store


class _Broken:
    def people(self):
        raise RuntimeError("no registry")


HHI.get_store = lambda: _Broken()
try:
    r = post([GOOD, {"kind": "mention:person", "value": "ava"}], native="people-3")
    check("the registry unreadable: every mention:person dropped, never trusted", (rows(r["item_id"]), r["dropped"]),
          ([GOOD_ROW], {"person": 1}))
finally:
    HHI.get_store = real_store

print("── only a malformed body is refused (400) ──")
for label, body in (("not a dict", [GOOD]),
                    ("an item's text", {"item_id": "x", "source": "text", "hooks": [GOOD], "text": "the message"}),
                    ("an item's body", {"item_id": "x", "source": "text", "hooks": [GOOD], "body": "the message"}),
                    ("no item_id", {"source": "text", "hooks": [GOOD]}),
                    ("no source", {"item_id": "x", "hooks": [GOOD]}),
                    ("no hooks", {"item_id": "x", "source": "text"}),
                    ("hooks not a list", {"item_id": "x", "source": "text", "hooks": GOOD}),
                    ("more than 200 hooks", {"item_id": "x", "source": "text", "hooks": [GOOD] * 201})):
    try:
        H.put_device("alex", "admin", "mac-1", body, dcon)
        check(f"refused: {label}", None, 400)
    except H.HookError as e:
        check(f"refused: {label}", e.status, 400)


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
