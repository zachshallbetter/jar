#!/usr/bin/env python3
"""Unit tests for agent_runtime_lib — parser, tool registry, compaction, execute_tool.

Run directly:   python3 scripts/test_agent.py
Via agent tool: self_test  (no args — agent calls this automatically)
"""
import sys
from pathlib import Path

_HERE = str(Path(__file__).parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _run_suite() -> tuple[int, int, list[str]]:
    from agent_runtime_lib import (  # noqa: PLC0415
        parse_tool_calls,
        strip_tool_noise,
        _compact_messages,
        build_tool_system_prompt,
        execute_tool,
        _SAFE_STEP_TOOLS,
        _DEVICE_TOOLS,
        _TOOL_REGISTRY,
        _format_field_state,
    )

    lines: list[str] = []
    p = f = 0

    def ok(name: str) -> None:
        nonlocal p
        lines.append(f"  ✓ {name}")
        p += 1

    def fail(name: str, detail: object = None) -> None:
        nonlocal f
        lines.append(f"  ✗ {name}: {detail!r}")
        f += 1

    def check(name: str, cond: bool, detail: object = None) -> None:
        (ok if cond else lambda n: fail(n, detail))(name)

    # ── parse_tool_calls ──────────────────────────────────────────────────────
    lines.append("parse_tool_calls")

    r = parse_tool_calls('<tool_call>{"tool": "python_eval", "args": {"expression": "1+1"}}</tool_call>')
    check("standard <tool_call>",
          r == [{"tool": "python_eval", "args": {"expression": "1+1"}}], r)

    r = parse_tool_calls('<tool_code>print(default_api.find_files(query="jellybean"))</tool_code>')
    check("fallback: default_api prefix",
          len(r) == 1 and r[0]["tool"] == "find_files" and r[0]["args"].get("query") == "jellybean", r)

    r = parse_tool_calls('<tool_code>print(find_files(query="x", folder="~/Projects"))</tool_code>')
    check("fallback: no prefix, multi-arg",
          len(r) == 1 and r[0]["tool"] == "find_files" and r[0]["args"].get("folder") == "~/Projects", r)

    check("plain text → empty list", parse_tool_calls("just some text") == [], None)
    check("empty string → empty list", parse_tool_calls("") == [], None)

    # ── strip_tool_noise ──────────────────────────────────────────────────────
    lines.append("strip_tool_noise")

    check("<tool_call> removed",
          strip_tool_noise("before <tool_call>{}</tool_call> after") == "before  after", None)
    check("<tool_code> removed",
          strip_tool_noise("ok <tool_code>print(x)</tool_code> done") == "ok  done", None)
    check("clean text passes through",
          strip_tool_noise("clean response") == "clean response", None)
    check("empty string ok",
          strip_tool_noise("") == "", None)

    # ── _compact_messages ─────────────────────────────────────────────────────
    lines.append("_compact_messages")

    # Compaction triggers on total size (_COMPACT_MAX_CHARS), not message count, and summarises the
    # dropped turns with an LLM — stub that out so the test is offline and deterministic.
    import agent_runtime_lib as _lib
    sys_m = {"role": "system", "content": "you are an agent"}
    big = "x" * (_lib._COMPACT_MAX_CHARS // 20)
    conv = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} {big}"} for i in range(25)]
    full = [sys_m] + conv

    _orig_summ = _lib._summarize_turns
    _lib._summarize_turns = lambda _msgs: ""
    try:
        c = _compact_messages(full)
    finally:
        _lib._summarize_turns = _orig_summ
    non_sys = [m for m in c if m["role"] != "system"]
    sys_msgs = [m for m in c if m["role"] == "system"]

    check("keeps last 10 non-system messages", len(non_sys) == 10, len(non_sys))
    check("original system message preserved",
          any(m["content"] == "you are an agent" for m in sys_msgs), None)
    check("compaction note inserted",
          any("compacted" in m.get("content", "").lower() for m in sys_msgs), sys_msgs)
    check("recent message is last conv msg",
          non_sys[-1]["content"] == conv[-1]["content"], non_sys[-1]["content"])

    short = [sys_m] + conv[:5]
    check("no-op under threshold", _compact_messages(short) == short, None)

    # ── build_tool_system_prompt ──────────────────────────────────────────────
    lines.append("build_tool_system_prompt")

    prompt = build_tool_system_prompt()
    check("non-empty", len(prompt) > 200, len(prompt))
    for kw in ["<tool_call>", "run_shell", "- **files**:", "python_eval"]:   # #1251 C: find_files is a files mode
        check(f"mentions {kw!r}", kw in prompt, None)

    # With allowed subset — only subset tools should appear in structured output section.
    sub_prompt = build_tool_system_prompt({"python_eval", "get_datetime"})
    check("subset prompt non-empty", len(sub_prompt) > 50, len(sub_prompt))

    # ── execute_tool (host-only, no network) ──────────────────────────────────
    lines.append("execute_tool")

    check("python_eval: 1+1 == '2'",
          execute_tool("python_eval", {"expression": "1+1"}) == "2", None)
    check("python_eval: len('hi') == '2'",
          execute_tool("python_eval", {"expression": "len('hi')"}) == "2", None)
    check("python_eval: missing expr → error",
          "error" in execute_tool("python_eval", {}).lower(), None)
    check("python_eval: forbidden import → error",
          "error" in execute_tool("python_eval", {"expression": "import os"}).lower(), None)
    check("count_chars: mentions 'total'",
          "total" in execute_tool("count_chars", {"text": "hello"}).lower(), None)
    dt = execute_tool("get_datetime", {})
    check("get_datetime: returns non-empty string",
          isinstance(dt, str) and len(dt) > 5, dt)
    check("unknown tool: graceful error",
          "not" in execute_tool("nonexistent_tool_xyz", {}).lower()
          or "unknown" in execute_tool("nonexistent_tool_xyz", {}).lower()
          or "error" in execute_tool("nonexistent_tool_xyz", {}).lower(), None)

    # ── _SAFE_STEP_TOOLS policy ────────────────────────────────────────────────
    lines.append("_SAFE_STEP_TOOLS")

    for t in ["get_screen", "ax_read", "search_contacts", "get_frontmost_app",
              "get_field_state", "kb_retrieve", "web_search", "file_read", "file_list",
              "python_eval", "epistemic_query"]:
        check(f"allowed: {t}", t in _SAFE_STEP_TOOLS, None)

    for t in ["run_shell", "send_message", "write_file", "run_applescript",
              "ax_click", "mouse_click", "create_skill", "ax_type"]:
        check(f"blocked: {t}", t not in _SAFE_STEP_TOOLS, None)

    # ── _DEVICE_TOOLS completeness + risk levels ───────────────────────────────
    lines.append("_DEVICE_TOOLS")

    for t in ["get_screen", "ax_read", "ax_click", "ax_type", "ax_focus", "mouse_click",
              "search_contacts", "create_contact", "send_message", "draft_email",
              "undo_last_action", "find_files", "read_file", "write_file",
              "run_shell", "run_applescript", "get_frontmost_app", "get_running_apps",
              "get_field_state", "create_skill", "run_skill", "list_skills"]:
        check(f"declared: {t}", t in _DEVICE_TOOLS, None)

    for tool, expected in {
        "get_screen": "read", "ax_read": "read", "search_contacts": "read",
        "get_field_state": "read",
        "ax_click": "write",  "mouse_click": "write", "send_message": "write",
        "run_shell": "dangerous", "run_applescript": "dangerous", "write_file": "dangerous",
    }.items():
        actual = _DEVICE_TOOLS.get(tool, {}).get("risk", "MISSING")
        check(f"risk {tool}={expected}", actual == expected, actual)

    # ── _format_field_state ───────────────────────────────────────────────────
    lines.append("_format_field_state")

    _SAMPLE_FIELD_STATE = {
        "snapshot_at": "2026-06-22T18:00:00Z",
        "attention_centroid": "com.apple.dt.Xcode",
        "bodies": [
            {"id": "com.apple.dt.Xcode", "name": "Xcode", "kind": "app",
             "frontmost": True, "attention": 1.0, "memory": 0.82, "heat": 0.54,
             "density": 0.71, "dwell_today_min": 42, "activations_today": 7,
             "window_frame": {"x": 0, "y": 25, "w": 1440, "h": 875}},
            {"id": "com.apple.Terminal", "name": "Terminal", "kind": "app",
             "frontmost": False, "attention": 0.45, "memory": 0.3, "heat": 0.2,
             "density": 0.22, "dwell_today_min": 18, "activations_today": 3},
            {"id": "com.figma.Desktop", "name": "Figma", "kind": "app",
             "frontmost": False, "attention": 0.08, "memory": 0.1, "heat": 0.05,
             "density": 0.05, "dwell_today_min": 5, "activations_today": 1},
            {"id": "evt-abc", "name": "Team sync", "kind": "calendar_event",
             "heat": 0.85, "density": 0.6, "minutes_until": 38, "calendar": "Work"},
        ],
        "relations": [
            {"from": "com.apple.dt.Xcode", "to": "com.apple.Terminal",
             "kind": "co-session", "strength": 0.72, "memory": 0.18, "active": True}
        ]
    }
    import json as _json
    _raw = _json.dumps(_SAMPLE_FIELD_STATE)
    _fmt = _format_field_state(_raw)

    check("frontmost app line", "Xcode" in _fmt and "42m today" in _fmt, _fmt)
    check("window frame in frontmost line", "1440×875" in _fmt, _fmt)
    check("recent apps list", "Terminal" in _fmt, _fmt)
    check("no window title from older clients", "window" not in _fmt.split("[")[0], _fmt)
    _with_title = _json.loads(_raw)
    _with_title["bodies"][0].update({"window_title": "AgentHTTPClient.swift — jellybean", "document": "~/Projects/jellybean/mac/Sources/Jellybean/AgentHTTPClient.swift"})
    _fmt2 = _format_field_state(_json.dumps(_with_title))
    check("window title + file in frontmost line",
          "window ⟦untrusted source=screen" in _fmt2 and "AgentHTTPClient.swift — jellybean⟦/untrusted" in _fmt2
          and "file ~/Projects/jellybean/mac" in _fmt2, _fmt2)   # the title is screen text (#473)
    check("low-attention app excluded from recent", "Figma" not in _fmt, _fmt)
    check("upcoming event ≤120m included", "Team sync" in _fmt, _fmt)
    check("event timing format (in 38m)", "38m" in _fmt, _fmt)
    check("invalid json → passthrough", _format_field_state("not json") == "not json", None)
    check("empty bodies → empty string", _format_field_state('{"bodies":[]}') == "", None)

    # ── _TOOL_REGISTRY completeness ───────────────────────────────────────────
    lines.append("_TOOL_REGISTRY")

    for t in ["python_eval", "count_chars", "get_datetime", "web_search",
              "kb_retrieve", "kb_list", "file_read", "file_write", "file_edit",
              "plan_task", "run_task", "epistemic_query", "self_test"]:
        check(f"declared: {t}", t in _TOOL_REGISTRY, None)

    return p, f, lines


def run_self_test() -> str:
    """Run the test suite and return a formatted text report (for the self_test tool)."""
    try:
        p, f, lines = _run_suite()
    except Exception as exc:
        return f"Test suite failed to load: {exc}"
    status = "All tests passed." if f == 0 else f"{f} test(s) FAILED."
    bar = "─" * 42
    return "\n".join([f"Agent self-test — {p} passed, {f} failed", bar] + lines + [bar, status])


if __name__ == "__main__":
    try:
        p, f, lines = _run_suite()
    except Exception as exc:
        print(f"Test suite failed: {exc}")
        sys.exit(1)
    bar = "─" * 42
    print("\n".join([f"Agent self-test — {p} passed, {f} failed", bar] + lines + [bar,
          "All tests passed." if f == 0 else f"{f} test(s) FAILED."]))
    sys.exit(0 if f == 0 else 1)
