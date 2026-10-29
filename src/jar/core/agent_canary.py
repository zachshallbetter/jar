#!/usr/bin/env python3
"""Household-agent canary — proves the agent can actually think, use tools, and remember.

Fleet preflight checks that services answer /health; this checks that the *agent works*,
which is what silently broke for weeks (Gemini key invalid + model retired, local model
not running, tools receiving no arguments, KB auth and writes failing) while preflight
stayed green. Run by titan-agent-canary.timer every 10 minutes; alerts on change via
titan_alert (log always; phone via TITAN_ALERT_NTFY_URL).

Checks (required unless marked optional):
  local_model  llama-server answers a /no_think completion with non-empty text
  gemini       the Gemini brain answers (key valid, model still available)
  ai_gateway   (optional) Vercel AI Gateway answers when AI_GATEWAY_API_KEY is set
  agent_api    the HTTP API (Jellybean/ASE front door) reports healthy
  tool_turn    a real tool-using turn through _run_agent_turn — the loop every surface
               shares — returns the right answer (catches arg-passing / protocol bugs);
               still passing, it warns when the trivial turn took more than 3 model calls
               or any post-answer re-ask or Correction fired (#1550: 6-8 Gemini calls
               went unnoticed 10-05..10-10)
  kb           save a note and find it again (coordinator auth + retrieval writes/reads);
               upserts one fixed note, so it never accumulates
  jev          (optional) Jev answers a typed question

The tool turn runs in-process, not through POST /v1/prompt, so canary turns never land in
the agent's conversation memory or fact extraction.

  python3 scripts/agent_canary.py            # one line; exit 1 if a required check fails
  python3 scripts/agent_canary.py --json     # full per-check detail
  python3 scripts/agent_canary.py --no-alert # don't touch alert state
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("AGENT_TURN_SOURCE", "canary")   # keep canary turns out of the routing stats
import agent_runtime_lib as lib  # noqa: E402
from titan_alert import EdgeAlert  # noqa: E402
from ops_ports import AGENT_API_PORT  # noqa: E402

LAST = "/ai/logs/agent-canary.json"
HISTORY = "/ai/logs/agent-canary.jsonl"
OPTIONAL = {"jev", "ai_gateway"}


def _http(url: str, payload: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def check_local_model() -> str:
    ep = os.environ.get("AGENT_ENDPOINT", "http://127.0.0.1:%s" % os.environ.get("AGENT_PORT", "8010")).rstrip("/")
    r = _http(ep + "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "Reply with exactly the word: ready /no_think"}],
        "max_tokens": 24, "temperature": 0}, timeout=60)
    text = ((r.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
    if not text:
        raise RuntimeError("empty completion (thinking mode? /no_think ignored?)")
    return text[:40]


def check_gemini() -> str:
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise RuntimeError("GEMINI_API_KEY not set")
    r = lib._gemini(lib.GEMINI_MODEL, [{"text": "Reply with exactly the word: ready"}], timeout=30)
    parts = ((r.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    text = " ".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("empty reply from %s" % lib.GEMINI_MODEL)
    return "%s: %s" % (lib.GEMINI_MODEL, text[:30])


def check_ai_gateway() -> str:
    if not lib.ai_gateway_configured():
        raise RuntimeError("not configured")
    # Some gateway Gemini builds spend a small max_tokens budget on thinking and leave
    # content empty; ask simply and allow room + reasoning/text fallbacks.
    r = lib._ai_gateway_call({
        "model": lib.AI_GATEWAY_MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly one word: ready"}],
        "max_tokens": 64,
        "temperature": 0,
    }, timeout=45)
    msg = ((r.get("choices") or [{}])[0].get("message") or {})
    text = str(msg.get("content") or msg.get("reasoning") or msg.get("reasoning_content") or "").strip()
    if not text:
        # last resort: any string field on the choice
        ch = (r.get("choices") or [{}])[0]
        text = str(ch.get("text") or "").strip()
    if not text:
        raise RuntimeError("empty reply from %s" % lib.AI_GATEWAY_MODEL)
    return "%s: %s" % (lib.AI_GATEWAY_MODEL, text[:40])


def check_agent_api() -> str:
    port = os.environ.get("AGENT_API_PORT", str(AGENT_API_PORT))
    r = _http("http://127.0.0.1:%s/health" % port, timeout=10)
    if r.get("status") != "ok":
        raise RuntimeError("health=%s" % r.get("status"))
    return "ok (up %.0f min)" % (float(r.get("uptime_seconds", 0)) / 60)


# #1550: the canary's turn is one tool call and a number. More model calls than this, or any re-ask or Correction
# after the answer, means a check misfired on a trivial turn (10-05..10-10: 6-8 Gemini calls, 1.9 s -> 11 s).
TOOL_TURN_MAX_CALLS = 3
_REASK_FIELDS = {"degenerate_retries": "degenerate", "claim_fix_retry": "claim", "source_retry": "source",
                 "no_access_retry": "no_access", "required_read_retry": "required_read",
                 "required_act_retry": "required_act", "mac_out_retry": "mac_out", "claim_retry": "notes"}
_CORRECTION_FIELDS = ("claims_unbacked", "answer_corrections", "date_corrections")


def tool_turn_warning(info: dict) -> str | None:
    """Why a passing tool turn cost too much, in words naming the kinds; None when it didn't."""
    calls = int(info.get("gemini_calls") or 0)
    used = info.get("postanswer") if isinstance(info.get("postanswer"), dict) else {}
    skipped = info.get("postanswer_skipped") if isinstance(info.get("postanswer_skipped"), dict) else {}
    reasked = (set(used) | {kind for f, kind in _REASK_FIELDS.items() if info.get(f)}
               | ({"self_check"} if info.get("self_check") in ("ok", "revised", "failed") else set()))
    corrected = sorted(f for f in _CORRECTION_FIELDS if info.get(f))
    if calls <= TOOL_TURN_MAX_CALLS and not reasked and not skipped and not corrected:
        return None
    parts = ["%d model calls" % calls]
    if reasked:
        parts.append("re-asked: " + ", ".join(sorted(reasked)))
    if skipped:
        parts.append("over budget: " + ", ".join(sorted(skipped)))
    if corrected:
        parts.append("corrections: " + ", ".join(corrected))
    return "; ".join(parts)


