#!/usr/bin/env python3
"""
bench_llm_endpoint.py — repeatable throughput/latency benchmark for an
OpenAI-compatible chat-completions endpoint (llama.cpp llama-server, etc).

Usage:
  python3 scripts/tools/bench_llm_endpoint.py --url http://127.0.0.1:8198 \
      --model qwen3.6-35b-a3b --label "ctx32768-gpu0+2" \
      --sizes short,medium,long --iterations 5 --out /tmp/bench.json

Prompt sizes (approximate prompt tokens, built from repeated filler lines):
  short   ~300 tokens   — single-shot chat turn
  medium  ~4000 tokens  — mid-loop agentic context
  long    ~12000 tokens — late-loop agentic context (where the original
                          truncation bug reproduced)
  xlong   ~22000 tokens — stress case (only include explicitly; slow)

Also runs an optional multi-turn simulation (--multiturn N) that appends
each response back into the next request, emulating a real agentic loop's
growing context instead of independent single-shot requests.

Outputs per-size latency (mean/median/p95), tokens/sec, and error count to
stdout and, if --out is given, a JSON report for later diffing between runs.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
import urllib.error
import urllib.request

FILLER = "function example{n}(){{ return {n}; }}\n"

SIZE_REPEATS = {
    "short": 15,     # ~300 tokens
    "medium": 200,   # ~4000 tokens
    "long": 600,     # ~12000 tokens
    "xlong": 1250,   # ~22000 tokens
}


def build_prompt(repeats: int, tail: str, offset: int = 0) -> str:
    # offset shifts every line's content so repeated calls don't share a
    # long common prefix with prior calls — llama.cpp's prompt cache would
    # otherwise turn identical-prefix repeats into near-free cache hits,
    # masking the real prefill cost this benchmark exists to measure.
    body = "".join(FILLER.format(n=i + offset) for i in range(repeats))
    return body + "\n" + tail


def call(url: str, model: str, messages: list[dict], max_tokens: int, timeout: int) -> dict:
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        body = json.loads(resp.read())
        elapsed = time.perf_counter() - t0
        usage = body.get("usage", {})
        content = body["choices"][0]["message"]["content"]
        return {
            "ok": True,
            "elapsed": elapsed,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "content_preview": content[:80],
        }
    except urllib.error.HTTPError as e:
        return {"ok": False, "elapsed": time.perf_counter() - t0, "error": f"HTTP {e.code}: {e.read()[:200]}"}
    except Exception as e:
        return {"ok": False, "elapsed": time.perf_counter() - t0, "error": str(e)}


def pctl(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def run_size(url: str, model: str, size: str, iterations: int, timeout: int) -> dict:
    repeats = SIZE_REPEATS[size]
    results = []
    for i in range(iterations):
        # Bounded random offset (not i * repeats): a monotonically growing
        # offset increases digit-count in the filler numbers each iteration,
        # quietly growing actual token count and confounding "same size"
        # comparisons. Random-but-bounded keeps token count roughly constant.
        prompt = build_prompt(repeats, "Summarize the above in exactly 5 words.", offset=random.randint(0, 900))
        r = call(url, model, [{"role": "user", "content": prompt}], max_tokens=30, timeout=timeout)
        results.append(r)
        status = "ok" if r["ok"] else f"FAIL: {r.get('error')}"
        print(f"  [{size}] iter {i+1}/{iterations}: {r['elapsed']:.2f}s — {status}", file=sys.stderr)
    oks = [r for r in results if r["ok"]]
    errors = len(results) - len(oks)
    latencies = [r["elapsed"] for r in oks]
    toks_per_sec = [r["completion_tokens"] / r["elapsed"] for r in oks if r["elapsed"] > 0]
    return {
        "size": size,
        "prompt_repeats": repeats,
        "iterations": iterations,
        "errors": errors,
        "mean_latency_s": round(statistics.mean(latencies), 2) if latencies else None,
        "median_latency_s": round(statistics.median(latencies), 2) if latencies else None,
        "p95_latency_s": round(pctl(latencies, 0.95), 2) if latencies else None,
        "mean_tok_per_s": round(statistics.mean(toks_per_sec), 2) if toks_per_sec else None,
        "sample_prompt_tokens": oks[0]["prompt_tokens"] if oks else None,
    }


def run_multiturn(url: str, model: str, turns: int, timeout: int) -> dict:
    """Simulate a growing agentic-loop conversation: each turn appends the
    previous response, so context grows turn-over-turn like a real loop."""
    messages = [{"role": "user", "content": "Write a short Python function that adds two numbers. Just the code."}]
    turn_results = []
    total_start = time.perf_counter()
    for t in range(turns):
        r = call(url, model, messages, max_tokens=150, timeout=timeout)
        turn_results.append({"turn": t + 1, "elapsed": round(r["elapsed"], 2), "ok": r["ok"],
                              "prompt_tokens": r.get("prompt_tokens")})
        print(f"  [multiturn] turn {t+1}/{turns}: {r['elapsed']:.2f}s prompt_tokens={r.get('prompt_tokens')} ok={r['ok']}",
              file=sys.stderr)
        if not r["ok"]:
            break
        messages.append({"role": "assistant", "content": r["content_preview"] + " ...(truncated for next-turn context)"})
        messages.append({"role": "user", "content": f"Now add a docstring and type hints. (turn {t+2})"})
    total_elapsed = time.perf_counter() - total_start
    return {"turns_completed": len(turn_results), "total_elapsed_s": round(total_elapsed, 2), "per_turn": turn_results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--label", default="run")
    ap.add_argument("--sizes", default="short,medium,long")
    ap.add_argument("--iterations", type=int, default=5)
    ap.add_argument("--multiturn", type=int, default=0, help="also run an N-turn growing-context simulation")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    report = {"label": args.label, "url": args.url, "model": args.model, "sizes": {}}
    for size in args.sizes.split(","):
        size = size.strip()
        print(f"== {size} ==", file=sys.stderr)
        report["sizes"][size] = run_size(args.url, args.model, size, args.iterations, args.timeout)

    if args.multiturn > 0:
        print(f"== multiturn x{args.multiturn} ==", file=sys.stderr)
        report["multiturn"] = run_multiturn(args.url, args.model, args.multiturn, args.timeout)

    print(json.dumps(report, indent=2))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)


if __name__ == "__main__":
    main()
