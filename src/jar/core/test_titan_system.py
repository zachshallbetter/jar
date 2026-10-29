#!/usr/bin/env python3
# gate: hermetic
"""The engineering client and its audit (#355): titan_system.py reads with the engineering token only (no token →
exit 2, never the master token), says its output is data, caps it; engineering_audit counts every read and drops
anything that isn't titan's own system item."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP = Path(tempfile.mkdtemp(prefix="eng-client-"))
os.environ["ENGINEERING_AUDIT_LOG"] = str(_TMP / "engineering-reads.jsonl")
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "WORLD_DIR": "world"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha-token")
os.environ.pop("TITAN_DISCOVERY", None)
os.environ.pop("GEMINI_API_KEY", None)
for var in ("ENGINEERING_TOKEN_FILE", "AGENT_ENGINEERING_TOKEN_FILE", "TITAN_AGENT_URL"):
    os.environ.pop(var, None)
os.environ["AGENT_API_TOKEN"] = "m" * 40          # a master token in the env is never used
import engineering_audit as EA  # noqa: E402
import titan_system as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


TOK = "e" * 40
seen: list[tuple[str, str]] = []
ENTITIES = {"entities": [{"id": "system:fetch-runtime", "name": "fetch-runtime", "bubble": "system", "facts": [
    {"attr": "port", "value": "8300", "confirmed": "2026-10-06"},
    {"attr": "status", "value": "up", "confirmed": "2026-10-06"}]}]}
KB = {"entries": [{"id": "kb-runbook", "title": "Runbook", "preview": "Topology and rules", "body": "ports and units",
                   "owner": None, "audience": "admins"},
                  {"id": "kb-decision", "title": "Decision: nftables", "preview": "default deny", "body": "firewall",
                   "owner": None, "audience": "admins"}]}
PAGE = {"id": "system:fetch-runtime", "name": "fetch-runtime", "facts": [
    {"attr": "port", "label": "port", "answer": "fetch-runtime's port is 8300."}]}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        seen.append((self.path, self.headers.get("X-Agent-Token") or ""))
        if self.headers.get("X-Agent-Token") != TOK:
            return self._send(401, {"error": "unauthorized"})
        path = self.path.split("?")[0]
        if path == "/v1/world/entities":
            return self._send(200, ENTITIES)
        if path == "/v1/world/entities/system:fetch-runtime":
            return self._send(200, PAGE)
        if path.startswith("/v1/world/entities/"):
            return self._send(404, {"error": "not found"})
        if path == "/v1/kb":
            return self._send(200, KB)
        return self._send(404, {})

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["TITAN_AGENT_URL"] = f"http://127.0.0.1:{srv.server_port}"

# no token: exit 2, nothing sent
os.environ["ENGINEERING_TOKEN_FILE"] = str(_TMP / "missing")
check("no token file → exit 2", T.main(["services"]), 2)
check("…and nothing reached titan (never the master token)", seen, [])
(_TMP / "empty").write_text("\n")
os.environ["ENGINEERING_TOKEN_FILE"] = str(_TMP / "empty")
check("an empty token file → exit 2", T.main(["notes"]), 2)

# with the token
(_TMP / "tok").write_text(TOK + "\n")
os.environ["ENGINEERING_TOKEN_FILE"] = str(_TMP / "tok")
out = T.run(["services", "fetch"])
check("the output says it's data", out.splitlines()[0], T.HEADER)
check("a service line: id, name, facts, when checked", out.splitlines()[1],
      "system:fetch-runtime  fetch-runtime: port 8300; status up (checked 2026-10-06)")
check("it asked the system bubble with facts and the query",
      sorted(seen[-1][0].split("?")[1].split("&")), ["bubble=system", "facts=1", "q=fetch"])
check("it sent the engineering token", seen[-1][1], TOK)
check("one service's facts", T.run(["service", "system:fetch-runtime"]).splitlines()[1:],
      ["system:fetch-runtime  fetch-runtime", "  port: fetch-runtime's port is 8300."])
check("an unknown service", T.run(["service", "system:nope"]).splitlines()[1], "no service system:nope")
check("notes filtered by the query (title or body)", T.run(["notes", "firewall"]).splitlines()[1:],
      ["kb-decision  Decision: nftables: default deny"])
check("no match says so", T.run(["notes", "zzz"]).splitlines()[1], "no system notes matched")
check("a bad command is a usage error", T.main(["delete", "x"]), 1)
check("service without an id is a usage error", T.main(["service"]), 1)
T.MAX_CHARS = 60
check("the output is capped", len(T.run(["notes"])) <= 60 and T.run(["notes"]).endswith("…"), True)
T.MAX_CHARS = 4000
os.environ["AGENT_ENGINEERING_TOKEN_FILE"] = "/x"
check("the token file wins over titan's own variable", T.token_path(), _TMP / "tok")
del os.environ["AGENT_ENGINEERING_TOKEN_FILE"]
os.environ["TITAN_AGENT_URL"] = "http://127.0.0.1:1"
check("titan down → exit 3", T.main(["services"]), 3)
srv.shutdown()

# the audit: counts, never text; a leak is counted and dropped
system_row = {"id": "system:fetch-runtime", "bubble": "system", "name": "fetch-runtime"}
household_row = {"id": "household:maple-animal-clinic", "bubble": "household", "name": "Maple Animal Clinic"}
spoof = {"id": "system:x", "bubble": "household", "name": "a household row with a system-looking id"}
check("world: only system-bubble rows", EA.only_system("world", [system_row, household_row, spoof]), [system_row])
check("world: a page goes by its id", EA.only_system("world", [{"id": "system:a"}, {"id": "zach:b"}]), [{"id": "system:a"}])
check("world: leaks counted", EA.leaks("world", [system_row, household_row, spoof]), 2)
note = {"id": "kb-runbook", "owner": None, "audience": "admins"}
for label, item in (("a household note", {"owner": None, "audience": "household"}),
                    ("an owned note", {"owner": "robin", "audience": "admins"}),
                    ("a private note", {"owner": "zach", "audience": "private"}),
                    ("not a dict", "kb-runbook")):
    check(f"kb: {label} is a leak", EA.leaks("kb", [note, item]), 1)
check("kb: a system note is not", EA.only_system("kb", [note]), [note])
row = EA.record("/v1/kb", "kb", [note, {"owner": None, "audience": "household", "title": "Leo's school pickup"}])
logged = [json.loads(l) for l in Path(os.environ["ENGINEERING_AUDIT_LOG"]).read_text().splitlines()]
check("the audit line is counts only", (sorted(logged[-1]), logged[-1]["items"], logged[-1]["leaks"]),
      (["items", "leaks", "route", "ts"], 2, 1))
check("…no title or text in the log", "Leo" in Path(os.environ["ENGINEERING_AUDIT_LOG"]).read_text(), False)
check("an unwritable log never fails the read", EA.record("/v1/kb", "kb", [note], log=Path("/dev/null/x"))["items"], 1)

# the API: the engineering routes count each read and drop a leak even if the rules upstream let one through
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
import world as W  # noqa: E402

os.environ["AGENT_ENGINEERING_TOKEN_FILE"] = str(_TMP / "tok")
api._AGENT_TOKEN = "m" * 40
hh.reset_store(hh.HouseholdStore(_TMP / "api-household.db"))
leaky_kb = {"items": [note, {"id": "hh-1", "owner": None, "audience": "household", "title": "Leo's pickup"}]}
api._kb_run = lambda cmd, stdin="", timeout=15: (
    {"item": leaky_kb["items"][1]} if "get" in cmd else leaky_kb)
W.get = lambda: None
W.entity_list = lambda *a, **k: {"entities": [system_row, household_row], "counts": {}, "truncated": False}
W.entity_page = lambda w, cid, *a, **k: {"id": cid, "name": "x", "facts": []}
c = TestClient(api.app)
E = {"X-Agent-Token": TOK}
Path(os.environ["ENGINEERING_AUDIT_LOG"]).unlink()
check("GET /v1/kb as engineering: the household entry is dropped",
      [e["id"] for e in c.get("/v1/kb", headers=E).json()["entries"]], ["kb-runbook"])
check("GET /v1/kb/{id}: a leaked entry comes back empty", c.get("/v1/kb/hh-1", headers=E).json(), {})
check("GET /v1/world/entities: only system rows",
      [e["id"] for e in c.get("/v1/world/entities", headers=E).json()["entities"]], ["system:fetch-runtime"])
check("GET /v1/world/entities/{id}: a non-system page is not found",
      c.get("/v1/world/entities/household:maple-animal-clinic", headers=E).status_code, 404)
check("…a system page is", c.get("/v1/world/entities/system:fetch-runtime", headers=E).status_code, 200)
rows = [json.loads(l) for l in Path(os.environ["ENGINEERING_AUDIT_LOG"]).read_text().splitlines()]
check("each read is in the audit with its leaks", [(r["route"], r["items"], r["leaks"]) for r in rows],
      [("/v1/kb", 2, 1), ("/v1/kb/{id}", 1, 1), ("/v1/world/entities", 2, 1), ("/v1/world/entities/{id}", 1, 1),
       ("/v1/world/entities/{id}", 1, 0)])
check("the admin's reads aren't filtered or audited",
      (len(c.get("/v1/kb", headers={"X-Agent-Token": "m" * 40}).json()["entries"]),
       len(Path(os.environ["ENGINEERING_AUDIT_LOG"]).read_text().splitlines())), (2, 5))
check("engineering still can't write", c.post("/v1/kb", json={"title": "x", "body": "y"}, headers=E).status_code, 403)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
