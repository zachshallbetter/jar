#!/usr/bin/env python3
# gate: hermetic
"""An iPhone holds referenced items (titan-node#776): it declares what it holds (PATCH /v1/devices/self {holds}),
drains its inbox without becoming a Mac, is woken by a content-available push, answers in the owner's own turn on it
as a device call (item_body never offered to the model), and says "didn't answer" in fixed words.
Run: python3 scripts/test_iphone_holders.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-iphone-holders-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "REFERENCES_DB": "references.db", "ITEM_REFS_DB": "item_refs.db",
             "PROACTIVE_DB": "proactive.db", "AGENT_MEMORY_DIR": "memory", "KB_ACL_DB": "kb_acl.db",
             "HOUSEHOLD_PREFS": "household.json", "PUSH_DB": "push.db", "CONNECTORS_LEDGER": "ledger.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["ITEM_REFERENCES"] = "mail,scans"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
sys.path.insert(0, str(Path(__file__).parent))
import cross_device as xd  # noqa: E402
import household_identity as hh  # noqa: E402
import item_open as io_  # noqa: E402
import push  # noqa: E402
import references  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")


print("── an iPhone declares what it holds ──")
import agent_api_server as api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, platform="ios", person=None):
    body = {"person_id": person} if person else {"new_person": {"name": name, "role": role}}
    sp = c.post("/v1/pair/start", json=body, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": sp["code"], "device_name": f"{name}'s {platform}", "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, sp["person"]["id"], cl["device"]


PH, rob, rdev = pair("Robin", "adult")
MAC, _, mdev = pair("Robin", "adult", "mac", person=rob)
KIT, _, _ = pair("Kitchen", "household")
r = c.patch("/v1/devices/self", json={"holds": ["scan"]}, headers=PH)
check("her iPhone: holds scan", (r.status_code, r.json().get("holds")), (200, ["scan"]))
check("…stored on her device row", next(d for p in st.people() for d in p["devices"] if d["id"] == rdev["id"])["holds"], ["scan"])
check("a Mac can't declare it (it holds mail by presence)", c.patch("/v1/devices/self", json={"holds": ["scan"]},
      headers=MAC).status_code, 400)
check("a shared household device can't", c.patch("/v1/devices/self", json={"holds": ["scan"]}, headers=KIT).status_code, 400)
check("an unknown kind: 400", c.patch("/v1/devices/self", json={"holds": ["photo"]}, headers=PH).status_code, 400)

print("── it drains its inbox without becoming a Mac ──")
got = c.get("/v1/device/inbox?wait=0", headers=PH)
check("its own token drains it", (got.status_code, got.json().get("requests")), (200, []))
check("…and it isn't recorded as a Mac", [m for m in st.macs_for(rob) if m.get("device_id") == rdev["id"]], [])

print("── it's woken by a content-available push; a Mac by Wake-on-LAN ──")
woke = []
saved = (xd.PUSH_WAKE, xd.WAKER.wake, xd.PHONE_WAKE_WAIT_S, xd.WAKE_WAIT_S)
xd.PUSH_WAKE = lambda dev: woke.append(("push", dev["device_id"])) or []
xd.WAKER.wake = lambda m: woke.append(("wol", m.get("device_id"))) or []
xd.PHONE_WAKE_WAIT_S = xd.WAKE_WAIT_S = 0.05
try:
    for dev in ({"device_id": "ph-1", "device_key": "ph-1", "name": "iPhone", "platform": "ios"},
                {"device_id": "mac-1", "device_key": "mac-1", "name": "Mac", "platform": "mac"}):
        res = xd.route({"tool": "item_body", "args": {}, "risk": "read"}, person_id="p", mac=dev, turn_id="t",
                       origin={"device": "x", "platform": "ios"}, session="s", emit=lambda *a: None,
                       ask_confirm=lambda p: {"approved": False})
finally:
    xd.PUSH_WAKE, xd.WAKER.wake, xd.PHONE_WAKE_WAIT_S, xd.WAKE_WAIT_S = saved
check("an iPhone is pushed, a Mac gets Wake-on-LAN", woke, [("push", "ph-1"), ("wol", "mac-1")])
check("…nobody came: the request is withdrawn (no wake)", res["result"], xd.NO_WAKE)
queued = []
saved_enq = xd.CHANNEL.enqueue
xd.CHANNEL.enqueue = lambda key, item: queued.append(item["approved"])
saved_w = (xd.WAKER.wake, xd.WAKE_WAIT_S)
xd.WAKER.wake, xd.WAKE_WAIT_S = (lambda m: []), 0.01
try:
    for ap in (True, False):
        xd.route({"tool": "item_body", "args": {}}, person_id="p", mac={"device_key": "k", "name": "Mac"}, turn_id="t",
                 origin={}, session="s", emit=lambda *a: None, ask_confirm=lambda p: {"approved": False}, approved=ap)
finally:
    xd.CHANNEL.enqueue = saved_enq
    xd.WAKER.wake, xd.WAKE_WAIT_S = saved_w
check("route(approved=True) queues the item already approved; the default doesn't", queued, [True, False])
push.register("ph-1", "e" * 64, "development", "com.zachshallbetter.jellybean.ios")
sent = []
check("the push: nothing but inbox, content-available, that device only",
      (push.nudge_inbox("ph-1", send=lambda t, b: sent.append((t["device_id"], b)) or (200, "")), sent),
      (["ph-1: 200"], [("ph-1", {"aps": {"content-available": 1}, "inbox": 1})]))
check("no token: nothing sent", push.nudge_inbox("ph-none", send=lambda t, b: (200, "")), [])

print("── holders: her Macs and the iPhones that declared what they hold ──")
import agent_runtime_lib as lib  # noqa: E402
hs = lib._item_holders(rob)
check("her iPhone with holds is a holder", [(h["device_id"], h["platform"], h["holds"]) for h in hs if h["platform"] == "ios"],
      [(rdev["id"], "ios", ["scan"])])
c.patch("/v1/devices/self", json={"holds": []}, headers=PH)
check("…not once it clears holds", [h for h in lib._item_holders(rob) if h["platform"] == "ios"], [])
c.patch("/v1/devices/self", json={"holds": ["scan"]}, headers=PH)

print("── item_body: never offered to the model, kept in the phone's turn tools ──")
tools = lib.device_tools_for_client("iphone", ["item_body", "list_calendar_events"])
check("the phone's turn keeps item_body", "item_body" in tools, True)
lib._TURN_IO.client, lib._TURN_IO.device_tools, lib._TURN_IO.role = "iphone", tools, "adult"
lib._TURN_IO.device = lambda req: {"result": "x"}
check("…but the model is never offered it", "item_body" in lib._turn_device_tools(), False)
check("…and it isn't one of the phone's model tools", "item_body" in lib.PHONE_DEVICE_TOOLS, False)

print("── opening a scan the iPhone holds ──")
scan = references.put(rob, "adult", rdev["id"], {"kind": "scan", "native_id": "lease.pdf", "title": "Lease renewal",
                                                 "content_hash": "sha256:" + "a" * 64})
PEOPLE = {rob: {"name": "Robin", "role": "adult"}}
calls = []


def route(req, owner, mac, approve):
    calls.append((mac["device_id"], req["args"]["kind"], approve is not None))
    return {"result": json.dumps({"found": True, "title": "Lease renewal", "text": "Renewal at $1,950 from Nov 1.",
                                  "text_hash": "sha256:" + "a" * 64})}


def run(r=route, holders=None, asker=rob, role="adult", what="lease renewal"):
    return io_.open_item({"what": what}, asker=asker, role=role, asker_name="Robin", people=PEOPLE,
                         holders=holders or lib._item_holders, route=r, derived=lambda ref: [],
                         wrap=lambda t, s, f: f"⟦{s}⟧{t}", add_card=lambda *a, **k: None, consent_timeout=0.1)


out = run()
check("routed to her iPhone, kind scan, as the owner", (calls[-1], "⟦scan⟧Renewal at $1,950" in out), ((rdev["id"], "scan", False), True))
quiet = lambda req, o, m, ap: {"result": xd.NO_WAKE, "completed": False}  # noqa: E731
check("her iPhone didn't answer: fixed words", "on your Robin's ios, which didn't answer" in run(r=quiet), True)
check("an iPhone that doesn't hold scans: needs an update",
      "needs an update" in run(holders=lambda o: [{"device_id": rdev["id"], "platform": "ios", "name": "iPhone", "holds": ["mail"]}]),
      True)
check("a scan on a Mac: Macs don't hold scans", io_.MAC_NO_SCANS in run(holders=lambda o: [{"device_id": rdev["id"],
      "platform": "mac", "name": "Mac", "device_tools": ["item_body"]}]), True)

print("── the server: her own turn on that iPhone is a device call; anything else goes by the inbox ──")
ident = hh.Identity({"id": rob, "name": "Robin", "role": "adult"}, rdev, "adult", "device")
api._turns["t-own"] = {"source": "user"}
hooked, routed = [], []
saved_route = xd.route
xd.route = lambda req, **kw: routed.append(kw["mac"]["device_id"]) or {"result": xd.NO_WAKE}
lib._TURN_IO.device = lambda req: hooked.append(req["tool"]) or {"result": "{\"found\": true}"}
try:
    holder = {"device_id": rdev["id"], "device_key": rdev["id"], "name": "iPhone", "platform": "ios", "holds": ["scan"]}
    api._item_route("t-own", ident, {"tool": "item_body", "args": {}}, rob, holder, None)
    check("the owner on the holding iPhone: a device call in this turn, no inbox", (hooked, routed), (["item_body"], []))
    api._item_route("t-own", ident, {"tool": "item_body", "args": {}}, rob, {**holder, "device_id": "other-phone"}, None)
    check("another device holds it: the inbox (and a push)", routed, ["other-phone"])
    order = []
    xd.route = lambda req, **kw: order.append(("queued", kw.get("approved"))) or {"result": xd.NO_WAKE}
    api._item_route("t-own", ident, {"tool": "item_body", "args": {}}, rob, holder,
                    lambda: order.append(("asked owner", None)) or True)
    check("someone else's read: the owner is asked first, then it's queued approved (never in-turn)",
          order, [("asked owner", None), ("queued", True)])
    order.clear()
    r = api._item_route("t-own", ident, {"tool": "item_body", "args": {}}, rob, holder,
                        lambda: order.append(("asked owner", None)) or False)
    check("the owner says no: nothing is queued at all", (order, r["result"]), ([("asked owner", None)], xd.DECLINED))
    order.clear()
    api._item_route("t-own", ident, {"tool": "item_body", "args": {}}, rob, {**holder, "device_id": "other-phone"}, None)
    check("the owner's own read from another device: queued unapproved (for_owner, no prompt)", order, [("queued", False)])
finally:
    xd.route = saved_route
    api._turns.pop("t-own", None)
    lib._TURN_IO.device = lib._TURN_IO.device_tools = lib._TURN_IO.client = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
