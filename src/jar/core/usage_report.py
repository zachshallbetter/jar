#!/usr/bin/env python3
"""API usage and cost, per service per day and month (#1388), from what titan already records. Counts only: no
prompt, reply or decision content is read.

Sources:
  - the agent turn log (AGENT_TURN_LOG, /ai/logs/agent-turns.jsonl): per turn the brain (gemini | local), prompt
    tokens read fresh and from cache, and output tokens (#1388 adds `output_tokens`);
  - Jev's own usage log ($JEV_HOME/usage.jsonl): per decision call its cost in USD (OpenRouter's usage.cost, or
    the client's per-token price), latency and ok.
Prices and subscriptions are Zach's to set, in USAGE_CONFIG (/ai/datasets/connectors/usage_config.json, 0600):
  {"prices": {"gemini": {"input_per_mtok": <USD>, "cached_per_mtok": <USD>, "output_per_mtok": <USD>}},
   "subscriptions": [{"service": "Railway", "tier": "Pro", "monthly_usd": <USD>, "renews_day": 14}]}
A service with no price shows tokens and "price not set": this script never guesses a price. The local model costs
nothing per token (it's titan's own GPUs); Jev's cost is what Jev's log recorded.

  usage_report.py [--month YYYY-MM] [--days N] [--json]
Admin and system information only: run it on titan as the deploy user; nothing here is served to other members.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

TURN_LOG = Path(os.environ.get("AGENT_TURN_LOG", "/ai/logs/agent-turns.jsonl"))
JEV_LOG = Path(os.environ.get("JEV_HOME") or Path.home() / ".claude" / "jev") / "usage.jsonl"
CONFIG = Path(os.environ.get("USAGE_CONFIG", "/ai/datasets/connectors/usage_config.json"))


def _lines(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue   # a torn last line while the agent writes: skipped, never fatal
    except FileNotFoundError:
        return


def _day(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    try:
        cfg = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {"prices": {}, "subscriptions": []}
    return {"prices": cfg.get("prices") or {}, "subscriptions": cfg.get("subscriptions") or []}


def collect(turns: Iterable[dict[str, Any]], jev: Iterable[dict[str, Any]],
            keep: Any = lambda day: True) -> dict[str, dict[str, dict[str, float]]]:
    """{day: {service: {turns|calls, prompt_fresh, prompt_cached, output, cost_usd}}}, counts only."""
    out: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for t in turns:
        day = _day(t.get("ts"))
        brain = t.get("brain")
        if not day or not keep(day) or brain not in ("gemini", "local"):
            continue
        row = out[day][brain]
        row["turns"] += 1
        for k_in, k_out in (("prompt_fresh", "prompt_fresh"), ("prompt_cached", "prompt_cached"),
                            ("output_tokens", "output")):
            v = t.get(k_in)
            if isinstance(v, (int, float)) and v >= 0:
                row[k_out] += v
    for j in jev:
        day = _day(j.get("ts"))
        if not day or not keep(day):
            continue
        row = out[day]["jev"]
        row["calls"] += 1
        c = j.get("cost")
        if isinstance(c, (int, float)) and c >= 0:
            row["cost_usd"] += c
    return out


def price(service: str, row: dict[str, float], prices: dict[str, Any]) -> float | None:
    """The estimated cost of a row, or None when Zach hasn't set this service's price (never guessed)."""
    if service == "local":
        return 0.0
    if service == "jev":
        return row.get("cost_usd", 0.0)
    p = prices.get(service)
    if not isinstance(p, dict):
        return None
    try:
        return (row.get("prompt_fresh", 0) * float(p["input_per_mtok"])
                + row.get("prompt_cached", 0) * float(p.get("cached_per_mtok", p["input_per_mtok"]))
                + row.get("output", 0) * float(p["output_per_mtok"])) / 1_000_000
    except (KeyError, TypeError, ValueError):
        return None


def summarize(days: dict[str, dict[str, dict[str, float]]], cfg: dict[str, Any]) -> dict[str, Any]:
    by_month: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for day, svcs in days.items():
        for svc, row in svcs.items():
            for k, v in row.items():
                by_month[day[:7]][svc][k] += v
    months = {}
    for month, svcs in sorted(by_month.items()):
        m = {}
        for svc, row in sorted(svcs.items()):
            cost = price(svc, row, cfg["prices"])
            m[svc] = {**{k: round(v, 6) for k, v in row.items() if k != "cost_usd"},
                      "cost_usd": None if cost is None else round(cost, 4),
                      **({} if cost is not None else {"note": "price not set"})}
        months[month] = m
    subs = [{"service": s.get("service"), "tier": s.get("tier"), "monthly_usd": s.get("monthly_usd"),
             "renews_day": s.get("renews_day")} for s in cfg["subscriptions"] if isinstance(s, dict)]
    fixed = sum(float(s["monthly_usd"]) for s in subs if isinstance(s.get("monthly_usd"), (int, float)))
    return {"months": months, "subscriptions": subs, "subscriptions_monthly_usd": round(fixed, 2),
            "days": {d: {s: {k: round(v, 6) for k, v in r.items()} for s, r in sv.items()} for d, sv in sorted(days.items())}}


def text(rep: dict[str, Any]) -> str:
    lines = []
    for month, svcs in rep["months"].items():
        lines.append(f"{month}")
        for svc, r in svcs.items():
            n = int(r.get("turns") or r.get("calls") or 0)
            cost = "price not set" if r["cost_usd"] is None else f"${r['cost_usd']:.2f}"
            toks = (f"  in {int(r.get('prompt_fresh', 0)):,} fresh + {int(r.get('prompt_cached', 0)):,} cached,"
                    f" out {int(r.get('output', 0)):,}") if svc != "jev" else ""
            lines.append(f"  {svc:7} {n:6} {'turns' if svc != 'jev' else 'calls'}{toks}  {cost}")
    if rep["subscriptions"]:
        lines.append(f"subscriptions: ${rep['subscriptions_monthly_usd']:.2f}/month")
        for s in rep["subscriptions"]:
            lines.append(f"  {s['service']} {s.get('tier') or ''}  ${s.get('monthly_usd')}  renews day {s.get('renews_day')}")
    else:
        lines.append(f"subscriptions: none recorded (add them to {CONFIG})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--month", help="YYYY-MM: only this month")
    ap.add_argument("--days", type=int, help="only the last N days")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    keep = lambda day: True  # noqa: E731
    if a.month:
        keep = lambda day: day.startswith(a.month)  # noqa: E731
    elif a.days:
        cut = datetime.now(timezone.utc).timestamp() - a.days * 86400
        cutday = datetime.fromtimestamp(cut, tz=timezone.utc).strftime("%Y-%m-%d")
        keep = lambda day: day >= cutday  # noqa: E731
    rep = summarize(collect(_lines(TURN_LOG), _lines(JEV_LOG), keep), load_config())
    print(json.dumps(rep, indent=1) if a.json else text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
