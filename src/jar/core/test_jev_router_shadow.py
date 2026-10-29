#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""#683: chat is all Gemini (#723 removed AGENT_LOCAL_CHAT), the Jev router's answer can't change the brain, so the
turn doesn't wait for it: it's asked on a background thread and logged as before. With local chat on it still
decides. Hermetic: Jev is faked, the turn log is a temporary file.

  python3 scripts/test_jev_router_shadow.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(tempfile.mkdtemp(), "agent-turns.jsonl")
os.environ["AGENT_TURN_LOG"] = LOG
os.environ["AGENT_JEV_ROUTER"] = "1"

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


class FakeJev:
    def __init__(self, delay: float, choice: str = "local", sure: float = 0.97, fail: bool = False) -> None:
        self.delay, self.choice, self.sure, self.fail, self.calls = delay, choice, sure, fail, 0

    def decide(self, payload, questions, **kw):
        self.calls += 1
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("broker down")
        return {"answers": {"brain": {"choice": self.choice, "confidence": self.sure}, "needs_tools": {"noul": 0.05}},
                "_meta": {"latency_ms": int(self.delay * 1000)}}


MSG = [{"role": "user", "content": "Hmm, honestly I'm not sure what I think about the new curtains yet."}]


def turn(jev: FakeJev, wait_before_log: float = 0.0) -> tuple[str, float, dict]:
    lib._jev_client = lambda: jev
    lib._TURN_INFO.route, lib._TURN_INFO.info = {}, {}
    t0 = time.monotonic()
    brain = lib._classify_turn(MSG)
    took = time.monotonic() - t0
    time.sleep(wait_before_log)
    lib._log_turn(time.monotonic(), 1, None)
    with open(LOG) as fh:
        rec = json.loads(fh.read().splitlines()[-1])
    return brain, took, rec


real_client = lib._jev_client
try:
    jev = FakeJev(0.4)
    brain, took, rec = turn(jev, wait_before_log=0.5)
    check("local chat off: the turn goes to Gemini", brain, "gemini")
    check("… without waiting for Jev (0.4 s)", took < 0.15, True)
    check("… Jev was still asked (the decision broker's ledger, the Decider trial)", jev.calls, 1)
    check("… and the log has its answer, as the blocking router logged it",
          (rec["route"], rec["jev"]), ("jev-local-gemini", {"choice": "local", "sure": 0.97, "needs_tools": 0.05, "ms": 400}))

    brain, took, rec = turn(FakeJev(0.2, choice="frontier", sure=0.8), wait_before_log=0.3)
    check("a frontier answer is logged as jev-frontier", (brain, rec["route"], rec["jev"]["choice"]), ("gemini", "jev-frontier", "frontier"))

    brain, took, rec = turn(FakeJev(1.0))
    check("a turn faster than Jev logs it as pending, never waits for it", (rec["route"], rec["jev"], took < 0.15),
          ("jev-frontier", {"pending": True}, True))

    brain, took, rec = turn(FakeJev(0.05, fail=True), wait_before_log=0.2)
    check("a Jev error: Gemini, logged as default with no answer", (brain, rec["route"], rec["jev"]), ("gemini", "default", None))

    os.environ["AGENT_JEV_ROUTER"] = "0"
    jev = FakeJev(0.4)
    brain, took, rec = turn(jev)
    check("router off: Jev isn't asked", (brain, jev.calls, rec["route"]), ("gemini", 0, "default"))
    os.environ["AGENT_JEV_ROUTER"] = "1"

finally:
    lib._jev_client = real_client
    lib._TURN_INFO.route, lib._TURN_INFO.info = {}, {}

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
