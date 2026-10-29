#!/usr/bin/env python3
"""Weekly evidence for agent routing decisions, from /ai/logs/agent-turns.jsonl (content-free).

Answers "is the Jev router earning its keep?": how many real turns each brain answered, why
(routing reason), how long they took, how often Gemini fell back, and how many Gemini calls the
Jev router saved by sending ambiguous turns to the local model. Canary and eval turns are excluded.
Jev spend comes from the decision-broker's own ledger (/broker/usage, as the titan-agent caller),
which covers every Jev call the agent makes, not just the logged turns.

Prompt cache (#405, fields from #193/#404): the share of prompt tokens served from the cache per brain,
the local model's prefill time, local turns that ran cold (named by prefix id and role, so the shape
to warm is visible), guides read with skill_guide, and a flag when the cache share drops against the
period before (a prompt change that broke the shared prefix shows up that way).

  python3 scripts/agent_turn_report.py [--days 7] [--notify]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from typing import Any
from collections import Counter
from pathlib import Path

LOG = Path("/ai/logs/agent-turns.jsonl")
AGENT_ENV = Path("/ai/projects/titan-local-agent/agent.env")
JEV_KEYS = ("JEV_AUTH", "JEV_BASE_URL", "JEV_COORDINATOR_KEY_FILE", "JEV_COORDINATOR_ID")


def pct(xs: list[int], q: float) -> int | None:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


COLD_SHARE = 0.5      # a local turn with less than this share of its prompt cached ran cold
DROP_POINTS = 15      # flag a cache-share drop of this many percentage points against the period before


def _share(rows: list[dict]) -> float | None:
    fresh = sum(r.get("prompt_fresh", 0) for r in rows)
    cached = sum(r.get("prompt_cached", 0) for r in rows)
    return cached / (fresh + cached) if fresh + cached else None


def egress_lines(rows: list[dict]) -> list[str]:
    """What left the house (#409): of the turns that logged `egress`, how many used any external service,
    which kinds, and the share per person. Empty when no turn logged it yet."""
    logged = [r for r in rows if "egress" in r]
    if not logged:
        return []
    out_ = [r for r in logged if r["egress"]]
    kinds = Counter(k for r in out_ for k in r["egress"])
    lines = [f"left the house: {len(out_)} of {len(logged)} turns"
             + (" (" + ", ".join(f"{k} {n}" for k, n in kinds.most_common()) + ")" if kinds else "")
             + f"; fully local: {len(logged) - len(out_)}"]
    people = Counter(r.get("person") or "?" for r in logged)
    if len(people) > 1 or "?" not in people:
        lines.append("by person: " + ", ".join(
            f"{p} {sum(1 for r in logged if (r.get('person') or '?') == p and r['egress']) / n:.0%} of {n}"
            for p, n in people.most_common()))
    return lines


def cache_lines(rows: list[dict], before: list[dict]) -> list[str]:
    """The prompt-cache lines of the report (empty when no turn logged usage). `before` is the period
    of the same length just before, for the week-over-week check."""
    used = [r for r in rows if "prompt_fresh" in r]
    if not used:
        return []
    out = []
    parts = []
    for brain in sorted({r.get("brain") or "?" for r in used}):
        mine = [r for r in used if (r.get("brain") or "?") == brain]
        share = _share(mine)
        part = f"{brain} {share:.0%} of {sum(r['prompt_fresh'] + r.get('prompt_cached', 0) for r in mine) // len(mine):,} tokens/turn"
        pf = [r["prefill_ms"] for r in mine if "prefill_ms" in r]
        if pf:
            part += f", prefill p50/p90 {pct(pf, .5) / 1000:.1f}/{pct(pf, .9) / 1000:.1f}s"
        parts.append(part)
    out.append("prompt cache: " + "; ".join(parts))
    local = [r for r in used if r.get("brain") == "local"]
    cold = [r for r in local if (_share([r]) or 0) < COLD_SHARE]
    if local:
        shapes = Counter(f"{r.get('prefix') or '?'} {r.get('role') or '?'}" for r in cold)
        out.append(f"local turns cold: {len(cold)} of {len(local)}"
                   + (" (prefix role: " + ", ".join(f"{k} ×{n}" for k, n in shapes.most_common(4)) + ")" if cold else ""))
    read = [r for r in rows if r.get("skills_read")]
    if read:
        guides = Counter(g for r in read for g in r["skills_read"])
        out.append(f"skill guides read: {len(read)} of {len(rows)} turns ("
                   + ", ".join(f"{g} {n}" for g, n in guides.most_common()) + ")")
    now, then = _share(used), _share([r for r in before if "prompt_fresh" in r])
    if now is not None and then is not None and (then - now) * 100 >= DROP_POINTS:
        out.append(f"Look into: cache share fell {then:.0%} → {now:.0%}: a prompt change may have broken the shared prefix "
                   "(turn log `prefix`; scripts/test_prompt_prefix.py)")
    return out


# When the world-model tickets waiting on real misses reopen (stated in the report so it's seen, not remembered).
MISSES_REOPEN = 20            # #377: real misses (corrected lookups + empty explicit searches) in 14 days
ACTIVE_REREAD = "10-17"       # #366: read measure_active again on clean data from this date


def knowledge_misses(trace_db: Path | None = None, now: float | None = None, admin: str | None = None) -> dict[str, Any]:
    """Content-free counts from the retrieval trace over 7 and 14 days: lookups, corrected ones, explicit searches that
    found nothing, and the real misses (those two), for the household and split into the admin's own and everyone
    else's. Never per person by name (a person's activity is theirs)."""
    import sqlite3
    if admin is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import household_identity as hh
        admin = hh.ADMIN_PERSON_ID
    db = Path(trace_db or os.environ.get("RETRIEVAL_TRACE_DB", "/ai/logs/retrieval-trace.db"))
    now = now or time.time()
    out: dict[str, Any] = {}
    if not db.exists():
        return out
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for days in (7, 14):
            rows = con.execute(
                "SELECT person, path, corrected > 0, NOT EXISTS (SELECT 1 FROM hits h WHERE h.query=q.id) FROM queries q "
                "WHERE at >= ?", (now - days * 86400,)).fetchall()
            c = {"lookups": len(rows), "corrected": sum(1 for r in rows if r[2]),
                 "empty_searches": sum(1 for r in rows if r[1] == "search" and r[3])}
            miss = [r for r in rows if r[2] or (r[1] == "search" and r[3])]
            c["misses"] = len(miss)
            c["admin_misses"] = sum(1 for r in miss if r[0] == admin)
            c["others_misses"] = len(miss) - c["admin_misses"]
            out[days] = c
    finally:
        con.close()
    return out


def misses_line(m: dict[str, Any]) -> list[str]:
    if not m:
        return []
    a, b = m[7], m[14]
    reopen = "ready to reopen" if b["misses"] >= MISSES_REOPEN else f"reopens at {MISSES_REOPEN} in 14 d"
    return [f"knowledge misses (7 d / 14 d): {a['misses']}/{b['misses']} real (corrected {a['corrected']}/{b['corrected']}, "
            f"empty searches {a['empty_searches']}/{b['empty_searches']}) of {a['lookups']}/{b['lookups']} lookups; "
            f"the admin's own {a['admin_misses']}/{b['admin_misses']}, others' {a['others_misses']}/{b['others_misses']}. "
            f"#377 {reopen}; #366 re-reads measure_active from {ACTIVE_REREAD}"]


def signals_line(m: dict[str, Any]) -> list[str]:
    """#377 P1: the content-free miss signals from knowledge_misses.counts, one count each; never a person or a qhash."""
    if not m:
        return []
    a, b = m[7], m[14]
    return [f"knowledge-miss signals (7 d / 14 d): kept nothing {a['kept_nothing']}/{b['kept_nothing']}, "
            f"rephrased {a['rephrase']}/{b['rephrase']}, KB-corrected lookups {a['kb_corrected']}/{b['kb_corrected']}"]


