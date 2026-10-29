#!/usr/bin/env python3
# gate: hermetic
"""A scan's text read from its owner's iPhone is used for that turn only (titan-node#776, #446 step 3's rule): it never
lands anywhere on titan. Two full turns through the API, a fake model and a fake iPhone answering item_body with a
marker in the text, both ways titan reaches the phone:
  - in-turn: the owner asks on the iPhone that holds the scan (a device call over the turn);
  - the inbox: the owner asks from her Mac, so titan queues it on the phone's inbox and wakes it.
Case 1, the model doesn't quote it ("Read it."): the marker appears nowhere. Not in any file under the test's state
(searched as bytes, SQLite included), not in any outbound HTTP body (a KB or record write would be one), not in what
the process printed. Case 2, the model quotes it: only in the reply and the asker's own conversation record (her own
messages), never in another person's history, memory facts, the KB, logs or anything else.
Run: python3 scripts/test_item_body_leak.py"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-item-leak-"))
# Every store a turn could write, here (the deploy gate's own list, so nothing lands outside this dir).
for k in ("HOUSEHOLD_DB", "REFERENCES_DB", "ITEM_REFS_DB", "PROACTIVE_DB", "KB_ACL_DB", "PUSH_DB", "CONNECTORS_LEDGER",
          "CONTINUITY_DB", "REFLECTION_DB", "ROUTINES_DB", "TODOS_DB", "PLACES_DB", "SCHEDULED_DB", "WORLD_DB",
          "FENCES_DB", "TASKS_DB", "GRANTS_DB", "REVIEW_DB", "RETRIEVAL_TRACE_DB", "TASK_LOG_DB", "CUSTODY_DB",
          "HELD_ACTIONS_DB", "KIDS_CHORES_DB", "FAMILY_MEETING_DB", "SPACES_DB", "MAIL_TRIAGE_DB", "CAPTURE_DB",
          "OUR_YEAR_DB", "MAIL_BILLS_DB", "MAIL_SUGGESTIONS_DB", "CLAUDE_CHATS_DB", "EVAL_BANK_DB", "UPLOADS_DB",
          "GENERATED_IMAGES_DB", "SITTER_DB", "HANDOFF_DB", "TV_UNLOCK_DB"):
    os.environ[k] = str(_TMP / (k.lower() + ".db"))
for k in ("AGENT_MEMORY_DIR", "DAILY_LOG_DIR", "WORLD_DIR", "CONNECTORS_DIR", "GENERATED_DIR", "UPLOADS_DIR",
          "AGENT_SPEECH_DIR", "KB_BACKUP_DIR", "STORE_BACKUP_DIR"):
    os.environ[k] = str(_TMP / k.lower())
for k in ("AGENT_TURN_LOG", "TURN_REVIEW_LOG", "AGENT_HISTORY_FILE", "GRANTS_AUDIT", "KB_LIFECYCLE_LOG",
          "HOUSEHOLD_PREFS", "HA_TOKEN_FILE"):
    os.environ[k] = str(_TMP / (k.lower() + ".log"))
for k in ("RECORD", "RETRIEVAL", "EVENT", "IDENTITY", "STATE", "CONTENT"):
    os.environ[f"TITAN_{k}_URL"] = "http://127.0.0.1:9"
os.environ.update({"ITEM_REFERENCES": "mail,scans", "AGENT_TURN_SOURCE": "user", "GEMINI_API_KEY": "test-key-not-real",
                   "AGENT_TOOL_BRAIN": "gemini", "AGENT_STREAM_TOKENS": "0", "MEMORY_HYBRID": "0"})
for k in ("AGENT_WORLD_MODEL", "AGENT_FENCES", "AGENT_TASK_AUTONOMY", "AGENT_WORLD_SITUATION", "AGENT_JEV_ROUTER",
          "AGENT_JEV_TURN_GATES", "CITE_INLINE", "AGENT_SPEAK_LOCAL", "MAIL_TRIAGE", "ITEM_ENVELOPES", "KB_RERANK",
          "AGENT_GEMINI_SCHEDULE_PREFETCH"):
    os.environ[k] = "0"
sys.path.insert(0, str(Path(__file__).parent))

# Every outbound HTTP body (a KB, record or event write is one), recorded and refused: nothing leaves the test.
OUTBOUND: list[bytes] = []
_real_request = http.client.HTTPConnection.request


def _capture(self, method, url, body=None, headers={}, **kw):   # noqa: B006 - http.client's own signature
    if self.host not in ("testserver",):
        OUTBOUND.append((body if isinstance(body, bytes) else str(body or "").encode()) + url.encode())
        raise ConnectionRefusedError("hermetic: no network")
    return _real_request(self, method, url, body, headers, **kw)


http.client.HTTPConnection.request = _capture

import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import cross_device as xd  # noqa: E402
import household_identity as hh  # noqa: E402
import references  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")


MARKER = "ZebraQuixoticLantern"
SCAN = f"Lease renewal. The rent stays the same and the lease renews in March. {MARKER}."
SCAN_HASH = "sha256:" + hashlib.sha256(SCAN.encode()).hexdigest()

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


PH, rob, rdev = pair("Robin", "adult")                  # Robin's iPhone holds the scan
MAC, _, _ = pair("Robin", "adult", "mac", person=rob)   # Robin's Mac
JH, jordan, _ = pair("Jordan", "adult")                 # someone else, who must see none of it
check("Robin's iPhone says it holds scans", c.patch("/v1/devices/self", json={"holds": ["scan"]}, headers=PH).status_code, 200)
references.put(rob, "adult", rdev["id"], {"kind": "scan", "native_id": "lease.pdf", "title": "Lease renewal",
                                          "content_hash": SCAN_HASH})

# The model: asks for the scan, then answers. QUOTE decides whether its answer quotes the scan.
QUOTE = {"on": False}
MODEL_SAW: list[str] = []


def fake_gemini(body, model=None, timeout=None):
    contents = body.get("contents") or []
    last = contents[-1] if contents else {}
    if any("functionResponse" in p for p in last.get("parts") or []):
        MODEL_SAW.append(json.dumps(last))
        text = f"It says: {MARKER}." if QUOTE["on"] else "Read it."
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    if body.get("tools"):
        return {"candidates": [{"content": {"parts": [{"functionCall": {"name": "open_item", "args": {"what": "lease renewal"}}}]}}]}
    return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}   # anything else (titles, headlines)


lib._gemini_call = fake_gemini
WOKE: list[str] = []
xd.PUSH_WAKE = lambda dev: WOKE.append(str(dev.get("device_id"))) or []
xd.PHONE_WAKE_WAIT_S = 20.0
INBOX_ANSWERED: list[bool] = []
ANSWER = {"found": True, "title": "Lease renewal", "text": SCAN, "text_hash": SCAN_HASH}


def ask(headers, body) -> dict:
    """POST /v1/prompt on a thread (it blocks until the turn ends); the main thread plays the iPhone meanwhile."""
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(c.post("/v1/prompt", json=body, headers=headers).json()), daemon=True)
    t.start()
    return {"thread": t, "out": out}


def play_phone_in_turn(until: float) -> bool:
    """The owner's own iPhone: answer the turn's item_body device call."""
    while time.time() < until:
        for tid, turn in list(api._turns.items()):
            req = turn.get("_device_req") or {}
            if req.get("tool") == "item_body":
                r = c.post(f"/v1/device/result/{tid}", json={"request_id": req["request_id"], "result": json.dumps(ANSWER)},
                           headers=PH)
                return r.status_code < 300
        time.sleep(0.05)
    return False


def play_phone_inbox(until: float) -> bool:
    """The iPhone woken by the push: drain its inbox, answer item_body."""
    while time.time() < until:
        items = c.get(f"/v1/device/inbox?wait=1&device_key={rdev['id']}", headers=PH).json().get("requests") or []
        for it in items:
            if it.get("tool") == "item_body":
                r = c.post(f"/v1/device/inbox/{it['id']}/result",
                           json={"status": "done", "result": json.dumps(ANSWER), "device_key": rdev["id"]}, headers=PH)
                INBOX_ANSWERED.append(r.status_code < 300)
                return r.status_code < 300
    return False


def leaks(allow_own_conversation: bool) -> list[str]:
    """Where the marker is, other than the reply. With `allow_own_conversation`, Robin's own conversation messages are
    allowed (the quoting model's reply, kept as her chat); nothing else."""
    found = []
    mem_db = Path(os.environ["AGENT_MEMORY_DIR"]) / "titan_memory.db"
    cont_db = Path(os.environ["CONTINUITY_DB"])
    # Her conversation's two stores (the messages, and the feed to her other devices), with SQLite's sidecar files:
    # read row by row below when quoting is allowed; every other file is searched as bytes.
    own = {str(d) + sfx for d in (mem_db, cont_db) for sfx in ("", "-wal", "-shm", "-journal")}
    for p in _TMP.rglob("*"):
        if not p.is_file():
            continue
        data = p.read_bytes()
        if MARKER.encode() not in data:
            continue
        if allow_own_conversation and str(p) in own:
            continue
        found.append(str(p.relative_to(_TMP)))
    if cont_db.exists():
        con = sqlite3.connect(cont_db)
        try:
            rows = con.execute("SELECT owner, kind, data FROM changes WHERE data LIKE ?", (f"%{MARKER}%",)).fetchall()
        finally:
            con.close()
        for owner, kind, data in rows:
            role = (json.loads(data) if str(data).startswith("{") else {}).get("role")
            if not (allow_own_conversation and owner == rob and kind == "message" and role == "assistant"):
                found.append(f"continuity ({kind}, {role}, {'her' if owner == rob else 'another'} feed)")
    if mem_db.exists():
        con = sqlite3.connect(mem_db)
        try:
            mine = hh.conv_id_for(rob)
            rows = con.execute("SELECT conv_id, role FROM messages WHERE content LIKE ?", (f"%{MARKER}%",)).fetchall()
            facts = con.execute("SELECT COUNT(*) FROM facts WHERE text LIKE ?", (f"%{MARKER}%",)).fetchone()[0]
        finally:
            con.close()
        for conv, role in rows:
            if not (allow_own_conversation and conv == mine and role == "assistant"):
                found.append(f"memory message ({role}, {'her' if conv == mine else 'another'} conversation)")
        if facts:
            found.append(f"memory facts ({facts})")
    found += [f"outbound HTTP ({b[:60]!r})" for b in OUTBOUND if MARKER.encode() in b]
    return found


def run(label: str, headers, body, phone, quote: bool):
    QUOTE["on"] = quote
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
        a = ask(headers, body)
        answered = phone(time.time() + 25)
        a["thread"].join(60)
    reply = str(a["out"].get("text") or a["out"].get("response") or "")
    check(f"{label}: the phone answered titan's item_body", answered, True)
    check(f"{label}: the model was given the scan for this turn", any(MARKER in s for s in MODEL_SAW[-1:]), True)
    check(f"{label}: the reply", (MARKER in reply) if quote else reply, True if quote else "Read it.")
    check(f"{label}: not in anything the process printed", MARKER in printed.getvalue(), False)
    return reply


print("── the search itself finds a leak (or the passes below mean nothing) ──")
planted = _TMP / "planted" / "deep.bin"
planted.parent.mkdir()
planted.write_bytes(b"\x00\x01" + MARKER.encode() + b"\x02")
check("a copy planted in a binary file is found", leaks(False), ["planted/deep.bin"])
planted.unlink()
try:
    http.client.HTTPConnection("127.0.0.1", 9).request("POST", "/kb", body=MARKER.encode())
except ConnectionRefusedError:
    pass
check("an outbound HTTP body is caught (a KB write would be)", [x for x in leaks(False) if x.startswith("outbound")] != [], True)
OUTBOUND.clear()
check("…and the slate is clean again", leaks(False), [])

print("── in-turn: Robin asks on the iPhone that holds it; the model doesn't quote it ──")
run("in-turn", PH, {"prompt": "What does my lease renewal say?", "device": True, "client": "iphone",
                    "device_tools": ["item_body"]}, play_phone_in_turn, quote=False)
check("in-turn: the marker is nowhere on titan (files as bytes, memory, KB writes, outbound HTTP)", leaks(False), [])

print("── the inbox: Robin asks from her Mac; titan wakes the iPhone ──")
run("inbox", MAC, {"prompt": "What does my lease renewal say?"}, play_phone_inbox, quote=False)
check("inbox: it went by her iPhone's inbox (not the Mac's turn)", INBOX_ANSWERED[-1:], [True])
check("inbox: the marker is nowhere on titan", leaks(False), [])

print("── the model quotes it: only the reply and Robin's own conversation ──")
run("in-turn, quoted", PH, {"prompt": "Read my lease renewal word for word.", "device": True, "client": "iphone",
                            "device_tools": ["item_body"]}, play_phone_in_turn, quote=True)
run("inbox, quoted", MAC, {"prompt": "Read my lease renewal word for word."}, play_phone_inbox, quote=True)
check("quoted: nowhere but Robin's own conversation (no other history, facts, KB, logs or files)", leaks(True), [])
jordan_conv = hh.conv_id_for(jordan)
con = sqlite3.connect(Path(os.environ["AGENT_MEMORY_DIR"]) / "titan_memory.db")
check("…and Jordan's conversation has none of it", con.execute(
    "SELECT COUNT(*) FROM messages WHERE conv_id=? AND content LIKE ?", (jordan_conv, f"%{MARKER}%")).fetchone()[0], 0)
con.close()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
