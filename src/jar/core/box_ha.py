#!/usr/bin/env python3
"""A box's Home Assistant, onboarded without a laptop (titan-node#411, docs/box-first-run.md step 5).

A fresh HA asks a person to visit its web page and make an owner account. On a box nobody has to: `python3 box_ha.py
onboard` runs HA's own onboarding API on localhost (proved against HA 2026.9.4 on 10-09):
  1. POST /api/onboarding/users      the owner user ("jellybean"), with a random password that's never stored or shown:
                                     the box talks to HA with a token, and a person who wants HA's web page can reset it
  2. POST /auth/token                the user step's auth code → an access token (30 minutes)
  3. POST /api/onboarding/core_config, analytics, integration   the rest of HA's first run
  4. websocket auth/long_lived_access_token   a 10-year token for the agent, written 0600 to HA_TOKEN_FILE
Then the agent's home tools (home_status, home_control) work as on titan. Locks, alarms, garage doors and cameras stay
hands-off (#413); discovering devices is HA's job after this.

Off unless BOX_SETUP=1 (titan's HA was onboarded by hand, long ago). Only a localhost HA (BOX_HA_URL, default
http://127.0.0.1:8123); refuses when HA's user step is already done, or when the token file exists. Standard library
only (titan has no websocket package): HA's websocket needs nothing past unfragmented text frames.

What the house has (`inventory`, for setup's "Connect the house" step): device counts by kind, and the devices that
stay hands-off by name (#413: locks, alarms, garage and gate doors, cameras; setup lists them and wires no control).

CLI: python3 box_ha.py onboard | status
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

LIFESPAN_DAYS = 3650


class OnboardError(Exception):
    pass


def ha_url() -> str:
    url = os.environ.get("BOX_HA_URL", "http://127.0.0.1:8123").rstrip("/")
    host = urllib.parse.urlparse(url).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise OnboardError(f"HA must be on localhost, not {host}: the owner's password never leaves the box")
    return url


def token_path() -> Path:
    """Where the agent's home tools read HA's token (home_assistant.TOKEN_FILE's default): the same file, or a box would
    onboard HA and the agent still wouldn't see it."""
    return Path(os.environ.get("HA_TOKEN_FILE", "/ai/secrets/homeassistant_token"))


def _call(method: str, path: str, body: Any = None, token: str | None = None, form: bool = False) -> Any:
    data, headers = None, {}
    if body is not None:
        data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(ha_url() + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        raise OnboardError(f"{method} {path}: HTTP {exc.code}") from None
    except OSError as exc:
        raise OnboardError(f"HA isn't answering at {ha_url()} ({type(exc).__name__})") from None
    return json.loads(raw) if raw else None


def steps() -> dict[str, bool]:
    """HA's onboarding steps: {user, core_config, analytics, integration} → done."""
    got = _call("GET", "/api/onboarding")
    return {s["step"]: bool(s["done"]) for s in got} if isinstance(got, list) else {}


class _WS:
    """Just enough of RFC 6455 for HA: one connection, unfragmented text frames, client frames masked."""

    def __init__(self) -> None:
        u = urllib.parse.urlparse(ha_url())
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /api/websocket HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OnboardError("HA closed the websocket during the handshake")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise OnboardError("HA refused the websocket upgrade")

    def send(self, obj: Any) -> None:
        p, mask = json.dumps(obj).encode(), os.urandom(4)
        n = len(p)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536
                               else bytes([0x80 | 127]) + struct.pack(">Q", n))
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(p)))

    def _need(self, n: int) -> None:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OnboardError("HA closed the websocket")
            self.buf += chunk

    def recv(self) -> Any:
        self._need(2)
        n, off = self.buf[1] & 0x7F, 2
        if n == 126:
            self._need(4)
            n, off = struct.unpack(">H", self.buf[2:4])[0], 4
        elif n == 127:
            self._need(10)
            n, off = struct.unpack(">Q", self.buf[2:10])[0], 10
        self._need(off + n)
        payload, self.buf = self.buf[off:off + n], self.buf[off + n:]
        return json.loads(payload)

    def close(self) -> None:
        self.sock.close()


