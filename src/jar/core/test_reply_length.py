#!/usr/bin/env python3
# gate: hermetic
"""How much titan says (jellybean#699, Zach 10-04): short by default, long only when asked ("tell me more", "in
detail", Think harder). The apps get the brief style unless the person's words or the composer ask for more; a caller
that isn't an app keeps the prompt as it was. Hermetic: only the request parsing.
Run: python3 scripts/test_reply_length.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-reply-length-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import agent_api_server as api  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r} expected={expected!r}"))


d = api._parse_detail
print("── short by default ──")
check("the iPhone sends nothing: brief", d({"prompt": "What's the weather?", "client": "iphone"}), "brief")
check("the Mac panel's brief stays brief", d({"prompt": "When is Ava's recital?", "detail": "brief", "client": "mac"}), "brief")
check("the TV and the Watch: brief", (d({"prompt": "Who's home?", "client": "tv"}), d({"prompt": "x", "client": "watch"})),
      ("brief", "brief"))
print("── long when asked ──")
for q in ("Tell me more about the Harbor Gas bill", "Explain the trip plan in detail", "Can you elaborate?",
          "Walk me through the new routine", "Give me the full list of Robin's appointments", "I want a longer answer"):
    check(f"{q!r}: full", d({"prompt": q, "client": "iphone", "detail": "brief"}), "full")
check("Think harder: full", d({"prompt": "Plan Saturday", "client": "iphone", "effort": "high"}), "full")
check("a client's own full is kept", d({"prompt": "What's next?", "detail": "full", "client": "mac"}), "full")
check("the words in `message` count too", d({"message": "tell me more", "client": "iphone"}), "full")
print("── everything else unchanged ──")
check("not an app (a script, a harness): no style, as before", d({"prompt": "What's on today?"}), None)
check("…unless it asks for brief", d({"prompt": "x", "detail": "brief"}), "brief")
check("'more' alone isn't asking for detail", d({"prompt": "Add more milk to the list", "client": "iphone"}), "brief")
check("the brief style is what the prompt gets", "[REPLY STYLE: brief]" in api._REPLY_STYLES[d({"prompt": "x", "client": "iphone"})],
      True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
