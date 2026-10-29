#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""The iPhone's Music tools (Jellybean iOS: play_music, music_control, now_playing).

Offered on a phone turn whose app reports them, never on a Mac turn, never routed to the Mac;
every role may use them; they say they're the phone's, not the TVs'. The result strings are the
phone's (Jellybean's MusicToolTests).
Run: python3 scripts/test_phone_music.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-music-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0
MUSIC = ("play_music", "music_control", "now_playing")


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


def turn(client=None, device_tools=None, remote=None):
    io = lib._TURN_IO
    io.client, io.device_tools, io.remote, io.role, io.device = client, device_tools, remote, None, object()


phone_now = lib.device_tools_for_client("iphone", sorted(lib.PHONE_DEVICE_TOOLS))
old_phone = lib.device_tools_for_client("iphone", sorted(lib.PHONE_DEVICE_TOOLS - set(MUSIC)))

print("── who's offered the music tools ──")
SHARED = ("music_control", "now_playing")   # the Mac's Apple Music too since jellybean#321 (Zach 10-02: "Music only")
for t in MUSIC:
    check(f"{t}: phone only unless shared with the Mac", t in lib.PHONE_ONLY_TOOLS, t not in SHARED)
    check(f"{t}: a current phone gets it", t in phone_now, True)
check("play_music is never routed to the Mac", "play_music" in lib.mac_tools_for_phone(old_phone), False)
turn(client="iphone", device_tools=phone_now)
check("a phone turn lists all three", all(t in lib._turn_device_tools() for t in MUSIC), True)
check("…in the iPhone's words", all(lib._turn_device_tools()[t]["description"].startswith("ON THE USER'S IPHONE.")
                                     for t in MUSIC), True)
turn(client=None, device_tools=None)
check("a Mac turn lists the shared two, not play_music", (all(t in lib._turn_device_tools() for t in SHARED),
      "play_music" in lib._turn_device_tools()), (True, False))
out = lib._execute_tool("play_music", {"query": "Kind of Blue"})
check("a Mac turn that calls it is told to ask from the iPhone", "ask from their iPhone" in out, True)

print("── words and roles ──")
for t in MUSIC:
    d = lib._DEVICE_TOOLS[t]["description"]
    check(f"{t}: says where it runs", d.startswith("ON THE USER'S IPHONE.") or "this device" in d, True)
    check(f"{t}: not the TVs", "TVs" in d, True)
    check(f"{t}: every role", all(hh.role_allows_tool(r, t) for r in ("adult", "kid")), True)
    check(f"{t}: a step label", bool(lib._TOOL_STATUS.get(t)), True)
check("now_playing is a read", lib._DEVICE_TOOLS["now_playing"]["risk"], "read")
check("play_music's parameters", sorted(lib._DEVICE_TOOLS["play_music"]["parameters"]), ["kind", "query", "shuffle"])

turn(client=None, device_tools=None)
lib._TURN_IO.device = None
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
