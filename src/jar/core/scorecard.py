#!/usr/bin/env python3
"""titan's scorecard: six areas, each measured from live data, each with a target (10-01 evening goal).

  system       titan's own health: failed titan-* units, disk, GPUs, the canary over the window
  service      real turns over the window (not tests, canaries or evals): errors, latency, fallbacks
  response     the latest nightly household eval, and this run's turn evals when --turns is given; plus what
               the reply checks did to real turns over the window (counts only, watched, never a miss)
  memory       what memory holds per person: junk the extraction rules would refuse, near-duplicates,
               persona sentences that guess
  knowledge    fixed knowledge-base probes with known right answers (as the admin, read-only)
  consistency  the same questions asked three times (--consistency): every answer must pass

Read-only apart from its own log line. Eval turns are "remember": false and never stored.

  python3 scripts/scorecard.py [--hours 6] [--turns] [--consistency] [--json]     (on titan)

Appends one JSON line to /ai/logs/scorecard.jsonl and prints one line per area: ok or MISS, the
numbers, and what missed. Exits 0 when every area is ok and 3 (MISS_EXIT) when one missed; anything else,
1 included (an uncaught exception), is a crash, which titan-scorecard.service then reports as a failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

# A miss is a finding, not a failure: its own exit status, so a crash (1) still fails the unit.
MISS_EXIT = 3

sys.path.insert(0, str(Path(__file__).resolve().parent))

LOGS = Path(os.environ.get("SCORECARD_LOGS", "/ai/logs"))
OUT = LOGS / "scorecard.jsonl"
# Not people: their turns don't count toward service.
NOT_PEOPLE = {"test", "canary", "eval", "probe"}

TARGETS = {
    "system": "0 failed titan units; every disk under 90% full; canary OK ≥ 98% over the window",
    "service": "real-turn errors ≤ 2%; p95 ≤ 20 s; local→Gemini fallbacks ≤ 5%",
    "response": "nightly household eval ≥ 95%; this run's turn evals all pass",
    "memory": "junk ≤ 5% of each person's facts; 0 near-duplicate pairs; 0 guessing persona sentences",
    "knowledge": "every probe right",
    "consistency": "every repeated question passes all three times",
}


CITE_MIN = 0.95   # #910/#912: inline cites stay on only at >= 95% precision


def cite_status(rows: list[Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """The nightly cite_precision line (logged runs only) and its miss: precision below CITE_MIN over 10+ cites."""
    last = next((r for r in reversed(rows) if isinstance(r, dict) and r.get("emitted") is not None), None)
    if not last:
        return None, []
    out = {k: last.get(k) for k in ("at", "emitted", "agreed", "precision", "lower_bound", "recall")}
    miss = []
    if (last.get("emitted") or 0) >= 10 and (last.get("precision") or 0) < CITE_MIN:
        miss.append(f"inline cites {last['agreed']}/{last['emitted']} = {last['precision']:.0%}, below "
                    f"{CITE_MIN:.0%}: turn CITE_INLINE off (#912)")
    return out, miss


def bank_misses(rows: list[Any]) -> list[str]:
    """#862's bank on the scorecard: a kind below its baseline, or a case that passed last night and misses now."""
    banks = [x["bank"] for x in rows if isinstance(x, dict) and isinstance(x.get("bank"), dict)]
    if not banks or not (isinstance(rows[-1], dict) and isinstance(rows[-1].get("bank"), dict)):
        return []
    out, bank = [], banks[-1]
    if bank.get("below_baseline"):
        out.append("KB bank below baseline: " + ", ".join(bank["below_baseline"]))
    if len(banks) > 1:
        new = sorted(set(bank.get("misses") or []) - set(banks[-2].get("misses") or []))
        if new:
            out.append(f"KB bank: {len(new)} new miss(es): {', '.join(new[:5])}")
    return out


def _jsonl(path: Path, since: float | None = None) -> list[dict[str, Any]]:
    out = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                ts = d.get("ts")
                if since and isinstance(ts, (int, float)) and ts < since:
                    continue
                out.append(d)
    except OSError:
        pass
    return out