def _jev_env() -> dict[str, str]:
    """The JEV_* settings from the environment, else from agent.env (the report unit doesn't load it)."""
    env = {k: os.environ[k] for k in JEV_KEYS if os.environ.get(k)}
    if len(env) < len(JEV_KEYS) and AGENT_ENV.exists():
        for line in AGENT_ENV.read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k in JEV_KEYS and k not in env:
                env[k] = v.strip().strip("'\"")
    return env


def broker_spend(days: float) -> str | None:
    """One line of Jev spend from the decision-broker ledger, or None if it isn't in use/reachable."""
    env = _jev_env()
    if env.get("JEV_AUTH", "").lower() != "coordinator" or not env.get("JEV_BASE_URL") or not env.get("JEV_COORDINATOR_KEY_FILE"):
        return None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from jev_client import mint_coordinator_jwt
        token = mint_coordinator_jwt(env["JEV_COORDINATOR_KEY_FILE"], env.get("JEV_COORDINATOR_ID", "titan-agent"))
        url = f"{env['JEV_BASE_URL'].rstrip('/')}/broker/usage?days={max(1, round(days))}"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=10) as r:
            u = json.load(r)
    except Exception as e:  # the report must still go out
        return f"Jev spend: decision-broker unavailable ({type(e).__name__})"
    ds = u.get("days") or []
    calls, cached, errors = (sum(d.get(k, 0) for d in ds) for k in ("calls", "cached", "errors"))
    cost = sum(d.get("cost_usd", 0.0) for d in ds)
    peak = max((d.get("cost_usd", 0.0) for d in ds), default=0.0)
    return (f"Jev spend (decision-broker): ${cost:.4f} over {calls} calls ({cached} cached, {errors} errors); "
            f"peak day ${peak:.4f} of ${u.get('daily_budget_usd', 0):g} budget")


