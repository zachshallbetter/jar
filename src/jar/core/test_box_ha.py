#!/usr/bin/env python3
# gate: hermetic
"""A box's HA onboarding (titan-node#411 step 5), against a stub HA that answers the same calls the real one did on
10-09 (HA 2026.9.4, a throwaway container): onboarding steps, the user step's auth code, /auth/token, the websocket's
auth and auth/long_lived_access_token, /api/. It refuses off a box, for a non-local HA, with a token file already there,
or when HA already has an owner; the token is written 0600 and is never printed; the password sent never comes back.
Run: python3 scripts/test_box_ha.py
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import io
import json
import os
import struct
import sys
import tempfile
import threading
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-boxha-"))
os.environ["HA_TOKEN_FILE"] = str(_TMP / "ha-token")
sys.path.insert(0, str(Path(__file__).parent))
import box_ha  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


LONG = "llt-" + "x" * 179
STATE = {"steps": {"user": False, "core_config": False, "analytics": False, "integration": False}, "seen": [], "pw": None}


class HA(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        STATE["seen"].append(("GET", self.path))
        if self.path == "/api/onboarding":
            return self._json(200, [{"step": k, "done": v} for k, v in STATE["steps"].items()])
        if self.path == "/api/":
            ok = self.headers.get("Authorization") == "Bearer " + LONG
            return self._json(200 if ok else 401, {"message": "API running."} if ok else {"message": "401"})
        if self.path == "/api/websocket":
            return self._ws()
        self._json(404, {})

    def do_POST(self):
        body = self._body()
        STATE["seen"].append(("POST", self.path))
        if self.path == "/api/onboarding/users":
            d = json.loads(body)
            STATE["pw"] = d["password"]
            STATE["steps"]["user"] = True
            return self._json(200, {"auth_code": "code-1"})
        if self.path == "/auth/token":
            ok = b"code=code-1" in body and b"grant_type=authorization_code" in body
            return self._json(200 if ok else 400, {"access_token": "acc-1", "refresh_token": "r", "token_type": "Bearer",
                                                   "expires_in": 1800} if ok else {"error": "invalid_request"})
        step = self.path.rsplit("/", 1)[-1]
        if self.path.startswith("/api/onboarding/") and step in STATE["steps"]:
            if self.headers.get("Authorization") != "Bearer acc-1":
                return self._json(401, {})
            STATE["steps"][step] = True
            return self._json(200, {"auth_code": "code-2"} if step == "integration" else {})
        self._json(404, {})

    def _ws(self):
        key = self.headers["Sec-WebSocket-Key"]
        acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc)
        self.end_headers()
        self.wfile.flush()

        def send(obj):
            p = json.dumps(obj).encode()
            hdr = bytes([0x81]) + (bytes([len(p)]) if len(p) < 126 else bytes([126]) + struct.pack(">H", len(p)))
            self.wfile.write(hdr + p)
            self.wfile.flush()

        def recv():
            b1, b2 = self.rfile.read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self.rfile.read(2))[0]
            mask = self.rfile.read(4)
            data = self.rfile.read(n)
            STATE["masked"] = bool(b2 & 0x80)
            return json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

        send({"type": "auth_required", "ha_version": "2026.9.4"})
        a = recv()
        send({"type": "auth_ok" if a.get("access_token") == "acc-1" else "auth_invalid"})
        m = recv()
        STATE["llt_request"] = m
        send({"id": m["id"], "type": "result", "success": True, "result": LONG})
        self.close_connection = True


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HA)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["BOX_HA_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"


def cli(*args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = box_ha.main(["box_ha.py", *args])
    return rc, buf.getvalue()


print("── it refuses ──")
os.environ.pop("BOX_SETUP", None)
rc, out = cli("onboard")
check("off a box (titan): refused, HA never called for a user", (rc, "isn't a box" in out,
      ("POST", "/api/onboarding/users") in STATE["seen"]), (1, True, False))
os.environ["BOX_SETUP"] = "1"
real = os.environ["BOX_HA_URL"]
os.environ["BOX_HA_URL"] = "http://10.0.0.5:8123"
rc, out = cli("onboard")
check("an HA that isn't on localhost: refused (the password never leaves the box)", (rc, "localhost" in out), (1, True))
os.environ["BOX_HA_URL"] = real

print("── onboarding a fresh HA ──")
rc, out = cli("onboard")
tok = Path(os.environ["HA_TOKEN_FILE"])
check("done: every step, in HA's order", (rc, STATE["steps"]), (0, {"user": True, "core_config": True, "analytics": True,
                                                                  "integration": True}))
check("the token file holds the long-lived token, 0600", (tok.read_text().strip() == LONG, oct(tok.stat().st_mode & 0o777)),
      (True, "0o600"))
check("neither the token nor the password is printed", (LONG in out, STATE["pw"] in out, "acc-1" in out), (False, False, False))
check("the password is random and long (never stored: the box uses the token)", len(STATE["pw"]) >= 40, True)
check("a 10-year token named Jellybean, over masked frames", (STATE["llt_request"]["type"], STATE["llt_request"]["lifespan"],
      STATE["llt_request"]["client_name"], STATE["masked"]), ("auth/long_lived_access_token", 3650, "Jellybean", True))
check("…and it was checked against /api/ before success", ("GET", "/api/") in STATE["seen"], True)

print("── and never twice ──")
rc, out = cli("onboard")
check("the token file exists: refused, nothing overwritten", (rc, "already connected" in out, tok.read_text().strip() == LONG),
      (1, True, True))
tok.unlink()
rc, out = cli("onboard")
check("HA already has an owner: refused", (rc, "already has an owner" in out), (1, True))
rc, out = cli("status")
check("status: steps and whether a token file exists, no token", (rc, json.loads(out)["token_file"], LONG in out), (0, False, False))
srv.shutdown()
rc, out = cli("status")
check("HA not answering: a plain error, exit 1", (rc, "isn't answering" in out), (1, True))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
