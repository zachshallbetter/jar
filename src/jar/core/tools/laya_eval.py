#!/usr/bin/env python3
"""Laya vs Jev on titan's decision questions (titan-node#475). Local only: Laya runs on titan, and every
state sent anywhere is synthetic (fixtures, eval prompts), never household data.

Laya (Convai Innovations, Apache 2.0, ModernBERT-large + a decision head) takes Jev's question shapes
(choice / score / noul) and answers in one forward pass. This measures, per question:
  agreement   Laya's answer = Jev's (Jev through the decision broker, source "eval")
  accuracy    against the set's truth, where it has one, for both
  latency     p50 / p95 per call
  cost        Jev's reported cost; Laya's is the GPU it occupies (no per-call charge)

Sets (all synthetic):
  household   claude-jev's 66 household messages (category, action_today), with truth
  facts       titan's lasting_fact gate: the household eval's "Remember …" prompts (true) and its
              questions (false), with truth; every turn-eval prompt for agreement only
  router      titan's brain / needs_tools router over every turn-eval prompt, agreement only

  CUDA_VISIBLE_DEVICES= /ai/models/laya-venv/bin/python scripts/tools/laya_eval.py --household PATH
      [--model english|typed-decisions] [--no-jev] [--json OUT]        (on titan, agent.env loaded for Jev)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _p(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


# ── the sets ─────────────────────────────────────────────────────────────────────────────────────

def household_set(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spec = json.loads(path.with_name("household.spec.json").read_text())
    items = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    ctx = spec.get("context") or ""
    return spec["questions"], [{"id": i["id"], "state": {"context": ctx, "item": i["text"]}, "truth": i.get("truth") or {},
                                "tags": i.get("tags") or []} for i in items]


def _turn_prompts() -> list[tuple[str, str]]:
    import turn_eval as te
    out, seen = [], set()
    for grp in ("CASES", "PHONE_CASES", "WRITE_CASES", "HONESTY_CASES", "LONG_CASES", "INJECTION_CASES"):
        for c in getattr(te, grp, []):
            try:
                q = c[1]()
            except Exception:  # noqa: BLE001
                continue
            if q not in seen:
                seen.add(q)
                out.append((c[0], q))
    return out


def facts_set() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    import agent_runtime_lib as lib
    items: list[dict[str, Any]] = []
    for line in Path(lib.__file__).with_name("household-agent-eval.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        sc = json.loads(line)
        for i, p in enumerate(sc.get("ingest") or []):
            items.append({"id": f"{sc['name']}-tell{i}", "state": {"message": p}, "truth": {"lasting_fact": True}})
        for i, c in enumerate(sc.get("checks") or []):
            items.append({"id": f"{sc['name']}-ask{i}", "state": {"message": c["q"]}, "truth": {"lasting_fact": False}})
    items += [{"id": n, "state": {"message": q}, "truth": {}} for n, q in _turn_prompts()]
    return lib._JEV_FACT_QUESTION, items


def router_set() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    import agent_runtime_lib as lib
    return lib._JEV_BRAIN_QUESTIONS, [{"id": n, "state": {"message": q}, "truth": {}} for n, q in _turn_prompts()]


# ── the backends: one shape out ({question: {"value", "sure"}}, ms, cost) ───────────────────────

def _sure(a: dict[str, Any]) -> float:
    if a.get("type") == "noul" or ("noul" in a and "choice" not in a):
        p = float(a.get("noul", 0.5))
        return max(p, 1 - p)
    return float(a.get("confidence", a.get("probability", 0.0)) or 0.0)


def _value(a: dict[str, Any]) -> Any:
    if "choice" in a:
        return a["choice"]
    if "score" in a:
        return a["score"]
    return float(a.get("noul", 0.5)) >= 0.5


def jev_backend() -> Callable[[dict, dict], tuple[dict, float, float]]:
    import agent_runtime_lib as lib
    jev = lib._jev_client()
    if jev is None:
        raise SystemExit("no Jev client (broker credentials) here")

    def call(state: dict, questions: dict) -> tuple[dict, float, float]:
        r = jev.decide(state, questions, timeout=20, retries=1, source="laya-eval",
                       origin={"source": "eval", "person": "", "role": ""})
        ans = {k: {"value": _value(a), "sure": _sure(a)} for k, a in r["answers"].items()}
        return ans, float(r["_meta"]["latency_ms"]), float(r["_meta"].get("cost_usd") or 0.0)
    return call


def laya_backend(model: str) -> Callable[[dict, dict], tuple[dict, float, float]]:
    """One Laya checkpoint ("english", "typed-decisions"), on CPU (titan's GPUs are the local agent's,
    the code model's and the Decider's). The first call loads it; that load isn't counted."""
    from laya import Router   # the laya venv only
    router = Router(device="cpu", max_loaded=1)
    router.predict({"message": "warm up"}, {"x": {"type": "noul", "instructions": "Is this a test?"}}, model=model)

    def call(state: dict, questions: dict) -> tuple[dict, float, float]:
        t0 = time.perf_counter()
        r = router.predict(state, questions, model=model)
        ms = (time.perf_counter() - t0) * 1000
        ans = {k: {"value": _value(a), "sure": _sure(a)} for k, a in (r.get("answers") or {}).items()}
        return ans, ms, 0.0
    return call


