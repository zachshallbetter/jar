#!/usr/bin/env python3
# gate: hermetic
"""The household's documents (jellybean#320): household-audience only, for every caller, the admin included; KB text
served, device originals listed but not served ("not shared as a file"); anything else is the same 404 whether it
exists or not. Two people, a kid, the household screen. Fictional (Alex, Robin, Ava); temporary stores.
Run: python3 scripts/test_documents.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-documents-test-"))
os.environ["REFERENCES_DB"] = str(_TMP / "references.db")
sys.path.insert(0, str(Path(__file__).parent))
import documents as D  # noqa: E402
import references as R  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


SOURCES = [{"source_id": "note-recycling", "source_type": "note", "title": "Recycling days", "updated_at": "2026-10-04"},
           {"source_id": "note-alex-private", "source_type": "note", "title": "Alex's own notes", "updated_at": "2026-10-05"},
           {"source_id": "note-robin-private", "source_type": "note", "title": "Robin's plans", "updated_at": "2026-10-05"},
           {"source_id": "note-adults", "source_type": "note", "title": "Budget", "updated_at": "2026-10-03"},
           {"source_id": "cx-mail-1", "source_type": "connector:mail", "title": "Harbor Gas bill", "updated_at": "2026-10-05"}]
ACL = {"note-recycling": ("alex", "household"), "note-alex-private": ("alex", "private"),
       "note-robin-private": ("p_robin", "private"), "note-adults": ("alex", "adults"), "cx-mail-1": ("alex", "household")}
acl = lambda sid, md: ACL[sid]  # noqa: E731
TEXT = {"note-recycling": "Recycling goes out Tuesday night.", "note-alex-private": "secret", "note-robin-private": "secret"}
con = R._db(None)
for rid, owner, aud, kind, nid in (("rf-0000000000000001", "alex", "household", "scan", "scan-1.pdf"),
                                   ("rf-0000000000000002", "p_robin", "private", "scan", "scan-2.pdf"),
                                   ("rf-0000000000000003", "alex", "household", "mail", "m-1")):
    con.execute("INSERT INTO refs (id, owner, audience, device_id, kind, native_id, title, dates, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,1,1)", (rid, owner, aud, "dev-1", kind, nid, f"{kind} {rid[-1]}", "{}"))
con.commit()
con.close()


def ids(role):
    return [d["id"] for d in D.listing(role, SOURCES, acl)["documents"]]


print("── the list: household documents only ──")
want = ["kb:note-recycling", "rf-0000000000000001"]
for role in ("admin", "adult", "kid", "household"):
    check(f"{role}: the household note and the household scan, nothing else", sorted(ids(role)), want)
check("the admin's own private note is never listed here", "kb:note-alex-private" in ids("admin"), False)
check("another person's private note never", "kb:note-robin-private" in ids("adult"), False)
check("adults-only isn't household", "kb:note-adults" in ids("adult"), False)
check("mail is never a document", any("cx-mail" in i for i in ids("admin")), False)
check("a household reference that isn't a scan, file or photo isn't either", "rf-0000000000000003" in ids("admin"), False)
docs = {d["id"]: d for d in D.listing("kid", SOURCES, acl)["documents"]}
check("a KB document: titan's text, not an original", (docs["kb:note-recycling"]["source"], docs["kb:note-recycling"]["original"],
                                                     docs["kb:note-recycling"]["available"]), ("kb", False, True))
check("a device original: listed, not available", (docs["rf-0000000000000001"]["source"],
                                                    docs["rf-0000000000000001"]["available"],
                                                    docs["rf-0000000000000001"]["content_type"]), ("device", False, "application/pdf"))

print("── content ──")
st, body, ct, etag = D.content("kb:note-recycling", "household", SOURCES, acl, text_of=TEXT.get)
check("the household screen reads a household note's text", (st, body.decode(), ct.split(";")[0], bool(etag)),
      (200, "Recycling goes out Tuesday night.", "text/markdown", True))
st, body, _, _ = D.content("rf-0000000000000001", "kid", SOURCES, acl, names={"alex": "Alex Hale"})
check("a device original: not shared as a file, never its bytes", (st, body),
      (200, {"available": False, "reason": "on Alex's device; not shared as a file"}))
for label, did, role in (("a private note, even for its owner the admin", "kb:note-alex-private", "admin"),
                         ("another person's private note", "kb:note-robin-private", "adult"),
                         ("another person's private scan", "rf-0000000000000002", "admin"),
                         ("an id that doesn't exist (the same answer)", "kb:note-nope", "kid")):
    try:
        D.content(did, role, SOURCES, acl, text_of=TEXT.get)
        check(f"404: {label}", "served", 404)
    except D.Refused as e:
        check(f"404: {label}", e.status, 404)
try:
    D.listing("guest", SOURCES, acl)
    check("an unknown role gets nothing", "listed", 403)
except D.Refused as e:
    check("an unknown role gets nothing", e.status, 403)

print("── the routes ──")
os.environ.setdefault("HOUSEHOLD_DB", str(_TMP / "household.db"))
import household_identity as hh  # noqa: E402
check("every role may GET both routes", all(hh.route_allowed(r, "GET", p) for r in hh.ROLES
                                            for p in ("/v1/documents", "/v1/documents/kb:x/content")), True)
check("nobody may write there", any(hh.route_allowed(r, "POST", "/v1/documents") for r in hh.ROLES if r != "admin"), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