def broker_shadow(days: float) -> str | None:
    """One line from the decision-broker's shadow trial (local Decider-2B vs Jev), or None when
    the broker isn't in use or has no shadow data."""
    env = _jev_env()
    if env.get("JEV_AUTH", "").lower() != "coordinator" or not env.get("JEV_BASE_URL") or not env.get("JEV_COORDINATOR_KEY_FILE"):
        return None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from jev_client import mint_coordinator_jwt
        token = mint_coordinator_jwt(env["JEV_COORDINATOR_KEY_FILE"], env.get("JEV_COORDINATOR_ID", "titan-agent"))
        url = f"{env['JEV_BASE_URL'].rstrip('/')}/broker/shadow?days={max(1, min(31, round(days)))}"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=10) as r:
            sh = json.load(r)
    except Exception:
        return None
    qs = (sh.get("questions") or {}).values()
    compared = sum(q.get("compared", 0) for q in qs)
    if not sh.get("enabled") or not compared:
        return None
    agree = sum(q.get("agree", 0) for q in qs)
    both = sum(q.get("both_act", 0) for q in qs)
    both_agree = sum(q.get("both_act_agree", 0) for q in qs)
    jev_only = sum(q.get("jev_only_acts", 0) for q in qs)
    local_only = sum(q.get("shadow_only_acts", 0) for q in qs)
    ms = sh.get("median_ms") or {}
    acted = f"; when both were sure they agreed {both_agree}/{both}" if both else ""
    return (f"Local Decider shadow: agreed with Jev on {agree}/{compared} answers ({100 * agree // compared}%){acted}; "
            f"sure only Jev {jev_only}, only Decider {local_only}; median {ms.get('shadow', '?')} ms vs Jev {ms.get('jev', '?')} ms")


def card_lines(days: float, now: float | None = None, db: Path | None = None) -> list[str]:
    """Cards per source this week (#1039): shown / Done / snoozed / muted / expired untouched / acted, with the
    admin's own in brackets; a noisy source flagged (expired untouched + muted over 60% of shown, at least 10)."""
    import household_identity as hh
    import proactive
    now = time.time() if now is None else now
    got = proactive.outcomes(now - days * 86400, now, hh.ADMIN_PERSON_ID, db)
    if not got:
        return []
    lines = ["cards (shown/Done/snoozed/muted/expired untouched/acted; [yours: Done/snoozed/muted/acted]):"]
    for src, r in sorted(got.items(), key=lambda kv: -kv[1]["shown"]):
        m = r["mine"]
        lines.append(f"  {src}: {r['shown']}/{r['done']}/{r['snoozed']}/{r['muted']}/{r['expired_untouched']}/{r['acted']}"
                     f" [{m['done']}/{m['snoozed']}/{m['muted']}/{m['acted']}]" + ("  ← noisy source" if r["noisy"] else ""))
    return lines