def _long_lived(access: str) -> str:
    ws = _WS()
    try:
        if ws.recv().get("type") != "auth_required":
            raise OnboardError("HA's websocket didn't ask for auth")
        ws.send({"type": "auth", "access_token": access})
        if ws.recv().get("type") != "auth_ok":
            raise OnboardError("HA refused the new owner's token")
        ws.send({"id": 1, "type": "auth/long_lived_access_token", "client_name": "Jellybean", "lifespan": LIFESPAN_DAYS})
        got = ws.recv()
    finally:
        ws.close()
    if not got.get("success") or not isinstance(got.get("result"), str):
        raise OnboardError("HA didn't make a long-lived token")
    return got["result"]


def _write_token(token: str) -> Path:
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    tmp.replace(p)
    return p


def onboard() -> dict[str, Any]:
    """HA's whole first run, from the box. {token_file, steps}. Refuses on titan, a non-local HA, an HA whose owner
    exists, or an existing token file (nothing is overwritten)."""
    if os.environ.get("BOX_SETUP", "0").strip() != "1":
        raise OnboardError("BOX_SETUP isn't on: this isn't a box (titan's HA was onboarded by hand)")
    if token_path().exists():
        raise OnboardError(f"{token_path()} exists: HA is already connected")
    before = steps()
    if before.get("user"):
        raise OnboardError("HA already has an owner: connect it with a token made in HA's web page instead")
    client = ha_url() + "/"
    made = _call("POST", "/api/onboarding/users", {"client_id": client, "name": "Owner", "username": "jellybean",
                                                    "password": secrets.token_urlsafe(32), "language": "en"})
    code = (made or {}).get("auth_code")
    if not code:
        raise OnboardError("HA didn't return an auth code for the owner")
    tok = _call("POST", "/auth/token", {"grant_type": "authorization_code", "code": code, "client_id": client}, form=True)
    access = (tok or {}).get("access_token")
    if not access:
        raise OnboardError("HA didn't exchange the owner's auth code")
    for step, body in (("core_config", {}), ("analytics", {}),
                       ("integration", {"client_id": client, "redirect_uri": client + "?auth_callback=1"})):
        if not steps().get(step):
            _call("POST", f"/api/onboarding/{step}", body, token=access)
    path = _write_token(_long_lived(access))
    long_lived = path.read_text().strip()
    if (_call("GET", "/api/", token=long_lived) or {}).get("message") != "API running.":
        raise OnboardError("the new token doesn't read HA's API")
    return {"token_file": str(path), "steps": steps()}


# Hands-off in setup (#413): listed by name so the owner knows they're seen, never offered as a control.
HANDS_OFF = {"lock": "lock", "alarm_control_panel": "alarm", "camera": "camera"}
GARAGE_CLASSES = {"garage", "gate"}


def inventory() -> dict[str, Any]:
    """{devices: {kind: count}, hands_off: [{entity_id, name, kind}]} from HA's states, with the box's token."""
    try:
        token = token_path().read_text().strip()
    except OSError:
        raise OnboardError("HA isn't connected yet (no token)") from None
    states = _call("GET", "/api/states", token=token) or []
    counts: dict[str, int] = {}
    hands_off = []
    for s in states:
        eid = str(s.get("entity_id") or "")
        domain = eid.split(".", 1)[0]
        attrs = s.get("attributes") or {}
        kind = HANDS_OFF.get(domain)
        if domain == "cover" and attrs.get("device_class") in GARAGE_CLASSES:
            kind = "garage"
        if kind:
            hands_off.append({"entity_id": eid, "name": str(attrs.get("friendly_name") or eid), "kind": kind})
        elif domain in ("light", "switch", "fan", "climate", "media_player", "cover", "sensor", "binary_sensor",
                        "vacuum", "scene", "script"):
            counts[domain] = counts.get(domain, 0) + 1
    return {"devices": dict(sorted(counts.items())), "hands_off": sorted(hands_off, key=lambda h: (h["kind"], h["name"]))}


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    try:
        if cmd == "status":
            print(json.dumps({"ha": ha_url(), "steps": steps(), "token_file": token_path().exists()}))
        elif cmd == "onboard":
            out = onboard()
            print(f"HA is set up; its token is in {out['token_file']} (0600). Steps: {out['steps']}")
        else:
            print(__doc__)
            return 2
    except OnboardError as exc:
        print(f"Not done: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
