#!/usr/bin/env python3
"""
fleet_load_test.py — concurrent load test across the Rust runtime fleet,
beyond titan_preflight.sh's up/down health check.

For each runtime in runtime-registry.json, fires N concurrent requests at
its /health endpoint and reports p50/p95/p99 latency + error rate. Run
directly on titan-node (registry + runtimes are loopback-only).

Usage: python3 scripts/tools/fleet_load_test.py [--concurrency 20] [--requests 100]
"""
import argparse
import concurrent.futures
import json
import statistics
import time
import urllib.error
import urllib.request

REG = "/ai/projects/titan-node/scripts/runtime-registry.json"


def one_request(url: str) -> tuple[bool, float]:
    t0 = time.perf_counter()
    try:
        urllib.request.urlopen(url, timeout=10)
        return True, time.perf_counter() - t0
    except Exception:
        return False, time.perf_counter() - t0


def pctl(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def load_test_one(name: str, url: str, n_requests: int, concurrency: int) -> dict:
    latencies = []
    errors = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(one_request, url) for _ in range(n_requests)]
        for f in concurrent.futures.as_completed(futures):
            ok, elapsed = f.result()
            latencies.append(elapsed)
            if not ok:
                errors += 1
    return {
        "runtime": name,
        "requests": n_requests,
        "concurrency": concurrency,
        "errors": errors,
        "p50_ms": round(pctl(latencies, 0.50) * 1000, 2),
        "p95_ms": round(pctl(latencies, 0.95) * 1000, 2),
        "p99_ms": round(pctl(latencies, 0.99) * 1000, 2),
        "max_ms": round(max(latencies) * 1000, 2) if latencies else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--requests", type=int, default=100)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    runtimes = json.load(open(REG))["runtimes"]
    results = []
    print(f"{'runtime':<22} {'errors':>7} {'p50ms':>8} {'p95ms':>8} {'p99ms':>8} {'maxms':>8}")
    print("-" * 70)
    for r in runtimes:
        res = load_test_one(r["name"], r["health_url"], args.requests, args.concurrency)
        results.append(res)
        flag = " <-- ERRORS" if res["errors"] else ""
        print(f"{res['runtime']:<22} {res['errors']:>7} {res['p50_ms']:>8} {res['p95_ms']:>8} "
              f"{res['p99_ms']:>8} {res['max_ms']:>8}{flag}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