def _pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _p(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


# ── system ──────────────────────────────────────────────────────────────────────────────────────

def system(hours: float, run: Callable[[list[str]], str] | None = None) -> dict[str, Any]:
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout)
    misses = []
    failed = [l.split()[0] for l in run(["systemctl", "list-units", "--failed", "--no-legend", "--plain",
                                         "titan-*"]).splitlines() if l.strip()]
    if failed:
        misses.append(f"failed units: {', '.join(failed)}")
    disks = {}
    for mount in ("/", "/ai/datasets", "/ai/tmp"):
        try:
            u = shutil.disk_usage(mount)
            disks[mount] = _pct(u.used, u.total)
            if disks[mount] >= 90:
                misses.append(f"{mount} {disks[mount]}% full")
        except OSError:
            pass
    gpus = []
    # Hot (85 °C+) counts when the last hourly run saw that GPU hot too, or at 90 °C+ at once: one reading
    # during an eval (the 10-01 local before/after drove GPU1 to 85) is load, not a fault (TITAN X slows
    # down near 92).
    prev = (_jsonl(OUT) or [{}])[-1]
    prev_hot = {g.get("gpu") for g in ((prev.get("system") or {}).get("gpus") or []) if (g.get("temp_c") or 0) >= 85}
    for l in run(["nvidia-smi", "--query-gpu=index,temperature.gpu,memory.used,memory.total",
                  "--format=csv,noheader,nounits"]).splitlines():
        try:
            i, temp, used, total = (x.strip() for x in l.split(","))
            gpus.append({"gpu": int(i), "temp_c": int(temp), "mem_pct": _pct(float(used), float(total))})
            if int(temp) >= 90 or (int(temp) >= 85 and int(i) in prev_hot):
                misses.append(f"GPU{i} at {temp} °C" + (" (two runs running)" if int(temp) < 90 else ""))
        except ValueError:
            continue
    since = time.time() - hours * 3600
    canary = [c for c in _jsonl(LOGS / "agent-canary.jsonl") if _iso_ts(c.get("ts")) >= since]
    # A failure counts when the next run fails too, as the canary's own alert does (confirm=2): a
    # deploy's restart fails one run and is over by the next (10-01 02:20).
    failed_runs = [c.get("status") == "FAIL" for c in canary]
    # (Both runs of a confirmed pair are down; the latest run counts until the next one says otherwise.)
    confirmed = [i for i, f in enumerate(failed_runs) if f and (i + 1 == len(failed_runs) or failed_runs[i + 1]
                                                                or (i > 0 and failed_runs[i - 1]))]
    canary_pct = _pct(len(canary) - len(confirmed), len(canary))
    if canary and canary_pct < 98:
        bad = sorted({k for i in confirmed for k, v in (canary[i].get("checks") or {}).items()
                      if not v.get("ok") and not v.get("optional")})
        misses.append(f"canary OK {canary_pct}% ({', '.join(bad) or 'see agent-canary.jsonl'})")
    # the system bubble's last health check (#355): services down, and runbook ports that differ from what runs
    sw = [r for r in _jsonl(LOGS / "system-world.jsonl") if (r.get("ts") or 0) >= since]
    services = sw[-1] if sw else None
    if services and services.get("down"):
        misses.append(f"services down: {', '.join(services['down'])}")
    if services and services.get("port_differs"):
        misses.append(f"runbook ports differ from what runs: {', '.join(services['port_differs'])} (a doc ticket)")
    # an engineering agent's reads (#355): anything not titan's own system item is a leak, whatever the count
    eng = [r for r in _jsonl(LOGS / "engineering-reads.jsonl") if (r.get("ts") or 0) >= since]
    eng_leaks = sum(int(r.get("leaks") or 0) for r in eng)
    if eng_leaks:
        misses.append(f"engineering reads returned {eng_leaks} non-system items (dropped; engineering-reads.jsonl)")
    return {"ok": not misses, "misses": misses, "failed_units": failed, "disk_pct": disks, "gpus": gpus,
            "engineering": {"reads": len(eng), "leaks": eng_leaks},
            "services": {"n": services.get("services"), "down": len(services.get("down") or []),
                         "port_differs": len(services.get("port_differs") or [])} if services else None,
            "canary_runs": len(canary), "canary_ok_pct": canary_pct,
            "canary_blips": sum(failed_runs) - len(confirmed)}


