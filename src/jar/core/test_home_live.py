#!/usr/bin/env python3
# gate: hermetic
"""Live video on tap (titan-node#413, Zach 10-04): titan relays Home Assistant's HLS from memory to the TV and adults'
apps, never a kid's, home network only, local cameras only, never written or recorded; 5 minutes, 20 s idle, at most 2
at once, counted; HA_CAMERA_LIVE=1. The websocket client asks HA for camera/stream (hls) and nothing else, with a hard
timeout and a frame cap; a fake HA answers over a socket pair, garbage and hangs included. Made-up people.
Run: python3 scripts/test_home_live.py"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-homelive-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "TASK_LOG_DB": "task_log.db", "UPLOADS_DB": "uploads.db", "UPLOADS_DIR": "uploads",
             "TV_UNLOCK_DB": "tv_unlock.db", "LOCK_UP_DB": "lock_up.db", "SITTER_DB": "sitter.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL",
          "HA_LOCK_UP", "HA_CAMERA_SNAPSHOTS", "HA_CAMERA_LIVE"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import titan_alert  # noqa: E402
titan_alert.alert = lambda *a, **k: None

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


import home_security as hs  # noqa: E402

# ── a fake Home Assistant websocket, over a socket pair ──
SEEN: list[dict] = []


def frame(obj_or_bytes, op=1, fin=True, length=None):
    p = obj_or_bytes if isinstance(obj_or_bytes, bytes) else json.dumps(obj_or_bytes).encode()
    n = len(p) if length is None else length
    h = bytes([(0x80 if fin else 0) | op])
    h += bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, "big") if n < 65536 else bytes([127]) + n.to_bytes(8, "big")
    return h + p


def read_client_frame(s):
    h = s.recv(2)
    n = h[1] & 0x7F
    if n == 126:
        n = int.from_bytes(s.recv(2), "big")
    mask = s.recv(4)
    data = b""
    while len(data) < n:
        data += s.recv(n - len(data))
    return json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def fake_ws(script):
    """A connect() for stream_url: the server side runs `script(server_socket)` in a thread."""
    def connect():
        a, b = socket.socketpair()
        def serve():
            try:
                req = b""
                while b"\r\n\r\n" not in req:
                    req += b.recv(1024)
                key = [l.split(":", 1)[1].strip() for l in req.decode().split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
                acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                b.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {acc}\r\n\r\n".encode())
                script(b)
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=serve, daemon=True).start()
        return a
    return connect


def good(b):
    b.sendall(frame({"type": "auth_required", "ha_version": "2026.9.4"}))
    SEEN.append(read_client_frame(b))
    b.sendall(frame({"type": "auth_ok"}))
    SEEN.append(read_client_frame(b))
    b.sendall(frame(b"", op=9))   # a ping in between is skipped
    b.sendall(frame({"id": 1, "type": "result", "success": True, "result": {"url": "/api/hls/abc123/master_playlist.m3u8"}}))


print("── the websocket asks for the stream, and nothing else ──")
check("the HLS URL", hs.stream_url("camera.front_west", connect=fake_ws(good), token="t0k"), "/api/hls/abc123/master_playlist.m3u8")
check("auth, then exactly camera/stream with format hls; no other command",
      SEEN, [{"type": "auth", "access_token": "t0k"},
             {"id": 1, "type": "camera/stream", "entity_id": "camera.front_west", "format": "hls"}])


def fails(label, script, timeout=None):
    if timeout is not None:
        old = hs.WS_TIMEOUT
        hs.WS_TIMEOUT = timeout
    t0 = time.monotonic()
    try:
        hs.stream_url("camera.front_west", connect=fake_ws(script), token="t0k")
        check(label, "returned", "refused")
    except (hs.WSError, OSError) as exc:
        check(label, type(exc).__name__ in ("WSError", "timeout", "TimeoutError", "OSError", "ConnectionResetError",
                                           "BrokenPipeError"), True)
    finally:
        if timeout is not None:
            hs.WS_TIMEOUT = old
    return time.monotonic() - t0


fails("garbage instead of JSON", lambda b: b.sendall(frame(b"\x00\xffnot json")))
fails("a frame bigger than the cap", lambda b: b.sendall(frame(b"x" * 10, length=hs.WS_MAX_FRAME + 1)))
fails("a binary frame", lambda b: b.sendall(frame(b"\x01\x02", op=2)))
fails("auth refused", lambda b: (b.sendall(frame({"type": "auth_required"})), read_client_frame(b),
                                 b.sendall(frame({"type": "auth_invalid"}))))
fails("a stream URL that isn't HA's HLS", lambda b: (b.sendall(frame({"type": "auth_required"})), read_client_frame(b),
      b.sendall(frame({"type": "auth_ok"})), read_client_frame(b),
      b.sendall(frame({"id": 1, "type": "result", "success": True, "result": {"url": "http://elsewhere/x.m3u8"}}))))
took = fails("a hang: refused within the timeout", lambda b: time.sleep(5), timeout=1.0)
check("…in about a second, not forever", took < 3, True)
os.environ["HA_URL"] = "http://10.0.0.99:8123"
import importlib  # noqa: E402
import home_assistant as ha  # noqa: E402
importlib.reload(ha)
try:
    hs._local_ha(); check("HA anywhere but this machine is refused", False, True)
except PermissionError:
    check("HA anywhere but this machine is refused", True, True)
os.environ.pop("HA_URL")
importlib.reload(ha)
check("…and here it's allowed", hs._local_ha(), ("127.0.0.1", 8123))

# ── sessions ──
MARK = b"ZebraQuixoticLantern-live"
HLS = {"/api/hls/abc123/master_playlist.m3u8": b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000\nplaylist.m3u8\n",
       "/api/hls/abc123/playlist.m3u8": b"#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:2.0,\nsegment/0.m4s\n",
       "/api/hls/abc123/init.mp4": b"\x00\x00\x00\x18ftypmp42" + MARK,
       "/api/hls/abc123/segment/0.m4s": b"\x00\x00\x01\x00moof" + MARK * 20}
GOT: list[str] = []


def fake_get(path):
    GOT.append(path)
    return HLS[path]


SRC = {"camera.front_west": "local", "camera.front_east": "local", "camera.driveway": "cloud", "camera.shed": "unknown"}
start = lambda cam, dev, host="10.0.0.31", now=None: hs.live_start(cam, dev, host, now=now, source_of=SRC.get,  # noqa: E731
                                                                 url_for=lambda e: "/api/hls/abc123/master_playlist.m3u8")
fetch = lambda sid, p, dev, host="10.0.0.31", now=None: hs.live_fetch(sid, p, host, dev, now=now, get=fake_get)  # noqa: E731

print("\n── off unless exactly \"1\" ──")
for v in (None, "0", "true", " 1"):
    if v is None:
        os.environ.pop("HA_CAMERA_LIVE", None)
    else:
        os.environ["HA_CAMERA_LIVE"] = v
    check(f"HA_CAMERA_LIVE={v!r}: off", start("camera.front_west", "dev-tv", now=1000.0), (403, {"error": "off", "said": hs.LIVE_OFF}))
os.environ["HA_CAMERA_LIVE"] = "1"

print("\n── where and which ──")
for host in ("100.101.2.3", "fd7a:115c:a1e0::1", "8.8.8.8", None):
    check(f"a client at {host}: 403", start("camera.front_west", "dev-x", host, now=1000.0)[0], 403)
for cam, src in (("camera.driveway", "cloud"), ("camera.shed", "unknown")):
    code, out = start(cam, "dev-tv", now=1000.0)
    check(f"a {src} camera: 403", (code, out.get("source")), (403, src))
check("not a camera: 404", start("lock.front_door", "dev-tv", now=1000.0)[0], 404)

print("\n── a session, relayed from memory ──")
before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in _TMP.rglob("*") if p.is_file()}
code, s1 = start("camera.front_west", "dev-tv", now=1000.0)
check("started: 201, the playlist under its session, 5 minutes", (code, s1["playlist"].endswith("/master_playlist.m3u8"),
      s1["max_s"], len(s1["session"])), (201, True, 300, 32))
sid = s1["session"]
for p, kind in (("master_playlist.m3u8", "application/vnd.apple.mpegurl"), ("playlist.m3u8", "application/vnd.apple.mpegurl"),
                ("init.mp4", "video/mp4"), ("segment/0.m4s", "video/iso.segment")):
    code, body, ct = fetch(sid, p, "dev-tv", now=1001.0)
    check(f"{p}: relayed as {kind}", (code, body == HLS[f"/api/hls/abc123/{p}"], ct), (200, True, kind))
check("HA's relative paths are fetched under its own base", GOT[-1], "/api/hls/abc123/segment/0.m4s")
code, body, _ = fetch(sid, "init.mp4", None, now=1002.0)
check("the session id alone fetches (AVPlayer's segment requests)", code, 200)
leaked = [str(p) for p in _TMP.rglob("*") if p.is_file() and MARK in p.read_bytes()]
check("no byte of the stream is in any file under the state dir", leaked, [])
changed = sorted(str(p.relative_to(_TMP)) for p in _TMP.rglob("*") if p.is_file()
                 and before.get(p) != (p.stat().st_size, p.stat().st_mtime_ns))
check("…the only file that changed is the counts database", all(x.startswith("lock_up.db") for x in changed), True)
import sqlite3  # noqa: E402
con = sqlite3.connect(os.environ["LOCK_UP_DB"])
check("counted: one session of Front West, numbers only", con.execute("SELECT camera, sessions FROM live_counts").fetchall(),
      [("camera.front_west", 1)])
con.close()

print("\n── who and which path ──")
check("another device's token: 404", fetch(sid, "init.mp4", "dev-alex", now=1003.0)[0], 404)
check("an unknown session: 404", fetch("0" * 32, "init.mp4", "dev-tv", now=1003.0)[0], 404)
for bad in ("../secrets.m3u8", "/etc/passwd", "segment/../../x.m4s", "clip.ts", "playlist.m3u8?x", "a//b.m4s", ""):
    check(f"path {bad!r}: 400", fetch(sid, bad, "dev-tv", now=1003.0)[0], 400)
check("a fetch from off the home network: 403, even with the session id",
      fetch(sid, "init.mp4", "dev-tv", host="100.101.2.3", now=1003.0)[0], 403)

print("\n── 5 minutes, 20 s idle, at most 2 ──")
t = 1003.0
while t + 15 < 1000.0 + 295:
    t += 15
    fetch(sid, "playlist.m3u8", "dev-tv", now=t)
check("still live at 4:59 with steady fetching", fetch(sid, "playlist.m3u8", "dev-tv", now=1299.0)[0], 200)
check("the 6th minute is refused: 410 time", fetch(sid, "playlist.m3u8", "dev-tv", now=1301.0)[:2], (410, {"ended": "time"}))
code, s2 = start("camera.front_east", "dev-mac", now=2000.0)
check("…its slot is free again", code, 201)
code, s3 = start("camera.front_west", "dev-iphone", now=2001.0)
check("a second at once: fine", code, 201)
check("a third at once: 429", start("camera.front_west", "dev-ipad", now=2002.0), (429, {"error": "two cameras are live already"}))
check("closing one (DELETE) frees a slot", (hs.live_end(s2["session"], "dev-mac"), start("camera.front_west", "dev-ipad", now=2003.0)[0]),
      (204, 201))
check("…the closed one says closed", fetch(s2["session"], "playlist.m3u8", "dev-mac", now=2004.0)[:2], (410, {"ended": "closed"}))
check("someone else can't close yours", hs.live_end(s3["session"], "dev-tv"), 404)
check("21 s without a fetch: 410 idle", fetch(s3["session"], "playlist.m3u8", "dev-iphone", now=2001.0 + 21)[:2],
      (410, {"ended": "idle"}))
code, s4 = start("camera.front_east", "dev-tv2", now=3000.0)
os.environ["HA_CAMERA_LIVE"] = "0"
check("turned off mid-session: 410 off", fetch(s4["session"], "playlist.m3u8", "dev-tv2", now=3001.0)[:2], (410, {"ended": "off"}))
os.environ["HA_CAMERA_LIVE"] = "1"

print("\n── through the API: roles ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app, client=("10.0.0.21", 50000))
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}


ALEX = pair("Alex Hale", "adult", "Alex's iPhone")
AVA = pair("Ava Hale", "kid", "Ava's iPhone")
TV = pair("Living room", "household", "Living room TV", platform="tvos")
hs.sources = lambda http=None: dict(SRC)
hs.stream_url = lambda e, **k: "/api/hls/abc123/master_playlist.m3u8"
hs._ha_get = fake_get
hs._live.clear()
check("a kid: 403 on start", c.post("/v1/home/cameras/camera.front_west/live", headers=AVA).status_code, 403)
r = c.post("/v1/home/cameras/camera.front_west/live", headers=TV)
check("the TV: 201", r.status_code, 201)
pl = r.json()["playlist"]
check("a kid's token on the TV's session: 403", c.get(pl, headers=AVA).status_code, 403)
check("a kid can't close it: 403", c.delete(f"/v1/home/live/{r.json()['session']}", headers=AVA).status_code, 403)
r2 = c.get(pl, headers=TV)
check("the TV fetches its playlist: no-store", (r2.status_code, r2.headers.get("cache-control")), (200, "no-store"))
check("with no token, the session id alone (AVPlayer's segments)", c.get(pl).status_code, 200)
check("Alex's token on the TV's session: 404", c.get(pl, headers=ALEX).status_code, 404)
check("a made-up token: 401", c.get(pl, headers={"X-Agent-Token": "nope"}).status_code, 401)
check("an adult starts one too", c.post("/v1/home/cameras/camera.front_east/live", headers=ALEX).status_code, 201)
check("from Tailscale through the API: 403", TestClient(api.app, client=("100.101.2.3", 1)).get(pl).status_code, 403)
check("the TV closes it: 204", c.delete(f"/v1/home/live/{r.json()['session']}", headers=TV).status_code, 204)
check("…and then 410", c.get(pl, headers=TV).status_code, 410)
import logging  # noqa: E402
rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', ("10.0.0.21:5", "GET",
                        f"/v1/home/live/{'a1' * 16}/segment/0.m4s", "1.1", 200), None)
for f in logging.getLogger("uvicorn.access").filters:
    f.filter(rec)
check("the access log never names a live session's id", (rec.getMessage().count("a1a1"), "/v1/home/live/-/segment/0.m4s" in rec.getMessage()),
      (0, True))
check("the session id is 128 bits from secrets (a CSPRNG)", "secrets.token_hex(16)" in (Path(__file__).parent / "home_security.py").read_text(), True)
src = (Path(__file__).parent / "home_security.py").read_text()
check("the module never sends call_service over the websocket", "call_service" in src, False)

print("\n── HA gzips its playlists (Zach's first live view, 10-04): the client gets plain text ──")
import gzip  # noqa: E402
import http.server  # noqa: E402
ASKED: list[tuple[str, str]] = []
REAL_MASTER = b"""#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1902617,CODECS="avc1.4d001f"
playlist.m3u8
"""
REAL_MEDIA = b"""#EXTM3U
#EXT-X-VERSION:6
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MAP:URI="init.mp4"
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-DISCONTINUITY-SEQUENCE:0
#EXT-X-PART-INF:PART-TARGET=1.000
#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=2.000
#EXT-X-START:TIME-OFFSET=-2.000,PRECISE=YES
#EXT-X-PART:DURATION=0.945,URI="./segment/0.0.m4s",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.946,URI="./segment/0.1.m4s"
#EXT-X-PART:DURATION=0.945,URI="./segment/0.2.m4s",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.945,URI="./segment/0.3.m4s"
#EXT-X-PART:DURATION=0.946,URI="./segment/0.4.m4s",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.945,URI="./segment/0.5.m4s"
#EXT-X-PART:DURATION=0.249,URI="./segment/0.6.m4s"
#EXT-X-PROGRAM-DATE-TIME:2026-10-04T21:43:03.748Z
#EXTINF:5.921,
./segment/0.m4s
#EXT-X-PART:DURATION=0.945,URI="./segment/1.0.m4s",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.945,URI="./segment/1.1.m4s"
#EXT-X-PART:DURATION=0.946,URI="./segment/1.2.m4s",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.945,URI="./segment/1.3.m4s"
#EXT-X-PART:DURATION=0.945,URI="./segment/1.4.m4s",INDEPENDENT=YES
#EXT-X-PRELOAD-HINT:TYPE=PART,URI="./segment/1.5.m4s"
"""
GZ = {   # HA's own shapes, one master playlist written with absolute paths to prove they're made relative
    "/api/hls/zz9/master_playlist.m3u8": b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000\n/api/hls/zz9/playlist.m3u8\n",
    "/api/hls/zz9/playlist.m3u8": b'#EXTM3U\n#EXT-X-MAP:URI="http://127.0.0.1:8123/api/hls/zz9/init.mp4"\n#EXTINF:2.0,\nsegment/7.m4s\n',
    "/api/hls/zz9/init.mp4": b"\x00\x00\x00\x18ftypmp42" + MARK,
    "/api/hls/zz9/segment/7.m4s": b"\x00\x00\x01\x00moof" + MARK * 30,
    "/api/hls/zz9/bomb.m3u8": gzip.compress(b"#" * (hs.LIVE_MAX_BYTES + 10)),
    # Low-latency HLS as HA's stream writes it (Zach's second live view stalled on its parts, 10-04).
    "/api/hls/ll1/master_playlist.m3u8": b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1200000,CODECS=\"avc1.640028\"\n./playlist.m3u8\n",
    "/api/hls/ll1/playlist.m3u8": b"""#EXTM3U