def coder_line(days: float, db: Path | None = None, now: float | None = None) -> str | None:
    """The autonomous coder's merge rate (#1099; #874 stops widening generated fixes on it): this period's proposals
    and how they ended, from its calibration DB, read-only. None when there's no DB or nothing this period."""
    import sqlite3
    path = db or Path(os.environ.get("AUTONOMOUS_CODER_CALIBRATION_DB", "/ai/logs/titan-autonomous-coder-calibration.db"))
    if not path.exists():
        return None
    end = time.time() if now is None else now
    cut = end - days * 86400
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT outcome, COUNT(*) FROM calibration WHERE created_at >= ? AND created_at <= ? "
                               "GROUP BY outcome", (cut, end)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    n = {str(o or "opened"): c for o, c in rows}
    total = sum(n.values())
    if not total:
        return None
    merged, closed = n.get("merged", 0), n.get("closed_unmerged", 0)
    rate = f", merge rate {merged * 100 // (merged + closed)}% of resolved" if merged + closed else ""
    return (f"autonomous coder ({days:g} d): {total} proposed, {merged} merged, {closed} closed unmerged, "
            f"{total - merged - closed} not yet resolved{rate}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--notify", action="store_true", help="send the summary through titan_alert (phone)")
    ap.add_argument("--log", default=str(LOG))
    a = ap.parse_args()
    cut = time.time() - a.days * 86400
    rows, before = [], []
    for line in Path(a.log).read_text().splitlines() if Path(a.log).exists() else []:
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("source", "user") != "user":
            continue
        if r.get("ts", 0) >= cut:
            rows.append(r)
        elif r.get("ts", 0) >= cut - a.days * 86400:
            before.append(r)
    if not rows:
        msg = f"No real agent turns logged in the last {a.days:g} days."
    else:
        brains = Counter(r.get("brain") or "error" for r in rows)
        routes = Counter(r.get("route") or f"mode={r.get('mode')}" for r in rows)
        lat = {b: [r["ms"] for r in rows if r.get("brain") == b and not r.get("error")] for b in brains}
        jev_local = [r for r in rows if r.get("route") == "jev-local"]
        asked = [r for r in rows if r.get("jev")]
        fallbacks = sum(1 for r in rows if r.get("fallback"))
        errors = sum(1 for r in rows if r.get("error"))
        lines = [f"{len(rows)} real turns in {a.days:g} days: " + ", ".join(f"{b} {n}" for b, n in brains.most_common()),
                 "why: " + ", ".join(f"{k} {n}" for k, n in routes.most_common()),
                 "latency p50/p90 (s): " + ", ".join(f"{b} {pct(v, .5) / 1000:.1f}/{pct(v, .9) / 1000:.1f}"
                                                    for b, v in lat.items() if v)]
        if asked:
            jl = [r["ms"] for r in jev_local if not r.get("error")]
            lines.append(f"Jev router: asked on {len(asked)} turns, sent {len(jev_local)} to local (≈{len(jev_local)} Gemini "
                         f"calls saved)" + (f"; those took p50 {pct(jl, .5) / 1000:.1f}s" if jl else "")
                         + f"; its own check p50 {pct([r['jev']['ms'] for r in asked], .5)} ms")
        lines.append(f"Gemini fallbacks to local: {fallbacks} · turn errors: {errors}")
        lines += cache_lines(rows, before)
        lines += egress_lines(rows)
        msg = "\n".join(lines)
    try:
        for line in misses_line(knowledge_misses()):
            msg += "\n" + line
    except Exception as exc:  # noqa: BLE001 - the report goes out without it
        print(f"(knowledge misses skipped: {type(exc).__name__})", file=sys.stderr)
    try:   # #377 P1: kept nothing, rephrase, KB-only corrected; counts only
        import knowledge_misses
        for line in signals_line(knowledge_misses.counts()):
            msg += "\n" + line
    except Exception as exc:  # noqa: BLE001 - the report goes out without it
        print(f"(miss signals skipped: {type(exc).__name__})", file=sys.stderr)
    try:   # #377 half 2: the overnight proposals' calibration, counts only
        import miss_proposals
        import world as _W
        mp = miss_proposals.counts(_W.get(), a.days)
        if mp["made"]:
            msg += (f"\nknowledge-miss proposals ({a.days:g} d): {mp['made']} made, {mp['kept']} kept, "
                    f"{mp['not_useful']} not useful, {mp['open']} open"
                    + (f", {mp['held']} structural held for the replay gate" if mp.get("held") else ""))
    except Exception as exc:  # noqa: BLE001 - the report goes out without it
        print(f"(miss proposals skipped: {type(exc).__name__})", file=sys.stderr)
    try:
        for line in card_lines(a.days):
            msg += "\n" + line
    except Exception as exc:  # noqa: BLE001 - the report goes out without it
        print(f"(card outcomes skipped: {type(exc).__name__})", file=sys.stderr)
    try:
        line = coder_line(a.days)
        if line:
            msg += "\n" + line
    except Exception as exc:  # noqa: BLE001 - the report goes out without it
        print(f"(coder merge rate skipped: {type(exc).__name__})", file=sys.stderr)
    for line in (broker_spend(a.days), broker_shadow(a.days)):
        if line:
            msg += "\n" + line
    print(msg)
    if a.notify:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from titan_alert import alert
        alert("Titan agent — weekly routing report", msg, priority="low", tags="bar_chart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
