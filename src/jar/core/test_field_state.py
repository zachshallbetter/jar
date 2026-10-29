#!/usr/bin/env python3
"""_format_field_state turns an OSFieldScanner snapshot into the context lines.

Parsing only, with a synthetic snapshot (no Jellybean needed). The live round trip goes through
the turn's device hook and is covered by test_device_bridge.py; the SSH-era --live mode, which
spoke the stdout sentinel, was removed in #721.

    python3 scripts/test_field_state.py
"""
import sys
import json
import argparse
from pathlib import Path

_HERE = str(Path(__file__).parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from agent_runtime_lib import _format_field_state  # noqa: E402


# ---------------------------------------------------------------------------
# Mock snapshot — representative of what OSFieldScanner returns
# ---------------------------------------------------------------------------
MOCK_SNAPSHOT = {
    "snapshot_at": "2026-06-22T18:30:00Z",
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
            "dwell_today_min": 42,
            "activations_today": 7,
            "window_frame": {"x": 0, "y": 25, "w": 1440, "h": 875},
        },
        {
            "id": "com.apple.Terminal",
            "name": "Terminal",
            "kind": "app",
            "frontmost": False,
            "attention": 0.45,
            "memory": 0.30,
            "heat": 0.20,
            "density": 0.22,
            "dwell_today_min": 18,
            "activations_today": 3,
        },
        {
            "id": "evt-standup",
            "name": "Daily standup",
            "kind": "calendar_event",
            "heat": 0.9,
            "density": 0.65,
            "minutes_until": 12,
            "calendar": "Work",
        },
    ],
    "relations": [
        {
            "from": "com.apple.dt.Xcode",
            "to": "com.apple.Terminal",
            "kind": "co-session",
            "strength": 0.72,
            "memory": 0.18,
            "active": True,
        }
    ],
}


def run_mock() -> int:
    raw = json.dumps(MOCK_SNAPSHOT)
    print("── raw snapshot (truncated) ─────────────────────")
    print(raw[:300] + "…")
    print()
    print("── _format_field_state output ───────────────────")
    formatted = _format_field_state(raw)
    print(formatted or "(empty)")
    print()

    # Assertions
    failures = []

    def check(label: str, cond: bool) -> None:
        if cond:
            print(f"  ✓ {label}")
        else:
            print(f"  ✗ {label}")
            failures.append(label)

    check("frontmost app present", "Xcode" in formatted)
    check("dwell time present", "42m today" in formatted)
    check("window frame present", "1440×875" in formatted)
    check("recent app: Terminal", "Terminal" in formatted)
    check("calendar event present", "standup" in formatted)
    check("event timing: in 12m", "12m" in formatted)
    check("relations field in raw", len(MOCK_SNAPSHOT["relations"]) > 0)
    check("edge has memory field", MOCK_SNAPSHOT["relations"][0]["memory"] >= 0)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("All mock checks passed.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="accepted for old callers; the mock is the only mode")
    parser.parse_args()
    sys.exit(run_mock())
