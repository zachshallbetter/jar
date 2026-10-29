#!/usr/bin/env python3
"""fetch-runtime before/after for the warm Chrome pool (titan-node#882): the runtime itself, measured through titan's
own signed calls, one arm per run against a fetch-runtime on a spare port (never the live one).

Per arm: the first request (a cold pool launches its browser), then for each concurrency level N, rounds of N URLs at
once: latency p50/p95 and errors; the runtime's whole process tree's peak memory (PSS-free RSS from /proc, sampled
every 50 ms); and per URL the title and content length, for parity between arms.

  python3 scripts/tools/poolbench.py --url http://127.0.0.1:18301 --pid <runtime pid> --urls urls.txt --out new.json
  python3 scripts/tools/poolbench.py --compare old.json new.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path


def tree_rss_mb(root: int) -> float:
    parent, rss = {}, {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            for line in open(f"/proc/{d}/status"):
                if line.startswith("PPid:"):
                    parent[int(d)] = int(line.split()[1])
                elif line.startswith("VmRSS:"):
                    rss[int(d)] = int(line.split()[1])
        except OSError:
            continue

    def in_tree(p: int) -> bool:
        for _ in range(64):
            if p == root:
                return True
            p = parent.get(p, 0)
            if p in (0, 1):
                return False
        return False
    return sum(kb for p, kb in rss.items() if in_tree(p)) / 1024


def run(url: str, pid: int, urls: list[str], levels: list[int], rounds: int) -> dict:
    os.environ["TITAN_FETCH_URL"] = url
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import agent_runtime_lib as lib
    peak = [0.0]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            peak[0] = max(peak[0], tree_rss_mb(pid))
            time.sleep(0.05)
    threading.Thread(target=sample, daemon=True).start()

    def one(u: str, out: list):
        t0 = time.monotonic()
        try:
            r = lib._runtime_request("fetch", "/v1/parse", "POST", {"url": u}, timeout=90)
            out.append({"url": u, "ms": (time.monotonic() - t0) * 1000, "ok": bool(r.get("success")),
                        "title": str(r.get("title") or "")[:120], "chars": len(str(r.get("content") or ""))})
        except Exception as exc:  # noqa: BLE001
            out.append({"url": u, "ms": (time.monotonic() - t0) * 1000, "ok": False, "error": str(exc)[:120]})

    first: list = []
    one(urls[0], first)
    res = {"first": first[0], "levels": {}, "pages": {}}
    for n in levels:
        lat, errs = [], 0
        for r in range(rounds):
            batch = [urls[(r * n + i) % len(urls)] for i in range(n)]
            out: list = []
            ts = [threading.Thread(target=one, args=(u, out)) for u in batch]
            [t.start() for t in ts]
            [t.join() for t in ts]
            for o in out:
                lat.append(o["ms"])
                errs += not o["ok"]
                if o.get("ok") and o["url"] not in res["pages"]:
                    res["pages"][o["url"]] = {"title": o["title"], "chars": o["chars"]}
        lat.sort()
        res["levels"][str(n)] = {"p50_ms": int(statistics.median(lat)), "p95_ms": int(lat[int(len(lat) * 0.95) - 1 if len(lat) > 1 else 0]),
                                 "requests": len(lat), "errors": errs}
    stop.set()
    res["peak_rss_mb"] = round(peak[0])
    try:
        res["metrics"] = lib._runtime_request("fetch", "/v1/metrics", "GET")
    except Exception:  # noqa: BLE001
        res["metrics"] = None
    return res


def compare(a: dict, b: dict) -> str:
    lines = [f"first request: {a['first']['ms']:.0f} ms -> {b['first']['ms']:.0f} ms",
             f"peak memory (process tree): {a['peak_rss_mb']} MB -> {b['peak_rss_mb']} MB"]
    for n in a["levels"]:
        x, y = a["levels"][n], b["levels"].get(n, {})
        lines.append(f"{n} at once: p50 {x['p50_ms']} -> {y.get('p50_ms')} ms, p95 {x['p95_ms']} -> {y.get('p95_ms')} ms, "
                     f"errors {x['errors']} -> {y.get('errors')}")
    same = diff = 0
    for u, p in a["pages"].items():
        q = b["pages"].get(u)
        if not q:
            continue
        ok = p["title"] == q["title"] and abs(p["chars"] - q["chars"]) <= max(50, 0.05 * max(p["chars"], 1))
        same += ok
        diff += not ok
        if not ok:
            lines.append(f"  parity differs: {u}: title {p['title'][:40]!r} vs {q['title'][:40]!r}, chars {p['chars']} vs {q['chars']}")
    lines.append(f"parity: {same} pages the same, {diff} different")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--urls")
    ap.add_argument("--levels", default="1,4,8")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs=2)
    a = ap.parse_args()
    if a.compare:
        print(compare(*(json.loads(Path(p).read_text()) for p in a.compare)))
        return 0
    urls = [u.strip() for u in Path(a.urls).read_text().splitlines() if u.strip() and not u.startswith("#")]
    r = run(a.url, a.pid, urls, [int(x) for x in a.levels.split(",")], a.rounds)
    Path(a.out).write_text(json.dumps(r, indent=1))
    print(json.dumps({k: r[k] for k in ("first", "levels", "peak_rss_mb")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
