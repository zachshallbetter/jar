#!/usr/bin/env python3
# gate: hermetic
"""Share the file (#1081): an owner shares and unshares one item's original; a kid can't share; another adult reads the
original only while it's shared; never on the household screen; every read is audited and the owner gets one quiet
card a day, never for their own reads; the owner's device offline is {available: false, reason} with nothing audited.
Through the real routes and stores; the owner's device is a fake. Fictional people (Robin and Alex, adults; Ava, a kid;
a living-room screen); temporary stores; no model.
Run: python3 scripts/test_file_share.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-fileshare-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "HOUSEHOLD_PREFS": "household.json",
             "REFERENCES_DB": "references.db", "PROACTIVE_DB": "proactive.db", "PUSH_DB": "push.db",
             "KB_ACL_DB": "kb_acl.db", "AGENT_TURN_LOG": "turns.jsonl", "SPACES_DB": "spaces.db",
             "SITTER_DB": "sitter.db", "LOCK_UP_DB": "lock_up.db", "TV_UNLOCK_DB": "tv_unlock.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import file_share  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive  # noqa: E402
import push  # noqa: E402

push.push_card_async = lambda card: None
lib.kb_sources = lambda *a, **k: []          # no KB documents in this test
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ROBIN, robin = pair("Robin Hale", "adult", "Robin's Mac", platform="mac")
ALEX, alex = pair("Alex Hale", "adult", "Alex's iPhone")
AVA, ava = pair("Ava Hale", "kid", "Ava's iPhone")
TV, _ = pair("Living room", "household", "Living room TV", platform="tvos")
lease = c.post("/v1/references", json={"kind": "file", "native_id": "Lease.pdf", "title": "Lease.pdf",
                                       "audience": "household"}, headers=ROBIN).json()
private = c.post("/v1/references", json={"kind": "file", "native_id": "Taxes.pdf", "title": "Taxes.pdf"},
                 headers=ROBIN).json()
drawing = c.post("/v1/references", json={"kind": "file", "native_id": "Drawing.pdf", "title": "Drawing.pdf",
                                         "audience": "household"}, headers=AVA).json()
DEVICE = {"up": True, "asked": 0, "requester": None, "changed": None}
REAL_FETCH = api._fetch_original


def fake_fetch(ref, requester=None):
    DEVICE["asked"] += 1
    DEVICE["requester"] = requester
    if DEVICE["changed"]:
        return {"changed": True, "new_hash": DEVICE["changed"], "reason": "isn't available right now"}
    if not DEVICE["up"]:
        return {"reason": "on Robin's device, which couldn't send it just now"}
    return {"data": b"%PDF-1.4 lease", "content_type": "application/pdf"}


api._fetch_original = fake_fetch


def listed(h):
    return {d["id"]: (d["available"], d.get("file_shared")) for d in c.get("/v1/documents", headers=h).json()["documents"]}


def content(h, rid):
    r = c.get(f"/v1/documents/{rid}/content", headers=h)
    return r.status_code, (r.content if r.headers.get("content-type", "").startswith("application/pdf") else r.json())


def cards_for(h):
    return [(x["title"], x["detail"]) for x in c.get("/v1/proactive", headers=h).json()["cards"] if x.get("source") == "file_share"]


print("── before any share ──")
check("listed as an original, not available, not shared", listed(ALEX).get(lease["id"]), (False, False))
check("content: not shared as a file; the owner's device never asked",
      (content(ALEX, lease["id"])[1], DEVICE["asked"]), ({"available": False, "reason": "on Robin's device; not shared as a file"}, 0))
check("a private reference isn't a household document at all", content(ALEX, private["id"])[0], 404)

print("── who may share ──")
check("a kid can't share (her originals stay text-only)",
      c.put(f"/v1/references/{drawing['id']}/file_share", json={"shared": True}, headers=AVA).status_code, 403)
check("Alex can't share Robin's (404, as if it didn't exist)",
      c.put(f"/v1/references/{lease['id']}/file_share", json={"shared": True}, headers=ALEX).status_code, 404)
check("the screen can't share", c.put(f"/v1/references/{lease['id']}/file_share", json={"shared": True},
                                      headers=TV).status_code, 403)
check("a bad body: 400", c.put(f"/v1/references/{lease['id']}/file_share", json={"shared": "yes"}, headers=ROBIN).status_code, 400)
r = c.put(f"/v1/references/{lease['id']}/file_share", json={"shared": True}, headers=ROBIN)
check("Robin shares it", (r.status_code, r.json()), (200, {"id": lease["id"], "file_shared": True}))
check("the listing says so", listed(ALEX)[lease["id"]], (True, True))

print("── reading while shared ──")
check("Alex reads the original", content(ALEX, lease["id"]), (200, b"%PDF-1.4 lease"))
asked = DEVICE["asked"]
check("Ava (a kid in its audience) reads the text, never the original: refused before the device is asked",
      (content(AVA, lease["id"]), DEVICE["asked"]),
      ((200, {"available": False, "reason": "originals open for adults; you can read the text"}), asked))
check("never on the household screen (and the device isn't asked)",
      (content(TV, lease["id"])[1]["available"], DEVICE["asked"]), (False, asked))
content(ROBIN, lease["id"])
reads = file_share.reads_of(robin)
check("every read audited: who and from which device; a kid's refusal is no read", sorted((x["reader"], bool(x["device"])) for x in reads),
      sorted([(alex, True), (robin, True)]))
check("Robin gets one quiet card for the day (not one per read), never for his own read",
      cards_for(ROBIN), [("Alex opened “Lease.pdf”", "Alex opened “Lease.pdf”")])   # the kid's refusal is no read
check("nobody else sees that card", (cards_for(ALEX), cards_for(AVA), cards_for(TV)), ([], [], []))
DEVICE["up"] = False
n = len(file_share.reads_of(robin))
check("the owner's device can't send it: available false, a reason, nothing audited",
      (content(ALEX, lease["id"])[1]["available"], len(file_share.reads_of(robin))), (False, n))
DEVICE["up"] = True

print("── #1170: who asked, and a file that changed since it was shared ──")
content(ALEX, lease["id"])
check("the read hands the owner's Mac the requester: Alex's id only", DEVICE["requester"], alex)
SENT = {}
REAL_ROUTE, REAL_MAC = api._xd.route, api._xd.mac_for
api._xd.mac_for = lambda owner: "mac-robin"
api._xd.route = lambda req, **kw: (SENT.update(req), {"result": '{"refused": "changed", "content_hash": "sha-new"}'})[1]
got = REAL_FETCH({"id": lease["id"], "owner": robin, "kind": "file", "native_id": "Lease.pdf", "content_hash": "sha-old"},
                 requester=alex)
check("the real item_original args carry requester (the id, nothing else about them)",
      (SENT["args"]["requester"], sorted(SENT["args"])), (alex, ["content_hash", "kind", "max_bytes", "native_id", "ref_id",
                                                               "requester"]))
check("the Mac's {refused: changed} becomes changed + the new hash", (got["changed"], got["new_hash"]), (True, "sha-new"))
api._xd.route, api._xd.mac_for = REAL_ROUTE, REAL_MAC


def changed_cards(h):
    return [x for x in c.get("/v1/proactive", headers=h).json()["cards"] if x.get("source") == "file-share-changed"]


DEVICE["changed"] = "sha-v2"
n = len(file_share.reads_of(robin))
r1 = content(ALEX, lease["id"])[1]
check("a changed file: the reader hears only that it isn't available right now, never why",
      (r1, "chang" in str(r1).lower()), ({"available": False, "reason": "isn't available right now"}, False))
check("…nothing is audited (nothing was read)", len(file_share.reads_of(robin)), n)
cc = changed_cards(ROBIN)
check("Robin gets one quiet card, 7 days", ([x["title"] for x in cc], [x["urgency"] for x in cc],
      [round((x["expires_at"] - x["created_at"]) / 86400) for x in cc]),
      (["“Lease.pdf” changed since you shared it: share this version?"], [0], [7]))
check("…with Share this version, Stop sharing and Later", [a["label"] for a in cc[0]["actions"]],
      ["Share this version", "Stop sharing", "Later"])
content(AVA, lease["id"])
check("a second refused read updates the same card in place: still one", len(changed_cards(ROBIN)), 1)
check("only the owner sees it: not Alex, not Ava, not the screen",
      (changed_cards(ALEX), changed_cards(AVA), changed_cards(TV)), ([], [], []))
cid = cc[0]["id"]


def tap(h, action):
    return c.post(f"/v1/proactive/{cid}/action", json={"action_id": action}, headers=h).json()


check("Alex can't act on Robin's card", tap(ALEX, "share").get("ok"), False)
t = tap(ROBIN, "later")
check("Later: the card stays", (t.get("ok"), len(changed_cards(ROBIN))), (True, 1))
t = tap(ROBIN, "share")
import sqlite3  # noqa: E402
h = sqlite3.connect(os.environ["REFERENCES_DB"]).execute("SELECT content_hash FROM refs WHERE id=?", (lease["id"],)).fetchone()[0]
check("Share this version: shared with the new hash, the card done", (t.get("ok"), h, file_share.is_shared(lease["id"])),
      (True, "sha-v2", True))
DEVICE["changed"] = None
check("…and Alex reads the new version", content(ALEX, lease["id"]), (200, b"%PDF-1.4 lease"))
DEVICE["changed"] = "sha-v3"
content(ALEX, lease["id"])
cid = changed_cards(ROBIN)[0]["id"]
t = tap(ROBIN, "stop")
check("Stop sharing (on a later change): unshared at once", (t.get("ok"), file_share.is_shared(lease["id"])), (True, False))
DEVICE["changed"] = None
c.put(f"/v1/references/{lease['id']}/file_share", json={"shared": True}, headers=ROBIN)

print("── unsharing takes effect at once ──")
c.put(f"/v1/references/{lease['id']}/file_share", json={"shared": False}, headers=ROBIN)
asked = DEVICE["asked"]
check("Alex can't read it any more, and the device isn't asked",
      (content(ALEX, lease["id"])[1], DEVICE["asked"]), ({"available": False, "reason": "on Robin's device; not shared as a file"}, asked))
check("the listing says file_shared false, so File Providers evict their copy", listed(AVA)[lease["id"]], (False, False))
check("the audit is Robin's own to read", (c.get("/v1/references/file_reads", headers=ROBIN).json()["reads"][0]["ref_id"],
                                          c.get("/v1/references/file_reads", headers=ALEX).json()["reads"]), (lease["id"], []))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
