#!/usr/bin/env python3
# gate: hermetic
"""The titan MCP server (titan_mcp.py): JSON-RPC over stdio, read-only tools; reads with the engineering token only and
never sends a token to /health; labels each address's path by the shared vectors; says what to do when nothing
answers; every result is marked as data and capped. A local fake titan stands in for the real one."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = Path(tempfile.mkdtemp(prefix="titan-mcp-"))
for var in ("AGENT_ENGINEERING_TOKEN_FILE", "TITAN_AGENT_URL", "TITAN_ADDRESSES"):
    os.environ.pop(var, None)
os.environ["ENGINEERING_TOKEN_FILE"] = str(_TMP / "no-token")   # never the real ~/.config/titan/engineering_token
os.environ["AGENT_API_TOKEN"] = "m" * 40          # a master token in the env is never used
import titan_mcp as M  # noqa: E402
import titan_system as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


# ── path labels: the shared vectors ──────────────────────────────────────────────────────────
vectors = json.loads((Path(HERE).parent / "docs" / "titan-path-vectors.json").read_text())["vectors"]
check("vectors present", len(vectors) >= 15, True)
for v in vectors:
    check(f"path_of {v['url']}", M.path_of(v["url"]), v["path"])

# the default addresses are Jellybean's household order, and cover titan's own server list
check("default order: home, mdns, tailscale, tailscale", [M.path_of(u) for u in M.DEFAULT_ADDRESSES],
      ["home", "mdns", "tailscale", "tailscale"])
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
import household_identity as H  # noqa: E402
check("covers household_identity.DEFAULT_SERVER_URLS", set(H.DEFAULT_SERVER_URLS) <= set(M.DEFAULT_ADDRESSES), True)
check("TITAN_ADDRESSES wins, deduped, no trailing slash",
      M.addresses({"TITAN_ADDRESSES": "http://a.local:1/ http://a.local:1 http://100.64.0.1:1"}),
      ["http://a.local:1", "http://100.64.0.1:1"])
check("empty TITAN_ADDRESSES: the defaults", M.addresses({"TITAN_ADDRESSES": " "}), list(M.DEFAULT_ADDRESSES))

# ── verdicts ────────────────────────────────────────────────────────────────────────────────
HOME, MDNS, TSIP, TSN = M.DEFAULT_ADDRESSES


def rows(ok: set[str]):
    return M.reachability(list(M.DEFAULT_ADDRESSES), lambda u: (u in ok, "" if u in ok else "no answer (timed out)"))


r = rows(set())
check("rows keep the given order", [x["url"] for x in r], list(M.DEFAULT_ADDRESSES))
check("a failed row has its error", r[0]["error"], "no answer (timed out)")
v = M.verdict(r)
check("nothing answered: power + BIOS AC + network cable", ("power" in v, "AC power" in v, "cable" in v), (True, True, True))
check("…and names the paths tried", ("home network" in v, "Tailscale" in v), (True, True))
check("by name, not the home IP: the address changed", "address changed" in M.verdict(rows({MDNS, TSIP, TSN})), True)
check("Tailscale only: away, or the address changed", "Tailscale only" in M.verdict(rows({TSIP})), True)
check("home but not Tailscale: Tailscale is off", "Tailscale is off" in M.verdict(rows({HOME, MDNS})), True)
check("everything answers", M.verdict(rows({HOME, MDNS, TSIP, TSN})), "titan answers on every path tried.")
check("no addresses", M.verdict([]).startswith("No addresses"), True)
text = M.reachability_text(list(M.DEFAULT_ADDRESSES), lambda u: (u == TSN, "" if u == TSN else "refused"))
check("text: one line per address, then the verdict", len(text.splitlines()), len(M.DEFAULT_ADDRESSES) + 2)
check("text: no FAIL/PASS words (release.sh greps them)", ("FAIL" in text, "PASS" in text), (False, False))

# ── the probe: /health, never a token ───────────────────────────────────────────────────────
req = M.health_request("http://10.0.0.170:7770/")
check("probe path", req.full_url, "http://10.0.0.170:7770/health")
check("probe sends no token", (req.get_header("X-agent-token"), req.get_header("Authorization")), (None, None))
check("refused → a plain reason", M._why(ConnectionRefusedError(61, "Connection refused")).startswith("refused"), True)
check("timeout → a plain reason", M._why(TimeoutError("timed out")), "no answer (timed out)")

# ── a fake titan ─────────────────────────────────────────────────────────────────────────────
TOK = "e" * 40
seen: list[tuple[str, str | None]] = []
SUMMARY = {"ok": False, "problems": ["GPU1 at 90 °C"], "disks": [{"mount": "/", "used_pct": 41, "free_gib": 300}],
           "gpus": [{"index": 1, "temp_c": 90, "mem_used_mib": 11000, "mem_total_mib": 12288}],
           "failed_units": ["fetch-runtime.service"], "canary": {"status": "OK", "ts": "2026-10-14T08:00:00Z"},
           "memory_free_gib": 40.2}
state = {"summary": SUMMARY, "summary_code": 200}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: object) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        seen.append((self.path, self.headers.get("X-Agent-Token")))
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if self.headers.get("X-Agent-Token") != TOK:
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/v1/host/summary":
            return self._send(state["summary_code"], state["summary"])
        if self.path.startswith("/v1/world/entities"):
            return self._send(200, {"entities": [{"id": "system:fetch-runtime", "name": "fetch-runtime",
                                                  "facts": [{"attr": "port", "value": "8300"}]}]})
        if self.path == "/v1/kb":
            return self._send(200, {"entries": [{"id": "kb-runbook", "title": "Runbook", "preview": "read first"}]})
        return self._send(404, {"error": "not found"})


srv = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
os.environ["TITAN_AGENT_URL"] = BASE
S = M.Server()


def call(name: str, args: dict | None = None) -> dict:
    out = S.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": args or {}}})
    return out["result"]


# no token file: a clear error, no request made, never the master token
seen.clear()
res = call("titan_host_summary")
check("no token: an error result", (res.get("isError"), "no engineering token" in res["content"][0]["text"]), (True, True))
check("no token: nothing sent", len(seen), 0)

tokf = _TMP / "eng_token"
tokf.write_text(TOK + "\n")
os.environ["ENGINEERING_TOKEN_FILE"] = str(tokf)

seen.clear()
res = call("titan_host_summary")
txt = res["content"][0]["text"]
check("summary: not an error", res.get("isError"), None)
check("summary: header first", txt.splitlines()[0], T.HEADER)
check("summary: problem, disk, GPU, failed unit, canary",
      [s in txt for s in ("GPU1 at 90 °C", "disk /: 41% used", "GPU1: 90 °C", "fetch-runtime.service", "agent canary: OK")],
      [True] * 5)
check("summary: the engineering token, nothing else", seen == [("/v1/host/summary", TOK)], True)

res = call("titan_boot_check")
check("boot: not reported yet → says so", "doesn't report boot units yet" in res["content"][0]["text"], True)
state["summary"] = dict(SUMMARY, boot={"default_target": "multi-user.target", "ok": 30, "not_enabled": 1, "missing": 0,
                                       "bios": ["Restore AC power loss: set to Power On"]},
                        network={"hostname": "titan-node", "ipv4": ["10.0.0.170"], "tailscale_ipv4": "100.77.69.85"})
btxt = call("titan_boot_check")["content"][0]["text"]
check("boot: counts and the BIOS note", ("30 ok, 1 not enabled, 0 missing" in btxt, "Restore AC power" in btxt), (True, True))
stxt = call("titan_host_summary")["content"][0]["text"]
check("summary: network identity when titan reports it", "titan-node 10.0.0.170, Tailscale 100.77.69.85" in stxt, True)

state["summary_code"] = 403
res = call("titan_host_summary")
check("403: says the token isn't allowed", (res.get("isError"), "isn't allowed" in res["content"][0]["text"]), (True, True))
state["summary_code"] = 200

check("services", "system:fetch-runtime" in call("titan_services")["content"][0]["text"], True)
check("service needs an id", call("titan_service").get("isError"), True)
check("notes", "Runbook" in call("titan_notes", {"q": "runbook"})["content"][0]["text"], True)

seen.clear()
res = call("titan_reachability", {"addresses": [BASE + "/", "http://127.0.0.1:9"]})
rtxt = res["content"][0]["text"]
check("reachability: the fake answers, port 9 doesn't", (rtxt.count("\nok  "), rtxt.count("\n--  ")), (1, 1))
check("reachability: no token on /health", [h for p, h in seen if p == "/health"], [None])
check("reachability: a non-http address is refused",
      call("titan_reachability", {"addresses": ["file:///etc/passwd"]}).get("isError"), True)

os.environ["TITAN_AGENT_URL"] = "http://127.0.0.1:9"
titan_down = call("titan_services")
check("titan down: points at titan_reachability",
      (titan_down.get("isError"), "titan_reachability" in titan_down["content"][0]["text"]), (True, True))
os.environ["TITAN_AGENT_URL"] = BASE

old_cap = T.MAX_CHARS
T.MAX_CHARS = 60
check("capped", len(call("titan_host_summary")["content"][0]["text"]), 60)
T.MAX_CHARS = old_cap

check("unknown tool", call("titan_restart").get("isError"), True)

# ── the protocol ────────────────────────────────────────────────────────────────────────────
init = S.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
check("initialize echoes the client's version", init["result"]["protocolVersion"], "2025-03-26")
check("server name", init["result"]["serverInfo"]["name"], "titan")
names = [t["name"] for t in S.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
check("tools", names, ["titan_reachability", "titan_host_summary", "titan_boot_check", "titan_services",
                       "titan_service", "titan_notes"])
check("read-only: no tool that changes titan",
      [n for n in names if any(w in n for w in ("restart", "deploy", "enable", "stop", "start", "write"))], [])
check("a notification gets no reply", S.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}), None)
check("unknown method", S.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})["error"]["code"], -32601)

# over stdio, as a client runs it
p = subprocess.run([sys.executable, os.path.join(HERE, "titan_mcp.py")], capture_output=True, text=True, timeout=30,
                   input="\n".join([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                                    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                                    "not json",
                                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})]) + "\n",
                   env={**os.environ, "TITAN_AGENT_URL": "http://127.0.0.1:9"})
replies = [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]
check("stdio: exit 0", p.returncode, 0)
check("stdio: three replies (initialize, parse error, tools/list)", [r.get("id") for r in replies], [1, None, 2])
check("stdio: parse error code", replies[1]["error"]["code"] if len(replies) > 1 else None, -32700)
check("stdio: nothing on stderr", p.stderr, "")

srv.shutdown()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
