#!/usr/bin/env python3
# gate: hermetic
"""#418, the pairing-day rehearsal: before another adult's devices pair, run their first synced items through the
real path, connector ingest (owned by them) → mail_bills' record → the world model's own routing
(world.node_owner_audience, the rule sync_all uses) → bubbles, and check who can see what. Their calendar series and
their bill land in their own bubble, private: they see them; the admin, a kid and a shared screen don't. Their mail's
KB copy has its sharing row (theirs, private). Fictional household (Robin an adult, Ava a kid, a living-room TV);
temporary stores; the runtime and the model are fakes. Run: python3 scripts/test_pairing_rehearsal.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-pairing-"))
for k, v in {"WORLD_DIR": "world", "HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json",
             "KB_ACL_DB": "kb_acl.db", "CONNECTORS_LEDGER": "ledger.db", "FENCES_DB": "fences.db",
             "MAIL_BILLS_DB": "mail_bills.db", "PROACTIVE_DB": "proactive.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["AGENT_TZ"] = "America/Los_Angeles"
for k in ("MAIL_TRIAGE",):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import connectors_runner as cx  # noqa: E402
import household_identity as hh  # noqa: E402
import kb_privacy as P  # noqa: E402
import local_time  # noqa: E402
import mail_bills as mb  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


st = hh.HouseholdStore(_TMP / "people.db")
hh.reset_store(st)
robin = st.add_person("Robin Hale", "adult")["id"]
ava = st.add_person("Ava Hale", "kid")["id"]
tv = st.add_person("Living room", "household")["id"]
ADMIN = hh.ADMIN_PERSON_ID
VIEWERS = {"Robin (owner)": (robin, "adult"), "the admin": (ADMIN, "admin"), "Ava (kid)": (ava, "kid"),
           "the TV": (tv, "household")}


class Runtime:
    def __init__(self):
        self.kb, self.nodes = {}, {}

    def __call__(self, service, path, method, payload):
        if service == "retrieval" and method == "POST":
            self.kb[payload["source_id"]] = payload
        elif service == "record" and path == "/v1/nodes" and method == "POST":
            self.nodes[payload["node_id"]] = payload
        elif service == "record" and method == "PATCH":
            self.nodes[path.rsplit("/", 1)[-1]]["data"] = payload["data"]
        return {}


rt = Runtime()
ledger = _TMP / "ledger.db"

print("── Robin's calendar: a weekly class, synced from her own device ──")
occ = [{"source_id": f"POT-{d}", "observed_at": f"2026-09-{d}T19:00:00-07:00", "kind": "appointment",
        "title": "Pottery class", "text": "Pottery class", "account": "iCloud",
        "fields": {"start": f"2026-09-{d}T19:00:00-07:00", "end": f"2026-09-{d}T21:00:00-07:00", "calendar": "Robin"}}
       for d in ("10", "17", "24")]
out = cx.ingest("calendar", occ, runtime=rt, ledger=ledger, owner=robin)
check("three occurrences written", out["written"], 3)
cal_nodes = [n for n in rt.nodes.values() if n.get("node_type") == "appointment"]
check("each record names Robin as its owner", sorted({(n["data"]["provenance"].get("owner")) for n in cal_nodes}), [robin])
check("…and reads as hers, private (the world's own rule)", sorted({W.node_owner_audience(n) for n in cal_nodes}),
      [(robin, "private")])
cal_obs = W.calendar_observations(cal_nodes, W.node_owner_audience, local_time.zone())
check("the series is observed", len(cal_obs) > 0, True)
check("…routed to Robin's own bubble, private", sorted({(o.bubble, o.owner, o.audience) for o in cal_obs}),
      [(W.bubble_name(robin), robin, "private")])

print("── Robin's mail: a bill, kept in the KB as hers, filed as her record ──")
mail = {"source_id": "MSG-77", "observed_at": "2026-10-01T08:00:00-07:00", "kind": "mail",
        "title": "Your Cedar Water bill is ready", "text": "Amount due $48.20 by Oct 20. Cedar Water.", "account": "iCloud",
        "fields": {"from": "billing@cedar.example"}}
cx.ingest("mail", [mail], runtime=rt, ledger=ledger, owner=robin)
mid = next(i for i in rt.kb if i.startswith("cx-mail-"))
check("the mail's KB copy is Robin's, private", (rt.kb[mid]["metadata"].get("owner"), rt.kb[mid]["metadata"].get("audience")),
      (robin, "private"))
check("…and its sharing row says so (#1273)", P.acl_rows([mid]).get(mid), (robin, "private"))
posted = []


def request(svc, path, method="GET", body=None):
    if method == "GET":
        return {"nodes": []}
    posted.append(body)
    return {}


llm = lambda prompt: json.dumps(dict(type="bill", vendor="Cedar Water", amount=48.20, due_date="2026-10-20",  # noqa: E731
                                     payment_status="due"))
src = [{"source_id": mid, "source_type": "connector:mail", "title": mail["title"], "updated_at": "1",
        "metadata": rt.kb[mid]["metadata"]}]
mb.run(True, src, {mid: mail["text"]}.get, llm, request, sqlite3.connect(":memory:"))
bill = next((b for b in posted if (b.get("data") or {}).get("vendor") == "Cedar Water"), None)
check("the bill record is filed", bill is not None, True)
check("…private to Robin, provenance back to her mail",
      (bill["data"]["provenance"].get("owner"), bill["data"]["provenance"].get("audience"),
       bill["data"]["provenance"].get("item_id")) if bill else None, (robin, "private", mid))
bill_obs = W.bill_observations([bill], W.node_owner_audience) if bill else []
check("…observed into Robin's own bubble", sorted({(o.bubble, o.owner, o.audience) for o in bill_obs}),
      [(W.bubble_name(robin), robin, "private")])

print("── a contact Robin saves in chat ──")
import agent_runtime_lib as lib  # noqa: E402
saved = []


def record_rt(svc, path, method="POST", payload=None, timeout=30):
    if method in ("POST", "PATCH") and svc == "record":
        saved.append(payload)
        return {}
    return {"nodes": [], "results": []}


_real_rt = lib._runtime_request
lib._runtime_request = record_rt
lib._TURN_IO.person, lib._TURN_IO.role, lib._TURN_IO.source = {"id": robin, "name": "Robin Hale"}, "adult", "user"
try:
    lib.execute_tool("save_contact", {"name": "Lin Dental", "phone": "(555) 0142", "relation": "dentist"})
finally:
    lib._runtime_request = _real_rt
    lib._TURN_IO.person = lib._TURN_IO.role = None
contact = next((c for c in saved if (c.get("data") or {}).get("name") == "Lin Dental"), None)
check("the contact record is Robin's, private", (contact["data"]["provenance"].get("owner"),
                                                 contact["data"]["provenance"].get("audience")) if contact else None,
      (robin, "private"))
con_obs = W.contact_observations([contact], W.node_owner_audience) if contact else []
check("…observed into Robin's own bubble", sorted({(o.bubble, o.owner, o.audience) for o in con_obs}),
      [(W.bubble_name(robin), robin, "private")])

print("── who sees what: only Robin ──")
w = W.Bubbles(_TMP / "world")
w.sync("calendar", cal_obs)
w.sync("bills", bill_obs)
w.sync("contacts", con_obs)
for thing in ("Pottery class", "Cedar Water", "Lin Dental"):
    seen = {who: bool(w.find(thing, pid, role)) for who, (pid, role) in VIEWERS.items()}
    check(f"{thing}: Robin sees it; the admin, a kid and the TV don't", seen,
          {"Robin (owner)": True, "the admin": False, "Ava (kid)": False, "the TV": False})
check("no bubble was made for anyone but Robin (nothing landed in the admin's)",
      sorted(b for b in w.bubbles() if b != "household"), [W.bubble_name(robin)])

print("── the legacy rule stays as it was ──")
check("a node with no provenance at all is the admin's, private (the pre-provenance default)",
      W.node_owner_audience({"node_id": "x", "data": {}}), ("zach", "private"))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
