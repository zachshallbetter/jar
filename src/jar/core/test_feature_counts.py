#!/usr/bin/env python3
# gate: hermetic   (temp counts file, stores and turn log; Gemini stubbed)
"""Content-free counters for 10-09's live-outcome gaps: an Ask-about 404 or 400 (answered before any turn), an
original refused to a kid or a shared screen (refused before any audit), and how many untrusted spans a Gemini turn
sent as their own user part (#1328, in the turn log). Counts and roles only: the counts file takes an event and a role
from fixed lists, never an id, title or text. Fictional people only.
Run: python3 scripts/test_feature_counts.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-feature-counts-"))
for k, v in {"FEATURE_COUNTS": "counts.jsonl", "HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory",
             "PROACTIVE_DB": "proactive.db", "AGENT_TURN_LOG": "turns.jsonl", "HOUSEHOLD_PREFS": "household.json",
             "KB_ACL_DB": "kb_acl.db", "REFERENCES_DB": "references.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["GEMINI_API_KEY"] = "test-key-not-real"
os.environ["AGENT_TOOL_NARROW"] = "0"
sys.path.insert(0, str(Path(__file__).parent))
import feature_counts as F  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def rows():
    try:
        return [json.loads(x) for x in F.PATH.read_text().splitlines()]
    except OSError:
        return []


print("── the counts file takes only fixed words ──")
F.note("about_404", "kid", "document")
F.note("about_404", "adult", "a title someone typed")
F.note("Lease renewal for Robin", "adult")
F.note("about_404", "Robin")
check("an allowed event and role: one line, with an allowed kind", rows()[0] | {"ts": 0},
      {"ts": 0, "event": "about_404", "role": "kid", "kind": "document"})
check("a free-text kind is dropped, the count kept", {k: v for k, v in rows()[1].items() if k != "ts"},
      {"event": "about_404", "role": "adult"})
check("a free-text event or role writes nothing", len(rows()), 2)
check("the file holds no name or title", [w for w in ("Robin", "Lease", "title") if w in F.PATH.read_text()], [])
check("report: counts by event/role/kind in the window", F.report(1), {"about_404/adult": 1, "about_404/kid/document": 1})
check("…an older line falls outside", F.report(1, now=time.time() + 3 * 86400), {})
F.note("about_404", "kid", path=Path("/dev/null/nope"))
check("an unwritable path never raises", True, True)

print("── an original refused to a kid or a screen ──")
import file_share  # noqa: E402

fetched = []
REF = {"id": "rf-0000000000000001", "owner": "robin", "kind": "scan"}
for role, reader, why in (("kid", "ava", "kid"), ("household", None, "screen")):
    out = file_share.read(REF, reader, role, "d_x", lambda r, **k: fetched.append(1) or {}, reader_name="Ava",
                          owner_name="Robin")
    check(f"{role}: refused, and counted as original_refused/{role}/{why}",
          (out[1].get("available"), {k: v for k, v in rows()[-1].items() if k != "ts"}),
          (False, {"event": "original_refused", "role": role, "kind": why}))
check("…and the owner's device was never asked", fetched, [])
n = len(rows())
file_share.read(REF, "alex", "adult", "d_y", lambda r, **k: {}, reader_name="Alex", owner_name="Robin")
check("an adult isn't counted as refused (not shared is a different answer)", len(rows()), n)

print("── an Ask-about refused before any turn ──")
import about_ref  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

KID = hh.Identity({"id": "ava", "name": "Ava", "role": "kid"}, None, "kid", "device")
real = about_ref.resolve
for status in (404, 400):
    about_ref.resolve = lambda *a, _s=status, **k: (_ for _ in ()).throw(about_ref.Refused(_s, "no"))
    resp = asyncio.run(api._about_for({"about": {"kind": "document", "id": "kb:robin-private"}}, KID, None))
    check(f"a {status}: the same answer, and about_{status}/kid/document counted",
          (resp.status_code, {k: v for k, v in rows()[-1].items() if k != "ts"}),
          (status, {"event": f"about_{status}", "role": "kid", "kind": "document"}))
about_ref.resolve = real
check("the counts never hold the id asked about", "robin-private" in F.PATH.read_text(), False)

print("── span_parts in the turn log (#1328) ──")
import agent_runtime_lib as lib  # noqa: E402
import untrusted as U  # noqa: E402

lib._gemini_call = lambda body, model=None, timeout=None: {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
N = U.new_nonce()
lib._TURN_IO.untrusted_nonce, lib._TURN_IO.untrusted_spans, lib._TURN_IO.untrusted_texts = N, 0, []
lib._TURN_INFO.info = {}
msgs = [{"role": "system", "content": "You are titan."},
        {"role": "system", "content": "[EARLIER CHAT]\n" + U.wrap("hi", "chats", N, "earlier chat")},
        {"role": "system", "content": "(about this)\n" + U.wrap("Lease renewal for Robin", "file", N, "document")},
        {"role": "user", "content": "Tell me about this."}]
lib._run_gemini_agent(msgs, max_rounds=1, allowed_tools=set())
check("two span messages: span_parts 2", lib._TURN_INFO.info.get("span_parts"), 2)
lib._TURN_INFO.info = {}
lib._run_gemini_agent([msgs[0], msgs[3]], max_rounds=1, allowed_tools=set())
check("none: no span_parts field", "span_parts" in lib._TURN_INFO.info, False)
lib._TURN_INFO.info = {"span_parts": 2, "role": "adult"}
lib._log_turn(time.monotonic(), 1, None)
line = json.loads(Path(os.environ["AGENT_TURN_LOG"]).read_text().splitlines()[-1])
check("the turn log keeps span_parts (a count)", line.get("span_parts"), 2)
check("…and nothing of the span", [w for w in ("Lease", "Robin", "untrusted") if w in json.dumps(line)], [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
