#!/usr/bin/env python3
"""A stuck turn ends its stream (agent_api_server.TURN_STUCK_S), and an eval turn has a wall clock (turn_eval.ask's
`wall`). 10-15: privacy_eval's kid-files turn kept its stream open on keep-alives for 25 minutes, the per-read timeout
never tripped, and the nightly household eval hit its 1800 s limit.
# gate: hermetic
"""
from __future__ import annotations

import http.server
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SCHEDULED_DB", str(Path(tempfile.mkdtemp()) / "scheduled.db"))
import agent_api_server as S  # noqa: E402
import turn_eval as te  # noqa: E402

failures: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{'' if ok else f': {got!r} != {want!r}'}")
    if not ok:
        failures.append(name)


print("── a stuck turn ──")
release = threading.Event()


def waiting_forever_in_a_tool():   # stands in for whatever a stuck turn's thread is blocked on
    release.wait(30)


th = threading.Thread(target=waiting_forever_in_a_tool, daemon=True)
th.start()
time.sleep(0.1)
now = time.time()
dev = threading.Event()
S._init_turn("t-stuck")
S._turns["t-stuck"].update({"created_at": now - S.TURN_STUCK_S - 5, "thread_ident": th.ident, "source": "eval",
                            "device_event": dev, "state": "device_pending"})
S._init_turn("t-slow")
S._turns["t-slow"].update({"created_at": now - S.TURN_STUCK_S + 30})
S._init_turn("t-done")
S._turns["t-done"].update({"created_at": now - S.TURN_STUCK_S - 5, "state": "done"})

import contextlib  # noqa: E402
import io  # noqa: E402
err = io.StringIO()
with contextlib.redirect_stderr(err):
    first = S._stuck("t-stuck", now=now)
    again = S._stuck("t-stuck", now=now)
check("past TURN_STUCK_S: an error, once", (first, again, S._turns["t-stuck"]["state"], S._turns["t-stuck"]["error"]),
      (True, False, "error", "stuck"))
check("…its device wait is released", dev.is_set(), True)
check("…the person's words, not the reason", S._turns["t-stuck"]["message"].startswith("This took too long"), True)
log = err.getvalue()
check("…where its thread is, logged once: the function it's blocked in, as code locations",
      (log.count("[turn-stuck] t-stuck (eval)"), "waiting_forever_in_a_tool" in log, "test_turn_stuck.py:" in log), (1, True, True))
check("a slow turn inside the limit, and a finished one, are left alone",
      (S._stuck("t-slow", now=now), S._turns["t-slow"]["state"], S._stuck("t-done", now=now), S._turns["t-done"]["state"]),
      (False, "running", False, "done"))
check("a turn with no thread recorded still ends (no stack)", S._turn_stack(None), "")
release.set()


async def first_event(turn_id):
    async for chunk in S._sse_follow(turn_id):
        return chunk


import asyncio  # noqa: E402
S._init_turn("t-stream")
S._turns["t-stream"].update({"created_at": time.time() - S.TURN_STUCK_S - 1})
with contextlib.redirect_stderr(io.StringIO()):
    chunk = asyncio.run(asyncio.wait_for(first_event("t-stream"), 5))
check("the stream of a stuck turn ends with an error event", (chunk or "").startswith("event: error"), True)
check("…saying it took too long", "took too long" in (chunk or ""), True)
src = (Path(__file__).resolve().parent / "agent_api_server.py").read_text()
check("every turn records its thread", src.count('_turns[turn_id]["thread_ident"] = threading.get_ident()'), 1)

print("── an eval turn's wall clock ──")


class KeepAlives(http.server.BaseHTTPRequestHandler):
    """A server that answers a turn with keep-alives and never a done."""
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for _ in range(200):
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), KeepAlives)
threading.Thread(target=srv.serve_forever, daemon=True).start()
te.API = f"http://127.0.0.1:{srv.server_address[1]}"
te._token = lambda: "test-token"
t0 = time.time()
turn = te.ask("Anything", timeout=1, wall=1.5)
took = time.time() - t0
check("keep-alives never answer: the turn ends at its wall clock, not the read timeout",
      (took < 4, turn.error.startswith("no answer in 1 s")), (True, True))
check("the default wall is twice the read timeout", "wall = float(wall if wall is not None else 2 * timeout)" in
      (Path(__file__).resolve().parent / "turn_eval.py").read_text(), True)
srv.shutdown()

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed")
sys.exit(1 if failures else 0)
