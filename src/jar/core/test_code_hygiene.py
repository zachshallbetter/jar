#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""The 10-02 code audit's safe items: failures that mustn't break a turn are no longer silent (one stderr line per
place, its type only, counted on /health); the per-turn stores don't grow forever; the Gemini key travels in a
header, never a URL. Run: python3 scripts/test_code_hygiene.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time
from contextlib import redirect_stderr
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-hygiene-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "nodir/turns.jsonl",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "test"
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── a quiet failure says so, once, without its text ──")
lib.QUIET_FAILURES.clear()
err = io.StringIO()
with redirect_stderr(err):
    for _ in range(3):
        lib.quiet_failure("memory recall", ValueError("Robin's secret note"))
check("one line the first time, then counted", (err.getvalue().count("[quiet]"), lib.QUIET_FAILURES["memory recall"]), (1, 3))
check("…its type only, never the message", ("ValueError" in err.getvalue(), "secret" in err.getvalue()), (True, False))
err = io.StringIO()
with redirect_stderr(err):
    for _ in range(97):
        lib.quiet_failure("memory recall", ValueError("x"))
check("…and again every 100th", err.getvalue().count("100 so far"), 1)

src_api = (Path(lib.__file__).with_name("agent_api_server.py")).read_text()
check("memory recall, the fact save and the turn log use it",
      ('_lib.quiet_failure("memory recall", exc)' in src_api, '_lib.quiet_failure("fact save", exc)' in src_api,
       'quiet_failure("turn log", exc)' in Path(lib.__file__).read_text()), (True, True, True))
check("/health shows the counts", '"quiet_failures": dict(_lib.QUIET_FAILURES)' in src_api, True)

print("── the stores are bounded ──")
import agent_api_server as api  # noqa: E402
now = time.time()
api._processes.clear()
api._processes.update({
    "w:old": {"status": "exited", "started_at": now - 9000, "ended_at": now - 7200},
    "w:recent": {"status": "failed", "started_at": now - 120, "ended_at": now - 60},
    "w:running": {"status": "running", "started_at": now - 9000},
})
api._prune_processes(now)
check("ended over an hour ago: gone; recently ended and running: kept", sorted(api._processes), ["w:recent", "w:running"])
cap = api.PROCESS_CAP
api.PROCESS_CAP = 3
for i in range(5):
    api._processes[f"w:e{i}"] = {"status": "killed", "started_at": now, "ended_at": now - 100 + i}
api._prune_processes(now)
check("past the cap: the oldest ended go first, never a running one",
      (len(api._processes) <= 3, "w:running" in api._processes, "w:e4" in api._processes), (True, True, True))
api.PROCESS_CAP = cap
api._processes.clear()
check("a turn's token stats are taken when it ends, the total kept",
      "_session_token_stats.pop(turn_id, None)" in src_api and "_token_savings_done +=" in src_api, True)

print("── the Gemini key is a header, never in a URL ──")
import agent_memory as am  # noqa: E402
seen = {}


class FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}'


import urllib.request as ur  # noqa: E402
real = ur.urlopen
ur.urlopen = lambda req, timeout=None: (seen.update(url=req.full_url, headers=dict(req.header_items())) or FakeResp())
os.environ["GEMINI_API_KEY"] = "test-key-123"
os.environ["MEMORY_EXTRACT_BRAIN"] = "gemini"
try:
    fn = next(getattr(c, n) for c in [am] + [v for v in vars(am).values() if isinstance(v, type)]
              for n in dir(c) if "generateContent" in (getattr(getattr(c, n), "__code__", None) and
                                                       " ".join(map(str, getattr(c, n).__code__.co_consts)) or ""))
except StopIteration:
    fn = None
if fn is not None:
    try:
        fn(None, "hi") if "self" in fn.__code__.co_varnames[:1] else fn("hi")
    except Exception:  # noqa: BLE001 - the shape of the call is what's checked
        pass
ur.urlopen = real
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("MEMORY_EXTRACT_BRAIN", None)
src_mem = Path(am.__file__).read_text()
check("no ?key= in any URL the memory module builds", "?key=" in src_mem, False)
check("…the key goes as x-goog-api-key", '"x-goog-api-key": gemini_key' in src_mem, True)
if seen:
    check("…and a real call sends it that way", ("key=" in seen["url"], seen["headers"].get("X-goog-api-key")),
          (False, "test-key-123"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
