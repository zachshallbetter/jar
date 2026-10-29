#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""The iPhone's health_summary (Jellybean iOS, Apple Health, read only, opt-in on the phone).

Offered on a phone turn whose app reports it, never on a Mac turn, never routed to the Mac; a read;
every role (their own data, on their own phone); the description tells the model to report the
numbers as given, not to keep them, not to tell anyone else. The strings are the phone's
(Jellybean's HealthTests).
Run: python3 scripts/test_phone_health.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-health-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


def turn(client=None, device_tools=None, remote=None):
    io = lib._TURN_IO
    io.client, io.device_tools, io.remote, io.role, io.device = client, device_tools, remote, None, object()


T = "health_summary"
phone_now = lib.device_tools_for_client("iphone", sorted(lib.PHONE_DEVICE_TOOLS))
old_phone = lib.device_tools_for_client("iphone", sorted(lib.PHONE_DEVICE_TOOLS - {T}))

check("phone only", T in lib.PHONE_ONLY_TOOLS, True)
check("a current phone gets it", T in phone_now, True)
check("an older phone doesn't", T in old_phone, False)
check("never routed to the Mac", T in lib.mac_tools_for_phone(old_phone), False)
turn(client="iphone", device_tools=phone_now)
check("a phone turn lists it", T in lib._turn_device_tools(), True)
turn(client=None, device_tools=None)
check("a Mac turn doesn't", T in lib._turn_device_tools(), False)
check("a Mac turn that calls it: ask from the iPhone", "ask from their iPhone" in lib._execute_tool(T, {}), True)
# 10-01 (SkillOpt health-mac-says-phone): on the Mac it searched notes and the KB, then "I don't have
# access to your sleep tracking data". The Mac prompt says where Health is.
turn()
check("the Mac's prompt: Health is on the iPhone, ask there, don't dig",
      "\"how did I sleep?\" can't reach it: say to ask from the iPhone" in lib.build_tool_system_prompt(), True)

spec = lib._DEVICE_TOOLS[T]
check("a read", spec["risk"], "read")
check("its parameters", sorted(spec["parameters"]), ["metric", "period"])
d = spec["description"]
check("says where it runs", d.startswith("ON THE USER'S IPHONE."), True)
check("never guess", "never guess" in d, True)
check("not to memory", "Don't save" in d, True)
check("never to anyone else", "never mention them to anyone else" in d, True)
check("every role (their own)", all(hh.role_allows_tool(r, T) for r in ("adult", "kid")), True)
check("a step label", lib._TOOL_STATUS.get(T), "Checking Health…")

lib._TURN_IO.device = None
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
