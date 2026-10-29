#!/usr/bin/env python3
# gate: hermetic
"""An account's page (#951 step 5, account@1): GET /v1/surfaces/account and the word routes (say, share, take back)
through the real routes over a fictional house, with golden vectors per viewer (scripts/evalsets/surfaces/
account.robin.json, account.alex.json; `--write` rewrites them). The page is the viewer's own settlement: a word
stays its speaker's until they share it (decision (b)), a text read on a device is its owner's alone, and a kid or a
shared screen gets no account page; an account the viewer can't see is the same 404 as a missing one.
Run: python3 scripts/test_surfaces_account.py [--write]"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

T0 = 1791565200.0   # 2026-10-09T15:00:00Z
_TMP = Path(tempfile.mkdtemp(prefix="titan-account-surface-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "HOUSEHOLD_PREFS": "household.json",
             "AGENT_TURN_LOG": "turns.jsonl", "MAIL_BILLS_DB": "bills.db", "LINKS_DB": "links.db",
             "LINK_SALT_FILE": "salt", "FENCES_DB": "fences.db", "KB_ACL_DB": "kb_acl.db", "SITTER_DB": "sitter.db",
             "PUSH_DB": "push.db", "PROACTIVE_DB": "proactive.db", "SURFACES_DB": "surfaces.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily",
             "TODOS_DB": "todos.db", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db", "LOCK_UP_DB": "lock_up.db",
             "TV_UNLOCK_DB": "tv_unlock.db", "ROUTINES_DB": "routines.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
VECTORS = HERE / "evalsets" / "surfaces"
WRITE = "--write" in sys.argv
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import obligation_signals as SG  # noqa: E402
import secrets as _secrets  # noqa: E402
from types import SimpleNamespace as _NS  # noqa: E402
from jsonschema import Draft7Validator  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


_pseq = iter(range(1, 1000))
hh.secrets = _NS(**{n: getattr(_secrets, n) for n in dir(_secrets) if not n.startswith("_")})
hh.secrets.token_hex = lambda n=None: f"{next(_pseq):08x}" if n == 4 else _secrets.token_hex(n)
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)


def pair(name, role, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers={"X-Agent-Token": MASTER}).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": f"{name}'s phone", "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ALEX, alex = pair("Alex Quarry", "adult")
ROBIN, robin = pair("Robin Quarry", "adult")
AVA, ava = pair("Ava Quarry", "kid")
SCREEN, _ = pair("Living room", "household", "tvos")
H = {"alex": ALEX, "robin": ROBIN, "ava": AVA, "screen": SCREEN}


def bill(nid, vendor, amount, due, mailed, owner, audience, status="due", doc="bill", paid=None):
    d = {"doc_type": doc, "vendor": vendor, "amount": amount, "due_date": due, "mail_date": mailed,
         "payment_status": status, "provenance": {"source": "mail", "owner": owner, "audience": audience}}
    if paid:
        d["paid_date"] = paid
    return {"node_id": nid, "node_type": doc, "data": d}


RECORDS = [bill("b-water-sep", "Hale Water", 46.10, "2026-09-20", "2026-08-31", alex, "household"),
           bill("r-water-sep", "Hale Water", 46.10, None, "2026-09-18", alex, "household", doc="receipt", paid="2026-09-18"),
           bill("b-water-oct", "Hale Water", 48.20, "2026-10-20", "2026-09-30", alex, "household"),
           bill("b-power", "Hale Power", 90.00, "2026-10-12", "2026-10-01", alex, "adults", "failed"),
           bill("b-gym", "Ridge Gym", 30.00, "2026-10-15", "2026-10-03", alex, "private")]
SEES = {alex: {"b-water-sep", "r-water-sep", "b-water-oct", "b-power", "b-gym"},
        robin: {"b-water-sep", "r-water-sep", "b-water-oct", "b-power"}}
lib.record_nodes = lambda t, *a, **k: ([r for r in RECORDS if r["node_type"] == t], True)
lib._node_visible = lambda n, pid, role: n["node_id"] in SEES.get(pid, set())
lib._local_now = lambda: datetime(2026, 10, 9, 8, 0)
_real_ot = lib._obligation_threads
lib._obligation_threads = lambda recs, today, pid, role: _real_ot(recs, date(2026, 10, 9), pid, role)
WATER, POWER, GYM = SG.ref("hale water"), SG.ref("hale power"), SG.ref("ridge gym")
schema = Draft7Validator(json.loads((HERE.parent / "docs" / "surfaces.schema.json").read_text()))


def page(who, ref):
    time.time, real = (lambda: T0), time.time
    try:
        r = c.get(f"/v1/surfaces/account?ref={ref}", headers=H[who])
    finally:
        time.time = real
    return r.status_code, r.json()


def norm(v):
    s = json.dumps(v, sort_keys=True)
    for raw, name in ((alex, "p-alex"), (robin, "p-robin"), (ava, "p-ava")):
        s = s.replace(raw, name)
    return json.loads(s)


def vector(name, got):
    p = VECTORS / f"{name}.json"
    if WRITE:
        p.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    check(f"golden vector {name}", got, json.loads(p.read_text()) if p.exists() else None)


def ids(s, gid):
    return [m["subject"]["id"] for g in s["groups"] if g["id"] == gid for m in g["members"]]


print("── who gets a page ──")
check("a kid: 403 (no bills)", page("ava", WATER)[0], 403)
check("a shared screen: 403", page("screen", WATER)[0], 403)
check("another adult's private account is the same 404 as a missing one",
      (page("robin", GYM), page("robin", "acct-0000000000000000")),
      ((404, {"error": "no such account"}), (404, {"error": "no such account"})))

print("── Robin says the water's paid, from the page ──")
r = c.post(f"/v1/obligations/{WATER}/say", json={"kind": "paid"}, headers=ROBIN)
check("say: recorded; a household bill, so Share is offered once", (r.status_code, r.json()), (200, {"kind": "paid", "share_ask": True}))
check("a kid can't say", c.post(f"/v1/obligations/{WATER}/say", json={"kind": "paid"}, headers=AVA).status_code, 403)
check("a bad kind is a 400", c.post(f"/v1/obligations/{WATER}/say", json={"kind": "forgiven"}, headers=ROBIN).status_code, 400)
check("handled_by needs a household member", c.post(f"/v1/obligations/{WATER}/say", json={"kind": "handled_by",
      "handled_by": "the landlord"}, headers=ROBIN).status_code, 400)
st, rp = page("robin", WATER)
check("Robin's page: 200, valid jb-1", (st, sorted(e.message[:80] for e in schema.iter_errors(rp))[:1]), (200, []))
check("…now: resolved by Robin's own word", next(m["slots"]["status"] for g in rp["groups"] if g["id"] == "g-now"
                                                 for m in g["members"]), {"text": "paid", "tone": "positive"})
check("…the cycles, newest first: October's evidence, then September's history",
      [(g["id"], g["role"]) for g in rp["groups"]],
      [("g-now", "primary"), ("g-cycle-2026-09-30", "evidence"), ("g-cycle-2026-08-31", "history")])
pieces = [(m["subject"]["kind"], m["basis"][0]["kind"], m["piece"]["kind"], m["piece"]["relation"])
          for g in rp["groups"] if g["id"] == "g-cycle-2026-09-30" for m in g["members"]]
check("…October: the bill (observed, context), Robin's word (declared, supports)", pieces,
      [("bill", "observed", "due", "context"), ("fact", "declared", "said_paid", "supports")])
check("…September: billed, then paid by a receipt (observed, supports)",
      [(m["piece"]["kind"], m["piece"]["relation"]) for g in rp["groups"] if g["id"] == "g-cycle-2026-08-31"
       for m in g["members"]], [("due", "context"), ("paid", "supports")])
check("…operations: say paid / disputed / not mine, and Share (a household bill, Robin's word still private)",
      sorted(o["id"].rsplit("-acct", 1)[0] for o in rp["operations"]),
      ["op-say-disputed", "op-say-not_mine", "op-say-paid", "op-share"])
st, ap = page("alex", WATER)
check("Alex (another adult) before Robin shares: still due, no word of Robin's", (
      next(m["slots"]["status"]["text"] for g in ap["groups"] if g["id"] == "g-now" for m in g["members"]),
      [m["piece"]["kind"] for g in ap["groups"] for m in g["members"] if "piece" in m and m["piece"]["kind"].startswith("said")]),
      ("due", []))

print("── Robin shares ──")
check("share: shared", c.post(f"/v1/obligations/{WATER}/share", json={"yes": True}, headers=ROBIN).json(), {"result": "shared"})
st, ap = page("alex", WATER)
check("Alex now sees it resolved, by Robin's word (declared, 'Robin said')", (
      next(m["slots"]["status"]["text"] for g in ap["groups"] if g["id"] == "g-now" for m in g["members"]),
      [m["slots"]["title"] for g in ap["groups"] for m in g["members"] if m.get("piece", {}).get("kind") == "said_paid"]),
      ("paid", ["Robin said it's done Oct 9"]))
check("…and Alex has no Share or take-back of Robin's word (only their own word's operations)",
      sorted(o["id"].rsplit("-acct", 1)[0] for o in ap["operations"]), ["op-say-disputed", "op-say-not_mine", "op-say-paid"])
st, rp = page("robin", WATER)
check("Robin's page now offers take-back instead of Share", sorted(o["id"].rsplit("-acct", 1)[0] for o in rp["operations"]),
      ["op-say-disputed", "op-say-not_mine", "op-say-paid", "op-take-back"])

print("── a text on Robin's own device: the power payment failed ──")
SG.put(robin, "adult", {"kind": "obligation_signal", "signal": "payment_failed", "account_ref": POWER, "amount": 90.0,
                        "on": "2026-10-08", "confidence": 0.9, "sig_id": "a1a1a1a1a1a1a1a1"}, __import__("hooks").connect(),
       lambda: {POWER: "hale power"}, date(2026, 10, 9))
st, pp = page("robin", POWER)
check("Robin: the text is a derived piece (message), on the attention group's cycle", (
      [g["role"] for g in pp["groups"] if g["id"] == "g-now"],
      [(m["subject"]["kind"], m["basis"][0]["kind"], m["piece"].get("via")) for g in pp["groups"] for m in g["members"]
       if m.get("piece", {}).get("via")]), (["attention"], [("message", "derived", "text")]))
st, pa = page("alex", POWER)
check("Alex never sees Robin's text", [m for g in pa["groups"] for m in g["members"] if m.get("piece", {}).get("via")], [])
check("no Share or take-back while Robin has no word of their own on it (a text is never shared)",
      [o["id"] for o in pp["operations"] if o["verb"] in ("share", "undo")], [])

print("── golden vectors ──")
vector("account.robin", norm(page("robin", WATER)[1]))
vector("account.alex", norm(page("alex", WATER)[1]))
vector("account.robin.power", norm(page("robin", POWER)[1]))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
