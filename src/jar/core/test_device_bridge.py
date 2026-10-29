#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""The device bridge feeds the turn's context: _situational_context() asks the device for
get_field_state through the turn's device hook (_TURN_IO.device, the HTTP API's channel since
jellybean#474) and folds the snapshot into the context text.

The hook here plays Jellybean: it answers get_field_state with a synthetic OSFieldScanner
snapshot and records every tool it is asked for. (The SSH-era stdout sentinel path this test
used to drive was removed in #721.)

Run:
  python3 scripts/test_device_bridge.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402

# A snapshot representative of what the new OSFieldScanner returns.
SIM_SNAPSHOT = {
    "snapshot_at": "2026-06-22T19:00:00Z",
    "attention_centroid": "com.apple.dt.Xcode",
    "bodies": [
        {
            "id": "com.apple.dt.Xcode",
            "name": "Xcode",
            "kind": "app",
            "frontmost": True,
            "attention": 1.0,
            "memory": 0.82,
            "heat": 0.54,
            "density": 0.71,
            "dwell_today_min": 44,
            "activations_today": 8,
            "window_frame": {"x": 0, "y": 25, "w": 1440, "h": 875},
        },
        {
            "id": "com.apple.Terminal",
            "name": "Terminal",
            "kind": "app",
            "frontmost": False,
            "attention": 0.43,
            "memory": 0.31,
            "heat": 0.21,
            "density": 0.19,
            "dwell_today_min": 19,
            "activations_today": 3,
        },
        {
            "id": "evt-standup",
            "name": "Daily standup",
            "kind": "calendar_event",
            "heat": 0.88,
            "density": 0.61,
            "minutes_until": 8,
            "calendar": "Work",
        },
    ],
    "relations": [
        {
            "from": "com.apple.dt.Xcode",
            "to": "com.apple.Terminal",
            "kind": "co-session",
            "strength": 0.74,
            "memory": 0.21,
            "active": True,
        }
    ],
}


def run_sim(snapshot=None) -> int:
    snapshot = snapshot or SIM_SNAPSHOT
    calls_seen: list[str] = []

    def device(req: dict) -> dict:
        tool = req.get("tool", "unknown")
        calls_seen.append(tool)
        if tool == "get_field_state":
            return {"result": json.dumps(snapshot)}
        return {"result": f"(stub response for {tool})"}

    lib._TURN_IO.device = device
    try:
        ctx_result = lib._situational_context()
    finally:
        lib._TURN_IO.device = None

    print("── _situational_context() output ─────────────────")
    print(ctx_result or "(empty)")
    print()

    failures = []

    def check(label: str, cond: bool, detail: object = None) -> None:
        if cond:
            print(f"  ✓ {label}")
        else:
            print(f"  ✗ {label}" + (f": {detail!r}" if detail is not None else ""))
            failures.append(label)

    check("get_field_state was called through the device hook", "get_field_state" in calls_seen, calls_seen)
    check("context result non-empty", bool(ctx_result))
    if ctx_result:
        check("active app in context", "Xcode" in ctx_result)
        check("dwell time in context", "44m today" in ctx_result)
        check("window frame in context", "1440×875" in ctx_result)
        check("recent apps in context", "Terminal" in ctx_result)
        check("calendar event in context", "standup" in ctx_result.lower())
        check("event timing in context", "8m" in ctx_result)
    check("no device hook, no device call", (lib._device_bridge_enabled(), calls_seen.count("get_field_state")), (False, 1))

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("All device-bridge checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run_sim())
