#!/usr/bin/env python3
# gate: hermetic
"""POST /v1/files (titan-node#713): a person's file for a turn, kept as theirs. Size cap and type allow-list, served
back to its owner only, read by their turns (and their audience's) and nobody else's, gone after its time, never in
the knowledge base. Hermetic: temp uploads dir and dbs, the API through TestClient, made-up people.

Run: python3 scripts/test_uploads.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp())
os.environ["UPLOADS_DIR"] = str(_TMP / "uploads")
os.environ["UPLOADS_DB"] = str(_TMP / "uploads.db")
os.environ["UPLOADS_MAX_MB"] = "1"
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
os.environ["AGENT_TURN_LOG"] = str(_TMP / "turns.jsonl")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uploads as U  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PDF = b"%PDF-1.7\n" + b"x" * 40
DOCX = b"PK\x03\x04" + b"\x00" * 40

print("── names and types ──")
check("a path's last part only", U.safe_name("../../etc/passwd"), "passwd")
check("odd characters become _", U.safe_name("my photo (1)?.jpg"), "my photo (1)_.jpg")
check("no hidden files", U.safe_name(".bashrc.txt"), "bashrc.txt")
check("long names keep their extension", U.safe_name("a" * 300 + ".pdf")[-4:] + str(len(U.safe_name("a" * 300 + ".pdf"))), ".pdf100")
check("a PNG is a PNG", U.kind_of("a.png", PNG), "image/png")
check("a .png that isn't one is refused", U.kind_of("a.png", PDF), None)
check("a PDF", U.kind_of("bill.PDF", PDF), "application/pdf")
check("a Word document (zip)", U.kind_of("letter.docx", DOCX), U.TYPES["docx"][0])
check("text in UTF-8", U.kind_of("notes.md", "Grocery list — eggs".encode()), "text/markdown")
check("text with NULs isn't text", U.kind_of("notes.txt", b"ab\x00cd"), None)
check("HEIC from an iPhone", U.kind_of("IMG_1.HEIC", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20), "image/heic")
for bad in ("run.sh", "app.exe", "page.html", "x.svg", "archive.zip", "noext"):
    check(f"not allowed: {bad}", U.kind_of(bad, b"#!/bin/sh\n"), None)

print("\n── save, owner, audience ──")
NOW = time.time()
code, a = U.save("alex", "Receipt.pdf", PDF, now=NOW)
check("201 with an id, the name, a path under the uploads dir", (code, len(a["id"]), a["name"], a["path"].startswith(str(U.UPLOADS_DIR) + "/")),
      (201, 24, "Receipt.pdf", True))
check("private by default", a["audience"], [])
check("expires after the TTL", a["expires_at"], int(NOW + U.TTL_SECONDS))
check("the file is only readable by titan's user", oct(os.stat(a["path"]).st_mode & 0o777), "0o600")
code, b = U.save("robin", "Receipt.pdf", PDF, audience=["Alex", "robin", "bad id!", "jordan"], now=NOW)
check("same name, another person: its own folder", (code, b["path"] != a["path"]), (201, True))
check("the audience: other people only, cleaned", b["audience"], ["alex", "jordan"])
check("no person, no upload", U.save(None, "a.pdf", PDF)[0], 400)
check("no extension", U.save("alex", "README", b"hello")[0], 400)
check("empty", U.save("alex", "a.txt", b"")[0], 400)
check("too large", U.save("alex", "big.pdf", b"%PDF-" + b"x" * (U.MAX_BYTES + 1))[0], 413)
check("a script is refused", U.save("alex", "run.sh", b"#!/bin/sh\nrm -rf /\n")[0], 415)

print("\n── served back to its owner only ──")
check("its owner", (U.for_owner(a["id"], "alex", now=NOW) or {}).get("name"), "Receipt.pdf")
check("not someone else", U.for_owner(a["id"], "robin", now=NOW), None)
check("not the audience either (GET is the owner's)", U.for_owner(b["id"], "alex", now=NOW), None)
check("not an odd id", U.for_owner("../" + a["id"], "alex", now=NOW), None)
check("not after its time", U.for_owner(a["id"], "alex", now=NOW + U.TTL_SECONDS + 1), None)

print("\n── read by turns: the owner's and the audience's ──")
check("the owner's turn", U.may_read(a["path"], "alex", now=NOW), True)
check("someone else's turn", U.may_read(a["path"], "robin", now=NOW), False)
check("the audience's turn", U.may_read(b["path"], "jordan", now=NOW), True)
check("a turn with no person", U.may_read(a["path"], None, now=NOW), False)
check("the uploads folder itself", U.may_read(str(U.UPLOADS_DIR), "alex", now=NOW), False)
check("a folder without its file", U.may_read(str(Path(a["path"]).parent), "alex", now=NOW), False)
check("into another's folder with ..", U.may_read(f"{Path(a['path']).parent}/../{b['id']}/Receipt.pdf", "alex", now=NOW), True)
check("…which is Robin's shared with Alex: allowed; with Ava: no",
      U.may_read(f"{Path(a['path']).parent}/../{b['id']}/Receipt.pdf", "ava", now=NOW), False)
link = _TMP / "elsewhere.pdf"
link.symlink_to(a["path"])
check("a symlink from outside into Alex's file is still Alex's", (U.may_read(str(link), "alex", now=NOW), U.may_read(str(link), "robin", now=NOW)), (True, False))
check("a path outside the uploads: other rules decide", U.may_read("/etc/hosts", "robin"), True)
check("expired: nobody", U.may_read(a["path"], "alex", now=NOW + U.TTL_SECONDS + 1), False)

print("\n── the agent's tools ──")
import agent_runtime_lib as lib  # noqa: E402
lib._viewer = lambda: ("robin", "adult")
check("a file tool on Alex's upload, in Robin's turn: refused", lib._touches_others_upload({"path": a["path"]}), True)
check("inside a command too", lib._touches_others_upload({"command": f"cat {a['path']} | head"}), True)
check("in a list of paths", lib._touches_others_upload({"paths": ["/tmp/x", a["path"]]}), True)
lib._viewer = lambda: ("alex", "adult")
check("Alex's own turn: fine", lib._touches_others_upload({"path": a["path"]}), False)
check("other paths: untouched", lib._touches_others_upload({"path": "/ai/tmp/notes.txt", "n": 3}), False)
real_now = time.time()
code, live = U.save("alex", "today.txt", b"milk, eggs")
out = lib._execute_tool("file_read", {"path": live["path"]})
check("file_read: Alex reads her own file", "milk, eggs" in out, True)
lib._viewer = lambda: ("robin", "adult")
out = lib._execute_tool("file_read", {"path": live["path"]})
check("file_read: Robin's turn is refused, nothing read", ("Refused" in out, "milk" in out), (True, False))

print("\n── sweep ──")
_, later = U.save("alex", "later.txt", b"still here", now=NOW + U.TTL_SECONDS)   # sent a TTL later
os.makedirs(U.UPLOADS_DIR / ("f" * 24))   # a folder without a record
(U.UPLOADS_DIR / "keep-me").mkdir()          # not an upload's: left alone
gone = U.sweep(now=NOW + U.TTL_SECONDS + 10)
check("expired uploads (Alex's, Robin's, today.txt) and the orphan folder removed",
      (gone, Path(a["path"]).exists(), Path(b["path"]).exists(), Path(live["path"]).exists(), (U.UPLOADS_DIR / ("f" * 24)).exists()),
      (4, False, False, False, False))
check("records gone too", (U.record(a["id"]), U.record(b["id"])), (None, None))
check("anything else in the folder is left", (U.UPLOADS_DIR / "keep-me").exists(), True)
check("a live one stays", Path(later["path"]).exists() and U.record(later["id"]) is not None, True)

print("\n── the API ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
store = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(store)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device):
    st = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}, "label": device}, headers=M).json()
    r = c.post("/v1/pair/claim", json={"code": st["code"], "device_name": device, "platform": "mac"}).json()
    return {"X-Agent-Token": r["token"]}, r["person"]["id"]


ALEX, alex_id = pair("Alex", "adult", "Alex's Mac")
ROBIN, robin_id = pair("Robin", "adult", "Robin's Mac")
TV, _ = pair("Living room TV", "household", "Living room TV")
r = c.post("/v1/files?name=Scan%201.pdf", content=PDF, headers=ALEX)
up = r.json()
check("POST /v1/files: 201, the path the turn names", (r.status_code, up["name"], up["path"].startswith(str(U.UPLOADS_DIR))),
      (201, "Scan 1.pdf", True))
check("its owner gets it back", (c.get(f"/v1/files/uploads/{up['id']}", headers=ALEX).content, ), (PDF,))
check("with its type", c.get(f"/v1/files/uploads/{up['id']}", headers=ALEX).headers["content-type"], "application/pdf")
check("someone else: 404", c.get(f"/v1/files/uploads/{up['id']}", headers=ROBIN).status_code, 404)
check("the admin: 404 too (it's Alex's)", c.get(f"/v1/files/uploads/{up['id']}", headers=M).status_code, 404)
check("a household device can't upload", c.post("/v1/files?name=a.pdf", content=PDF, headers=TV).status_code, 403)
check("or fetch one not shared with it (a voice note can be: test_voice_notes)", c.get(f"/v1/files/uploads/{up['id']}", headers=TV).status_code, 404)
check("no token: 401", c.post("/v1/files?name=a.pdf", content=PDF).status_code, 401)
check("too large: 413", c.post("/v1/files?name=a.pdf", content=b"%PDF-" + b"x" * U.MAX_BYTES, headers=ALEX).status_code, 413)
check("not allowed: 415", c.post("/v1/files?name=a.html", content=b"<script>", headers=ALEX).status_code, 415)
check("the name from X-Filename", c.post("/v1/files", content=PNG, headers={**ALEX, "X-Filename": "pic.png"}).json().get("name"), "pic.png")
r = c.post(f"/v1/files?name=plan.txt&audience={robin_id}", content=b"the plan", headers=ALEX).json()
check("an audience on the way in", r["audience"], [robin_id])
check("the route table: people only", [hh.route_allowed(x, "POST", "/v1/files") for x in ("admin", "adult", "kid", "household", hh.ENGINEERING)],
      [True, True, True, False, False])
check("…and GET by a well-formed id only", (hh.route_allowed("adult", "GET", "/v1/files/uploads/" + "a" * 24),
                                            hh.route_allowed("adult", "GET", "/v1/files/uploads/../x")), (True, False))
check("nothing went into the knowledge base", any("uploads" in str(p) for p in (_TMP).glob("**/kb*")), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