def _iso_ts(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))   # titan's Python 3.10 reads no bare Z
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return 0.0


# ── service ─────────────────────────────────────────────────────────────────────────────────────

WIDENED_MIN, WIDENED_MAX_PCT = 20, 10   # #683: judged from 20 narrowed turns; more than 10% widening is a miss
# #876: high-effort turns run one self-check. If it never revises a reply over 40 checks (about two weeks at today's
# rate), titan raising effort itself (AGENT_EFFORT_AUTO) costs ~1.5 s a turn for nothing: the note says turn it off.
SELF_CHECK_MIN = 40


ITEMS_P95_BUDGET_MS = 250        # #1181: the items surface's warm budget per view
ITEMS_MIN_REQUESTS = 10          # …judged once a view has at least this many requests in the window


def service(hours: float, turns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    since = time.time() - hours * 3600
    rows = turns if turns is not None else _jsonl(LOGS / "agent-turns.jsonl", since)
    real = [r for r in rows if str(r.get("source") or "") not in NOT_PEOPLE]
    n = len(real)
    errors = [r for r in real if r.get("error")]
    # Time spent waiting on the person to confirm a write (confirm_ms) is theirs, not titan's: an unanswered
    # confirmation runs to its 120 s timeout and made one turn the window's p95 (10-01 22:57).
    ms = [max(0.0, float(r["ms"]) - _waited_ms(r)) for r in real if isinstance(r.get("ms"), (int, float))]
    local_failed = [r for r in real if str(r.get("fallback") or "").startswith("local failed")]
    gemini_failed = [r for r in real if str(r.get("fallback") or "").startswith("gemini failed")]
    budget = [r for r in real if r.get("budget_hit")]
    out = {"turns": n, "errors": len(errors), "error_pct": _pct(len(errors), n),
           "p50_s": round(statistics.median(ms) / 1000, 1) if ms else 0.0, "p95_s": round(_p(ms, 0.95) / 1000, 1),
           "local_failed": len(local_failed), "gemini_failed": len(gemini_failed),
           "fallback_pct": _pct(len(local_failed) + len(gemini_failed), n), "budget_hits": len(budget),
           "brains": {b: sum(1 for r in real if r.get("brain") == b) for b in ("gemini", "local")},
           # Which Mac tools the time goes to (device_times, 10-01): p95 per tool, slowest first.
           "slow_tools": _slow_tools(real),
           # Which tools fail most (#651): failed results per tool, with the most common first words.
           "failing_tools": _failing_tools(real)}
    # #683: Gemini is declared a narrowed tool set; a turn that reached outside it widened to every tool. A rising
    # rate means the groups miss what people ask for.
    narrowed = [r for r in real if r.get("tools_declared")]
    widened = [r for r in narrowed if r.get("tools_widened")]
    out.update(narrowed=len(narrowed), widened=len(widened), widened_pct=_pct(len(widened), len(narrowed)))
    high = [r for r in real if r.get("effort") == "high"]
    checked = [r for r in high if r.get("self_check") in ("ok", "revised")]
    revised = [r for r in checked if r.get("self_check") == "revised"]
    out.update(effort_high=len(high), effort_high_pct=_pct(len(high), n), self_checked=len(checked),
               self_revised=len(revised), self_revised_pct=_pct(len(revised), len(checked)))
    misses = []
    if n and out["error_pct"] > 2:
        kinds: dict[str, int] = {}
        for r in errors:
            k = re.sub(r"\d+", "N", str(r.get("error")))[:60]
            kinds[k] = kinds.get(k, 0) + 1
        misses.append(f"errors {out['error_pct']}%: " + "; ".join(f"{k} ×{v}" for k, v in
                                                               sorted(kinds.items(), key=lambda x: -x[1])[:3]))
    if n and out["p95_s"] > 20:
        slow = ", ".join(f"{t} {v} s" for t, v in list(out["slow_tools"].items())[:3])
        misses.append(f"p95 {out['p95_s']} s" + (f" (slowest Mac tools at p95: {slow})" if slow else ""))
    if len(checked) >= SELF_CHECK_MIN and not revised:
        misses.append(f"the self-check revised none of {len(checked)} high-effort replies: consider AGENT_EFFORT_AUTO "
                      "off (#876)")
    if len(narrowed) >= WIDENED_MIN and out["widened_pct"] > WIDENED_MAX_PCT:
        misses.append(f"tools widened on {out['widened_pct']}% of {len(narrowed)} narrowed turns (the groups miss some asks)")
    if n and out["fallback_pct"] > 5:
        misses.append(f"fallbacks {out['fallback_pct']}% (local failed {len(local_failed)}, Gemini failed {len(gemini_failed)})")
    # #1181's guard: the main window's items surface, per view, from real requests (no ids); a warm p95 over budget
    # is a miss once a view has enough requests to say so
    lat: dict[str, list[float]] = {}
    for r in _jsonl(LOGS / "items-latency.jsonl", since):
        if r.get("status") == 200 and isinstance(r.get("ms"), (int, float)):
            lat.setdefault(str(r.get("view") or "?"), []).append(float(r["ms"]))
    out["items_latency"] = {v: {"n": len(xs), "p50_ms": round(statistics.median(xs)), "p95_ms": round(_p(xs, 0.95))}
                            for v, xs in sorted(lat.items())}
    slow_views = [f"{v} p95 {x['p95_ms']} ms" for v, x in out["items_latency"].items()
                  if x["n"] >= ITEMS_MIN_REQUESTS and x["p95_ms"] > ITEMS_P95_BUDGET_MS]
    if slow_views:
        misses.append(f"items surface over its {ITEMS_P95_BUDGET_MS} ms p95 budget: {', '.join(slow_views)} (#1181)")
    out.update(ok=not misses, misses=misses)
    return out


def _waited_ms(r: dict[str, Any]) -> float:
    """The person's share of a turn: how long the app's question was up (asked_ms, #619) when the app says,
    else every write's whole round trip (confirm_ms), the older apps' upper bound."""
    if isinstance(r.get("asked_ms"), (int, float)):
        return float(r["asked_ms"])
    return float(r.get("confirm_ms") or 0)


def _failing_tools(turns: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by: dict[str, dict[str, int]] = {}
    for r in turns:
        for pair in r.get("tool_fails") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                heads = by.setdefault(str(pair[0]), {})
                heads[str(pair[1])] = heads.get(str(pair[1]), 0) + 1
    ranked = sorted(by.items(), key=lambda x: -sum(x[1].values()))[:8]
    return {t: {"n": sum(h.values()), "most": max(h.items(), key=lambda x: x[1])[0]} for t, h in ranked}


def _slow_tools(turns: list[dict[str, Any]]) -> dict[str, float]:
    by: dict[str, list[float]] = {}
    for r in turns:
        times, said = [], False
        for pair in r.get("device_times") or []:
            try:
                t = float(pair[1])
                if len(pair) > 2:   # [name, ms, asked_ms]: the app said how long its question was up (#619)
                    t, said = max(0.0, t - float(pair[2])), True
                times.append([str(pair[0]), t])
            except (TypeError, ValueError, IndexError):
                continue
        # An older app: the confirmation wait sits inside the write that asked for it, so take it off that
        # call (the slowest).
        wait = 0.0 if said else float(r.get("confirm_ms") or 0)
        if wait and times:
            slowest = max(times, key=lambda x: x[1])
            slowest[1] = max(0.0, slowest[1] - wait)
        for name, t in times:
            by.setdefault(name, []).append(t)
    p95 = {t: round(_p(v, 0.95) / 1000, 1) for t, v in by.items()}
    return dict(sorted(p95.items(), key=lambda x: -x[1])[:8])


# ── response ────────────────────────────────────────────────────────────────────────────────────

# The checks run on every reply (turn log keys, counts only): a retried empty or degenerate answer
# (#443), a Correction line against the tools (#344), a weekday fixed (#302), a request in someone
# else's text disclosed and a write it steered held (#473).
REPLY_CHECKS = ("degenerate_retries", "answer_corrections", "date_corrections", "injections_disclosed", "held_writes")


def reply_checks(hours: float, turns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """How often each reply check fired on real turns over the window: the turns it touched and the total,
    plus the turns that read someone else's text (untrusted_spans) for scale. Content-free by construction:
    the turn log holds counts only."""
    since = time.time() - hours * 3600
    rows = turns if turns is not None else _jsonl(LOGS / "agent-turns.jsonl", since)
    real = [r for r in rows if str(r.get("source") or "") not in NOT_PEOPLE]
    out: dict[str, Any] = {"turns": len(real), "read_others_text": sum(1 for r in real if r.get("untrusted_spans"))}
    for k in REPLY_CHECKS:
        vals = [int(r.get(k) or 0) for r in real if isinstance(r.get(k), (int, float))]
        out[k] = {"turns": sum(1 for v in vals if v), "total": sum(vals)}
    return out


def response(run_turns: bool = False, hours: float = 6, turns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    misses = []
    nightly = (_jsonl(LOGS / "household-eval.jsonl") or [{}])[-1]
    passed, total = int(nightly.get("passed") or 0), int(nightly.get("total") or 0)
    out: dict[str, Any] = {"nightly": {"ts": nightly.get("ts"), "passed": passed, "total": total,
                                       "failing": nightly.get("failing") or []}}
    if total and _pct(passed, total) < 95:
        misses.append(f"nightly {passed}/{total}: {', '.join(nightly.get('failing') or [])}")
    if run_turns:
        p = subprocess.run([sys.executable, str(Path(__file__).with_name("turn_eval.py"))],
                           capture_output=True, text=True, timeout=3600)
        m = re.search(r"checks passed: (\d+)/(\d+)", p.stdout)
        failing = re.findall(r"^  ([\w-]+): (?!.*known gap)", p.stdout, re.M)
        out["turn_eval"] = {"passed": int(m.group(1)) if m else 0, "total": int(m.group(2)) if m else 0,
                            "failing": failing}
        if not m or m.group(1) != m.group(2):
            misses.append(f"turn evals {out['turn_eval']['passed']}/{out['turn_eval']['total']}: {', '.join(failing)}")
    # The nightly truth eval (#385): each brain's latest score; a drop of 3+ from its previous night is a miss.
    lines = [x for x in _jsonl(LOGS / "truth-eval.jsonl") if isinstance(x, dict) and x.get("source") == "nightly"]
    truth = {}
    for brain in ("gemini", "local"):
        mine = [x for x in lines if x.get("brain") == brain]
        if not mine:
            continue
        cur, prev = mine[-1], (mine[-2] if len(mine) > 1 else None)
        truth[brain] = {"score": cur.get("score"), "total": cur.get("total"), "ts": cur.get("ts"),
                        "previous": prev.get("score") if prev else None}
        if prev and (prev.get("score") or 0) - (cur.get("score") or 0) >= 3:
            misses.append(f"truth [{brain}] {cur.get('score')}/{cur.get('total')}, down from {prev.get('score')}: "
                          + ", ".join((cur.get("misses") or [])[:6]))
    if truth:
        out["truth"] = truth
    out["replies"] = reply_checks(hours, turns)
    out.update(ok=not misses, misses=misses)
    return out


# ── memory ──────────────────────────────────────────────────────────────────────────────────────

def memory(facts_by_person: dict[str, list[str]] | None = None, persona: str | None = None) -> dict[str, Any]:
    import agent_memory as am
    if facts_by_person is None:
        import sqlite3
        db = sqlite3.connect(f"file:{am._DB_PATH}?mode=ro", uri=True)
        facts_by_person = {}
        for pid, text in db.execute("SELECT person_id, text FROM facts"):
            facts_by_person.setdefault(pid or am.OWNER, []).append(text)
        db.close()
    if persona is None:
        try:
            persona = am._PERSONA_MD.read_text(encoding="utf-8")
        except OSError:
            persona = ""
    misses, people = [], {}
    for pid, facts in sorted(facts_by_person.items()):
        junk = [f for f in facts if not am._keep_extracted(f, "")]
        dups = [(a, b) for i, a in enumerate(facts) for b in facts[i + 1:] if am._near_duplicate(a, [b])]
        people[pid] = {"facts": len(facts), "junk": len(junk), "junk_pct": _pct(len(junk), len(facts)),
                       "dup_pairs": len(dups), "junk_sample": [j[:80] for j in junk[:3]]}
        if facts and people[pid]["junk_pct"] > 5:
            misses.append(f"{pid}: {len(junk)}/{len(facts)} facts are junk")
        if dups:
            misses.append(f"{pid}: {len(dups)} near-duplicate pairs")
    body = persona.split("\n", 2)[-1]
    guesses = [s for s in re.split(r"(?<=[.!?])\s+", body) if s and am._GUESS.search(s)]
    if guesses:
        misses.append(f"persona: {len(guesses)} guessing sentences")
    return {"ok": not misses, "misses": misses, "people": people, "persona_guesses": len(guesses)}


# ── knowledge ───────────────────────────────────────────────────────────────────────────────────

# (name, query, passive, check(hits) -> problem or None). The right answers are what's stored on
# 10-01: nothing says when the trash goes out; the T-Mobile bill is stored; Ava's ballet repeats.
def _texts(hits: list[dict]) -> list[str]:
    return [str(h.get("chunk_text") or "").lower() for h in hits]


PROBES: list[tuple[str, str, bool, Callable[[list[dict]], str | None]]] = [
    # 10-01: the trash schedule is stored now (Zach, 09:19), so "nothing stored" asks something that isn't.
    ("nothing-stored", "what is the gate code for the boat storage unit", True,
     lambda h: None if not h else f"{len(h)} hits for something never stored"),
    ("trash-day", "when does the garbage go out", False,
     lambda h: None if any("wednesday" in t for t in _texts(h)) else "the garbage schedule (Wednesday nights) wasn't found"),
    ("bill", "T-Mobile bill", False,
     lambda h: None if any("t-mobile" in t for t in _texts(h)) else "the T-Mobile bill wasn't found"),
    ("series-once", "Ava ballet", False,
     lambda h: None if len([t for t in _texts(h) if t.startswith("ballet")]) <= 1 else "a recurring event listed more than once"),
    ("no-downloads-in-context", "what does Robin like", True,
     lambda h: None if not any((x.get("metadata") or {}).get("source") == "downloads" for x in h) else "downloads in a non-file question"),
]


def knowledge(query: Callable[[str, int, bool], list[dict]] | None = None) -> dict[str, Any]:
    if query is None:
        import agent_runtime_lib as lib
        import household_identity as hh
        lib._TURN_IO.role, lib._TURN_IO.person = "admin", {"id": hh.ADMIN_PERSON_ID, "name": hh.ADMIN_PERSON_NAME}

        def query(q: str, k: int, passive: bool) -> list[dict]:
            return lib._kb_query(q, k, timeout=10, passive=passive)
    results, misses, ms = {}, [], []
    for name, q, passive, check in PROBES:
        t0 = time.monotonic()
        try:
            problem = check(query(q, 5, passive))
        except Exception as exc:  # noqa: BLE001
            problem = f"error: {type(exc).__name__}"
        ms.append((time.monotonic() - t0) * 1000)
        results[name] = problem or "ok"
        if problem:
            misses.append(f"{name}: {problem}")
    # The nightly household retrieval eval (#276, retrieval_eval.py): its latest line, when there is one.
    rows = _jsonl(LOGS / "retrieval-eval.jsonl")
    nightly = (rows or [None])[-1]
    retrieval = None
    if isinstance(nightly, dict) and nightly.get("found_total"):
        retrieval = {k: nightly.get(k) for k in ("ts", "found", "found_total", "not_stored", "not_stored_total", "failing",
                                                 "bank")}
        misses += bank_misses(rows)
        if (nightly.get("found") or 0) < nightly["found_total"] or \
                (nightly.get("not_stored") or 0) < (nightly.get("not_stored_total") or 0):
            misses.append(f"nightly retrieval {nightly.get('found')}/{nightly['found_total']} found, "
                          f"{nightly.get('not_stored')}/{nightly.get('not_stored_total')} not stored: "
                          + ", ".join(nightly.get("failing") or []))
    # Inline cites (#912): the nightly cite_precision line, and a miss below 95%.
    cites, cite_miss = cite_status(_jsonl(LOGS / "cite-precision.jsonl"))
    misses += cite_miss
    # The nightly world-model eval (#364, world_eval.py --source nightly): a leak, a kid missing a household
    # fact, or the world model answering worse than the KB alone is a miss. Manual runs don't count.
    wl = [x for x in _jsonl(LOGS / "world-eval.jsonl") if isinstance(x, dict) and x.get("source") == "nightly"]
    world = None
    if wl:
        n = wl[-1]
        world = {k: n.get(k) for k in ("ts", "score", "world", "kb_only", "lookups", "privacy_ok", "privacy_errors",
                                       "household_kid_ok")}
        if n.get("privacy_ok") is False:
            misses.append("nightly world eval: the world model leaked to a viewer")
        if n.get("household_kid_ok") is False:
            misses.append(f"nightly world eval: a kid missed household facts ({n.get('household_kid')}/"
                          f"{n.get('household_kid_total')})")
        if (n.get("world") or 0) < (n.get("kb_only") or 0):
            misses.append(f"nightly world eval: world {n.get('world')}/{n.get('lookups')} below KB-only "
                          f"{n.get('kb_only')}/{n.get('lookups')}")
    # The items surface's nightly red team (items_redteam.py --source nightly): any leak is a miss.
    il = [x for x in _jsonl(LOGS / "items-redteam.jsonl") if isinstance(x, dict) and x.get("source") == "nightly"]
    items_privacy = None
    if il:
        n = il[-1]
        items_privacy = {k: n.get(k) for k in ("ts", "leaks", "views")}
        if n.get("leaks"):
            misses.append(f"nightly items red team: {n.get('leaks')}/{n.get('views')} views leaked to a viewer")
    return {"ok": not misses, "misses": misses, "probes": results, "p50_ms": int(statistics.median(ms)) if ms else 0,
            "retrieval": retrieval, "world": world, "cites": cites, "items_privacy": items_privacy}


# ── consistency ─────────────────────────────────────────────────────────────────────────────────

# Questions whose right answer doesn't change between asks; each is a turn_eval case.
CONSISTENCY_CASES = ("capabilities-limits", "capabilities-where-run", "calendar-tomorrow", "capabilities-whats-new",
                     "capabilities-cant-do")   # 10-01: its self-description just changed (#263)


def consistency(times: int = 3) -> dict[str, Any]:
    import turn_eval as te
    cases = {c[0]: c for c in te.CASES}
    out, misses = {}, []
    for name in CONSISTENCY_CASES:
        c = cases.get(name)
        if not c:
            continue
        _, q, hist, check = c[:4]
        runs = []
        for _ in range(times):
            t = te.ask(q(), hist)
            problems = [f"error: {t.error}"] if t.error else check(t)
            runs.append(problems)
        passed = sum(1 for p in runs if not p)
        out[name] = f"{passed}/{times}"
        if passed < times:
            first = next(p for p in runs if p)
            misses.append(f"{name} {passed}/{times} ({'; '.join(first)[:120]})")
    return {"ok": not misses, "misses": misses, "cases": out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--turns", action="store_true", help="also run every turn eval (about 8 minutes)")
    ap.add_argument("--consistency", action="store_true", help="ask the steady questions three times each")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    card: dict[str, Any] = {"ts": time.time(), "hours": a.hours}
    areas: list[tuple[str, Callable[[], dict]]] = [
        ("system", lambda: system(a.hours)), ("service", lambda: service(a.hours)),
        ("response", lambda: response(a.turns, a.hours)), ("memory", memory), ("knowledge", knowledge)]
    if a.consistency:
        areas.append(("consistency", consistency))
    for name, fn in areas:
        try:
            card[name] = fn()
        except Exception as exc:  # noqa: BLE001 - one broken area never hides the others
            card[name] = {"ok": False, "misses": [f"couldn't measure: {type(exc).__name__}: {str(exc)[:120]}"]}
    card["ok"] = all(card[n]["ok"] for n, _ in areas)
    try:
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(card, default=str) + "\n")
    except OSError:
        pass
    if a.json:
        print(json.dumps(card, indent=2, default=str))
    else:
        for name, _ in areas:
            r = card[name]
            print(f"{'ok  ' if r['ok'] else 'MISS'} {name:<12} " + ("; ".join(r["misses"]) if r["misses"] else _summary(name, r)))
    return 0 if card["ok"] else MISS_EXIT


def _replies_line(rc: dict[str, Any] | None) -> str:
    """"; replies over 41 turns: 2 corrected, 1 retried, 0 weekdays fixed, 1 injection disclosed, 0 held"."""
    if not rc or not rc.get("turns"):
        return ""
    said = {"answer_corrections": "corrected", "degenerate_retries": "retried", "date_corrections": "weekdays fixed",
            "injections_disclosed": "injections disclosed", "held_writes": "writes held"}
    return (f"; replies over {rc['turns']} turns ({rc['read_others_text']} read others' text): "
            + ", ".join(f"{rc[k]['turns']} {said[k]}" for k in REPLY_CHECKS))


def _summary(name: str, r: dict[str, Any]) -> str:
    if name == "system":
        sv = r.get("services")
        return (f"canary {r['canary_ok_pct']}% of {r['canary_runs']}, disks {r['disk_pct']}"
                + (f", services {sv['n'] - sv['down']}/{sv['n']} up, runbook port drift {sv['port_differs']}" if sv else ""))
    if name == "service":
        return (f"{r['turns']} turns, {r['error_pct']}% errors, p50 {r['p50_s']} s, p95 {r['p95_s']} s, fallbacks {r['fallback_pct']}%"
                + ("; items p95 " + ", ".join(f"{v} {x['p95_ms']} ms" for v, x in r["items_latency"].items())
                   if r.get("items_latency") else "")
                + (f", tools widened {r['widened_pct']}% of {r['narrowed']}" if r.get("narrowed") else "")
                + (f", thought harder on {r['effort_high_pct']}% (self-check revised {r['self_revised_pct']}% of "
                   f"{r['self_checked']})" if r.get("effort_high") else ""))
    if name == "response":
        n = r["nightly"]
        return (f"nightly {n['passed']}/{n['total']}" + (f", turn evals {r['turn_eval']['passed']}/{r['turn_eval']['total']}"
                                                         if "turn_eval" in r else "")
                + "".join(f", truth {b} {v['score']}/{v['total']}" for b, v in (r.get("truth") or {}).items())
                + _replies_line(r.get("replies")))
    if name == "memory":
        return ", ".join(f"{p}: {v['facts']} facts" for p, v in r["people"].items())
    if name == "knowledge":
        n = r.get("retrieval")
        return (f"{len(r['probes'])} probes right, p50 {r['p50_ms']} ms"
                + (f"; nightly retrieval {n['found']}/{n['found_total']} found, {n['not_stored']}/{n['not_stored_total']} not stored"
                   if n else "")
                + (f"; KB bank {n['bank']['found']}/{n['bank']['cases']} found, {n['bank']['wrong_zone']} wrong zone"
                   if n and isinstance(n.get("bank"), dict) else "")
                + (f"; inline cites {r['cites']['precision']:.0%} of {r['cites']['emitted']} (lower bound "
                   f"{r['cites']['lower_bound']})" if r.get("cites") and r["cites"].get("precision") is not None else "")
                + (f"; world {r['world']['world']}/{r['world']['lookups']}"
                   f"{'' if r['world'].get('privacy_ok') else ', LEAK'}" if r.get("world") else ""))
    if name == "consistency":
        return ", ".join(f"{k} {v}" for k, v in r["cases"].items())
    return ""


if __name__ == "__main__":
    sys.exit(main())
