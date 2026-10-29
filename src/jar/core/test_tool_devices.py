#!/usr/bin/env python3
# gate: hermetic
"""The turn log's tool_devices (titan-node#1366): each device-tool call's platform (mac, iphone, ipad, watch, tv) next
to its tool name, from where the device hook sent it; never an argument, a result, a device id or a name, and nothing
outside that vocabulary. fetch_report and feature_counts count them by platform and tool for a person's own turns.
Throwaway turn log; a fake device hook. Fictional data.
Run: python3 scripts/test_tool_devices.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-tool-devices-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402
import feature_counts  # noqa: E402
import fetch_report  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


print("── device_platform: a fixed vocabulary ──")
check("paired platforms", [lib.device_platform(p) for p in ("ios", "ipados", "mac", "watchos", "tvos")],
      ["iphone", "ipad", "mac", "watch", "tv"])
check("no paired platform: the client's", (lib.device_platform(None, "iphone"), lib.device_platform("other", "mac")), ("iphone", "mac"))
check("neither says: None (never a guess)", (lib.device_platform(None, None), lib.device_platform("other", "")), (None, None))

print("── a turn's device calls, into the turn log ──")
SECRET_ARG, SECRET_RESULT, DEVICE_ID = "Robin's surprise party at Hale's", "Alex: the code is 4417", "dev-7f3a9c"
LOG = Path(os.environ["AGENT_TURN_LOG"])


def run_turn(calls):
    """calls: [(tool, args, ran_on)]. One logged turn; returns its line (text) and record."""
    lib._TURN_IO.source = "user"
    lib._TURN_INFO.info = {}
    for tool, args, ran_on in calls:
        lib._TURN_IO.device = (lambda req, r=ran_on: {"result": SECRET_RESULT, "ran_on": r, "device_id": DEVICE_ID,
                                                       "echo": json.dumps(req.get("args"))})
        lib._device_request(tool, args)
    LOG.write_text("")
    lib._log_turn(time.monotonic(), 1, None)
    line = LOG.read_text().strip().splitlines()[-1]
    lib._TURN_IO.device = lib._TURN_IO.source = None
    return line, json.loads(line)


line, rec = run_turn([("search_messages", {"query": SECRET_ARG}, "mac"),
                      ("list_calendar_events", {"start": "2026-10-10", "end": "2026-10-11", "title": SECRET_ARG}, "iphone")])
check("one [tool, platform] per call, in call order", rec.get("tool_devices"),
      [["search_messages", "mac"], ["list_calendar_events", "iphone"]])
check("no argument text, result text or device id anywhere in the line",
      [s for s in (SECRET_ARG, "surprise", SECRET_RESULT, "4417", DEVICE_ID, "Hale") if s in line], [])
line, rec = run_turn([("search_messages", {"query": SECRET_ARG}, "Robin's iPhone"),
                      ("search_messages", {"query": "x"}, None), ("search_messages", {"query": "y"}, SECRET_ARG)])
check("a platform outside the vocabulary (a name, nothing, an argument): no entry", ("tool_devices" in rec, SECRET_ARG in line,
      "Robin" in line), (False, False, False))
line, rec = run_turn([("search_messages", {"query": str(i)}, "iphone") for i in range(lib.TURN_LOG_MAX_TOOLS + 5)])
check("bounded like tool_names", len(rec["tool_devices"]), lib.TURN_LOG_MAX_TOOLS)

print("── the API's device hook says where it sent the call ──")
src = (Path(__file__).parent / "agent_api_server.py").read_text()
check("a phone turn's Mac tool: ran_on mac, set after the Mac's answer (the Mac's own keys can't override it)",
      '{**(_route_to_mac(turn_id, req, ident, remote) or {}), "ran_on": "mac"}' in src, True)
check("the asking device's own call: its platform, set after the app's answer",
      ('"ran_on": asker_platform}' in src, "asker_platform = _lib.device_platform((ident.device or {}).get(\"platform\"), client)" in src),
      (True, True))

print("── the reports: by platform and tool, a person's own turns only ──")
now = time.time()
rows = [{"ts": now - 60, "source": "user", "tool_devices": [["search_messages", "mac"], ["list_calendar_events", "iphone"],
                                                            ["list_calendar_events", "iphone"]]},
        {"ts": now - 50, "source": "eval", "tool_devices": [["search_messages", "iphone"]]},
        {"ts": now - 40, "source": "user", "pin": "x", "tool_devices": [["search_messages", "iphone"]]},
        {"ts": now - 30, "source": "user", "tool_devices": [["create_reminder", "Robin's iPhone"], ["create_reminder", "watch", "x"],
                                                            "junk", ["create_reminder", "watch"]]},
        {"ts": now - 3 * 86400, "source": "user", "tool_devices": [["search_messages", "tv"]]}]
TL = _TMP / "report-turns.jsonl"
TL.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
want = {"iphone list_calendar_events": 2, "mac search_messages": 1, "watch create_reminder": 1}
check("fetch_report.by_device: real turns in the window; eval, pinned, odd platforms and malformed entries skipped",
      fetch_report.by_device(TL.read_text().splitlines(), now - 86400, now + 1), want)
check("feature_counts.by_device: the same, keyed device/<platform>/<tool>",
      feature_counts.by_device(1.0, TL, now), {f"device/{k.split()[0]}/{k.split()[1]}": n for k, n in want.items()})
check("…an unreadable turn log: nothing", feature_counts.by_device(1.0, _TMP / "missing.jsonl", now), {})

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
