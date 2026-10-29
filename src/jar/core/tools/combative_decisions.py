#!/usr/bin/env python3
"""A combative run of titan's decision gates (#373; Zach, 10-02: "make a combative test run"): fresh,
adversarial, synthetic messages Jev was never calibrated on, with truth written by hand, never by a model.

Gates: brain + needs_tools (the router), lasting_fact (the save gate), category + action_today (claude-jev's
household questions), the skill pick. Kinds: near-misses, negations, sarcasm, two intents, injected
instructions in quoted text, kid phrasing, very short and very long, dates that look like numbers, mixed
household and work, "titan, ignore Jev". Items whose truth is genuinely arguable are marked and scored apart.

Judges, same items: Jev (the decision broker, source eval), the local Decider (127.0.0.1:8031, Jev's wire
format), Qwen3-8B (the agent's llama-server, asked for the same JSON) and Strands Decider (AWS Strands Labs,
127.0.0.1:8032 when served, #582). Synthetic only: nothing from the
household goes anywhere.

  python3 scripts/combative_decisions.py [--judges jev,decider,qwen] [--json OUT]      (on titan, agent.env loaded)
  python3 scripts/combative_decisions.py --report OUT1.json [OUT2.json …]               (join runs into one table)
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parents[1]   # scripts/ (this file is in scripts/tools/)
sys.path.insert(0, str(HERE))
ITEMS = HERE / "evalsets" / "combative_decisions.jsonl"

# claude-jev's household questions (calibration/household.spec.json), so the household gate asks what Jev was
# calibrated to answer; only the messages are new.
HOUSEHOLD_CONTEXT = "Messages received by a family household (two parents, two kids in elementary school)."
HOUSEHOLD_Q = {
    "category": {"type": "choice", "instructions": "What kind of message is `item`?", "criteria": {
        "bills": "A legitimate charge, payment request, balance, or statement from a company, landlord, utility, or service the household uses",
        "family": "A personal message from a relative or friend, whatever it is about",
        "spam": "Unsolicited marketing, a scam, or phishing, including fake notices that imitate real companies or suspicious links",
        "appointment": "Scheduling, confirming, or reminding about an appointment or service visit (doctor, dentist, vet, repair, haircut, DMV)",
        "delivery": "Package, order, or food delivery status from a retailer or carrier",
        "school": "A message from the kids' school, teacher, district, or PTA"}},
    "action_today": {"type": "noul", "instructions": "Someone in the household needs to do something today because of `item`.",
                     "criteria": {"true": "A deadline today or tomorrow, a same-day reply or pickup requested, or something goes wrong if it's ignored today",
                                  "false": "Informational, a later deadline, no action needed, or spam that should simply be ignored"}},
}


def skills() -> dict[str, str]:
    out = {}
    for f in sorted((HERE / "skills").glob("*/SKILL.md")):
        m = re.search(r"^description:\s*(.+)$", f.read_text(), re.M)
        out[f.parent.name] = (m.group(1).strip().strip('"') if m else f.parent.name)[:400]
    return out


def gate_questions(gate: str) -> dict[str, Any]:
    import agent_runtime_lib as lib
    if gate == "router":
        return lib._JEV_BRAIN_QUESTIONS
    if gate == "fact":
        return lib._JEV_FACT_QUESTION
    if gate == "household":
        return HOUSEHOLD_Q
    crit = {"none": "No special skill: small talk, general knowledge, advice, or sorting/checking text the user pasted",
            **skills(), "insufficient_information": "The message is too vague to tell"}
    return {"skill": {"type": "choice", "criteria": crit,
                      "instructions": "Which one skill does a home assistant need to handle `message`?"}}


def state_for(item: dict[str, Any]) -> dict[str, Any]:
    return {"context": HOUSEHOLD_CONTEXT, "item": item["text"]} if item["gate"] == "household" else {"message": item["text"]}


def _value(a: dict[str, Any]) -> Any:
    if "choice" in a:
        return a["choice"]
    return float(a.get("noul", 0.5)) >= 0.5


# ── judges: (state, questions) → ({question: value}, ms) ───────────────────────────────────────

def jev() -> Callable[[dict, dict], tuple[dict, float]]:
    import agent_runtime_lib as lib
    j = lib._jev_client()
    if j is None:
        raise SystemExit("no Jev client here (load agent.env)")

    def call(state: dict, qs: dict) -> tuple[dict, float]:
        r = j.decide(state, qs, timeout=20, retries=1, source="combative-eval", origin={"source": "eval", "person": "", "role": ""})
        return {k: _value(a) for k, a in r["answers"].items()}, float(r["_meta"]["latency_ms"])
    return call


def decider(url: str = "http://127.0.0.1:8031/v1/systemone") -> Callable[[dict, dict], tuple[dict, float]]:
    def call(state: dict, qs: dict) -> tuple[dict, float]:
        t0 = time.perf_counter()
        req = urllib.request.Request(url, data=json.dumps({"state": state, "questions": qs}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        return {k: _value(a) for k, a in (data.get("answers") or {}).items()}, (time.perf_counter() - t0) * 1000
    return call


def qwen(url: str = "http://127.0.0.1:8010/v1/chat/completions") -> Callable[[dict, dict], tuple[dict, float]]:
    """Qwen3-8B asked for the same answers as JSON: a choice's key, or true/false for a noul."""
    def call(state: dict, qs: dict) -> tuple[dict, float]:
        spec = {k: ({"type": "choice", "options": q["criteria"], "instructions": q.get("instructions", "")} if q["type"] == "choice"
                    else {"type": "yes/no", "instructions": q.get("instructions", ""), **({"meaning": q["criteria"]} if q.get("criteria") else {})})
                for k, q in qs.items()}
        prompt = ("Answer each question about the state below. For a choice, give exactly one option key. For yes/no, "
                  "give true or false. Reply with only a JSON object mapping each question name to its answer.\n\n"
                  f"STATE:\n{json.dumps(state, ensure_ascii=False)}\n\nQUESTIONS:\n{json.dumps(spec, ensure_ascii=False)}\n/no_think")
        t0 = time.perf_counter()
        req = urllib.request.Request(url, data=json.dumps({"model": "local", "messages": [{"role": "user", "content": prompt}],
                                                           "temperature": 0, "max_tokens": 200}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            text = json.loads(r.read())["choices"][0]["message"].get("content") or ""
        ms = (time.perf_counter() - t0) * 1000
        m = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", text, flags=re.S), re.S)
        got = json.loads(m.group(0)) if m else {}
        out = {}
        for k, q in qs.items():
            v = got.get(k)
            if q["type"] == "choice":
                out[k] = str(v) if v is not None else None
            else:
                out[k] = (v is True) or (str(v).strip().lower() in ("true", "yes", "1")) if v is not None else None
        return out, ms
    return call


def strands() -> Callable[[dict, dict], tuple[dict, float]]:
    """Strands Decider (AWS Strands Labs, #582): `strands-decider serve` speaks Jev's wire format, so it's the
    Decider's judge at another address (STRANDS_DECIDER_URL, default 127.0.0.1:8032)."""
    import os
    return decider(os.environ.get("STRANDS_DECIDER_URL", "http://127.0.0.1:8032/v1/systemone"))


JUDGES = {"jev": jev, "decider": decider, "qwen": qwen, "strands": strands}


def run(judges: list[str], items: list[dict[str, Any]]) -> dict[str, Any]:
    calls = {j: JUDGES[j]() for j in judges}
    rows = []
    for it in items:
        qs = gate_questions(it["gate"])
        row = {"id": it["id"], "gate": it["gate"], "kind": it["kind"], "truth": it["truth"], "ambiguous": it["ambiguous"]}
        for j, fn in calls.items():
            try:
                ans, ms = fn(state_for(it), qs)
                row[j] = {"answers": ans, "ms": round(ms, 1)}
            except Exception as exc:  # noqa: BLE001 - one failure is a row, not the run
                row[j] = {"error": f"{type(exc).__name__}: {str(exc)[:100]}"}
        rows.append(row)
    return {"judges": judges, "rows": rows}


def report(runs: list[dict[str, Any]], items: list[dict[str, Any]]) -> str:
    """One table: per gate and question, each judge's accuracy on the clear items and on the arguable ones,
    a confusion summary, latency, and the items Jev got wrong."""
    by_id = {it["id"]: it for it in items}
    merged: dict[str, dict[str, Any]] = {}
    judges: list[str] = []
    for r in runs:
        judges += [j for j in r["judges"] if j not in judges]
        for row in r["rows"]:
            merged.setdefault(row["id"], {**{k: row[k] for k in ("id", "gate", "kind", "truth", "ambiguous")}}).update(
                {j: row[j] for j in r["judges"] if j in row})
    lines = ["| gate · question | n clear / arguable | " + " | ".join(f"{j} clear" for j in judges) + " | "
             + " | ".join(f"{j} arguable" for j in judges) + " |",
             "|---|---|" + "---|" * (2 * len(judges))]
    conf: dict[str, Counter] = {}
    wrong: list[str] = []
    for gate in ("router", "fact", "household", "skill"):
        rows = [r for r in merged.values() if r["gate"] == gate]
        for q in (rows[0]["truth"] if rows else {}):
            clear = [r for r in rows if q not in r["ambiguous"]]
            arg = [r for r in rows if q in r["ambiguous"]]

            def acc(rs: list, j: str) -> str:
                got = [r for r in rs if isinstance(r.get(j), dict) and q in (r[j].get("answers") or {})]
                return f"{sum(r[j]['answers'][q] == r['truth'][q] for r in got)}/{len(got)}" if got else "—"
            lines.append(f"| {gate} · {q} | {len(clear)} / {len(arg)} | " + " | ".join(acc(clear, j) for j in judges) + " | "
                         + " | ".join(acc(arg, j) for j in judges) + " |")
            for j in judges:
                c = conf.setdefault(f"{j} · {q}", Counter())
                for r in clear:
                    a = (r.get(j) or {}).get("answers") or {}
                    if q in a and a[q] != r["truth"][q]:
                        c[f"{r['truth'][q]}→{a[q]}"] += 1
            for r in clear:
                a = (r.get("jev") or {}).get("answers") or {}
                if q in a and a[q] != r["truth"][q]:
                    wrong.append(f"- `{r['id']}` ({r['kind']}) {q}: truth {r['truth'][q]}, Jev {a[q]} — “{by_id[r['id']]['text'][:90]}”")
    lat = []
    for j in judges:
        ms = [r[j]["ms"] for r in merged.values() if isinstance(r.get(j), dict) and "ms" in r[j]]
        errs = sum(1 for r in merged.values() if "error" in (r.get(j) or {}))
        lat.append(f"{j}: p50 {statistics.median(ms):.0f} ms" if ms else f"{j}: —")
        lat[-1] += f", {errs} errors"
    out = ["\n".join(lines), "", "**Mistakes on clear items (truth→answer, count):**"]
    out += [f"- {k}: " + ", ".join(f"{t} ×{n}" for t, n in c.most_common()) for k, c in conf.items() if c]
    out += ["", "**Latency:** " + "; ".join(lat), "", "**Jev's wrong answers on clear items:**"] + (wrong or ["- none"])
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judges", default="jev,decider,qwen")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--report", nargs="+", type=Path)
    a = ap.parse_args()
    items = [json.loads(l) for l in ITEMS.read_text().splitlines() if l.strip()]
    if a.report:
        print(report([json.loads(p.read_text()) for p in a.report], items))
        return 0
    res = run([j for j in a.judges.split(",") if j in JUDGES], items)
    if a.json:
        a.json.write_text(json.dumps(res, indent=1))
    print(report([res], items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
