#!/usr/bin/env python3
# gate: hermetic   (gateway HTTP faked; tools recorded; nothing leaves the machine)
"""Vercel AI Gateway brain: OpenAI tool loop, Gemini→gateway fallback, gateway pin.

Run: python3 scripts/test_ai_gateway.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-aigw-"))
for k, v in {
    "HOUSEHOLD_DB": "household.db",
    "AGENT_MEMORY_DIR": "memory",
    "AGENT_TURN_LOG": "turns.jsonl",
    "PROACTIVE_DB": "proactive.db",
}.items():
    os.environ[k] = str(_TMP / v)
os.environ["AGENT_TURN_SOURCE"] = "eval"
os.environ["AI_GATEWAY_API_KEY"] = "test-gateway-key"
os.environ["AI_GATEWAY_MODEL"] = "google/gemini-2.5-flash"
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── config ──")
check("ai_gateway_configured with key", lib.ai_gateway_configured(), True)
os.environ.pop("AI_GATEWAY_API_KEY", None)
check("…without key", lib.ai_gateway_configured(), False)
os.environ["AI_GATEWAY_API_KEY"] = "test-gateway-key"

print("── OpenAI tool schema ──")
tools = lib._openai_function_tools({"count_chars", "python_eval"})
names = sorted(t["function"]["name"] for t in tools)
check("openai tools for the allowed set", names, ["count_chars", "python_eval"])
check("each tool is type=function", all(t.get("type") == "function" for t in tools), True)

print("── tool loop (HTTP faked) ──")
RAN: list[tuple[str, dict]] = []
ROUND = {"n": 0}


class FakeHTTP:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def fake_urlopen(req, timeout=None):
    url = getattr(req, "full_url", "") or str(req)
    if "ai-gateway.vercel.sh" not in url and "chat/completions" not in url:
        raise AssertionError(f"unexpected url {url}")
    headers = {k.lower(): v for k, v in req.header_items()}
    if not str(headers.get("authorization", "")).startswith("Bearer "):
        raise AssertionError("missing bearer auth")
    body = json.loads(req.data.decode() if req.data else b"{}")
    ROUND["n"] += 1
    if ROUND["n"] == 1:
        # Ask for count_chars
        payload = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "count_chars",
                            "arguments": json.dumps({"text": "canary bird"}),
                        },
                    }],
                }
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    else:
        payload = {
            "choices": [{
                "message": {"role": "assistant", "content": "10"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 2},
        }
    return FakeHTTP(json.dumps(payload).encode())


real_urlopen = urllib.request.urlopen
real_exec = lib.execute_tool
urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
lib.execute_tool = lambda name, args: RAN.append((name, dict(args))) or "Total letters (excluding spaces): 10."
lib._TURN_IO.role, lib._TURN_IO.person = "admin", {"id": "alex", "name": "Alex"}
try:
    ROUND["n"] = 0
    RAN.clear()
    out = lib._run_ai_gateway_agent(
        [{"role": "user", "content": "Count letters in canary bird."}],
        max_rounds=3,
        allowed_tools={"count_chars"},
    )
    check("gateway tool loop answer", out.strip(), "10")
    check("count_chars ran once", [n for n, _ in RAN], ["count_chars"])
finally:
    urllib.request.urlopen = real_urlopen
    lib.execute_tool = real_exec
    lib._TURN_IO.role = lib._TURN_IO.person = None

print("── turn routing: gateway pin ──")
os.environ["AGENT_TOOL_BRAIN"] = "gateway"
lib._run_ai_gateway_agent = lambda messages, max_rounds=6, model=None, allowed_tools=None: "gateway-ok"  # type: ignore
lib._run_local_agent = lambda *a, **k: "local-ok"  # type: ignore
lib._run_gemini_agent = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gemini should not run"))  # type: ignore
lib._with_situation = lambda m: m
lib._with_schedule_digest_gemini = lambda m: m
lib._note_context_sizes = lambda m: None
try:
    text = lib._run_agent_turn([{"role": "user", "content": "hi"}])
    info = lib.last_turn_info()
    check("AGENT_TOOL_BRAIN=gateway uses ai_gateway", (text, info.get("brain")), ("gateway-ok", "ai_gateway"))
finally:
    # restore by reimport is heavy; next tests set their own stubs
    pass

print("── turn routing: Gemini fail → gateway ──")
os.environ["AGENT_TOOL_BRAIN"] = "gemini"
os.environ["GEMINI_API_KEY"] = "fake-gemini"
calls = {"gemini": 0, "gateway": 0}


def boom_gemini(*a, **k):
    calls["gemini"] += 1
    raise RuntimeError("gemini down")


def ok_gateway(*a, **k):
    calls["gateway"] += 1
    return "from-gateway"


lib._run_gemini_agent = boom_gemini  # type: ignore
lib._run_ai_gateway_agent = ok_gateway  # type: ignore
lib._run_local_agent = lambda *a, **k: "from-local"  # type: ignore
try:
    text = lib._run_agent_turn([{"role": "user", "content": "hello"}])
    info = lib.last_turn_info()
    check("gemini failure falls through to gateway", (text, info.get("brain"), calls["gemini"] >= 1, calls["gateway"] >= 1),
          ("from-gateway", "ai_gateway", True, True))
    check("failover recorded", "gemini failed" in str(info.get("failover_from") or info.get("fallback") or ""), True)
finally:
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ["AGENT_TOOL_BRAIN"] = "local"

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
