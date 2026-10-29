#!/usr/bin/env python3
"""A fake Home Assistant for sandbox boxes (scripts/sandbox_box.sh): canned states, no real house, 127.0.0.1 only.

--fresh (a --first-run box, #411): an HA straight out of the box. Its onboarding isn't done; it answers the calls
box_ha.py makes (the same ones the real HA 2026.9.4 answered on 10-09): /api/onboarding, the user step's auth code,
/auth/token, the other steps, and the websocket's auth/long_lived_access_token; every /api/ read then needs that token.
Its house also has the hands-off kinds setup must only list (a lock, an alarm, a garage door, a camera).

Run: python3 scripts/sandbox_ha_stub.py <port> [--fresh]
"""
import base64
import hashlib
import json
import secrets
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

NOW = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def ent(eid, state, **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs, "last_changed": NOW, "last_updated": NOW}


STATES = [
    ent("climate.living_room", "heat_cool", friendly_name="Living Room Thermostat", hvac_modes=["off", "heat", "cool", "heat_cool"],
        current_temperature=71, target_temp_low=69, target_temp_high=73, min_temp=50, max_temp=90),
    ent("media_player.living_room_tv", "playing", friendly_name="Living Room TV", media_title="Nature Documentary",
        app_name="TV", volume_level=0.3),
    ent("light.kitchen", "off", friendly_name="Kitchen Light"),
]
FRESH_EXTRA = [
    ent("lock.front_door", "locked", friendly_name="Front Door"),
    ent("alarm_control_panel.home", "armed_home", friendly_name="Home Alarm"),
    ent("cover.garage_door", "closed", friendly_name="Garage Door", device_class="garage"),
    ent("camera.porch", "idle", friendly_name="Porch Camera"),
    ent("cover.living_room_blinds", "open", friendly_name="Living Room Blinds", device_class="blind"),
    ent("switch.coffee_maker", "off", friendly_name="Coffee Maker"),
]
FRESH = {"on": False, "steps": {"user": False, "core_config": False, "analytics": False, "integration": False},
         "codes": set(), "access": set(), "long": set()}
# Not the house: a stub location, so nothing here places the household.
CONFIG = {"location_name": "Stub Home", "latitude": 45.0, "longitude": -120.0, "unit_system": {"temperature": "°F"},
          "time_zone": "America/Los_Angeles", "version": "stub"}


class H(BaseHTTPRequestHandler):
    def _authed(self) -> bool:
        """Fresh mode: only a token this stub minted reads /api/ (as the real HA after onboarding)."""
        if not FRESH["on"]:
            return True
        tok = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        return tok in FRESH["long"] or tok in FRESH["access"]

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if FRESH["on"] and self.path == "/api/onboarding":
            return self._send(200, [{"step": k, "done": v} for k, v in FRESH["steps"].items()])
        if FRESH["on"] and self.path == "/api/websocket":
            return self._ws()
        if self.path.startswith("/api") and not self._authed():
            return self._send(401, {"message": "401: Unauthorized"})
        if self.path.startswith("/api/states"):
            self._send(200, STATES + (FRESH_EXTRA if FRESH["on"] else []))
        elif self.path.startswith("/api/config"):
            self._send(200, CONFIG)
        elif self.path.rstrip("/") == "/api":
            self._send(200, {"message": "API running."})
        else:
            self._send(404, {"message": "not in stub"})

    def do_POST(self):   # eval turns simulate control, so this shouldn't be reached; answer harmlessly
        body = self._body()
        if FRESH["on"] and self.path == "/api/onboarding/users":
            if FRESH["steps"]["user"]:
                return self._send(403, {"message": "User step already done"})
            FRESH["steps"]["user"] = True
            code = secrets.token_hex(8)
            FRESH["codes"].add(code)
            return self._send(200, {"auth_code": code})
        if FRESH["on"] and self.path == "/auth/token":
            form = dict(x.split("=", 1) for x in body.decode().split("&") if "=" in x)
            if form.get("grant_type") != "authorization_code" or form.get("code") not in FRESH["codes"]:
                return self._send(400, {"error": "invalid_request"})
            FRESH["codes"].discard(form["code"])
            acc = secrets.token_hex(16)
            FRESH["access"].add(acc)
            return self._send(200, {"access_token": acc, "refresh_token": secrets.token_hex(16), "token_type": "Bearer",
                                    "expires_in": 1800})
        step = self.path.rsplit("/", 1)[-1]
        if FRESH["on"] and self.path.startswith("/api/onboarding/") and step in FRESH["steps"]:
            if not self._authed():
                return self._send(401, {"message": "401: Unauthorized"})
            FRESH["steps"][step] = True
            return self._send(200, {"auth_code": secrets.token_hex(8)} if step == "integration" else {})
        if self.path.startswith("/api") and not self._authed():
            return self._send(401, {"message": "401: Unauthorized"})
        self._send(200, [])

    def _ws(self):
        acc = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                                            .encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc)
        self.end_headers()
        self.wfile.flush()

        def send(obj):
            p = json.dumps(obj).encode()
            self.wfile.write(bytes([0x81]) + (bytes([len(p)]) if len(p) < 126 else bytes([126]) + struct.pack(">H", len(p))) + p)
            self.wfile.flush()

        def recv():
            _, b2 = self.rfile.read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self.rfile.read(2))[0]
            mask = self.rfile.read(4)
            return json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(n))))

        send({"type": "auth_required", "ha_version": "stub"})
        ok = recv().get("access_token") in FRESH["access"]
        send({"type": "auth_ok" if ok else "auth_invalid"})
        if ok:
            m = recv()
            tok = secrets.token_hex(32)
            FRESH["long"].add(tok)
            send({"id": m.get("id"), "type": "result", "success": True, "result": tok})
        self.close_connection = True

    def log_message(self, *a):
        sys.stderr.write("[ha-stub] " + (a[0] % a[1:]) + "\n")


if __name__ == "__main__":
    FRESH["on"] = "--fresh" in sys.argv[2:]
    (ThreadingHTTPServer if FRESH["on"] else HTTPServer)(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
