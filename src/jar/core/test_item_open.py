#!/usr/bin/env python3
# gate: hermetic
"""Opening a referenced mail or scan (titan-node#446 step 3): off by default; the owner reads through their own
device; anyone else needs the owner's yes (a card; Always remembers); asleep, gone, changed and an old app
say so in fixed words; the owner lists and revokes Always grants; the text is wrapped as untrusted, redacted and
capped, and kept nowhere.
Run: python3 scripts/test_item_open.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-itemopen-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "REFERENCES_DB": "references.db", "ITEM_REFS_DB": "item_refs.db",
             "PROACTIVE_DB": "proactive.db", "AGENT_MEMORY_DIR": "memory", "KB_ACL_DB": "kb_acl.db",
             "HOUSEHOLD_PREFS": "household.json", "CONNECTORS_LEDGER": "ledger.db", "PUSH_DB": "push.db",
             "MAIL_BILLS_DB": "mail_bills.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("ITEM_REFERENCES", None)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
sys.path.insert(0, str(Path(__file__).parent))
import cross_device as xd  # noqa: E402
import item_open as io_  # noqa: E402
import references  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")


H = "sha256:" + "a" * 64
own = references.put("p-alex", "admin", "dev-mac", {"kind": "mail", "native_id": "m1", "title": "Plumber visit",
                                                    "content_hash": H})
shr = references.put("p-alex", "admin", "dev-mac", {"kind": "mail", "native_id": "m2", "title": "Water bill",
                                                    "content_hash": H, "audience": ["p-robin"]})
references.put("p-alex", "admin", "dev-mac", {"kind": "mail", "native_id": "m3", "title": "Water bill reminder",
                                              "content_hash": H})
PEOPLE = {"p-alex": {"name": "Alex Hale", "role": "admin"}, "p-robin": {"name": "Robin", "role": "adult"},
          "p-ava": {"name": "Ava", "role": "kid"}}
MAC = {"device_id": "dev-mac", "name": "MacBook", "device_tools": ["item_body", "find_files"]}
calls: list[dict] = []
cards: list[dict] = []
reply = {"found": True, "text": "Tuesday 9 AM. Card 4111 1111 1111 1111. " + "x" * 9000, "text_hash": H}


def route(req, owner, mac, approve):
    calls.append({"req": req, "owner": owner, "approve": approve is not None})
    if approve is not None and not approve():
        return {"result": xd.DECLINED, "completed": False}
    return {"result": json.dumps(reply)}


def answering(choice):
    def add_card(title, detail="", **kw):
        cards.append({"title": title, "detail": detail, **kw})
        io_.answer(f"{choice}:{kw['key'].split('consent-', 1)[1]}", "p-alex")
    return add_card


def run(asker="p-alex", role="admin", what="plumber visit", r=route, card=None, holders=None, **kw):
    return io_.open_item({"what": what, **kw}, asker=asker, role=role, asker_name=PEOPLE.get(asker, {}).get("name", "x").split()[0],
                         people=PEOPLE, holders=holders or (lambda o: [MAC] if o == "p-alex" else []), route=r,
                         derived=lambda ref: ["received 2026-10-01"], wrap=lambda t, s, f: f"⟦{s}|{f}⟧{t}⟦/⟧",
                         add_card=card or answering("decline"), consent_timeout=1)


print("── off by default: the tool isn't offered ──")
import agent_runtime_lib as lib  # noqa: E402
lib._TURN_IO.role, lib._TURN_IO.person, lib._TURN_IO.source = "adult", {"id": "p-robin", "name": "Robin"}, "user"
check("ITEM_REFERENCES off: hidden, for every role", lib._role_allows("open_item"), False)
os.environ["ITEM_REFERENCES"] = "mail"
check("on: an adult may use it", lib._role_allows("open_item"), True)
lib._TURN_IO.role = "household"
check("…a shared household device may not", lib._role_allows("open_item"), False)
lib._TURN_IO.role = "kid"
check("…a kid may (their own, or shared with them)", lib._role_allows("open_item"), True)

print("── the owner reads their own ──")
out = run()
check("asks the holding Mac, as the owner (their own confirmation, no consent card)",
      (calls[-1]["owner"], calls[-1]["approve"], calls[-1]["req"]["tool"], calls[-1]["req"]["args"]["for_owner"], cards), ("p-alex", False, "item_body", True, []))
a = calls[-1]["req"]["args"]
check("the request carries the contract's fields", sorted(a), ["asker", "content_hash", "for_owner", "kind", "max_chars",
                                                               "native_id", "purpose", "ref_id", "text_hash"])
check("the text: wrapped as untrusted, redacted, capped at 8000",
      ("⟦mail|None⟧" in out, "4111 1111 1111 1111" in out, len(out) < 8400), (True, False, True))
check("…and it says titan keeps no copy", "titan keeps no copy" in out, True)
check("quote is passed as the purpose", (run(purpose="quote"), calls[-1]["req"]["args"]["purpose"])[1], "quote")

print("── someone else reads a shared one: the owner's yes ──")
cards.clear()
out = run("p-robin", "adult", "water bill", card=answering("decline"))
check("the owner gets a card (only them, pushed), and No means no text",
      (len(cards), cards[0]["to"], cards[0]["urgency"], cards[0]["source"], "Tuesday" in out, "haven't said yes" in out),
      (1, ["p-alex"], 2, "item-consent", False, True))
check("…the card's words", (cards[0]["title"], [b["label"] for b in cards[0]["actions"]]),
      ("Robin asked titan about your “Water bill” email", ["Allow once", "Always for Robin", "No"]))
check("…iOS's shape in its data, buttons allow_once / always / decline",
      (cards[0]["data"], [b["id"] for b in cards[0]["actions"]]),
      ({"kind": "item_share", "asker_first_name": "Robin", "title": "Water bill", "item_kind": "mail",
        "actions": ["allow_once", "always", "decline"]}, ["allow_once", "always", "decline"]))
out = run("p-robin", "adult", "water bill", card=answering("allow_once"))
check("Allow once: the text, marked as Alex's", ("Tuesday" in out, "⟦mail|Alex⟧" in out, "Alex's MacBook" in out), (True, True, True))
cards.clear()
run("p-robin", "adult", "water bill", card=answering("always"))
run("p-robin", "adult", "water bill", card=answering("decline"))
check("Always for Robin: no card the next time", (len(cards), io_.granted("p-alex", "p-robin", "mail")), (1, True))
check("the owner can revoke it", (io_.revoke("p-alex", "p-robin"), io_.granted("p-alex", "p-robin", "mail")), (1, False))
check("an item not shared with them doesn't exist for them (no device asked)",
      (run("p-robin", "adult", "plumber visit"), len([c for c in calls if c["req"]["args"]["ref_id"] == own["id"]])),
      (io_.NOT_FOUND, 2))
check("no answer in time is no", "haven't said yes" in run("p-robin", "adult", "water bill", card=lambda *a, **k: None), True)
check("a shared household device never", run("p-kitchen", "household", "water bill"), io_.HOUSEHOLD)
# #1055: a kid, for an item that isn't shared with them (the private plumber mail; the adults' water bill)
n_calls = len(calls)
check("a kid: neither Alex's private mail nor the one he shared with Robin exists for them (no device asked)",
      (run("p-ava", "kid", "plumber visit"), run("p-ava", "kid", "water bill"), len(calls) - n_calls),
      (io_.NOT_FOUND, io_.NOT_FOUND, 0))

print("── the consent card's buttons ──")
check("a stale or unknown answer is refused", (io_.answer("allow_once:0123456789abcdef", "p-alex")[0], io_.answer("bogus", None)[0]),
      (False, False))
import threading  # noqa: E402
c = io_._Consent(); c.owner = "p-alex"; io_._PENDING["fedcba9876543210"] = c
check("only the owner may answer", io_.answer("allow_once:fedcba9876543210", "p-robin")[0], False)
check("…the owner can", io_.answer("allow_once:fedcba9876543210", "p-alex"), (True, "Shared this once."))
io_._PENDING.clear()
import proactive  # noqa: E402
c = io_._Consent(); c.owner = "p-alex"; io_._PENDING["0011223344556677"] = c
made = []
io_.consent_card("p-alex", "Robin", "mail", "Water bill", "0011223344556677",
                 lambda *a, **k: made.append(proactive.add_card(*a, **k)))
card = made[0]
check("the served card: action ids allow_once / always / decline, expires with the request, iOS's data",
      ([b["id"] for b in card["actions"]], card["expires_at"] is not None and card["expires_at"] - card["created_at"] <= 111,
       card["data"]["kind"], card["urgency"]), (["allow_once", "always", "decline"], True, "item_share", 2))
res = proactive.card_action(card["id"], "allow_once", person="p-alex", role="admin")
check("a tap on the card answers the waiting read (decided in code)", (res["ok"], c.answer), (True, "allow_once"))
io_._PENDING.clear()

other = proactive.add_card("A plain card", "x", actions=[{"label": "OK", "kind": "dismiss"}, {"id": "Bad Id!", "label": "B",
                                                                                            "kind": "dismiss"}])
check("other cards keep a0, a1… (and an id that isn't a plain word is replaced)", [b["id"] for b in other["actions"]],
      ["a0", "a1"])

print("── asleep, gone, changed, old app, no holder, unclear ──")
asleep = lambda req, o, m, ap: {"result": xd.NO_WAKE, "completed": False}  # noqa: E731
out = run(r=asleep)
check("the owner's Mac asleep: what's known, and fixed words; no stale copy",
      ("received 2026-10-01" in out, "on your MacBook, which is asleep or away" in out), (True, True))
check("someone else's: it's on Alex's Mac", "It's on Alex's MacBook, which isn't reachable right now." in
      run("p-robin", "adult", "water bill", r=asleep), True)
gone = lambda req, o, m, ap: {"result": json.dumps({"found": False})}  # noqa: E731
check("gone from the device: said, and the reference marked", ("no longer on your MacBook" in run(r=gone),
      "gone" in next(r for r in references.visible("p-alex", "admin") if r["id"] == own["id"])["dates"]), (True, True))
reply = {**reply, "text_hash": "sha256:" + "f" * 64}
out = run(what="water bill reminder")
check("changed since sync: said, and the hash updated", ("changed on your MacBook" in out,
      next(r for r in references.visible("p-alex", "admin") if r["native_id"] == "m3")["content_hash"]),
      (True, "sha256:" + "f" * 64))
n = len(calls)
old = {**MAC, "device_tools": ["find_files"]}
check("an older Jellybean: needs an update, nothing asked", ("needs an update" in run(holders=lambda o: [old]), len(calls)), (True, n))
check("no Mac on record for it: titan can't reach it", "titan can't reach" in run(holders=lambda o: []), True)
check("unclear: asks which", run(what="water").startswith("Several match"), True)

print("── the server: the Always grants for Settings › Sharing, and an eval's Mac ──")
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role):
    sp = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": sp["code"], "device_name": f"{name}'s iPhone", "platform": "ios"}).json()
    return {"X-Agent-Token": cl["token"]}, sp["person"]["id"], cl["device"]


ROB, rob, rdev = pair("Robin", "adult")
JOR, jor, _ = pair("Jordan", "adult")
KIT, _, _ = pair("Kitchen", "household")
io_.grant(rob, jor, "mail")
io_.grant(jor, rob, "scan")
g = c.get("/v1/sharing/grants", headers=ROB).json()["grants"]
check("GET: only the caller's own grants, by the asker's first name", [(x["asker_first_name"], x["kind"]) for x in g],
      [("Jordan", "mail")])
check("…ids are opaque", g[0]["id"].startswith("gr-") and len(g[0]["id"]) == 19, True)
check("another person can't revoke it (404)", c.delete(f"/v1/sharing/grants/{g[0]['id']}", headers=JOR).status_code, 404)
check("the owner can (204), and it's gone", (c.delete(f"/v1/sharing/grants/{g[0]['id']}", headers=ROB).status_code,
      io_.granted(rob, jor, "mail")), (204, False))
check("a shared household device: refused by the route table", c.get("/v1/sharing/grants", headers=KIT).status_code, 403)
pi = hh.Identity({"id": rob, "name": "Robin", "role": "adult"}, rdev, "adult", "device")
api._turns["t-eval"] = {"source": "eval", "eval_mac": "never", "eval_item": None}
check("an eval turn's Mac that never answers: asleep (no real device)",
      api._item_route("t-eval", pi, {"args": {"content_hash": H}}, "p-alex", MAC, None)["result"], xd.NO_WAKE)
api._turns["t-eval"] = {"source": "eval", "eval_mac": "live", "eval_item": {"text": "Fixture mail", "owner_answer": "once"}}
r = api._item_route("t-eval", pi, {"args": {"content_hash": H}}, "p-alex", MAC, lambda: True)
check("…a live one: the fixture item", json.loads(r["result"])["text"], "Fixture mail")
api._turns.pop("t-eval", None)

print("── a turn that opened an item is sensitive: no facts from it, nothing to the KB or memory ──")
check("open_item is a sensitive tool (like health_summary)", "open_item" in lib.SENSITIVE_TOOLS, True)
lib._TURN_INFO.info = {}
lib._TURN_IO.role, lib._TURN_IO.person, lib._TURN_IO.source = "admin", {"id": "p-alex", "name": "Alex"}, "user"
lib._TURN_IO.item_route = None
saved_open = io_.open_item
io_.open_item = lambda args, **kw: "stub"
try:
    lib.execute_tool("open_item", {"what": "plumber visit"})
finally:
    io_.open_item = saved_open
names = lib._TURN_INFO.info.get("tool_names") or []
check("…and the turn records it, so the server's rule (tool_names ∩ SENSITIVE_TOOLS) marks the turn",
      ("open_item" in names, bool(set(names) & lib.SENSITIVE_TOOLS)), (True, True))
lib._TURN_INFO.info = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
