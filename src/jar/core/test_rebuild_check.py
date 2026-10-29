#!/usr/bin/env python3
# gate: hermetic
"""rebuild_check.py's pure parts (the 10-09 rebuild): counting a surface answer, counting kb_retrieve hits, and the table:
counts and role labels only, never a title or a name. The outcome checks (Coordination, 10-09) on throwaway stores: mail
cards per viewer over 14 days, the thread's mail (ledger, KB, owner-only, triage), the calendar ledger against the Mac's
tracked count, the Mac-side counter, and the before/after compare that flags the kid or the screen. Fictional data.
Run: python3 scripts/test_rebuild_check.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-rebuild-check-"))
os.environ["PROACTIVE_DB"] = str(_TMP / "proactive.db")
os.environ["REFERENCES_DB"] = str(_TMP / "references.db")
sys.path.insert(0, str(Path(__file__).parent))
import rebuild_check as rc  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r} expected={expected!r}"))


check("a list under a known key", rc.count({"cards": [1, 2, 3]}), 3)
check("a surface's groups' members", rc.count({"groups": [{"members": ["a", "b"]}, {"members": ["c"]}]}), 3)
check("a surface nested under 'surface'", rc.count({"surface": {"groups": [{"members": ["a"]}]}}), 1)
check("days of events", rc.count({"days": [{"events": [1, 2]}, {"events": [3]}]}), 3)
check("a bare list", rc.count([1, 2]), 2)
check("nothing recognisable: 0", rc.count({"x": 1}), 0)
check("a schedule: each person's events plus the household's", rc.count({"people": [{"events": [1, 2]}, {"events": []}],
      "household": {"events": [3]}}), 3)
check("a status-only answer shows its code, never its message", rc.count({"status": "off", "message": "Threads are off: …"}),
      "status:off")
check("…a long or odd status isn't echoed", rc.count({"status": "Some Sentence With Words"}), 0)
check("kb hits: bullets and numbered lines", rc.kb_hits("[KB, 3 matches]\n• a\n• b\n2. c\nfooter"), 3)
check("kb: nothing found", rc.kb_hits("No matching notes."), 0)
t = rc.render({"admin": {"cards": 4, "kb:bill": 2}, "kid": {"cards": "403"}}, "before")
check("the table: role labels, counts, a 403 shown as is", ("== admin ==" in t, "cards                  4" in t, "403" in t), (True, True, True))
check("it reads no titles: only the counts it was given appear", "Robin" in rc.render({"adult": {"cards": 1}}, "x"), False)
print("── mail cards per viewer, 14 days ──")
import proactive  # noqa: E402
now = time.time()
con = proactive._db()
for i, (src, aud, age) in enumerate((("mail-bills", '["alex"]', 1), ("mail-bills", '["alex"]', 20), ("mail-suggestion", '["alex"]', 3),
                                     ("mail-suggestion", '["robin"]', 3), ("mail-bills", "adults", 2), ("todos", '["alex"]', 1),
                                     ("mail-suggestion", "household", 5))):
    con.execute("INSERT INTO cards (id, source, kind, title, created_at, audience) VALUES (?,?,?,?,?,?)",
                (f"c{i}", src, "info", "Hale's invoice", now - age * 86400, aud))
con.commit()
con.close()
check("the admin: their own mail cards in 14 days, plus adults' and household ones (not 20 days old, not someone else's)",
      rc.mail_cards("alex", "admin", now=now), {"mail:bills 14d": 2, "mail:suggestions 14d": 2})
check("a kid: only the household-audience one", rc.mail_cards("ava", "kid", now=now), {"mail:bills 14d": 0, "mail:suggestions 14d": 1})
check("no proactive store: zeros", rc.mail_cards("alex", "admin", db=_TMP / "none.db", now=now),
      {"mail:bills 14d": 0, "mail:suggestions 14d": 0})

print("── the thread's mail ──")
LEDGER, TRIAGE = _TMP / "ledger.db", _TMP / "triage.db"
L = sqlite3.connect(LEDGER)
L.execute("CREATE TABLE items (source TEXT, source_id TEXT, item_id TEXT, kb INTEGER, owner TEXT, deleted_at REAL)")
L.execute("CREATE TABLE versions (source TEXT, source_id TEXT, title TEXT, fields TEXT, owner TEXT, audience TEXT, at REAL)")
rows = [("m1", "Re: SWAIA board agenda", "Robin <robin@lakes.example.org>", "alex", "private", None, 1),
        ("m2", "Dues reminder", "office@swaia.example.org", "alex", "private", None, 1),
        ("m3", "Re: SWAIA picnic", "jordan@mail.example.com", "jordan", "private", None, 0),
        ("m4", "Re: SWAIA minutes", "x@y.example", "alex", "private", 5.0, 1),
        ("m5", "Swaiamore sale", "deals@shop.example", "alex", "private", None, 1),
        ("m6", "Lunch", "robin@lakes.example.org", "alex", "household", None, 1)]
for sid, title, frm, owner, aud, deleted, kb in rows:
    L.execute("INSERT INTO items VALUES ('mail', ?, ?, ?, ?, ?)", (sid, "kb-" + sid, kb, owner, deleted))
    L.execute("INSERT INTO versions VALUES ('mail', ?, ?, ?, ?, ?, ?)", (sid, "old title", json.dumps({"from": frm}), owner, aud, 1.0))
    L.execute("INSERT INTO versions VALUES ('mail', ?, ?, ?, ?, ?, ?)", (sid, title, json.dumps({"from": frm, "subject": title}),
                                                                         owner, aud, 2.0))
for i in range(4):
    L.execute("INSERT INTO items VALUES ('calendar', ?, ?, 1, ?, ?)", (f"e{i}", f"kb-e{i}", "alex" if i < 3 else "robin",
                                                                         5.0 if i == 2 else None))
L.commit()
L.close()
T = sqlite3.connect(TRIAGE)
T.execute("CREATE TABLE queue (item_id TEXT, owner TEXT, title TEXT, sender TEXT, category TEXT)")
T.executemany("INSERT INTO queue VALUES (?,?,?,?,?)", [("t1", "alex", "SWAIA newsletter", "news@swaia.example.org", "newsletter"),
              ("t2", "alex", "Re: SWAIA vote", "robin@lakes.example.org", None), ("t3", "alex", "Other", "a@b.example", "personal")])
T.commit()
T.close()
out = rc.thread_outcome("swaia", "swaia.example.org", "alex", {"kb-m1", "kb-m2"}, LEDGER, TRIAGE)
check("by subject word or sender domain; live only (the deleted one left out); a word inside another word isn't a match",
      out["ledger live"], 3)
check("…in the KB, owner-only (the admin's, private) vs anyone else's", (out["in kb"], out["owner-only"], out["not owner-only"]),
      (2, 2, 1))
check("…what triage holds of it, by category (waiting when undecided)", (out.get("triage newsletter"), out.get("triage waiting"),
      out.get("triage personal")), (1, 1, None))
check("…the KB list unreadable: 'err', never a 0 that reads as 'not there'",
      rc.thread_outcome("swaia", None, "alex", None, LEDGER, TRIAGE)["in kb"], "err")
check("counts only: no subject, sender or id in the outcome",
      [w for w in ("agenda", "robin", "lakes", "kb-m", "dues") if w in json.dumps(out).lower()], [])
check("matches(): the domain's subdomain too, not a lookalike", (rc.matches("", "a@mail.swaia.org", None, "swaia.org"),
      rc.matches("", "a@notswaia.org", None, "swaia.org")), (True, False))
check("KB's rule: the word in the sender's domain matches with no domain given; dots are boundaries, a longer word isn't",
      [rc.matches("Hi", s, "swaia") for s in ('"Office" <office@swaia.example.org>', "a@mail.swaia.org", "a@notswaia.org",
                                              "swaia@other.example", "SWAIA Board <b@lakes.example>")], [True, True, False, False, False])
check("…the domain after the last @", rc._domain('"a@b" <x@y@swaia.org>'), "swaia.org")

print("── the thread in mail_server.db ──")
SERVER = _TMP / "mail_server.db"
M = sqlite3.connect(SERVER)
M.execute("CREATE TABLE messages (account TEXT, folder TEXT, owner TEXT, item_id TEXT, message_id TEXT, subject TEXT, "
          "sender TEXT, derived_at REAL, kb INTEGER, category TEXT, passes TEXT)")
M.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
    ("a1", "INBOX", "alex", "srv-1", "<1@x>", "Re: SWAIA board", "robin@lakes.example.org", 1.0, 1, "personal", "ran"),
    ("a1", "Archive", "alex", "srv-1", "<1@x>", "Re: SWAIA board", "robin@lakes.example.org", 1.0, 1, "personal", "ran"),
    ("a1", "INBOX", "alex", "srv-2", "<2@x>", "Dues", "office@swaia.example.org", 2.0, 0, "newsletter", "ran"),
    ("a1", "INBOX", "alex", "srv-3", "<3@x>", "SWAIA picnic", "j@k.example", None, 0, None, None),
    ("a1", "INBOX", "jordan", "srv-4", "<4@x>", "SWAIA vote", "j@k.example", 3.0, 0, "Some Odd Value!", "old"),
    ("a1", "INBOX", "alex", "srv-5", "<5@x>", "Lunch", "a@b.example", 3.0, 1, "personal", "ran")])
M.commit()
M.close()
so = rc.server_outcome("swaia", "swaia.example.org", "alex", {"srv-1"}, SERVER)
check("one per message, not per folder; derived, the admin's, KB-flagged, in the KB's list",
      [so[k] for k in ("server messages", "server derived", "server admin's", "server kb flag", "server in kb")], [4, 3, 3, 1, 1])
check("…by category and passes (none when unset; an odd value isn't echoed)",
      ({k: v for k, v in so.items() if k.startswith(("server category", "server passes"))}),
      {"server category personal": 1, "server category newsletter": 1, "server category none": 1,
       "server passes ran": 2, "server passes none": 1, "server passes old": 1})
check("…never the sender domain, a subject or an id in the output",
      [w for w in ("swaia.example", "lakes", "board", "srv-", "<1@") if w in json.dumps(so).lower()], [])
check("no mail_server.db: '-'", rc.server_outcome("swaia", None, "alex", set(), _TMP / "none.db")["server messages"], "-")
check("thread_outcome includes the server store when given", "server messages" in
      rc.thread_outcome("swaia", None, "alex", set(), LEDGER, TRIAGE, SERVER), True)

print("── calendar: the ledger against the Mac ──")
check("the admin's live calendar items vs the Mac's count, with the difference", rc.calendar_outcome("alex", 4, LEDGER),
      {"ledger live (admin)": 2, "mac tracked": 4, "difference": 2})
check("…no Mac count given: shown as '-'", rc.calendar_outcome("alex", None, LEDGER),
      {"ledger live (admin)": 2, "mac tracked": "-"})

print("── calendar: inside the Mac's window, with a day's margin ──")
from datetime import datetime, timedelta, timezone  # noqa: E402
NOW = datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
inside = f"EVT-A@{iso(NOW - timedelta(days=28, hours=23))}"     # just inside the window less the margin
edge = f"EVT-B@{iso(NOW - timedelta(days=29, hours=12))}"       # inside the Mac's 30 days, within the margin: left out
outside = f"EVT-C@{iso(NOW - timedelta(days=30, hours=1))}"     # aged out of the Mac's window
future = f"EVT-D@{iso(NOW + timedelta(days=3))}"
check("occurrence(): the @date in a source_id; offsets and fractions; none without one",
      (rc.occurrence("X@2026-09-10T00:15:00Z"), rc.occurrence("X@2026-09-10T02:15:00.123+0200"), rc.occurrence("X")),
      (datetime(2026, 9, 10, 0, 15, tzinfo=timezone.utc).timestamp(),) * 2 + (None,))
check("in_window: just inside counts; at the edge (within the margin) and outside don't; the future and undated do",
      rc.in_window([inside, edge, outside, future, "EVT-E"], NOW.timestamp()), 3)
CAL = _TMP / "cal-ledger.db"
C2 = sqlite3.connect(CAL)
C2.execute("CREATE TABLE items (source TEXT, source_id TEXT, item_id TEXT, kb INTEGER, owner TEXT, deleted_at REAL)")
for sid in (inside, edge, outside, future):
    C2.execute("INSERT INTO items VALUES ('calendar', ?, ?, 1, 'alex', NULL)", (sid, "kb-" + sid[:5]))
C2.commit()
C2.close()
check("aged-out rows on titan don't make a difference: the Mac (tracking the 2 inside) matches inside the window",
      rc.calendar_outcome("alex", 3, CAL, mac_window=2, now=NOW.timestamp()),
      {"ledger live (admin)": 4, "mac tracked": 3, "ledger in window": 2, "mac in window": 2, "difference": 0})
SYNC2 = _TMP / "sync-window"
SYNC2.mkdir()
(SYNC2 / "calendar.json").write_text(json.dumps({"tracked": {inside: {}, edge: {}, future: {}}, "outbox": [], "gaveUpItems": {}}))
check("mac-count: the calendar's in-window count, by the same rule",
      rc.mac_count(SYNC2, NOW.timestamp())["calendar"], {"tracked": 3, "outbox": 0, "gave_up": 0, "in_window": 2})
SYNC = _TMP / "sync"
SYNC.mkdir()
(SYNC / "calendar.json").write_text(json.dumps({"tracked": {"a": {}, "b": {}}, "outbox": [{"title": "Hale's party"}],
                                                "gaveUpItems": {"c": {}}}))
(SYNC / "calendars.json").write_text(json.dumps({"selected": ["x"]}))
check("mac-count: per source, counts only (a prefs file without 'tracked' skipped)",
      {k: {f: n for f, n in v.items() if f != "in_window"} for k, v in rc.mac_count(SYNC).items()},
      {"calendar": {"tracked": 2, "outbox": 1, "gave_up": 1}})

print("── compare: before → after, the kid and the screen flagged ──")
before = {"viewers": {"admin": {"cards": 3}, "kid": {"cards": 1, "threads": "403", "kb:bill": 0},
                      "screen": {"documents": 7}}, "outcomes": {"calendar": {"ledger live (admin)": 150}}}
after = {"viewers": {"admin": {"cards": 9}, "kid": {"cards": 1, "threads": "403", "kb:bill": 2},
                     "screen": {"documents": 8}}, "outcomes": {"calendar": {"ledger live (admin)": 167}}}
lines, flags = rc.compare(before, after)
check("an admin increase isn't flagged; the kid's and the screen's are", flags, ["kid kb:bill: 0 -> 2", "screen documents: 7 -> 8"])
check("…a 403 that stays a 403 isn't", any("threads" in f for f in flags), False)
check("…a 403 that becomes a number is", rc.compare({"viewers": {"kid": {"threads": "403"}}},
                                                    {"viewers": {"kid": {"threads": 2}}})[1], ["kid threads: 403 -> 2"])
check("…the outcomes shown before → after", any("150" in l and "167" in l for l in lines), True)
print("── references: KB's live check, a verdict per viewer ──")
good = {"owner": {"tried": 5, "opened": 3, "refused": 0, "card": 0, "errors": 2, "error_reasons": {"device_offline": 2}},
        "other_adult": {"tried": 5, "opened": 0, "refused": 5, "card": 0, "errors": 0},
        "kid": {"tried": 5, "opened": 0, "refused": 5, "card": 0, "errors": 0},
        "screen": {"tried": 5, "opened": 0, "refused": 5, "card": 0, "errors": 0}}
check("the expected shape passes for every viewer", {v: r["verdict"] for v, r in rc.references_verdict(good).items()},
      {"owner": "pass", "other_adult": "pass", "kid": "pass", "screen": "pass"})
bad = json.loads(json.dumps(good))
bad["owner"].update(opened=2, errors=3, error_reasons={"device_offline": 2, "server_unreachable": 1, "Robin's Mac said no": 1})
bad["kid"]["opened"] = 1
bad["other_adult"]["opened"] = 1
bad["screen"]["card"] = 1
v = rc.references_verdict(bad)
check("owner with a server_unreachable, a kid opening one, an adult opening one, a screen's card: each a FAIL",
      [v[x]["verdict"] for x in rc.REF_VIEWERS], ["FAIL", "FAIL", "FAIL", "FAIL"])
check("…error reasons as short codes only (free text dropped)", {k: n for k, n in v["owner"].items() if k.startswith("error ")},
      {"error device_offline": 2, "error server_unreachable": 1})
check("owner: Mac-held ones (device_offline) aren't failures, but an unread reachable one is",
      rc.references_verdict({"owner": {"tried": 5, "opened": 2, "refused": 0, "card": 0, "errors": 2,
                                       "error_reasons": {"device_offline": 2}}})["owner"]["verdict"], "FAIL")
check("owner: only Mac-held ones sampled is not a pass", rc.references_verdict({"owner": {"tried": 3, "opened": 0, "refused": 0,
      "card": 0, "errors": 3, "error_reasons": {"device_offline": 3}}})["owner"]["verdict"], "nothing read (all on the Mac)")
check("other adult: a card is information, not a failure (a shared device reference)", rc.references_verdict(
      {"other_adult": {"tried": 5, "opened": 0, "refused": 3, "card": 2, "errors": 0}})["other_adult"]["verdict"], "pass")
check("nothing sampled is never a pass", rc.references_verdict({**good, "owner": {"tried": 0, "opened": 0, "refused": 0, "card": 0,
      "errors": 0}})["owner"]["verdict"],
      "nothing sampled")
check("no checker on this titan: '-' rows, no verdict", {x: r["verdict"] for x, r in rc.references_verdict(None).items()},
      {x: "-" for x in rc.REF_VIEWERS})
check("…a non-int count (a title where a number should be) is '-', no verdict",
      rc.references_verdict({**good, "kid": {**good["kid"], "tried": "Hale's bill"}})["kid"], {"tried": "-", "opened": 0, "refused": 5,
                                                                                             "card": 0, "errors": 0, "verdict": "-"})
import inspect  # noqa: E402
import references as _refs  # noqa: E402
_real = _refs.live_check
check("the real checker takes sample= and names the same viewers", ("sample" in inspect.signature(_real).parameters,
      tuple(_refs.LIVE_VIEWERS)), (True, rc.REF_VIEWERS))
_refs.live_check = lambda sample=5: {**good, "kid": {**good["kid"], "opened": 2}}
r = rc.references_check(5)
check("references_check calls the module's live_check", (r["owner"]["verdict"], r["kid"]["verdict"]), ("pass", "FAIL"))
_refs.live_check = lambda sample=5: 1 / 0
check("…a checker that raises: 'err' for every viewer", {x: r["verdict"] for x, r in rc.references_check(5).items()},
      {x: "err" for x in rc.REF_VIEWERS})
_refs.live_check = _real
lines, flags = rc.compare({"viewers": {}}, {"viewers": {}, "outcomes": {"references": rc.references_verdict(bad)}})
check("compare flags every references FAIL", sorted(flags), sorted(f"references {x}: FAIL" for x in rc.REF_VIEWERS))
check("…the render shows a block per viewer, counts and verdict only", ("== references: kid ==" in rc.render({}, "after",
      {"references": rc.references_verdict(good)}), "Robin" in "\n".join(lines)), (True, False))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
