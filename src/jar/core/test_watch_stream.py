#!/usr/bin/env python3
# gate: hermetic   (throwaway stores; the stream generator over turns made in memory, nothing runs)
"""What a kid's Watch hears while its turn runs (10-07 review W4, after #755): one generic status, the shaped `done`,
or a fixed error. Never a status label (it can name a tool or an item), a cross-device event, a device request, an
error's own words or a `tldr` headline. A phone's turn streams as before. Fictional people.
Run: python3 scripts/test_watch_stream.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-watch-stream-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "PUSH_DB": "push.db", "CONNECTORS_LEDGER": "ledger.db", "SERVER_IDENTITY_KEY": "id/key"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.300}"))


import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

KID_WATCH = hh.Identity({"id": "p_ava", "name": "Ava"}, None, "kid", "watch")
PHONE = hh.Identity({"id": "p_alex", "name": "Alex"}, None, "adult", "device")
LONG = "Ava's piano lesson with Robin moved to Thursday at 4 PM at Maple Hall. " * 20


async def _tldr(final, egress, make=None):
    return "zq-headline about Maple Hall"
api._reply_tldr_soon = _tldr


def stream(tid, turn):
    api._turns[tid] = turn
    out = []

    async def finish():   # a device request waits: answer it after a few polls
        await asyncio.sleep(0.3)
        turn["state"] = "done"

    async def go():
        if turn["state"] == "device_pending":
            asyncio.ensure_future(finish())
        async for chunk in api._sse_follow(tid):
            out.append(chunk)
    asyncio.run(go())
    api._turns.pop(tid, None)
    return [(c.split("\n")[0][7:], json.loads(c.split("\n")[1][6:])) for c in out]


def turn(ident, **kw):
    return {"identity": ident, "state": "done", "text": LONG, "status": ["Searching notes for Maple Hall…"],
            "events": [("remote", {"device": "Robin's MacBook"})], "egress": ["gemini"], "sources": [{"source_id": "s1"}],
            "blocks": [], **kw}


print("── a kid's Watch ──")
ev = stream("w1", turn(KID_WATCH))
check("events: one generic status, then the shaped done; nothing else", [e for e, _ in ev], ["status", "done"])
check("…the status is the fixed words, never the label", ev[0][1], {"text": api.WATCH_STATUS})
check("…no tldr headline, no remote event, no source", "zq-headline" in json.dumps(ev) or "Robin's MacBook" in json.dumps(ev)
      or "s1" in json.dumps(ev), False)
ev = stream("w2", turn(KID_WATCH, state="error", message="read_file failed: /Users/robin/Maple Hall lease.pdf",
                       actions=[{"id": "retry"}]))
check("an error: the fixed words, no actions", ev[-1], ("error", {"message": api.WATCH_ERROR, "actions": []}))
ev = stream("w3", turn(KID_WATCH, state="device_pending", _device_req={"request_id": "r1", "tool": "read_file"}))
check("a device request never goes to the Watch", "device" in [e for e, _ in ev], False)

print("── a phone, as before ──")
ev = stream("p1", turn(PHONE))
names = [e for e, _ in ev]
check("status label, remote event, delta, done and the tldr all stream", (names[:2], "delta" in names, names[-2:]),
      (["status", "remote"], True, ["done", "tldr"]))
ev = stream("p2", turn(PHONE, state="error", message="read_file failed", actions=[{"id": "retry"}]))
check("…its error keeps its own words and actions", ev[-1], ("error", {"message": "read_file failed", "actions": [{"id": "retry"}]}))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
