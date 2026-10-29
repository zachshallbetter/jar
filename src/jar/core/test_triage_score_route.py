#!/usr/bin/env python3
# gate: hermetic
"""How triage matches what each person does (#1644): GET /v1/me/triage-score reads the caller's own scorecard
(triage_scorecard.score), ?days= bounded, a shared screen gets 403, and nobody reads anyone else's. Fictional people
(Robin an adult, Ava a kid, a living-room screen); temporary stores; no model. Run: python3 scripts/test_triage_score_route.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-triage-score-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "SPACES_DB": "spaces.db", "CONNECTORS_LEDGER": "ledger.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "WORLD_DIR": "world",
             "ME_DELETE_DIR": "me-delete", "CLAUDE_CHATS_DB": "claude_chats.db", "MAIL_TRIAGE_DB": "triage.db", "MAIL_SERVER_DB": "mail.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.200} expected={expected!r:.200}"))


from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, platform="ios"):
    s_ = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    tok = c.post("/v1/pair/claim", json={"code": s_["code"], "device_name": f"{name}'s device",
                                         "platform": platform}).json()["token"]
    return {"X-Agent-Token": tok}, next(p["id"] for p in st.people() if p["name"] == name)


ROBIN, robin = pair("Robin Hale", "adult")
AVA, ava = pair("Ava Hale", "kid")
TV, _ = pair("Living room", "household", "tvos")
import time  # noqa: E402
import mail_moves as MM  # noqa: E402
import mail_server as MS  # noqa: E402
con = MS._db()
for i, (mid, cat) in enumerate((("<n1@x>", "newsletter"), ("<p1@x>", "personal")), 1):
    con.execute("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, thread_id, category) "
                "VALUES (?,?,?,?,?,?,?,?,?)", ("icloud", "INBOX", 1, i, robin, f"m{i}", mid, mid, cat))
con.commit()
con.close()
c2 = MM._db()
p = MM._store(c2, owner=robin, account="icloud", src="INBOX", dest="Archive",
              rows=[{"mid": m, "sender": "x", "date": ""} for m in ("<n1@x>", "<p1@x>")], now=time.time() - 60)
c2.execute("UPDATE mail_plans SET state='done', done_at=? WHERE id=?", (time.time() - 60, p["plan_id"]))
c2.commit()
c2.close()
r = c.get("/v1/me/triage-score", headers=ROBIN)
check("Robin's own figures: two cleared, one triage agreed with, agreement 0.5",
      (r.status_code, r.json()["days"], r.json()["cleared"], r.json()["agreed"], r.json()["agreement"]), (200, 30, 2, 1, 0.5))
check("…counts only: no sender, subject or Message-ID", any(x in r.text for x in ("@x", "icloud")), False)
check("Ava's are her own (none)", c.get("/v1/me/triage-score", headers=AVA).json()["cleared"], 0)
check("a shared screen gets 403", c.get("/v1/me/triage-score", headers=TV).status_code, 403)
check("days bounded to 90; not a number: 400", (c.get("/v1/me/triage-score?days=500", headers=ROBIN).json()["days"],
                                                 c.get("/v1/me/triage-score?days=x", headers=ROBIN).status_code), (90, 400))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
