#!/usr/bin/env python3
# gate: hermetic
"""#951 step 4, the titan half: a text's payment evidence from the owner's own device. The golden vector
(scripts/evalsets/obligation_signals/signals.json, what the Mac reads) is the oracle: the watch list per viewer, every
post's status and response through the real routes (paired devices), and the settlement per viewer: a signal is
evidence on its poster's view only, never the person's word and never widened. Fictional house; temporary stores.
Run: python3 scripts/test_obligation_signals.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-oblig-signals-"))
for k, v in {"LINKS_DB": "links.db", "MAIL_BILLS_DB": "bills.db", "HOUSEHOLD_DB": "household.db",
             "HOUSEHOLD_PREFS": "household.json", "FENCES_DB": "fences.db", "LINK_SALT_FILE": "salt",
             "KB_ACL_DB": "kb_acl.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import hooks  # noqa: E402
import household_identity as hh  # noqa: E402
import obligation_signals as S  # noqa: E402
import obligations as O  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


V = json.loads((HERE / "evalsets" / "obligation_signals" / "signals.json").read_text())
TODAY = date.fromisoformat(V["today"])
RECORDS = V["house"]["records"]
VIEWERS = V["house"]["viewers"]

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": "mac"}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


HEAD, PID = {}, {}
for who, spec in VIEWERS.items():   # a shared screen is a household "person" with its own device
    HEAD[who], PID[who] = pair(f"{who.title()} Quarry", "adult" if spec["role"] == "admin" else spec["role"],
                               f"{who}'s Mac")
NAME = {pid: who for who, pid in PID.items()}
ROLE = {PID[w]: VIEWERS[w]["role"] for w in PID}

# the house: these records, seen as the vector says; the date fixed; the admin is the vector's admin
lib.record_nodes = lambda t: ([r for r in RECORDS if r["node_type"] == t], None)
lib._node_visible = lambda n, pid, role: n["node_id"] in VIEWERS.get(NAME.get(pid, ""), {}).get("sees", [])
lib._local_now = lambda: datetime(TODAY.year, TODAY.month, TODAY.day, 12, 0)
_real_watch = lib._obligation_watch
lib._obligation_watch = lambda pid, role, today=None: _real_watch(pid, ROLE.get(pid, role), TODAY)
S_put = S.put
S.put = lambda person, role, body, con, accounts, today=None: S_put(person, ROLE.get(person, role), body, con, accounts, TODAY)

print("── the watch list, per viewer ──")
for who, want in V["watch"].items():
    if who not in HEAD:
        check(f"{who}: (no shared screen could be paired here)", True, True)
        continue
    r = c.get("/v1/obligations/watch", headers=HEAD[who])
    got = {"status": r.status_code, **({"obligations": r.json()["obligations"]} if r.status_code == 200 else {})}
    check(f"{who}: {want['status']}" + (f", {len(want.get('obligations', []))} obligation(s)" if want["status"] == 200 else ""),
          got, want)
check("a watch entry carries only ref, vendor, amount, due, state (never an owner or a source)",
      all(set(o) == {"account_ref", "vendor", "amount", "due", "state"} for w in V["watch"].values()
          for o in w.get("obligations", [])), True)

print("── posts: the vector's statuses and responses, through POST /v1/hooks ──")
for p in V["posts"]:
    if p["as"] not in HEAD:
        check(f"{p['name']} (no shared screen could be paired here)", True, True)
        continue
    r = c.post("/v1/hooks", json=p["body"], headers=HEAD[p["as"]])
    got = r.json() if r.status_code == 200 else None
    check(f"{p['name']}: {p['status']}", (r.status_code, got if "response" in p else None),
          (p["status"], p.get("response")))
    if r.status_code != 200:
        check(f"…its error never echoes a value", any(str(x) in json.dumps(r.json()) for x in p["body"].values()
                                                      if isinstance(x, str) and len(x) > 4 and x != "obligation_signal"),
              False)

con = sqlite3.connect(os.environ["LINKS_DB"])
cols = sorted(r[1] for r in con.execute("PRAGMA table_info(obligation_signals)"))
check("stored: the derived fact only, no column for any text, sender or vendor",
      cols, sorted(["owner", "sig_id", "signal", "account", "amount", "on_day", "confidence", "made_at"]))
check("…the poster's own, and the same sig_id replaced, not added",
      con.execute("SELECT owner, COUNT(*) FROM obligation_signals GROUP BY owner").fetchall(), [(PID["robin"], 3)])
check("…replacing kept the newer confidence", con.execute(
    "SELECT confidence FROM obligation_signals WHERE sig_id='a1a1a1a1a1a1a1a1'").fetchone()[0], 0.95)

print("── settlement, per viewer: evidence on the poster's own view only ──")
for who, want in V["settled"].items():
    pid, role = PID[who], VIEWERS[who]["role"]
    recs = [n for t in ("bill", "receipt") for n in lib.record_nodes(t)[0] if lib._node_visible(n, pid, role)]
    ts = {t.vendor: t for t in lib._obligation_threads(recs, TODAY, pid, role)}
    for vendor, w in want.items():
        t = ts.get(vendor)
        chain = t.chain() if t else ""
        got = (t.state if t else None, (w["chain_has"] in chain) if "chain_has" in w else (w["chain_lacks"] not in chain))
        check(f"{who} · {vendor}: {w['state']}" + (f", chain has {w['chain_has']!r}" if "chain_has" in w else
                                                   f", chain lacks {w['chain_lacks']!r}"), got, (w["state"], True))
check("the other adult's store view: none of Robin's signals", S.of(PID["alex"], hooks.connect()), [])
check("a text never opens a thread: a paid signal with no bill adds no obligation",
      [t.vendor for t in O.threads([], TODAY, (), [{"account": "hale water", "date": "2026-10-08", "kind": "paid",
                                                    "amount": 48.2}])], [])
check("a text outside the cycle's window joins nothing",
      "(a text)" in O.threads(RECORDS[:1], TODAY, (), [{"account": "hale water", "date": "2026-06-01", "kind": "paid",
                                                       "amount": 48.2}])[0].chain(), False)
check("weighed as evidence, never as the person's word: no 'said' in the chain",
      " said " in ts_chain if (ts_chain := next(t.chain() for t in lib._obligation_threads(
          [n for n in RECORDS if n["node_id"] == "b-water"], TODAY, PID["robin"], "adult"))) else True, False)

print("── the vector itself ──")
check("every post names its viewer and expected status", all({"name", "as", "body", "status"} <= set(p) for p in V["posts"]),
      True)
check("fixtures use fictional names only", not any(n in json.dumps(V) for n in ("Zach", "Miranda", "Sabrina", "Conor", "Kate")),
      True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