def check_tool_turn() -> str | tuple[str, str]:
    msgs = [{"role": "system", "content": lib.build_tool_system_prompt({"count_chars", "python_eval"})},
            {"role": "user", "content": "Use the count_chars tool on the text 'canary bird' and reply "
                                        "with only the total number of letters."}]
    text = lib._run_agent_turn(msgs, allowed_tools={"count_chars", "python_eval"}, max_rounds=3)
    info = lib.last_turn_info()
    if "10" not in text:
        raise RuntimeError("wrong/empty answer %r (brain=%s%s)" % (
            text[:60], info.get("brain"), ", " + info["fallback"] if info.get("fallback") else ""))
    brain = str(info.get("brain") or "")
    forced = (os.environ.get("AGENT_TOOL_BRAIN") or "").strip().lower()
    warn = tool_turn_warning(info)
    # Intentional local pin, or cloud outage (Gemini 429 / Gateway 402) with a correct local answer:
    # pass with a warning so the watchdog does not page a working last-resort path as an outage.
    if brain == "local":
        reason = info.get("fallback") or info.get("failover_from") or (
            "AGENT_TOOL_BRAIN=local" if forced == "local" else "no cloud brain")
        note = "local brain (%s)" % reason
        return ("brain=local", note if not warn else "%s; %s" % (note, warn))
    if brain not in ("gemini", "ai_gateway"):
        raise RuntimeError("unexpected brain %r" % brain)
    if warn:
        return "brain=%s" % info.get("brain"), warn   # passing, with a warning (#1550)
    return "brain=%s" % info.get("brain")


def check_kb() -> str:
    token = "canary-%d" % int(time.time())
    saved = lib.execute_tool("ingest_text", {"title": "Agent Canary",
                                             "content": "The agent canary token is %s." % token})
    if not saved.startswith("Saved"):
        raise RuntimeError("save failed: " + saved[:120])
    found = ""
    for _ in range(3):  # indexing is synchronous today; allow a beat just in case
        found = lib.execute_tool("kb_retrieve", {"query": "agent canary token " + token})
        if token in found:
            return "saved + found"
        time.sleep(1.5)
    raise RuntimeError("saved but not found by kb_retrieve: " + found[:120])


def check_jev() -> str:
    jev = lib._jev_client()
    if jev is None:
        raise RuntimeError("not configured")
    r = jev.decide("The sky is blue on a clear day.",
                   {"t": {"type": "noul", "instructions": "`state` is true."}},
                   timeout=8, retries=1, source="canary", origin={"source": "canary"})
    meta = r["_meta"]
    return "%.2f in %d ms via %s" % (r["answers"]["t"]["noul"], meta["latency_ms"], meta.get("provider", "?"))


