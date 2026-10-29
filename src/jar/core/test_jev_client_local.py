#!/usr/bin/env python3
# gate: hermetic   (a fake System One server on 127.0.0.1; no key, no network beyond loopback)
"""jev_client with a local System One server (#1434: Drex on The Box). What counts as local (loopback and private
addresses, never a hosted provider); a local base needs no key, and a cloud key in the environment is never sent to
it; no Authorization header goes when there's no key; a missing usage.cost is $0 for a local base and Jev's price for
a hosted one; a cost the server states is kept. Run: python3 scripts/test_jev_client_local.py
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

os.environ["JEV_HOME"] = tempfile.mkdtemp(prefix="titan-jev-local-")
for k in ("JEV_API_KEY", "JEV_BASE_URL", "JEV_AUTH", "JEV_MODEL", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_client as J  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


SEEN: list[dict] = []
REPLY = {"usage": {"input_tokens": 1200}}


class Fake(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        SEEN.append({"path": self.path, "auth": self.headers.get("Authorization"), "model": body.get("model")})
        out = {"model": body.get("model"), "answers": {k: {"choice": "yes", "p": 0.9} for k in body["questions"]}, **REPLY}
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
LOCAL = f"http://127.0.0.1:{srv.server_port}"
Q = {"go": {"type": "choice", "options": ["yes", "no"], "question": "Go?"}}

print("── what counts as local ──")
for base, want in (("http://127.0.0.1:8090", True), ("http://localhost:8090", True), ("http://[::1]:8090", True),
                   ("http://10.0.0.5:8090", True), ("http://192.168.1.20:8090", True), ("http://172.16.4.2:8090", True),
                   ("http://drex.localhost:8090", True), ("https://openrouter.ai/api", False),
                   ("https://api.typesafe.ai", False), ("http://8.8.8.8:8090", False), ("http://drex.example:8090", False)):
    check(f"{base}: {'local' if want else 'hosted'}", J._is_local(base), want)

print("── credentials ──")
os.environ["OPENROUTER_API_KEY"] = "or-key-not-for-local"
os.environ["JEV_BASE_URL"] = LOCAL
c = J.resolve_credentials()
check("a local base with no JEV_API_KEY: provider local, no key (the cloud key isn't used)",
      (c["provider"], c["key"], c["base"]), ("local", "", LOCAL))
check("…its default model is drex-v1.5 (JEV_MODEL overrides)", J.DEFAULT_MODEL["local"], "drex-v1.5")
os.environ["JEV_BASE_URL"] = "https://openrouter.ai/api"
check("a hosted JEV_BASE_URL with no JEV_API_KEY: unchanged (OpenRouter with its key)",
      (J.resolve_credentials()["provider"], J.resolve_credentials()["source"]), ("openrouter", "env:OPENROUTER_API_KEY"))
del os.environ["JEV_BASE_URL"]
check("no JEV_BASE_URL: unchanged", J.resolve_credentials()["provider"], "openrouter")

print("── a call to the local server ──")
os.environ["JEV_BASE_URL"] = LOCAL
r = J.decide({"x": 1}, Q, source="test")
check("it answered over /v1/systemone with the local model", (SEEN[-1]["path"], SEEN[-1]["model"], r["answers"]["go"]["choice"]),
      ("/v1/systemone", "drex-v1.5", "yes"))
check("…no Authorization header at all", SEEN[-1]["auth"], None)
check("…no usage.cost: $0, never Jev's price", (r["_meta"]["cost_usd"], r["_meta"]["provider"]), (0.0, "local"))
REPLY = {"usage": {"input_tokens": 1200, "cost": 0.0005}}
check("…a cost the server states is kept", J.decide({"x": 1}, Q, source="test")["_meta"]["cost_usd"], 0.0005)
os.environ["JEV_API_KEY"] = "local-server-key"
J.decide({"x": 1}, Q, source="test")
check("a JEV_API_KEY set for a local server goes as its bearer", SEEN[-1]["auth"], "Bearer local-server-key")
del os.environ["JEV_API_KEY"]

print("── a hosted base keeps Jev's price when usage.cost is missing ──")
REPLY = {"usage": {"input_tokens": 1_000_000}}
hosted = {"key": "k", "base": LOCAL, "provider": "typesafe", "source": "test"}   # the fake server, priced as hosted
r = J.decide({"x": 1}, Q, source="test", creds=hosted)
check("1M input tokens at Jev's price", round(r["_meta"]["cost_usd"], 6), round(J.PRICE_PER_INPUT_TOKEN * 1_000_000, 6))
check("…and its key goes as the bearer", SEEN[-1]["auth"], "Bearer k")

print("── egress: a local server isn't data leaving the house ──")
import egress  # noqa: E402
import agent_runtime_lib as A  # noqa: E402
wrapped = A._JevEgress(J)
os.environ["JEV_BASE_URL"] = LOCAL
noted: list[str] = []
real_note = egress.note
egress.note = lambda kind: noted.append(kind)
REPLY = {"usage": {"input_tokens": 10}}
wrapped.decide({"x": 1}, Q, source="test")
check("a decide() to the local server notes no egress", noted, [])
wrapped.decide({"x": 1}, Q, source="test", creds=hosted)
check("…one to a hosted provider notes jev", noted, ["jev"])
del os.environ["JEV_BASE_URL"]
os.environ.pop("OPENROUTER_API_KEY", None)
try:
    wrapped.decide({"x": 1}, Q, source="test")
except J.JevError:
    pass
check("…credentials that can't be resolved count as egress (unsure is egress)", noted, ["jev", "jev"])
egress.note = real_note

srv.shutdown()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
