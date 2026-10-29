#!/usr/bin/env python3
"""usage_report (#1388): per-service usage and cost from the turn log and Jev's usage log, counts only. Checks the
sums, that a price is never guessed (no price → "price not set", cost None), that the local model costs 0, that
Jev's recorded cost is used as is, month and day filters, subscriptions, torn lines, and that no content field from
a log line ever reaches the report. Also checks the turn log now records output tokens (Gemini and local).

Run: python3 scripts/test_usage_report.py
"""
# gate: hermetic
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="titan-usage-"))
os.environ["AGENT_TURN_LOG"] = str(TMP / "turns.jsonl")
os.environ["JEV_HOME"] = str(TMP / "jev")
os.environ["USAGE_CONFIG"] = str(TMP / "usage_config.json")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import usage_report as U  # noqa: E402

passed = failed = 0


def check(label, got, want):
    global passed, failed
    ok = got == want
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got {got!r} want {want!r}"))


from datetime import datetime, timezone  # noqa: E402
OCT9, OCT10, SEP30 = (datetime(2026, m, d, 12, tzinfo=timezone.utc).timestamp() for m, d in ((10, 9), (10, 10), (9, 30)))
turns = [
    {"ts": OCT9, "brain": "gemini", "prompt_fresh": 1000, "prompt_cached": 4000, "output_tokens": 200, "reply": "SECRET"},
    {"ts": OCT9 + 60, "brain": "gemini", "prompt_fresh": 500, "prompt_cached": 0, "output_tokens": 100},
    {"ts": OCT10, "brain": "local", "prompt_fresh": 900, "prompt_cached": 7000, "output_tokens": 80},
    {"ts": SEP30, "brain": "gemini", "prompt_fresh": 10, "output_tokens": 1},
    {"ts": OCT10, "brain": None},                          # a turn with no brain (refused early): not counted
]
jev = [{"ts": OCT9, "source": "router", "cost": 0.0001, "ok": True, "question": "SECRET"},
       {"ts": OCT10, "source": "facts", "cost": 0.0003, "ok": True}]
(TMP / "turns.jsonl").write_text("\n".join(json.dumps(t) for t in turns) + "\n{\"ts\": 17916")   # a torn last line
(TMP / "jev").mkdir()
(TMP / "jev" / "usage.jsonl").write_text("\n".join(json.dumps(j) for j in jev) + "\n")

print("── sums and pricing ──")
days = U.collect(U._lines(U.TURN_LOG), U._lines(U.JEV_LOG))
rep = U.summarize(days, U.load_config())
oct_ = rep["months"]["2026-10"]
check("gemini: 2 turns in October, tokens summed", (oct_["gemini"]["turns"], oct_["gemini"]["prompt_fresh"],
      oct_["gemini"]["prompt_cached"], oct_["gemini"]["output"]), (2, 1500, 4000, 300))
check("no price set: cost None and said, never guessed", (oct_["gemini"]["cost_usd"], oct_["gemini"]["note"]),
      (None, "price not set"))
check("the local model costs 0 per token", oct_["local"]["cost_usd"], 0.0)
check("Jev's recorded cost is used as is", (oct_["jev"]["calls"], oct_["jev"]["cost_usd"]), (2, 0.0004))
check("September is its own month", rep["months"]["2026-09"]["gemini"]["turns"], 1)
check("a torn last line and a brainless turn are skipped", sum(r["turns"] for r in oct_.values() if "turns" in r), 3)

cfg = {"prices": {"gemini": {"input_per_mtok": 0.10, "cached_per_mtok": 0.025, "output_per_mtok": 0.40}},
       "subscriptions": [{"service": "Railway", "tier": "Pro", "monthly_usd": 20, "renews_day": 14}]}
(TMP / "usage_config.json").write_text(json.dumps(cfg))
rep = U.summarize(days, U.load_config())
want = round((1500 * 0.10 + 4000 * 0.025 + 300 * 0.40) / 1e6, 4)
check("with a price: fresh, cached and output priced separately", rep["months"]["2026-10"]["gemini"]["cost_usd"], want)
check("subscriptions listed and summed", (rep["subscriptions_monthly_usd"], rep["subscriptions"][0]["service"]),
      (20.0, "Railway"))
bad = {"prices": {"gemini": {"input_per_mtok": "x"}}}
(TMP / "usage_config.json").write_text(json.dumps(bad))
check("a malformed price is 'not set', not a crash", U.summarize(days, U.load_config())["months"]["2026-10"]["gemini"]["cost_usd"], None)

print("── filters and privacy ──")
only10 = U.summarize(U.collect(U._lines(U.TURN_LOG), U._lines(U.JEV_LOG), lambda d: d == "2026-10-10"), U.load_config())
check("a day filter keeps only that day", sorted(only10["days"]), ["2026-10-10"])
blob = json.dumps(U.summarize(days, U.load_config())) + U.text(U.summarize(days, U.load_config()))
check("no content field from a log line reaches the report", "SECRET" in blob, False)
check("the text form says 'price not set' when it isn't", "price not set" in U.text(U.summarize(days, U.load_config())), True)

print("── the turn log records output tokens (#1388) ──")
import agent_runtime_lib as A  # noqa: E402
A._TURN_INFO.info = {}
A._note_prompt_usage({"usageMetadata": {"promptTokenCount": 120, "cachedContentTokenCount": 100,
                                        "candidatesTokenCount": 30, "thoughtsTokenCount": 12}}, "gemini")
check("gemini: output = candidates + thoughts; fresh/cached as before",
      (A._TURN_INFO.info.get("output_tokens"), A._TURN_INFO.info.get("prompt_fresh"), A._TURN_INFO.info.get("prompt_cached")),
      (42, 20, 100))
A._TURN_INFO.info = {}
A._note_prompt_usage({"timings": {"prompt_n": 50, "cache_n": 900, "prompt_ms": 400, "predicted_n": 64}}, "local")
check("local: output = predicted_n", A._TURN_INFO.info.get("output_tokens"), 64)
A._TURN_INFO.info = {}
A._note_prompt_usage({"usageMetadata": {}}, "gemini")
check("no usage block: nothing recorded", A._TURN_INFO.info, {})

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
