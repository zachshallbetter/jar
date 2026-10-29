#!/usr/bin/env python3
"""
Unit tests for what the agent knows about Titan itself: the host_status tool, the per-turn
"Services up/down" line (a runtime that serves only /v1/health is still up), and test turns
kept off the real household timeline. Hermetic: HTTP and the event runtime are faked.

Run: python3 scripts/test_host_awareness.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import agent_runtime_lib as lib


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def run():
    ok = True

    print("\n── host_status ──")
    out = lib.execute_tool("host_status", {})
    for section in ("disks:", "failed services:", "preflight:", "agent canary:"):
        ok &= check(f"has {section!r}", section in out, True)
    ok &= check("registered as a tool", "host_status" in lib._TOOL_REGISTRY, True)
    ok &= check("allowed in autonomous steps", "host_status" in lib._SAFE_STEP_TOOLS, True)

    print("\n── services line: /v1/health counts as up ──")
    orig_http, orig_req, orig_bases = lib._http_json, lib._runtime_request, dict(lib._RUNTIME_BASES)
    only_v1 = {"epistemic"}

    def fake_http(url, timeout=None, **kw):
        svc = next(s for s, b in lib._RUNTIME_BASES.items() if url.startswith(b))
        if svc in only_v1 and url.endswith("/health") and not url.endswith("/v1/health"):
            raise RuntimeError("404")
        if svc == "workflow":
            raise RuntimeError("connection refused")
        return {"status": "ok"}

    try:
        lib._http_json = fake_http
        lib._runtime_request = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no event runtime"))
        for s in lib._SITUATION_RUNTIMES:
            lib._RUNTIME_BASES.setdefault(s, f"http://fake-{s}")
        ctx = lib._situational_context(None)
        up = next((ln for ln in ctx.splitlines() if ln.startswith("Services up:")), "")
        down = next((ln for ln in ctx.splitlines() if ln.startswith("Services down:")), "")
        ok &= check("epistemic (v1-only) isn't reported down", "epistemic" in down, False)
        ok &= check("a refusing service is down", down, "Services down: workflow")
        ok &= check("healthy services aren't listed (they got read back as small talk)", up, "")
    finally:
        lib._http_json, lib._runtime_request = orig_http, orig_req
        lib._RUNTIME_BASES.clear()
        lib._RUNTIME_BASES.update(orig_bases)

    print("\n── recent household activity: last 48 hours only ──")
    import time as _time
    from datetime import datetime, timezone
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    orig_tl, orig_http2 = lib._timeline_events, lib._http_json
    try:
        lib._http_json = lambda *a, **k: {"status": "ok"}
        lib._timeline_events = lambda stream="household-timeline": [
            {"event_id": "old", "timestamp": iso(_time.time() - 5 * 86400), "payload": {"text": "ancient test reply"}},
            {"event_id": "new", "timestamp": iso(_time.time() - 3600), "payload": {"text": "bins went out"}}]
        ctx = lib._situational_context_inner(None)
        ok &= check("only the past 48 h", ("bins went out" in ctx, "ancient test reply" in ctx), (True, False))
    finally:
        lib._timeline_events, lib._http_json = orig_tl, orig_http2

    print("\n── test turns stay off the household timeline ──")
    posted = []
    orig_src = os.environ.get("AGENT_TURN_SOURCE")
    orig_daily = lib._append_daily_log
    try:
        # The daily-log connector writes real files and the connectors ledger; keep it out of this
        # test (the deploy gate ran it on titan and wrote a test line into the real daily log).
        lib._append_daily_log = lambda *a, **k: None
        lib._runtime_request = lambda *a, **k: posted.append(a) or {}
        for src, want_posts in (("eval", 0), ("canary", 0), ("user", 1)):
            posted.clear()
            os.environ["AGENT_TURN_SOURCE"] = src
            lib.execute_tool("log_event", {"event": "Changed the furnace filter"})
            ok &= check(f"source={src} writes", len(posted), want_posts)
    finally:
        lib._runtime_request = orig_req
        lib._append_daily_log = orig_daily
        if orig_src is None:
            os.environ.pop("AGENT_TURN_SOURCE", None)
        else:
            os.environ["AGENT_TURN_SOURCE"] = orig_src

    print("\n── HTTP device channel (per-turn hooks) ──")
    seen, labels = [], []
    lib._TURN_IO.device = lambda req: seen.append(req) or {"result": "Safari"}
    lib._TURN_IO.status = labels.append
    try:
        ok &= check("bridge on with a device hook", lib._device_bridge_enabled(), True)
        ok &= check("device result returned", lib._device_request("get_frontmost_app", {}), "Safari")
        ok &= check("request carries tool and risk", (seen[0]["tool"], seen[0]["risk"]),
                    ("get_frontmost_app", lib._DEVICE_TOOLS["get_frontmost_app"]["risk"]))
        lib._TURN_IO.device = lambda req: {"error": "Declined"}
        ok &= check("device error reported", lib._device_request("create_reminder", {}),
                    "Device action was not completed: Declined")
        lib._emit_status("kb_retrieve")
        ok &= check("status goes to the hook", labels, ["Searching the knowledge base…"])
    finally:
        lib._TURN_IO.device = None
        lib._TURN_IO.status = None
    ok &= check("bridge off without a hook", lib._device_bridge_enabled(), False)
    ok &= check("no hook, no device: a device call says so",
                lib._device_request_inner("get_frontmost_app", {}).startswith("No device is connected"), True)

    print("\n── relevant KB memory reaches the turn (signed request) ──")
    calls = []
    orig_req, orig_http = lib._runtime_request, lib._http_json
    try:
        lib._http_json = lambda url, **kw: {"status": "ok"}
        def fake_req(svc, path, method="POST", payload=None, timeout=30):
            calls.append((svc, path))
            if svc == "retrieval":
                return {"results": [{"source_id": "note-titan-services", "chunk_text": "Restart with sudo systemctl restart titan-embeddings."}]}
            raise RuntimeError("no event runtime")
        lib._runtime_request = fake_req
        for s in lib._SITUATION_RUNTIMES:
            lib._RUNTIME_BASES.setdefault(s, f"http://fake-{s}")
        ctx = lib._situational_context("How do I restart the embeddings service?")
        ok &= check("retrieval called through the signed path", ("retrieval", "/v1/retrieval/query") in calls, True)
        ok &= check("memory injected with its source", "[note-titan-services] Restart with sudo systemctl restart titan-embeddings." in ctx, True)
    finally:
        lib._runtime_request, lib._http_json = orig_req, orig_http

    print("\n── Mac images: view_image via internal fetch_image ──")
    import json as _json
    seen, sent = [], []
    lib._TURN_IO.device = lambda req: seen.append(req) or {"result": _json.dumps(
        {"mime": "image/jpeg", "width": 800, "height": 600, "data_base64": "QUJD"})}
    orig_gem = lib._gemini
    try:
        lib._gemini = lambda model, parts, **kw: sent.append(parts) or {"candidates": [{"content": {"parts": [{"text": "A receipt from PG&E."}]}}]}
        prompt = lib.build_tool_system_prompt()
        ok &= check("fetch_image not advertised", "fetch_image" in prompt, False)
        ok &= check("view_image advertised", "view_image" in prompt, True)
        ok &= check("fetch_image can't be called directly", lib.execute_tool("fetch_image", {"path": "~/a.png"}).startswith("fetch_image is internal"), True)
        out = lib.execute_tool("view_image", {"path": "~/Downloads/receipt.jpg", "question": "Who is it from?"})
        ok &= check("view_image answers", out.startswith("A receipt from PG&E."), True)
        ok &= check("device asked for the image", (seen[-1]["tool"], seen[-1]["args"]["path"]), ("fetch_image", "~/Downloads/receipt.jpg"))
        ok &= check("image data went to vision, not into the reply", ("QUJD" in out, sent[-1][0]["inline_data"]["data"]), (False, "QUJD"))
        lib._TURN_IO.device = lambda req: {"error": "File not found"}
        ok &= check("device error surfaces", "File not found" in lib.execute_tool("view_image", {"path": "~/nope.png"}), True)
    finally:
        lib._gemini = orig_gem
        lib._TURN_IO.device = None

    print("\n── dev activity kept out of household context ──")
    orig_req, orig_http = lib._runtime_request, lib._http_json
    try:
        lib._http_json = lambda url, **kw: {"status": "ok"}
        def fake_req(svc, path, method="POST", payload=None, timeout=30):
            if path.endswith("/household-timeline"):
                return {"events": [{"event_id": "h1", "event_type": "household.note", "payload": {"text": "Changed the furnace filter"}}]}
            if path.endswith("/dev-activity"):
                return {"events": [{"event_id": "d1", "event_type": "household.note", "payload": {"text": "titan-node abc123: refactor"}}]}
            return {"results": []}
        lib._runtime_request = fake_req
        home = lib._situational_context("When did I last change the furnace filter?")
        code = lib._situational_context("What changed in the titan repo today?")
        ok &= check("household question: no dev lines", ("Recent dev activity" in home, "furnace filter" in home), (False, True))
        ok &= check("code question: dev lines included", "titan-node abc123: refactor" in code, True)
        ok &= check("recall_events which=dev reads the dev stream",
                    "titan-node abc123" in lib.execute_tool("recall_events", {"which": "dev"}), True)
    finally:
        lib._runtime_request, lib._http_json = orig_req, orig_http
    import proactive as pa, tempfile as _tf
    posted = []
    orig_req = lib._runtime_request
    try:
        lib._runtime_request = lambda svc, path, method="POST", payload=None, timeout=30: posted.append(payload.get("stream_key")) or {}
        dbp = __import__("pathlib").Path(_tf.mkdtemp()) / "p.db"
        pa.submit_trigger({"source": "git", "summary": "repo abc: change"}, triage=lambda e: {"actionable": {"probabilities": {"no": 0.1}}, "urgency": {"score": 1}},
                          agent=lambda p: "PASSIVE\nok", db=dbp, background=False)
        ok &= check("git results go to the dev stream", posted, ["dev-activity"])
    finally:
        lib._runtime_request = orig_req

    print("\n── no Plex (#1625): the Mac is never asked for it, media questions included ──")
    calls = []
    orig_bridge, orig_dev = lib._device_bridge_enabled, lib._device_request
    try:
        lib._device_bridge_enabled = lambda: True
        lib._device_request = lambda n, a: calls.append(n) or "{}"
        for q, want in (("What are the newest 3 files in my downloads directory?", False),
                        ("What's playing on Plex?", True), ("what episode am I on", True),
                        ("the job queue and show me the results", False), ("play it safe with the deploy", False),
                        ("Is anything listening on port 8765?", False), ("what am I listening to?", True)):
            calls.clear()
            lib._situational_context(q)
            ok &= check(f"no Plex asked for {q!r}", "get_plex_state" in calls, False)
    finally:
        lib._device_bridge_enabled, lib._device_request = orig_bridge, orig_dev

    print("\n── read-only Mac tools (Jellybean 0.10.1) ──")
    for t in ("search_file_contents", "git_status", "list_listening_ports"):
        ok &= check(f"{t} declared read-only with a status label",
                    (lib._DEVICE_TOOLS.get(t, {}).get("risk"), bool(lib._TOOL_STATUS.get(t))), ("read", True))
    ok &= check("read_file takes tail", "tail" in lib._DEVICE_TOOLS["read_file"]["parameters"], True)
    # #1251 C: search_file_contents is the files tool's contents mode now (callable by name, never declared)
    ok &= check("prompt points inspection at them", "mode contents: text inside files" in lib.build_tool_system_prompt(set(lib._DEVICE_TOOLS)), True)

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
