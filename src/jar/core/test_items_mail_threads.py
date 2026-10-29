#!/usr/bin/env python3
# gate: hermetic
"""#1497 slice 1: a mail thread is ONE member in every items view (the newest mail, the thread's latest date, a
count, its subject without Re:/Fwd:), keyed by the stores' own thread ids, never by subject; every mail member Opens
somewhere real (its thread's KB entry, or a Mac-held mail's device reference; else no Open at all); a thread keeps
the narrowest audience of its mails; titan-made project names are short, a person's never change. Fictional mail and
household (Alex admin, Robin adult); temporary stores.
Run: python3 scripts/test_items_mail_threads.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mail-threads-"))
for k, v in {"SPACES_DB": "spaces.db", "HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json",
             "KB_ACL_DB": "kb_acl.db", "MAIL_SERVER_DB": "mail_server.db", "ITEM_REFS_DB": "item_refs.db",
             "REFERENCES_DB": "references.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import items as I  # noqa: E402
import linker  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


ALEX, ROBIN = "alex", "robin"
NOW = 1_791_000_000.0          # 2026-10-03, fixed

print("── subjects ──")
check("Re:/Fwd: in any number and case, Re[2]: too, spaces collapsed",
      [I.mail_subject(s) for s in ("Re: RE: Fwd: Harbor  deck plans", "FW: re[2]: Dock invoice", "Harbor deck plans",
                                   "Re:", "", "Regarding the dock")],
      ["Harbor deck plans", "Dock invoice", "Harbor deck plans", "(no subject)", "(no subject)", "Regarding the dock"])

print("── narrowest audience ──")
check("private wins; named lists intersect; else admins < adults < household",
      [I.narrowest(a) for a in (["household", "private"], ["household", "adults"], ['["alex","robin"]', '["alex"]'],
                                ['["alex"]', '["robin"]'], ["household", "admins"], ['["alex"]', "admins"],
                                ["household", "{bad"], [])],
      ["private", "adults", '["alex"]', "private", "admins", "private", "private", "private"])


def linked(iid, day, owner=ALEX, aud="private"):
    return linker.Item(iid, "mail", date.fromisoformat(day), owner, aud)


T1, T2 = "cx-mailthread-" + "a" * 16, "cx-mailthread-" + "b" * 16
three = [I.from_linked(linked(f"cx-mailsrv-{n:016x}", d), s, ["p1" if n != 3 else "p2"], {"thread": T1})
         for n, d, s in ((1, "2026-09-28", "Harbor deck plans"), (2, "2026-10-01", "Re: Harbor deck plans"),
                         (3, "2026-09-30", "RE: Re: Harbor deck plans"))]
kb_entry = I.from_mail({"source_id": T1, "title": "Re: Harbor deck plans",
                        "metadata": {"observed_at": "2026-10-01T09:00:00Z"}}, ALEX)
other_thread = I.from_linked(linked("cx-mailsrv-" + "9" * 16, "2026-10-02"), "Re: Harbor deck plans", ["p1"],
                             {"thread": T2})

print("── one member per thread ──")
out = I.collapse_threads(three + [kb_entry, other_thread])
by = {x.get("thread"): x for x in out}
t1 = by.get(T1) or {}
check("a thread's three mails and its KB entry: one member; the same subject in another thread stays its own",
      (len(out), sorted(by)), (2, [T1, T2]))
check("…the newest mail's id, a count of its mails (the KB entry is the thread, not a mail), the latest date",
      (t1.get("id"), t1.get("count"), t1.get("time", {}).get("at", "")[:10]),
      ("cx-mailsrv-" + "%016x" % 2, 3, "2026-10-01"))
check("…its subject without Re:/Fwd:", t1.get("title"), "Harbor deck plans")
check("…Open on the thread's KB entry (titan's derived view)", (t1.get("ops", [])[:1], t1.get("routes", {}).get("open")),
      (["open"], ("GET", f"/v1/kb/{T1}")))
check("…every project any of its mails is filed under", t1.get("projects"), ["p1", "p2"])
check("…and every mail it holds", t1.get("thread_items"), sorted([x["id"] for x in three] + [f"mail:{T1}"]))
check("one mail in its thread: count 1", by.get(T2, {}).get("count"), 1)

print("── a thread keeps the narrowest audience of its mails ──")
mixed = [I.from_linked(linked("cx-mailsrv-" + "c" * 16, "2026-10-01", aud="household"), "Re: Gate code", [], {"thread": T2}),
         I.from_linked(linked("cx-mailsrv-" + "d" * 16, "2026-09-30", aud="private"), "Gate code", [], {"thread": T2})]
check("household + private: private", I.collapse_threads(mixed)[0]["audience"], "private")

print("── Open goes somewhere real ──")
mac = I.from_linked(linked("cx-mail-" + "e" * 16, "2026-10-01"), "Re: Ferry times", [], {"reference": "rf-" + "1" * 16})
check("a Mac-held mail opens on the device that has it, by its reference",
      (mac["routes"].get("open"), mac.get("params")),
      (("GET", "/v1/references/rf-" + "1" * 16), {"open": {"reference": "rf-" + "1" * 16}}))
lost = I.from_linked(linked("cx-mail-" + "f" * 16, "2026-10-01"), "Re: Ferry times", [], None)
check("a mail neither store knows: no Open (never a dead button)", (lost["ops"], lost["routes"]), ([], {}))
check("a KB thread entry opens itself", kb_entry["routes"]["open"], ("GET", f"/v1/kb/{T1}"))
check("not mail: unchanged", I.from_linked(linker.Item("x1", "message", None, ALEX, "private"), "Re: hi", [], None)["title"],
      "Re: hi")

print("── through a view: members, slots, operations ──")


def view(viewer, role, its, pid="p1"):
    return I.resolve(f"project:{pid}", viewer, role, {"linked": lambda: [dict(x, routes=dict(x["routes"]),
                                                                              ops=list(x["ops"])) for x in its]},
                     now=NOW, project_ok=lambda p: True)


sv = view(ALEX, "admin", three + [kb_entry, mac])
g = next(g for g in sv["groups"] if g["id"] == "g-project")
ops = {o["id"]: o for o in sv["operations"]}
m = next(x for x in g["members"] if x["subject"]["id"] == "cx-mailsrv-" + "%016x" % 2)
check("the project page: the thread is one member (Zach's 3–4 identical rows)",
      sorted(x["subject"]["id"] for x in g["members"]), ["cx-mailsrv-" + "%016x" % 2])
check("…its slots: the clean subject and the count", (m["slots"]["title"], m["slots"].get("count")), ("Harbor deck plans", 3))
op = ops.get(f"op-open-{m['subject']['id']}") or {}
check("…its Open operation carries the route", op.get("route"), {"method": "GET", "path": f"/v1/kb/{T1}"})
sv2 = view(ALEX, "admin", [dict(mac, projects=["p1"])])
op2 = next((o for o in sv2["operations"] if o["verb"] == "open"), {})
check("…a Mac-held mail's Open carries its reference", (op2.get("route", {}).get("path"), op2.get("params")),
      ("/v1/references/rf-" + "1" * 16, {"reference": "rf-" + "1" * 16}))

print("── views × viewers (projection first: a viewer's readers return only what they may see) ──")
for who, role in ((ROBIN, "adult"), (None, "household")):
    sv_ = view(who, role, [])
    check(f"{who or 'the screen'}: none of Alex's thread, no count, no operation on it",
          ([m_ for g_ in sv_["groups"] for m_ in g_["members"]], [o for o in sv_["operations"] if o["verb"] == "open"]),
          ([], []))
seen_two = view(ALEX, "admin", three[:2])
m2 = next(x for g_ in seen_two["groups"] for x in g_["members"])
check("a viewer who sees two of a thread's mails: a count of two, never the third", m2["slots"].get("count"), 2)

print("── the stores' lookups ──")
import mail_server  # noqa: E402
con = mail_server._db(_TMP / "mail_server.db")
for n, (acct, tid) in enumerate((("icloud", "<t1@harbor.example>"), ("icloud", "<t1@harbor.example>"),
                                 ("work", "<t1@harbor.example>"))):
    con.execute("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, thread_id, subject) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (acct, "INBOX", 1, n, ALEX, f"cx-mailsrv-{n:016x}", f"<m{n}@x>", tid, "s"))
con.commit()
con.close()
got = mail_server.threads_of([f"cx-mailsrv-{n:016x}" for n in range(3)] + ["cx-mail-0", "cx-mailsrv-missing"],
                             _TMP / "mail_server.db")
check("threads_of: one thread key per (owner, account, thread id); another account's same id is its own",
      (len(got), got.get("cx-mailsrv-" + "0" * 16) == got.get("cx-mailsrv-" + "%016x" % 1),
       got.get("cx-mailsrv-" + "0" * 16) != got.get("cx-mailsrv-" + "%016x" % 2),
       got.get("cx-mailsrv-" + "0" * 16) == mail_server.thread_sid({"owner": ALEX, "name": "icloud"}, "<t1@harbor.example>")),
      (3, True, True, True))
check("…no store: nothing", mail_server.threads_of(["cx-mailsrv-1"], _TMP / "nowhere" / "x.db"), {})
import item_refs  # noqa: E402
rc = item_refs._db(_TMP / "item_refs.db")
rc.execute("INSERT INTO derived (item_id, source, owner, ref_id, created_at, updated_at) VALUES (?,?,?,?,?,?)",
           ("cx-mail-" + "e" * 16, "mail", ALEX, "rf-" + "1" * 16, 0, 0))
rc.commit()
rc.close()
check("refs_of: a Mac-held item's reference id", item_refs.refs_of(["cx-mail-" + "e" * 16, "cx-mail-x"], _TMP / "item_refs.db"),
      {"cx-mail-" + "e" * 16: "rf-" + "1" * 16})

print("── titan-made project names are short; a person's never change ──")
import projects as P  # noqa: E402
import spaces  # noqa: E402
check("short_name: Re:/Fwd: and [tags] gone, a few words, never ending on a filler word",
      [P.short_name(n) for n in ("Re: RE: Fwd: Harbor Co: Transfer Website Hosting",
                                 "Harbor Co: Transfer Website Hosting and DNS records before the cutover",
                                 "[EXT] Re: [Harbor] Fwd: Dock invoice", "Dock")],
      ["Harbor Co: Transfer Website Hosting", "Harbor Co: Transfer Website Hosting", "Dock invoice", "Dock"])
db = _TMP / "spaces2.db"
titan_p = spaces.create_project(ALEX, "admin", "Re: Re: Harbor Co: Transfer Website Hosting and DNS records before the cutover", db=db)
person_p = spaces.create_project(ALEX, "admin", "Re: my long name for the dock and gate and everything else", db=db)
c = sqlite3.connect(db)
P._db(db).close()       # the columns
c.execute("UPDATE projects SET name_source='earned' WHERE id=?", (titan_p["id"],))
c.execute("UPDATE projects SET name_source='person' WHERE id=?", (person_p["id"],))
c.commit()
c.close()
P._shortened.discard(str(db))
P._db(db).close()
check("a stored titan name, shortened once; a person's as typed (\"Named by you\")",
      (P.get(titan_p["id"], db)["name"], P.get(person_p["id"], db)["name"]),
      ("Harbor Co: Transfer Website Hosting", "Re: my long name for the dock and gate and everything else"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