#EXT-X-VERSION:9
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PART-INF:PART-TARGET=1.000
#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=2.000,HOLD-BACK=12.000,CAN-SKIP-UNTIL=36.000
#EXT-X-START:TIME-OFFSET=-2.000,PRECISE=YES
#EXT-X-MAP:URI="./init.mp4"
#EXT-X-PROGRAM-DATE-TIME:2026-10-04T21:10:00.000+00:00
#EXT-X-PART:DURATION=1.000,URI="./segment/0.0.m4s",INDEPENDENT=YES
#EXT-X-PART:DURATION=1.000,URI="./segment/0.1.m4s"
#EXTINF:6.000,
./segment/0.m4s
#EXT-X-PROGRAM-DATE-TIME:2026-10-04T21:10:06.000+00:00
#EXT-X-PART:DURATION=1.000,URI="./segment/1.0.m4s",INDEPENDENT=YES
#EXTINF:6.000,
./segment/1.m4s
#EXT-X-PART:DURATION=1.000,URI="./segment/2.0.m4s",INDEPENDENT=YES
#EXT-X-PRELOAD-HINT:TYPE=PART,URI="./segment/2.1.m4s"
#EXT-X-RENDITION-REPORT:URI="../other/playlist.m3u8",LAST-MSN=2,LAST-PART=0
""",
    "/api/hls/ll1/segment/1.m4s": b"\x00\x00\x01\x00moof" + MARK * 10,
    # HA 2026.9.4's own answers for a Lorex camera (Generic Camera, sub stream), captured on titan 10-04: tags only.
    "/api/hls/real/master_playlist.m3u8": REAL_MASTER,
    "/api/hls/real/playlist.m3u8": REAL_MEDIA,
}


class FakeHA(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ASKED.append((self.path, self.headers.get("Accept-Encoding", "")))
        body = GZ.get(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        if self.path.endswith("bomb.m3u8"):
            self.send_header("Content-Encoding", "gzip")
        elif self.path.endswith("weird.m3u8"):
            self.send_header("Content-Encoding", "br")
        else:
            body = gzip.compress(body)          # gzips everything, whatever was asked
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHA)
threading.Thread(target=srv.serve_forever, daemon=True).start()
saved_url, saved_get = ha.HA_URL, hs._ha_get
ha.HA_URL = f"http://127.0.0.1:{srv.server_address[1]}"
import importlib.util as _iu  # noqa: E402
_spec = _iu.spec_from_file_location("hs_fresh", Path(__file__).parent / "home_security.py")
fresh = _iu.module_from_spec(_spec); _spec.loader.exec_module(fresh)   # the real _ha_get (the API section swapped it)
hs._ha_get = fresh._ha_get
hs._live.clear()
code, gs = hs.live_start("camera.front_west", "dev-gz", "10.0.0.31", now=9000.0, source_of=SRC.get,
                         url_for=lambda e: "/api/hls/zz9/master_playlist.m3u8")
gsid = gs["session"]
code, body, ct = hs.live_fetch(gsid, "master_playlist.m3u8", "10.0.0.31", "dev-gz", now=9001.0)
check("the master playlist reaches the client as plain m3u8 text", (code, body[:7], ct), (200, b"#EXTM3U", "application/vnd.apple.mpegurl"))
check("…with its URI relative, so it resolves under /v1/home/live/<session>/", body.decode().splitlines()[-1], "playlist.m3u8")
code, body, _ = hs.live_fetch(gsid, "playlist.m3u8", "10.0.0.31", "dev-gz", now=9002.0)
check("the media playlist too: plain, every URI relative (no /api/hls, no HA address)",
      (code, b"/api/hls" in body, b"127.0.0.1" in body, b'URI="init.mp4"' in body, b"segment/7.m4s" in body),
      (200, False, False, True, True))
for pth, want in (("init.mp4", GZ["/api/hls/zz9/init.mp4"]), ("segment/7.m4s", GZ["/api/hls/zz9/segment/7.m4s"])):
    code, body, _ = hs.live_fetch(gsid, pth, "10.0.0.31", "dev-gz", now=9003.0)
    check(f"{pth}: unzipped, byte for byte", (code, body == want), (200, True))
check("titan asked HA for identity every time", sorted({a for _, a in ASKED}), ["identity"])
code, body, _ = hs.live_fetch(gsid, "bomb.m3u8", "10.0.0.31", "dev-gz", now=9004.0)
check("a body that unzips past the cap: refused, not relayed", code, 502)
try:
    hs.plain(b"abc", "br"); check("another encoding is refused, not passed on", False, True)
except ValueError:
    check("another encoding is refused, not passed on", True, True)
check("plain bytes stay as they are", hs.plain(b"#EXTM3U\n"), b"#EXTM3U\n")
r = c.post("/v1/home/cameras/camera.front_west/live", headers=TV)
hs._live[r.json()["session"]]["base"] = "/api/hls/zz9"
r2 = c.get(r.json()["playlist"].replace("master_playlist.m3u8", "playlist.m3u8"), headers=TV)
check("through the API: plain text, no Content-Encoding, relative URIs",
      (r2.status_code, r2.headers.get("content-encoding"), r2.content.startswith(b"#EXTM3U"), b"/api/hls" in r2.content),
      (200, None, True, False))
print("\n── HA's low-latency HLS: titan serves plain HLS, full segments only ──")
ASKED.clear()
r = c.post("/v1/home/cameras/camera.front_west/live", headers=TV)
hs._live[r.json()["session"]]["base"] = "/api/hls/ll1"
base_url = r.json()["playlist"].rsplit("/", 1)[0]
t0 = time.monotonic()
r3 = c.get(f"{base_url}/playlist.m3u8?_HLS_msn=2&_HLS_part=1", headers=TV)
took = time.monotonic() - t0
lines = r3.content.decode().splitlines()
check("a blocking reload (_HLS_msn/_HLS_part) is answered at once, with a playlist", (r3.status_code, took < 2, lines[0]),
      (200, True, "#EXTM3U"))
check("…and its query never reaches HA", [p for p, _ in ASKED], ["/api/hls/ll1/playlist.m3u8"])
for tag in ("#EXT-X-PART-INF", "#EXT-X-SERVER-CONTROL", "#EXT-X-PART:", "#EXT-X-PRELOAD-HINT", "#EXT-X-RENDITION-REPORT",
            "#EXT-X-SKIP", "#EXT-X-START"):
    check(f"no {tag} line left", [l for l in lines if l.startswith(tag)], [])
check("the full segments stay, each after its #EXTINF", [l for l in lines if not l.startswith("#")], ["./segment/0.m4s", "./segment/1.m4s"])
check("…with the map, the target duration and the program date-times",
      ('#EXT-X-MAP:URI="./init.mp4"' in lines, "#EXT-X-TARGETDURATION:6" in lines,
       sum(l.startswith("#EXT-X-PROGRAM-DATE-TIME") for l in lines), lines.count("#EXTINF:6.000,")), (True, True, 2, 2))
r4 = c.get(f"{base_url}/segment/1.m4s", headers=TV)
check("a full segment relays", (r4.status_code, r4.content == GZ["/api/hls/ll1/segment/1.m4s"]), (200, True))
check("the master playlist passes as it was (no low-latency tags in it)",
      c.get(f"{base_url}/master_playlist.m3u8", headers=TV).content.decode().splitlines()[-1], "./playlist.m3u8")
print("\n── HA 2026.9.4's real answers (captured 10-04), gzipped as HA sends them ──")
hs._live.clear()
r = c.post("/v1/home/cameras/camera.front_west/live", headers=TV)
hs._live[r.json()["session"]]["base"] = "/api/hls/real"
real = r.json()["playlist"].rsplit("/", 1)[0]
check("the real master playlist reaches the client exactly as HA wrote it, unzipped",
      c.get(f"{real}/master_playlist.m3u8", headers=TV).content, REAL_MASTER)
check("the real media playlist becomes exactly this plain HLS", c.get(f"{real}/playlist.m3u8?_HLS_msn=1&_HLS_part=5", headers=TV).content,
      b"""#EXTM3U