# ── running and scoring ──────────────────────────────────────────────────────────────────────────

def run(name: str, questions: dict, items: list[dict], backends: dict[str, Callable]) -> dict[str, Any]:
    rows = []
    lat: dict[str, list[float]] = {b: [] for b in backends}
    cost: dict[str, float] = {b: 0.0 for b in backends}
    for it in items:
        row = {"id": it["id"], "truth": it["truth"], "tags": it.get("tags") or []}
        for b, fn in backends.items():
            try:
                ans, ms, c = fn(it["state"], questions)
            except Exception as exc:  # noqa: BLE001 - one failure is a row, not the run
                row[b] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
                continue
            row[b] = ans
            lat[b].append(ms)
            cost[b] += c
        rows.append(row)
    out: dict[str, Any] = {"set": name, "items": len(items), "questions": {}}
    for q in questions:
        s: dict[str, Any] = {}
        for b in backends:
            got = [r for r in rows if isinstance(r.get(b), dict) and q in r[b]]
            truth = [r for r in got if q in r["truth"]]
            if truth:
                s[f"{b}_acc"] = round(sum(r[b][q]["value"] == r["truth"][q] for r in truth) / len(truth), 3)
                s[f"{b}_n_truth"] = len(truth)
                wrong_sure = [r["id"] for r in truth if r[b][q]["value"] != r["truth"][q] and r[b][q]["sure"] >= 0.9]
                s[f"{b}_confident_wrong"] = wrong_sure[:10]
            s[f"{b}_errors"] = sum(1 for r in rows if "error" in (r.get(b) or {}))
        if "jev" in backends and "laya" in backends:
            both = [r for r in rows if all(isinstance(r.get(b), dict) and q in r[b] for b in ("jev", "laya"))]
            s["agreement"] = round(sum(r["jev"][q]["value"] == r["laya"][q]["value"] for r in both) / len(both), 3) if both else None
            s["n_both"] = len(both)
        out["questions"][q] = s
    out["latency_ms"] = {b: {"p50": round(statistics.median(v), 1) if v else None, "p95": round(_p(v, 0.95), 1)}
                         for b, v in lat.items()}
    out["cost_usd"] = {b: round(c, 5) for b, c in cost.items()}
    out["rows"] = rows   # synthetic states only; kept for the review
    return out


def compare(jev_path: Path, laya_path: Path) -> int:
    """Agreement per question between a --jev-only run and a --no-jev Laya run of the same sets."""
    jev = {r["set"]: r for r in json.loads(jev_path.read_text())["results"]}
    laya = json.loads(laya_path.read_text())
    print(f"Laya {laya.get('model')} vs Jev")
    for lr in laya["results"]:
        jr = jev.get(lr["set"])
        if not jr:
            continue
        jrows = {r["id"]: r.get("jev") or {} for r in jr["rows"]}
        print(f"── {lr['set']}: latency laya {lr['latency_ms']['laya']}, jev {jr['latency_ms']['jev']}; "
              f"jev cost ${jr['cost_usd']['jev']}")
        for q in lr["questions"]:
            pairs = [(r["laya"][q], jrows[r["id"]][q], r["truth"].get(q)) for r in lr["rows"]
                     if q in (r.get("laya") or {}) and q in jrows.get(r["id"], {})]
            agree = sum(lv["value"] == jv["value"] for lv, jv, _ in pairs)
            truth = [(lv, jv, t) for lv, jv, t in pairs if t is not None]
            line = f"   {q}: agreement {agree}/{len(pairs)}"
            if truth:
                line += (f"; vs truth laya {sum(lv['value'] == t for lv, _, t in truth)}/{len(truth)}, "
                         f"jev {sum(jv['value'] == t for _, jv, t in truth)}/{len(truth)}")
            sure_dis = sum(1 for lv, jv, _ in pairs if lv["value"] != jv["value"] and jv["sure"] >= 0.9 and lv["sure"] >= 0.9)
            print(line + f"; both sure (≥0.9) and disagreeing: {sure_dis}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--household", type=Path, help="claude-jev calibration/household.jsonl (with its spec beside it)")
    ap.add_argument("--model", default="english", help="the Laya checkpoint: english | typed-decisions")
    ap.add_argument("--no-jev", action="store_true")
    ap.add_argument("--jev-only", action="store_true", help="Jev alone (titan's own python: the broker key needs "
                    "cryptography, which the laya venv doesn't have); --compare joins the two")
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("JEV_JSON", "LAYA_JSON"))
    ap.add_argument("--sets", default="household,facts,router")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    if a.compare:
        return compare(*a.compare)
    backends: dict[str, Callable] = {} if a.jev_only else {"laya": laya_backend(a.model)}
    if not a.no_jev:
        backends["jev"] = jev_backend()
    results = []
    for s in a.sets.split(","):
        if s == "household":
            if not a.household:
                continue
            q, items = household_set(a.household)
        elif s == "facts":
            q, items = facts_set()
        else:
            q, items = router_set()
        r = run(s, q, items, backends)
        results.append(r)
        print(f"── {s} ({r['items']} items) latency {r['latency_ms']} cost {r['cost_usd']}")
        for qn, st in r["questions"].items():
            print(f"   {qn}: " + ", ".join(f"{k}={v}" for k, v in st.items()))
    if a.json:
        a.json.write_text(json.dumps({"model": a.model, "results": results}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
