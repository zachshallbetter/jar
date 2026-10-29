#!/usr/bin/env python3
# gate: hermetic   (a local stub /health on an ephemeral port, temp stores; the canary stubbed)
"""Blessing a box's boot after an A/B update (#407 item 1): exit 0 only when /health answers, the canary passes and
every existing store reads clean; it FAILS CLOSED both ways (a boot that can't reach its own health check, a check
that raises or times out, a corrupt store: never blessed). Throwaway stores; no titan service is touched.
Run: python3 scripts/test_box_bless.py
"""
from __future__ import annotations

import http.server
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-box-bless-"))
sys.path.insert(0, str(Path(__file__).parent))
import box_bless as B  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


class Health(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802
        self.send_response(Health.status if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
nosleep = lambda s: None  # noqa: E731

print("── health ──")
B.API = f"http://127.0.0.1:{port}"
check("answers 200: ok", B.health(wait_s=0, sleep=nosleep), True)
Health.status = 503
check("answers 503 the whole wait: failed", B.health(wait_s=0, sleep=nosleep), False)
Health.status = 200
B.API = "http://127.0.0.1:9"   # nothing listens: the boot can't reach its own health check
check("unreachable the whole wait: failed (closed), never blessed", B.health(wait_s=0, sleep=nosleep), False)

print("── stores ──")
good = _TMP / "household.db"
c = sqlite3.connect(good)
c.execute("CREATE TABLE t (x)")
c.execute("INSERT INTO t VALUES (1)")
c.commit()
c.close()
check("a clean store, and an absent one (not made yet): ok", B.stores([good, _TMP / "absent.db"]), True)
bad = _TMP / "torn.db"
bad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
check("a torn store (a power cut mid-update): failed", B.stores([good, bad]), False)
check("…a store that exists but isn't a database at all: failed", B.stores([_TMP]), False)

print("── the decision ──")
ok = lambda: True  # noqa: E731
no = lambda: False  # noqa: E731


def boom():
    raise TimeoutError("canary hung")


check("every check passes: bless", B.bless({"health": ok, "canary": ok, "stores": ok}), (True, ["health ok", "canary ok", "stores ok"]))
check("health fails: don't, and stop there", B.bless({"health": no, "canary": ok, "stores": ok}), (False, ["health failed"]))
check("the canary fails: don't", B.bless({"health": ok, "canary": no, "stores": ok})[0], False)
check("a check that raises (a hung canary): don't (closed)", B.bless({"health": ok, "canary": boom, "stores": ok}),
      (False, ["health ok", "canary failed"]))
check("a check returning something other than True (None, 'yes'): don't",
      (B.bless({"health": lambda: None})[0], B.bless({"health": lambda: "yes"})[0]), (False, False))
check("no checks at all: don't", B.bless({})[0], False)
check("the real checks are health, canary and stores, in that order", list(B.CHECKS), ["health", "canary", "stores"])

print("── the canary call ──")
calls = []
_run = subprocess.run


def fake_run(cmd, *a, **k):
    calls.append(cmd[1:])
    raise subprocess.TimeoutExpired(cmd, k.get("timeout"))


subprocess.run = fake_run
check("a canary that times out: failed", B.canary(timeout_s=1), False)
subprocess.run = _run
check("…it runs agent_canary.py --no-alert, as the deploy does",
      (Path(calls[0][0]).name, calls[0][1:]), ("agent_canary.py", ["--no-alert"]))

print("── the command ──")
env = {**os.environ, "BOX_BLESS_API": "http://127.0.0.1:9", "BOX_BLESS_HEALTH_WAIT_S": "0"}
r = subprocess.run([sys.executable, str(Path(__file__).parent / "box_bless.py")], env=env, capture_output=True,
                   text=True, timeout=60)
check("with the API down: exit 1 and one line saying which check failed",
      (r.returncode, r.stdout.strip()), (1, "don't bless: health failed"))

srv.shutdown()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