#EXT-X-VERSION:6
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MAP:URI="init.mp4"
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-DISCONTINUITY-SEQUENCE:0
#EXT-X-PROGRAM-DATE-TIME:2026-10-04T21:43:03.748Z
#EXTINF:5.921,
./segment/0.m4s
""")
leaked = [str(p) for p in _TMP.rglob("*") if p.is_file() and MARK in p.read_bytes()]
check("still nothing of the stream on disk", leaked, [])
srv.shutdown()
ha.HA_URL, hs._ha_get = saved_url, saved_get

print("\n── Tailscale: adults' own devices only (Zach 10-04, #413) ──")
os.environ["HA_CAMERA_SNAPSHOTS"] = "1"
hs._proxy = lambda eid: b"\xff\xd8still\xff\xd9"
hs._live.clear()
hs._last_still.clear()
TS = TestClient(api.app, client=("100.104.2.22", 50000))
PUB = TestClient(api.app, client=("203.0.113.9", 50000))
LAN = TestClient(api.app, client=("10.0.0.21", 50000))
r = TS.get("/v1/home/cameras/camera.front_west/snapshot", headers=ALEX)
check("an adult from 100.x: the still", (r.status_code, r.content[:2]), (200, b"\xff\xd8"))
r = TS.post("/v1/home/cameras/camera.front_west/live", headers=ALEX)
check("an adult from 100.x: live starts", r.status_code, 201)
pl = r.json()["playlist"]
check("…and its playlist and segments fetch from 100.x with the session id alone", (TS.get(pl).status_code,
      TS.get(pl.replace("master_playlist.m3u8", "segment/0.m4s")).status_code), (200, 200))
check("…but not from a public address", PUB.get(pl).status_code, 403)
for who, h in (("the household TV", TV), ("a kid", AVA)):
    check(f"{who} from 100.x: no still", TS.get("/v1/home/cameras/camera.front_east/snapshot", headers=h).status_code, 403)
    check(f"{who} from 100.x: no live", TS.post("/v1/home/cameras/camera.front_east/live", headers=h).status_code, 403)
hs._live.clear()
r = LAN.post("/v1/home/cameras/camera.front_east/live", headers=TV)
check("the TV's session on the LAN: fine", r.status_code, 201)
check("…but its id from 100.x is refused (not an adult's session)", TS.get(r.json()["playlist"]).status_code, 403)
for who, h in (("an adult", ALEX), ("the household TV", TV), ("a kid", AVA)):
    check(f"{who} from a public address: no still, no live",
          (PUB.get("/v1/home/cameras/camera.front_west/snapshot", headers=h).status_code,
           PUB.post("/v1/home/cameras/camera.front_west/live", headers=h).status_code), (403, 403))
hs._last_still.clear()
check("the LAN as before: the TV and an adult see stills",
      (LAN.get("/v1/home/cameras/camera.front_west/snapshot", headers=TV).status_code,
       LAN.get("/v1/home/cameras/camera.front_east/snapshot", headers=ALEX).status_code), (200, 200))
check("Tailscale's IPv6 range and other private-looking ranges stay refused for adults too",
      [hs.may_view_from(h, "adult") for h in ("fd7a:115c:a1e0::5", "100.63.0.1", "100.128.0.1")], [False, False, False])
check("documentation and benchmark ranges aren't a home network (Python's is_private says they are)",
      [hs.on_home_network(h) for h in ("203.0.113.9", "192.0.2.1", "198.51.100.7", "198.18.0.1", "0.0.0.0")],
      [False, False, False, False, False])
check("…and 100.64/10 only for admin and adult", [hs.may_view_from("100.104.2.22", r) for r in ("admin", "adult", "household", "kid", None)],
      [True, True, False, False, False])

print("\n── a revoked device's sessions end at once (#1246) ──")
hs._live.clear()
os.environ["HA_CAMERA_LIVE"] = "1"
_, tv = start("camera.front_west", "dev-tv-r", now=5000.0)
_, phone = start("camera.front_east", "dev-phone-r", now=5000.0)
check("two sessions: the slots are full", start("camera.front_west", "dev-ipad-r", now=5001.0)[0], 429)
check("revoking the TV ends exactly its session", hs.end_device("dev-tv-r"), 1)
check("…its session answers 410 revoked (nobody can fetch it)", fetch(tv["session"], "init.mp4", None, now=5002.0)[:2],
      (410, {"ended": "revoked"}))
check("…the other device's session keeps playing", fetch(phone["session"], "init.mp4", "dev-phone-r", now=5002.0)[0], 200)
check("…and its slot is free now, not after LIVE_IDLE_S", start("camera.front_west", "dev-ipad-r", now=5003.0)[0], 201)
check("ending again, or no device, ends nothing", (hs.end_device("dev-tv-r"), hs.end_device(None), hs.end_device("")), (0, 0, 0))
api_src = (Path(__file__).parent / "agent_api_server.py").read_text()
check("DELETE /v1/devices/{id} ends them after the revoke",
      api_src.index("_store().revoke_device, device_id") < api_src.index("home_security.end_device, device_id"), True)
hs._live.clear()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
