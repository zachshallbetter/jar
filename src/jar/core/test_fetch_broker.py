#!/usr/bin/env python3
# gate: hermetic
"""#936's route: with FETCH_VIA_BROKER=1 a person's URL is read by the Railway fetch-runtime, never from the house; off
by default (the rollback is unsetting it). Through the broker it fails closed (unreachable, misconfigured or a 5xx: a
plain "couldn't fetch", and the local runtime is never asked); the direct GitHub and Reddit calls are skipped too, since
they'd go from the house. Three failures in a row alert once an hour. The route is logged beside each fetch's outcome,
counts only, and fetch_report adds a by-route line. Stub runtimes and resolver; no network.
Run: python3 scripts/test_fetch_broker.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-fetch-broker-"))
for k in ("HOUSEHOLD_DB", "PROACTIVE_DB", "WORLD_DIR", "LINKS_DB", "LINK_SALT_FILE", "AGENT_MEMORY_DIR", "SITTER_DB"):
    os.environ[k] = str(_TMP / k.lower())
os.environ.pop("FETCH_VIA_BROKER", None)
sys.path.insert(0, str(Path(__file__).parent))
import fetch_checks as F  # noqa: E402
import fetch_report as R  # noqa: E402
import agent_runtime_lib as L  # noqa: E402
import titan_alert  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


F._socket.getaddrinfo = lambda h, p: [(2, 1, 6, "", ("93.184.216.34", p))]
calls, alerts, direct = [], [], []
MODE = {"broker": "ok"}


def runtime(svc, path, method, body, timeout=60):
    calls.append(svc)
    if svc == "fetch_broker":
        if MODE["broker"] == "down":
            raise urllib.error.URLError("connection refused")
        if MODE["broker"] == "500":
            raise urllib.error.HTTPError("https://broker/v1/parse", 502, "Bad Gateway", {}, None)
        if MODE["broker"] == "403":
            raise urllib.error.HTTPError("https://broker/v1/parse", 403, "Forbidden", {}, None)
    return {"url": body["url"], "title": "Harbor Gas", "content": f"read by {svc}"}


L._runtime_request = runtime
F.github = lambda url, **k: (direct.append(("github", url)) or "harbor/app: a repo on GitHub") if "github.com" in url else None
F.reddit_embed = lambda url, **k: direct.append(("reddit", url)) or None
titan_alert.alert = lambda title, msg, **k: alerts.append((title, msg)) or []


def fetch(url, tool="fetch_url"):
    L._TURN_INFO.info = {}
    out = L.execute_tool(tool, {"url": url})
    return out, (L._TURN_INFO.info.get("web") or [None])[-1]


print("── off by default: as today ──")
out, web = fetch("https://harbor-gas.example/bill")
check("the local fetch-runtime reads it, and the route is logged as local",
      (calls[-1], "read by fetch" in out, web), ("fetch", True, ["fetch_url", "ok", "harbor-gas.example", "local"]))
fetch("https://github.com/harbor/app")
check("GitHub's API shortcut runs as before", direct[-1], ("github", "https://github.com/harbor/app"))

print("── FETCH_VIA_BROKER=1: the Railway copy reads it, nothing from the house ──")
os.environ["FETCH_VIA_BROKER"] = "1"
os.environ["TITAN_FETCH_BROKER_URL"] = "https://fetch-runtime.example.railway.app"
calls.clear(), direct.clear()
out, web = fetch("https://harbor-gas.example/bill")
check("through the broker, logged as railway", (calls, "read by fetch_broker" in out, web),
      (["fetch_broker"], True, ["fetch_url", "ok", "harbor-gas.example", "railway"]))
calls.clear()
fetch("https://github.com/harbor/app")
check("a GitHub link: the broker's browser reads the page; GitHub's API isn't called from the house", (calls, direct),
      (["fetch_broker"], []))
calls.clear()
out, _ = fetch("https://harbor-gas.example/bill", "ingest_url")
check("ingest_url's fetch goes through the broker too (then it indexes), never the local fetch-runtime",
      (calls[0], "fetch" in calls), ("fetch_broker", False))
out, web = fetch("http://192.168.1.1/admin")
check("the house's addresses are still refused first (#1334), classed not_public", (out, web[1]),
      (F.NOT_PUBLIC, "not_public"))

print("── fails closed ──")
MODE["broker"] = "down"
calls.clear()
out, web = fetch("https://harbor-gas.example/bill")
check("broker unreachable: the plain words, classed broker_down, and the local runtime never asked",
      (out, web[1], web[3], calls), (L.BROKER_DOWN, "broker_down", "railway", ["fetch_broker"]))
out, _ = fetch("https://harbor-gas.example/bill", "ingest_url")
check("ingest_url fails closed too", (out, "fetch" in calls), (L.BROKER_DOWN, False))
check("two failures: no alert yet", len(alerts), 0)
fetch("https://harbor-gas.example/bill")
check("three failures in a row: one alert, no URL in it", (len(alerts), any("harbor" in a[1] for a in alerts)), (1, False))
fetch("https://harbor-gas.example/bill")
check("…and not again within the hour", len(alerts), 1)
MODE["broker"] = "500"
check("a 5xx from the broker is down too", fetch("https://harbor-gas.example/bill")[0], L.BROKER_DOWN)
MODE["broker"] = "403"
out, web = fetch("https://harbor-gas.example/bill")
check("a 4xx (the broker's own guard refusing) is a refusal, not 'down'", (out == L.BROKER_DOWN, web[1]), (False, "refused"))
MODE["broker"] = "ok"
fetch("https://harbor-gas.example/bill")
check("a success resets the run of failures", L._BROKER_FAILS["n"], 0)
os.environ.pop("TITAN_FETCH_BROKER_URL")
calls.clear()
check("on but no broker URL configured: fails closed, never local", (fetch("https://harbor-gas.example/bill")[0], calls),
      (L.BROKER_DOWN, []))
check("…and no empty base is left behind (it would match every URL)", "fetch_broker" in L._RUNTIME_BASES, False)

print("── rollback ──")
os.environ.pop("FETCH_VIA_BROKER")
calls.clear()
check("unset the flag: local again", (fetch("https://harbor-gas.example/bill")[1][3], calls), ("local", ["fetch"]))

print("── fetch_report's by-route line ──")
rows = [json.dumps({"ts": 1000, "web": [["fetch_url", "ok", "a.example", "railway"], ["fetch_url", "broker_down", "b.example", "railway"],
                                        ["fetch_url", "ok", "c.example", "local"], ["web_search", "ok", "search"]]}),
        json.dumps({"ts": 1001, "web": [["fetch_url", "ok", "d.example"]]}),
        json.dumps({"ts": 1002, "source": "eval", "web": [["fetch_url", "ok", "e.example", "railway"]]})]
check("counts by route (an old entry unrecorded; eval turns apart), and broker-down runs",
      R.routes(rows, 0, 2000), {"local": 1, "railway": 2, "railway down": 1, "unrecorded": 1})
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
