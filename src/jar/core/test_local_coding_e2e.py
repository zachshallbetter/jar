#!/usr/bin/env python3
"""E2E: local brain + tool-enabled coding/math on the live Qwen3-8B (:8010).

Not in the deploy gate: needs titan-local-agent. Run on titan with agent.env sourced:
  set -a; . /ai/projects/titan-local-agent/agent.env; set +a
  python3 scripts/test_local_coding_e2e.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ["AGENT_TOOL_BRAIN"] = "local"
os.environ["AGENT_TURN_SOURCE"] = "eval"

import agent_runtime_lib as lib  # noqa: E402

checks: list[tuple[str, bool, str]] = []
ok = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok
    ok = ok and bool(cond)
    checks.append((name, bool(cond), detail))


def main() -> int:
    msgs = [
        {"role": "system", "content": lib.build_tool_system_prompt({"python_eval", "count_chars"})},
        {
            "role": "user",
            "content": (
                "Use python_eval to compute 17*19+3. Then use count_chars on the exact text "
                "Hello, Titan. Reply with two lines: MATH=<n> CHARS=<n> and nothing else."
            ),
        },
    ]
    text = lib._run_agent_turn(msgs, allowed_tools={"python_eval", "count_chars"}, max_rounds=5)
    info = lib.last_turn_info()
    print("REPLY:", repr(text))
    print("BRAIN:", info.get("brain"))
    print("FALLBACK:", info.get("fallback"))
    print("TOOLS:", info.get("tool_names"))
    print("RAN:", info.get("ran"))

    direct_math = lib.execute_tool("python_eval", {"code": "17*19+3"})
    direct_chars = lib.execute_tool("count_chars", {"text": "Hello, Titan"})
    print("DIRECT_MATH:", direct_math)
    print("DIRECT_CHARS:", direct_chars)

    check("brain_local", info.get("brain") == "local", str(info.get("brain")))
    check("no_fallback", not info.get("fallback"), str(info.get("fallback")))
    ran = list(info.get("ran") or []) + list(info.get("tool_names") or [])
    check("used_python_eval", any("python_eval" in str(x) for x in ran), str(ran))
    check("used_count_chars", any("count_chars" in str(x) for x in ran), str(ran))
    check("math_326", "326" in (text or ""), str(text))
    m = re.search(r"(\d+)", str(direct_chars))
    num = m.group(1) if m else None
    check(
        "chars_matches_tool",
        num is not None and num in (text or ""),
        f"tool={direct_chars!r} reply={text!r}",
    )

    # Second turn: small coding with optional python_eval verify
    msgs2 = [
        {"role": "system", "content": lib.build_tool_system_prompt({"python_eval"})},
        {
            "role": "user",
            "content": (
                "Write a Python function add(a, b) that returns a+b. "
                "Then use python_eval to call add(2, 40) and reply with only the number."
            ),
        },
    ]
    text2 = lib._run_agent_turn(msgs2, allowed_tools={"python_eval"}, max_rounds=5)
    info2 = lib.last_turn_info()
    print("CODING_REPLY:", repr(text2))
    print("CODING_BRAIN:", info2.get("brain"))
    print("CODING_TOOLS:", info2.get("tool_names") or info2.get("ran"))
    check("coding_brain_local", info2.get("brain") == "local", str(info2.get("brain")))
    ran2 = list(info2.get("ran") or []) + list(info2.get("tool_names") or [])
    check("coding_used_python_eval", any("python_eval" in str(x) for x in ran2), str(ran2))
    check("coding_result_42", "42" in (text2 or ""), str(text2))

    for name, passed, detail in checks:
        mark = "PASS" if passed else "FAIL"
        extra = f": {detail}" if detail else ""
        print(f"[{mark}] {name}{extra}")
    print("OVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
