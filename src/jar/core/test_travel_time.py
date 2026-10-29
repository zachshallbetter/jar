#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""travel_time: the iPhone's trip tool (Jellybean iOS, MapKit from where the phone is).

It's offered on a phone turn whose app reports it, never on a Mac turn, never routed from the
phone to the Mac, every role may use it, and a Mac turn that calls it anyway is told to ask from
the iPhone. The result strings are the phone's (tested in Jellybean's TravelToolTests).
Run: python3 scripts/test_travel_time.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-travel-test-"))
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


def turn(client=None, device_tools=None, remote=None, role=None, device=object()):
    """Set this thread's turn the way the HTTP API does."""
    io = lib._TURN_IO
    io.client, io.device_tools, io.remote, io.role, io.device = client, device_tools, remote, role, device


PHONE_NOW = sorted(lib.PHONE_DEVICE_TOOLS)             # a current phone reports all of these
OLD_PHONE = sorted(lib.PHONE_DEVICE_TOOLS - {"travel_time"})
# A Mac's own report (#739, jellybean#547): a current Mac runs travel_time, an older one doesn't say it does.
MAC_NOW = sorted(n for n, spec in lib._DEVICE_TOOLS.items() if not spec.get("internal") and n not in lib.PHONE_ONLY_TOOLS)
OLD_MAC = sorted(set(MAC_NOW) - {"travel_time"})

print("── who's offered travel_time ──")
check("a phone that reports it gets it",
      "travel_time" in lib.device_tools_for_client("iphone", PHONE_NOW), True)
check("an older phone that doesn't report it doesn't",
      "travel_time" in lib.device_tools_for_client("iphone", OLD_PHONE), False)
check("this device only (not phone-only since jellybean#547)",
      ("travel_time" in lib.THIS_DEVICE_TOOLS, "travel_time" in lib.PHONE_ONLY_TOOLS), (True, False))
check("never routed to the Mac (older phone)", "travel_time" in lib.mac_tools_for_phone(None), False)
check("never routed to the Mac (current phone)",
      "travel_time" in lib.mac_tools_for_phone(lib.device_tools_for_client("iphone", PHONE_NOW)), False)

turn(client="iphone", device_tools=lib.device_tools_for_client("iphone", PHONE_NOW))
check("a phone turn lists it", "travel_time" in lib._turn_device_tools(), True)
turn(client="iphone", device_tools=lib.device_tools_for_client("iphone", OLD_PHONE),
     remote={"tools": lib.mac_tools_for_phone(lib.device_tools_for_client("iphone", OLD_PHONE))})
check("an older phone's turn doesn't, even with the Mac reachable", "travel_time" in lib._turn_device_tools(), False)
turn(client=None, device_tools=lib.device_tools_for_client("mac", MAC_NOW))
check("a current Mac's turn lists it", "travel_time" in lib._turn_device_tools(), True)
turn(client=None, device_tools=None)
check("…and a Mac with no list (the registry)", "travel_time" in lib._turn_device_tools(), True)
turn(client=None, device_tools=lib.device_tools_for_client("mac", OLD_MAC))
check("an older Mac's turn doesn't", "travel_time" in lib._turn_device_tools(), False)
check("the other Mac tools are still there", "list_calendar_events" in lib._turn_device_tools(), True)

print("── an older Mac's turn that calls it anyway ──")
turn(client=None, device_tools=lib.device_tools_for_client("mac", OLD_MAC))
out = lib._execute_tool("travel_time", {"destination": "Pearl Dental"})
check("told to say the iPhone can, with no number (#739)", (lib.TRIP_UNAVAILABLE in out, "Your iPhone can" in out), (True, True))

print("── roles and the step label ──")
for role in ("adult", "kid"):
    check(f"a {role} may use it", hh.role_allows_tool(role, "travel_time"), True)
check("the phone's step label", lib._TOOL_STATUS.get("travel_time"), "Working out the trip…")
spec = lib._DEVICE_TOOLS["travel_time"]
check("read tier (never asks)", spec["risk"], "read")
check("its parameters", sorted(spec["parameters"]), ["arrive_by", "destination", "mode", "on"])
check("says where it runs (#201): the device asked from", spec["description"].startswith("ON THE DEVICE THE USER IS ASKING FROM (their iPhone or Mac)."), True)

turn(client=None, device_tools=None, device=None)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