CHECKS = [("local_model", check_local_model), ("gemini", check_gemini), ("ai_gateway", check_ai_gateway),
          ("agent_api", check_agent_api), ("tool_turn", check_tool_turn), ("kb", check_kb), ("jev", check_jev)]


def main() -> int:
    ap = argparse.ArgumentParser(description="household-agent canary")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--only", nargs="*", help="run just these checks")
    a = ap.parse_args()

    if (not os.environ.get("GEMINI_API_KEY", "").strip()
            and not lib.ai_gateway_configured() and not a.only):
        # Run without the agent's environment (10-01 03:43: a FAIL for every credential, logged as an
        # outage). That's a misconfigured run, not titan's health: say so and record nothing.
        print("agent canary skipped: not configured (run it with the agent's environment, agent.env)")
        return 2

    results: dict[str, dict] = {}
    for name, fn in CHECKS:
        if a.only and name not in a.only:
            continue
        t0 = time.monotonic()
        warn = None
        try:
            detail, ok = fn(), True
            if isinstance(detail, tuple):   # (detail, warning): passing, but worth a look (#1550)
                detail, warn = detail
        except Exception as exc:  # noqa: BLE001
            detail, ok = "%s: %s" % (type(exc).__name__, str(exc)[:240]), False
        results[name] = {"ok": ok, "ms": int((time.monotonic() - t0) * 1000), "detail": detail,
                         "optional": name in OPTIONAL, **({"warn": warn} if warn else {})}

    # Cloud quota/billing blips are not host outages when a working path remains.
    def _cloud_blip(detail: str) -> bool:
        d = detail.lower()
        return any(s in d for s in ("429", "too many requests", "quota", "402", "payment required"))

    local_ok = bool(results.get("local_model", {}).get("ok"))
    tool_ok = bool(results.get("tool_turn", {}).get("ok"))
    gateway_ok = bool(results.get("ai_gateway", {}).get("ok"))

    if results.get("gemini") and not results["gemini"]["ok"] and tool_ok and (gateway_ok or local_ok):
        detail = str(results["gemini"].get("detail") or "")
        if _cloud_blip(detail):
            results["gemini"]["optional"] = True
            cover = "AI Gateway" if gateway_ok else "local"
            results["gemini"]["warn"] = "rate-limited/quota; %s covering chat" % cover

    # Gateway 402 (billing) is already OPTIONAL, but stamp a clear warn when local covers.
    if results.get("ai_gateway") and not results["ai_gateway"]["ok"] and local_ok and tool_ok:
        detail = str(results["ai_gateway"].get("detail") or "")
        if _cloud_blip(detail):
            results["ai_gateway"]["warn"] = "billing/quota; local covering chat"

    failing = {k: v["detail"] for k, v in results.items() if not v["ok"] and not v["optional"]}
    warning = {k: v["detail"] for k, v in results.items() if not v["ok"] and v["optional"]}
    status = "FAIL" if failing else ("WARN" if warning else "OK")
    # A passing check's warning (#1550: the tool turn cost too many model calls) is recorded and printed but leaves the
    # status alone: the watchdog (host_summary) reads any non-OK status as a host problem, and a slow turn isn't one.
    warned = {k: v["warn"] for k, v in results.items() if v["ok"] and v.get("warn")}
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": status, "checks": results,
              **({"warnings": warned} if warned else {})}
    for path, mode, body in ((LAST, "w", json.dumps(record, indent=1)), (HISTORY, "a", json.dumps(record) + "\n")):
        try:
            with open(path, mode) as f:
                f.write(body)
        except Exception:
            pass
    if not a.no_alert and not a.only:
        # Two failing runs in a row (10 min apart) before paging: one-off Gemini errors cleared
        # on the next run and only made noise.
        EdgeAlert("agent-canary", "Titan household agent", confirm=2).update(failing)

    if a.json:
        print(json.dumps(record, indent=1))
    else:
        parts = ["%s %s" % (k, ("ok (warn)" if v.get("warn") else "ok") if v["ok"] else ("WARN" if v["optional"] else "FAIL"))
                 for k, v in results.items()]
        print("agent canary %s: %s" % (status, ", ".join(parts))
              + "".join(" | %s: %s" % (k, d) for k, d in {**failing, **warning, **warned}.items()))
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
