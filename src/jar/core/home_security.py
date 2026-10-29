#!/usr/bin/env python3
"""The house's locks and doors, camera stills, and Lock Up (titan-node#413, decided by Zach 10-04 for the TV
redesign jellybean#803). Contract: the #413 comment "Contract (iOS session, titan side)".

  status     read-only: lock.* states, doors (binary_sensor door/opening/window, cover door/gate) and garage doors
             (cover garage). Admin, adult and household screens; never a kid's device. Alarm panels are never read.
  snapshots  a still from Home Assistant's camera proxy, passed through from memory: never written, never logged,
             only to a client on the home network (not Tailscale), at most once per 30 s per device and camera.
             Counted per day and camera, nothing else. HA_CAMERA_SNAPSHOTS=1, off otherwise.
  lock up    lock only. A request (the TV, the chat, a phone) becomes a card to the adults asked; only a tap on that
             adult's own iPhone runs lock.lock, for the locks still unlocked; titan reads them again and says what
             it found; every confirmation is an audit row. HA_LOCK_UP=1, off otherwise.

Nothing here unlocks, opens, closes, arms or disarms. The one service this module can call is lock.lock
(`_lock_one`); there's no other services path in it, and no route or card action asks for one.
"""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

import local_time

DB = Path("/ai/datasets/connectors/lock_up.db")
REQUEST_TTL = 120          # a Lock Up card is answerable this long; a stale one never locks
SETTLE_S = 10              # how long titan re-reads a lock that's still locking
SNAPSHOT_EVERY = 30        # at most one still per device and camera this often
ADULTS = ("admin", "adult")
VIEWERS = ("admin", "adult", "household")
OFF = "Locking from Jellybean is turned off."
SNAPSHOTS_OFF = "Camera stills are turned off."
NO_LOCKS = "No locks are connected to Jellybean yet."
# Where a camera's picture comes from, by its Home Assistant integration (Coordination 10-04 for Zach: stills only from a
# local source in slice 1). Local: titan reads the camera on the LAN (Lorex through ONVIF or RTSP, and the like). Cloud:
# the integration goes through the maker's servers (Blink). Anything else is unknown, and unknown is not local.
LOCAL_CAMERA_INTEGRATIONS = ("onvif", "generic", "ffmpeg", "amcrest", "reolink", "unifiprotect", "axis", "foscam",
                             "motioneye", "hikvision")
CLOUD_CAMERA_INTEGRATIONS = ("blink", "ring", "nest", "arlo", "wyze", "simplisafe", "logi_circle", "canary", "eufy")
DOOR_SENSORS = {"door", "opening", "window"}
DOOR_COVERS = {"door", "gate"}

Http = Callable[[str, str, Any], Any]
_lock = threading.Lock()
_last_still: dict[tuple[str, str], float] = {}


def lock_up_on() -> bool:
    return os.environ.get("HA_LOCK_UP", "") == "1"


def snapshots_on() -> bool:
    return os.environ.get("HA_CAMERA_SNAPSHOTS", "") == "1"


def _http() -> Http:
    import home_assistant as ha
    return ha._http   # resolved per call, so a test can swap the module's client


def _db() -> sqlite3.Connection:
    p = Path(os.environ.get("LOCK_UP_DB") or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, device_id TEXT, device_name TEXT,"
                " person_id TEXT, locks TEXT, created_at REAL, expires_at REAL, state TEXT, card_id TEXT,"
                " sent_to TEXT, said TEXT, results TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS audit (request_id TEXT, person_id TEXT, at REAL, results TEXT)")
    if "via" not in {c[1] for c in con.execute("PRAGMA table_info(audit)")}:
        con.execute("ALTER TABLE audit ADD COLUMN via TEXT")   # jellybean#900: "watch" for a tap relayed from the Watch
    con.execute("CREATE TABLE IF NOT EXISTS snapshot_counts (day TEXT, camera TEXT, count INTEGER,"
                " PRIMARY KEY (day, camera))")
    return con


# ── status ──

def _name(s: dict) -> str:
    return str((s.get("attributes") or {}).get("friendly_name") or s.get("entity_id") or "")


def _row(s: dict) -> dict[str, str]:
    return {"id": s["entity_id"], "name": _name(s), "state": str(s.get("state") or "unknown")}


def read(http: Http | None = None) -> dict[str, list[dict[str, str]]]:
    """Locks, doors and garage doors from GET /api/states. Nothing else is kept from the answer."""
    states = (http or _http())("GET", "/api/states", None) or []
    locks, doors, garage = [], [], []
    for s in states if isinstance(states, list) else []:
        eid = str(s.get("entity_id") or "")
        dc = str((s.get("attributes") or {}).get("device_class") or "")
        if eid.startswith("lock."):
            locks.append(_row(s))
        elif eid.startswith("binary_sensor.") and dc in DOOR_SENSORS:
            r = _row(s)
            r["state"] = {"on": "open", "off": "closed"}.get(r["state"], r["state"])
            doors.append(r)
        elif eid.startswith("cover.") and dc in DOOR_COVERS:
            doors.append(_row(s))
        elif eid.startswith("cover.") and dc == "garage":
            garage.append(_row(s))
    return {"locks": locks, "doors": doors, "garage": garage}


def summary(st: dict[str, list[dict[str, str]]]) -> str:
    """"Doors locked · Garage closed"; a part with nothing behind it is left out (no locks connected: nothing about locks)."""
    parts = []
    locks = st["locks"]
    if locks:
        open_ = [l["name"] for l in locks if l["state"] != "locked"]
        parts.append("Doors locked" if not open_ else f"{', '.join(open_)} {'is' if len(open_) == 1 else 'are'} unlocked")
    if st["garage"]:
        up = [g["name"] for g in st["garage"] if g["state"] != "closed"]
        parts.append("Garage closed" if not up else f"{', '.join(up)} {'is' if len(up) == 1 else 'are'} open")
    return " · ".join(parts)


def status(http: Http | None = None) -> tuple[int, dict[str, Any]]:
    try:
        st = read(http)
    except PermissionError as exc:
        return 503, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return 503, {"error": f"Home Assistant didn't answer ({type(exc).__name__})"}
    return 200, {**st, "summary": summary(st), "lock_up": lock_up_on(), "snapshots": snapshots_on()}


# ── camera stills ──

_TAILNET4 = ipaddress.ip_network("100.64.0.0/10")
_HOME4 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16"))
_HOME6 = tuple(ipaddress.ip_network(n) for n in ("::1/128", "fe80::/10", "fc00::/7"))
_TAILNET6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _ip(host: str | None) -> Any:
    try:
        ip = ipaddress.ip_address(str(host or "").split("%")[0])
    except ValueError:
        return None
    return getattr(ip, "ipv4_mapped", None) or ip


def in_tailnet(host: str | None) -> bool:
    """Tailscale's private range, 100.64.0.0/10 (Zach 10-04, #413: an adult's own phone away from home)."""
    ip = _ip(host)
    return ip is not None and ip.version == 4 and ip in _TAILNET4


def may_view_from(host: str | None, role: str | None) -> bool:
    """Where the cameras may be seen from: the home network for every role that may see them (admin, adult, household;
    kids never reach these routes), and Tailscale for an adult's or the admin's own devices too (Zach 10-04, #413).
    Any other address, never."""
    return on_home_network(host) or (role in ADULTS and in_tailnet(host))


OFF_NETWORK = "Cameras are for the home network, or an adult's own phone or Mac over Tailscale."


def on_home_network(host: str | None) -> bool:
    """A client address on the home network: private IPv4, loopback, IPv6 link-local or ULA. Tailscale (100.64/10,
    fd7a:115c:a1e0::/48) is somewhere else, so it's not."""
    try:
        ip = ipaddress.ip_address(str(host or "").split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip in _TAILNET4 or ip in _TAILNET6:
        return False
    # The ranges by name: Python's is_private also counts documentation and benchmark ranges (192.0.2/24, 203.0.113/24,
    # 198.18/15…), which aren't anyone's home network.
    return any(ip in n for n in (_HOME4 if ip.version == 4 else _HOME6))


def cameras(http: Http | None = None) -> tuple[int, dict[str, Any]]:
    if not snapshots_on():
        return 403, {"error": "off", "said": SNAPSHOTS_OFF}
    try:
        states = (http or _http())("GET", "/api/states", None) or []
    except Exception as exc:  # noqa: BLE001
        return 503, {"error": f"Home Assistant didn't answer ({type(exc).__name__})"}
    try:
        src = sources(http)
    except Exception:  # noqa: BLE001 - not knowing a camera's source makes it "unknown", never local
        src = {}
    cams = [{"id": s["entity_id"], "name": _name(s), "source": src.get(s["entity_id"], "unknown")}
            for s in states if str(s.get("entity_id") or "").startswith("camera.")]
    return 200, {"cameras": cams, "refresh_s": SNAPSHOT_EVERY}


def sources(http: Http | None = None) -> dict[str, str]:
    """camera entity → "local" | "cloud", from Home Assistant's integration_entities (one template call)."""
    names = LOCAL_CAMERA_INTEGRATIONS + CLOUD_CAMERA_INTEGRATIONS
    tpl = "{{ {" + ", ".join(f"'{n}': integration_entities('{n}')" for n in names) + "} | tojson }}"
    raw = (http or _http())("POST", "/api/template", {"template": tpl})
    data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    out: dict[str, str] = {}
    for integ, ents in (data or {}).items():
        kind = "local" if integ in LOCAL_CAMERA_INTEGRATIONS else "cloud" if integ in CLOUD_CAMERA_INTEGRATIONS else None
        for e in ents or []:
            if kind and str(e).startswith("camera.") and out.get(str(e)) != "cloud":
                out[str(e)] = kind   # in both lists somehow: cloud wins
    return out


def snapshot(entity_id: str, device_id: str, client_host: str | None, *, fetch: Callable[[str], bytes] | None = None,
             now: float | None = None, source_of: Callable[[str], str] | None = None,
             role: str | None = None) -> tuple[int, bytes | dict[str, Any], dict[str, str]]:
    """(status, JPEG bytes or an error body, headers). The bytes are only ever in memory."""
    now = time.time() if now is None else now
    if not snapshots_on():
        return 403, {"error": "off", "said": SNAPSHOTS_OFF}, {}
    if not entity_id.startswith("camera.") or not entity_id[7:].replace("_", "").isalnum():
        return 404, {"error": "no such camera"}, {}
    if not may_view_from(client_host, role):
        return 403, {"error": OFF_NETWORK}, {}
    try:
        source = (source_of or (lambda e: sources().get(e, "unknown")))(entity_id)
    except Exception:  # noqa: BLE001
        source = "unknown"
    if source != "local":
        return 403, {"error": "Only cameras titan reads on the home network show stills here.", "source": source}, {}
    key = (device_id, entity_id)
    with _lock:
        last = _last_still.get(key, 0.0)
        if now - last < SNAPSHOT_EVERY:
            wait = int(SNAPSHOT_EVERY - (now - last)) + 1
            return 429, {"error": "too soon", "retry_after": wait}, {"Retry-After": str(wait)}
        _last_still[key] = now
    try:
        data = (fetch or _proxy)(entity_id)
    except Exception as exc:  # noqa: BLE001
        return 503, {"error": f"Home Assistant didn't answer ({type(exc).__name__})"}, {}
    _count(entity_id, now)
    return 200, data, {"Cache-Control": "no-store", "Content-Type": "image/jpeg"}


def _proxy(entity_id: str) -> bytes:
    import urllib.request
    import home_assistant as ha
    tok = ha._token()
    if not tok:
        raise PermissionError(ha.NOT_CONNECTED)
    req = urllib.request.Request(f"{ha.HA_URL}/api/camera_proxy/{entity_id}", headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def _count(camera: str, now: float) -> None:
    day = local_time.day(now)
    with _lock:
        con = _db()
        with con:
            con.execute("INSERT INTO snapshot_counts VALUES (?,?,1) ON CONFLICT(day, camera) DO UPDATE SET count=count+1",
                        (day, camera))
        con.close()


# ── Lock Up ──

def _lock_one(entity_id: str, http: Http) -> None:
    """The one service this module calls."""
    if not entity_id.startswith("lock."):
        raise ValueError("only a lock")
    http("POST", "/api/services/lock/lock", {"entity_id": entity_id})


def adults_to_ask(asker: dict[str, Any], people: list[dict[str, Any]], where: Any = None) -> list[str]:
    """An adult asking from their own device confirms it themselves; otherwise the adults at home (tv_unlock's rule)."""
    if asker.get("role") in ADULTS and asker.get("person_id"):
        return [str(asker["person_id"])]
    import tv_unlock
    return tv_unlock.at_home(people, where)


def request(asker: dict[str, Any], people: list[dict[str, Any]], *, http: Http | None = None, now: float | None = None,
            add_card: Any = None, where: Any = None) -> tuple[int, dict[str, Any]]:
    """Lock Up asked for. `asker`: {role, person_id, device_id, device_name}. (status, body)."""
    now = time.time() if now is None else now
    if not lock_up_on():
        return 403, {"error": "off", "said": OFF}
    if asker.get("role") not in VIEWERS:
        return 403, {"error": "forbidden"}
    try:
        locks = read(http)["locks"]
    except Exception as exc:  # noqa: BLE001
        return 503, {"error": f"Home Assistant didn't answer ({type(exc).__name__})"}
    todo = [l for l in locks if l["state"] != "locked"]
    if not locks:
        return 200, {"already": True, "said": NO_LOCKS}
    if not todo:
        return 200, {"already": True, "said": "Everything's locked."}
    asked = adults_to_ask(asker, people, where)
    if not asked:
        return 409, {"error": "there's no adult in the household to ask"}
    if add_card is None:
        import proactive
        add_card = proactive.add_card
    rid = secrets.token_hex(8)
    names = [l["name"] for l in todo]
    device_id = str(asker.get("device_id") or "")
    _retire_pending(device_id)
    where_from = asker.get("device_name") if asker.get("role") == "household" else None
    detail = f"Lock {_and(names)}." + (f" Asked from the {where_from}." if where_from else "")
    card = add_card("Lock up?", detail, kind="info", urgency=2, source="lock-up", to=asked, key=f"lock-up:{rid}",
                    expires_in=REQUEST_TTL,
                    actions=[{"id": "lock", "label": "Lock", "kind": "reply", "value": f"lock:{rid}"},
                             {"id": "decline", "label": "Not now", "kind": "reply", "value": f"decline:{rid}"}],
                    data={"kind": "lock_up", "request_id": rid, "locks": names, "asked_from": where_from,
                          "confirm_on": "iphone"})
    with _lock:
        con = _db()
        with con:
            con.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, device_id, str(asker.get("device_name") or ""), str(asker.get("person_id") or ""),
                         json.dumps([l["id"] for l in todo]), now, now + REQUEST_TTL, "pending",
                         str(card.get("id") or ""), json.dumps(asked), None, None))
        con.close()
    return 202, {"request_id": rid, "locks": names, "asked": len(asked), "expires_at": now + REQUEST_TTL}


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _retire_pending(device_id: str) -> None:
    if not device_id:
        return
    with _lock:
        con = _db()
        rows = con.execute("SELECT card_id FROM requests WHERE device_id=? AND state='pending'", (device_id,)).fetchall()
        with con:
            con.execute("UPDATE requests SET state='replaced' WHERE device_id=? AND state='pending'", (device_id,))
        con.close()
    for r in rows:
        _ack(r["card_id"])


def _ack(card_id: str | None) -> None:
    if not card_id:
        return
    try:
        import proactive
        proactive.ack_card(card_id)
    except Exception:  # noqa: BLE001
        pass


# A lock tap relayed from the Watch (jellybean#900): the Watch shows Not now only, with this same line.
WATCH_LOCK = "Lock it on your iPhone."


def own_iphone(device: dict[str, Any] | None, role: str | None) -> bool:
    """The person's own iPhone: an adult's device row on iOS. Never a household device, a Mac, an iPad or a Watch."""
    return bool(device) and role in ADULTS and str(device.get("platform") or "") == "ios"


def answer(value: str, person: str | None, role: str | None, device: dict[str, Any] | None, *,
           http: Http | None = None, now: float | None = None, sleep: Callable[[float], None] = time.sleep,
           via: str | None = None) -> tuple[bool, str]:
    """A tap on the card (proactive.card_action, source "lock-up"): lock:<request> or decline:<request>. (done, said).
    via "watch" (jellybean#900): the tap was relayed from the Watch by the iPhone, so the device is the iPhone's but the
    tap wasn't on it: lock is refused with WATCH_LOCK, decline still works. The iPhone's own claim: defence in depth."""
    now = time.time() if now is None else now
    verb, _, ref = str(value or "").partition(":")
    if verb not in ("lock", "decline"):
        return False, "titan didn't understand that."
    who = str(person or "")
    with _lock:
        con = _db()
        r = con.execute("SELECT * FROM requests WHERE id=?", (ref,)).fetchone()
        con.close()
    if not r or who not in json.loads(r["sent_to"] or "[]"):
        return False, "That request isn't there any more."
    if verb == "decline":
        _settle(ref, "declined", "OK, nothing was locked.", [])
        return True, "OK, nothing was locked."
    if via == "watch":
        _audit(ref, who, now, [], via)   # the refused wrist tap is kept too: results empty, nothing was sent
        return False, WATCH_LOCK
    if not lock_up_on():
        return False, OFF
    if role not in ADULTS:
        return False, "Only an adult can lock up."
    if not own_iphone(device, role):
        return False, "Confirm Lock Up on your iPhone."
    if r["state"] != "pending":
        return False, "That's been answered already."
    if now > r["expires_at"]:
        _settle(ref, "expired", "That request ran out. Ask again.", [])
        return False, "That request ran out. Ask again."
    with _lock:   # the first tap wins
        con = _db()
        with con:
            n = con.execute("UPDATE requests SET state='locking' WHERE id=? AND state='pending'", (ref,)).rowcount
        con.close()
    if not n:
        return False, "That's been answered already."
    http = http or _http()
    wanted = set(json.loads(r["locks"] or "[]"))
    try:
        for l in read(http)["locks"]:
            if l["id"] in wanted and l["state"] != "locked":
                _lock_one(l["id"], http)
        results = _settled(http, wanted, now, sleep)
    except Exception as exc:  # noqa: BLE001
        said = f"Home Assistant didn't answer ({type(exc).__name__}); nothing is known to have locked."
        _settle(ref, "failed", said, [])
        _audit(ref, who, now, [], via)
        return False, said
    ok = all(x["state"] == "locked" for x in results)
    said = " ".join(f"{x['name']} locked." if x["state"] == "locked" else f"{x['name']} didn't lock ({x['state']})."
                    for x in results) or "Nothing needed locking."
    _settle(ref, "locked" if ok else "partly", said, results)
    _audit(ref, who, now, results, via)
    _ack(r["card_id"])
    return True, said


def _settled(http: Http, wanted: set[str], start: float, sleep: Callable[[float], None]) -> list[dict[str, str]]:
    """Read the locks again until each is locked or not moving any more, at most SETTLE_S."""
    waited = 0.0
    while True:
        rows = [l for l in read(http)["locks"] if l["id"] in wanted]
        if all(l["state"] not in ("locking", "unlocking") for l in rows) or waited >= SETTLE_S:
            return [{"name": l["name"], "state": l["state"]} for l in rows]
        sleep(1.0)
        waited += 1.0


def _settle(rid: str, state: str, said: str, results: list[dict[str, str]]) -> None:
    with _lock:
        con = _db()
        with con:
            con.execute("UPDATE requests SET state=?, said=?, results=? WHERE id=?", (state, said, json.dumps(results), rid))
        con.close()


def _audit(rid: str, person: str, at: float, results: list[dict[str, str]], via: str | None = None) -> None:
    with _lock:
        con = _db()
        with con:
            con.execute("INSERT INTO audit (request_id, person_id, at, results, via) VALUES (?,?,?,?,?)",
                        (rid, person, at, json.dumps(results), via))
        con.close()


def result(rid: str, device_id: str, *, now: float | None = None) -> tuple[int, dict[str, Any]]:
    """GET /v1/home/lock-up/{id}: the asking device follows its request."""
    now = time.time() if now is None else now
    with _lock:
        con = _db()
        r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        con.close()
    if not r or r["device_id"] != device_id:
        return 404, {"error": "no such request"}
    state = r["state"]
    if state == "pending" and now > r["expires_at"]:
        state = "expired"
    shown = {"locking": "pending", "replaced": "expired", "failed": "partly"}.get(state, state)
    return 200, {"state": shown, "results": json.loads(r["results"] or "[]"), "said": r["said"] or ""}


def tool(args: dict[str, Any], person_id: str, role: str) -> str:
    """The chat's lock_up tool: the asker confirms it on their own iPhone."""
    if not lock_up_on():
        return OFF
    if role not in ADULTS:
        return "Only an adult can lock up."
    import household_identity as hh
    code, out = request({"role": role, "person_id": person_id, "device_id": f"chat:{person_id}", "device_name": "chat"},
                        hh.get_store().people())
    if code == 202:
        return f"I've sent Lock up to your iPhone: tap Lock there to lock {_and(out['locks'])}."
    return out.get("said") or out.get("error") or "That didn't work."


# ── live video (Zach 10-04: on tap, the TV and adults' apps, home network only, never recorded) ──
#
# titan asks Home Assistant for the camera's HLS stream (websocket `camera/stream`, format hls) and relays the playlist,
# init and segments to the device that asked, from memory: nothing is written, nothing is recorded. A session lasts at
# most LIVE_MAX_S, ends after LIVE_IDLE_S without a fetch, on DELETE, or when HA_CAMERA_LIVE goes off; at most
# LIVE_SLOTS at once across the house. The session id in the path is the credential for the fetches (AVPlayer's
# segment requests): 128 bits, home network only, short-lived; a request that does carry a token must be the device's
# that started it.

LIVE_MAX_S = 300
LIVE_IDLE_S = 20
LIVE_SLOTS = 2
LIVE_OFF = "Live video is turned off."
LIVE_MAX_BYTES = 8 * 1024 * 1024        # one playlist or segment; a sub stream's are far smaller
WS_TIMEOUT = 8.0                         # the whole websocket exchange
WS_MAX_FRAME = 64 * 1024                 # HA's answers are a few hundred bytes
_LIVE_PATH = __import__("re").compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*\.(m3u8|mp4|m4s)$")
_live: dict[str, dict[str, Any]] = {}


def live_on() -> bool:
    return os.environ.get("HA_CAMERA_LIVE", "") == "1"


def _local_ha() -> tuple[str, int]:
    """Home Assistant on this machine, or nothing: the websocket client never talks to anything else."""
    from urllib.parse import urlparse
    import home_assistant as ha
    u = urlparse(ha.HA_URL)
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise PermissionError("live video talks to Home Assistant on titan only")
    return u.hostname, u.port or 8123


class WSError(Exception):
    pass


class _Buffered:
    """The socket after the handshake: bytes read past the HTTP headers (the first frame can arrive with them) come
    first."""
    def __init__(self, sock: Any, rest: bytes) -> None:
        self.sock, self.rest = sock, rest

    def recv(self, n: int) -> bytes:
        if self.rest:
            out, self.rest = self.rest[:n], self.rest[n:]
            return out
        return self.sock.recv(n)

    def sendall(self, data: bytes) -> None:
        self.sock.sendall(data)


def _recv_exact(sock: Any, n: int, deadline: float) -> bytes:
    buf = b""
    while len(buf) < n:
        if time.monotonic() > deadline:
            raise WSError("timed out")
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WSError("closed")
        buf += chunk
    return buf


def _ws_recv(sock: Any, deadline: float) -> dict[str, Any]:
    """One text message (fragments joined), at most WS_MAX_FRAME; pings and pongs are skipped; anything else ends it."""
    data = b""
    while True:
        h = _recv_exact(sock, 2, deadline)
        fin, op, masked, n = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x80, h[1] & 0x7F
        if n == 126:
            n = int.from_bytes(_recv_exact(sock, 2, deadline), "big")
        elif n == 127:
            n = int.from_bytes(_recv_exact(sock, 8, deadline), "big")
        if n > WS_MAX_FRAME or len(data) + n > WS_MAX_FRAME:
            raise WSError("frame too big")
        mask = _recv_exact(sock, 4, deadline) if masked else b""
        payload = _recv_exact(sock, n, deadline) if n else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if op in (0x9, 0xA):
            continue
        if op not in (0x0, 0x1):
            raise WSError(f"opcode {op}")
        data += payload
        if fin:
            try:
                out = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise WSError("not JSON") from exc
            if not isinstance(out, dict):
                raise WSError("not an object")
            return out


def _ws_send(sock: Any, obj: dict[str, Any]) -> None:
    payload = json.dumps(obj).encode()
    mask = os.urandom(4)
    n = len(payload)
    head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big"))
    sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def stream_url(entity_id: str, *, connect: Callable[[], Any] | None = None, token: str | None = None) -> str:
    """HA's HLS URL path for a camera (`/api/hls/<token>/master_playlist.m3u8`): one `camera/stream` call, nothing else."""
    import base64
    import hashlib
    import socket
    import home_assistant as ha
    tok = token if token is not None else ha._token()
    if not tok:
        raise PermissionError(ha.NOT_CONNECTED)
    deadline = time.monotonic() + WS_TIMEOUT
    if connect is None:
        host, port = _local_ha()
        connect = lambda: socket.create_connection((host, port), timeout=WS_TIMEOUT)  # noqa: E731
        host_header = f"{host}:{port}"
    else:
        host_header = "127.0.0.1:8123"
    sock = connect()
    try:
        sock.settimeout(WS_TIMEOUT)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET /api/websocket HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            if len(head) > 8192 or time.monotonic() > deadline:
                raise WSError("bad handshake")
            chunk = sock.recv(1024)
            if not chunk:
                raise WSError("closed")
            head += chunk
        raw, rest = head.split(b"\r\n\r\n", 1)
        lines = raw.decode("latin-1").split("\r\n")
        want = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        hdrs = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
        if " 101 " not in lines[0] + " " or hdrs.get("sec-websocket-accept") != want:
            raise WSError("not a websocket")
        ws = _Buffered(sock, rest)
        if _ws_recv(ws, deadline).get("type") != "auth_required":
            raise WSError("unexpected greeting")
        _ws_send(ws, {"type": "auth", "access_token": tok})
        if _ws_recv(ws, deadline).get("type") != "auth_ok":
            raise WSError("auth refused")
        _ws_send(ws, {"id": 1, "type": "camera/stream", "entity_id": entity_id, "format": "hls"})
        while True:
            m = _ws_recv(ws, deadline)
            if m.get("id") == 1 and m.get("type") == "result":
                url = str(((m.get("result") or {}) if m.get("success") else {}).get("url") or "")
                if not url.startswith("/api/hls/") or ".." in url:
                    raise WSError("no stream")
                return url
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass


def _end_stale(now: float) -> None:
    for sid, s in list(_live.items()):
        if s["ended"]:
            continue
        if not live_on():
            s["ended"] = "off"
        elif now - s["started"] > LIVE_MAX_S:
            s["ended"] = "time"
        elif now - s["last"] > LIVE_IDLE_S:
            s["ended"] = "idle"
    for sid in [k for k, s in _live.items() if s["ended"] and now - s["started"] > LIVE_MAX_S + 60]:
        del _live[sid]


def live_start(entity_id: str, device_id: str, client_host: str | None, *, now: float | None = None,
               source_of: Callable[[str], str] | None = None, url_for: Callable[[str], str] | None = None,
               role: str | None = None) -> tuple[int, dict[str, Any]]:
    now = time.time() if now is None else now
    if not live_on():
        return 403, {"error": "off", "said": LIVE_OFF}
    if not entity_id.startswith("camera.") or not entity_id[7:].replace("_", "").isalnum():
        return 404, {"error": "no such camera"}
    if not may_view_from(client_host, role):
        return 403, {"error": OFF_NETWORK}
    try:
        source = (source_of or (lambda e: sources().get(e, "unknown")))(entity_id)
    except Exception:  # noqa: BLE001
        source = "unknown"
    if source != "local":
        return 403, {"error": "Only cameras titan reads on the home network play here.", "source": source}
    with _lock:
        _end_stale(now)
        if sum(1 for s in _live.values() if not s["ended"]) >= LIVE_SLOTS:
            return 429, {"error": "two cameras are live already"}
        sid = secrets.token_hex(16)
        # An adult's session may also be fetched over Tailscale (its segments may come with the session id alone).
        _live[sid] = {"device": device_id, "camera": entity_id, "base": None, "started": now, "last": now, "ended": None,
                      "adult": role in ADULTS}
    try:
        url = (url_for or stream_url)(entity_id)
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _live.pop(sid, None)
        return 503, {"error": f"Home Assistant didn't stream it ({type(exc).__name__})"}
    with _lock:
        _live[sid]["base"] = url.rsplit("/", 1)[0]
    _count_live(entity_id, now)
    return 201, {"session": sid, "playlist": f"/v1/home/live/{sid}/{url.rsplit('/', 1)[1]}", "expires_at": now + LIVE_MAX_S,
                 "max_s": LIVE_MAX_S}


def live_fetch(sid: str, path: str, client_host: str | None, device_id: str | None, *, now: float | None = None,
               get: Callable[[str], bytes] | None = None) -> tuple[int, bytes | dict[str, Any], str]:
    """(status, bytes or an error body, content type). `device_id`: the device a token named, None without one."""
    now = time.time() if now is None else now
    if not _LIVE_PATH.match(path or "") or ".." in path or "//" in path:
        return 400, {"error": "bad path"}, ""
    if not (on_home_network(client_host) or in_tailnet(client_host)):
        return 403, {"error": OFF_NETWORK}, ""
    with _lock:
        _end_stale(now)
        s = _live.get(sid)
        if not s or (device_id is not None and device_id != s["device"]):
            return 404, {"error": "no such session"}, ""
        if not on_home_network(client_host) and not s.get("adult"):
            return 403, {"error": OFF_NETWORK}, ""
        if s["ended"]:
            return 410, {"ended": s["ended"]}, ""
        if s["base"] is None:
            return 404, {"error": "not ready"}, ""
        s["last"] = now
        base = s["base"]
    try:
        data = plain((get or _ha_get)(f"{base}/{path}"))
        if path.endswith(".m3u8"):
            data = plain_hls(relative_playlist(data, base))
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": f"Home Assistant didn't answer ({type(exc).__name__})"}, ""
    kind = {"m3u8": "application/vnd.apple.mpegurl", "mp4": "video/mp4", "m4s": "video/iso.segment"}[path.rsplit(".", 1)[1]]
    return 200, data, kind


def live_end(sid: str, device_id: str | None) -> int:
    with _lock:
        s = _live.get(sid)
        if not s or (device_id is not None and device_id != s["device"]):
            return 404
        s["ended"] = s["ended"] or "closed"
    return 204


def end_device(device_id: str | None) -> int:
    """A revoked device's open live sessions end now (#1246): a revoked TV can't close its own (its token already gets
    401), and its session would otherwise hold one of the LIVE_SLOTS until LIVE_IDLE_S. Another device's stay. The
    number ended."""
    if not device_id:
        return 0
    n = 0
    with _lock:
        for s in _live.values():
            if s["device"] == device_id and not s["ended"]:
                s["ended"] = "revoked"
                n += 1
    return n


def _ha_get(path: str) -> bytes:
    """One playlist or segment from HA, as plain bytes. HA gzips its playlists (Content-Encoding: gzip): titan asks for
    identity, and unzips whatever comes gzipped anyway; a relayed body never carries an encoding the client isn't told
    of (a gzipped playlist sent as text stopped Zach's first live view, 10-04)."""
    import urllib.request
    import home_assistant as ha
    _local_ha()
    tok = ha._token()
    headers = {"Accept-Encoding": "identity"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(ha.HA_URL + path, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        data = r.read(LIVE_MAX_BYTES + 1)
        encoding = r.headers.get("Content-Encoding")
    if len(data) > LIVE_MAX_BYTES:
        raise ValueError("too big")
    return plain(data, encoding)


def plain(data: bytes, encoding: str | None = None) -> bytes:
    """The body unzipped when it's gzip (said, or its 1f 8b magic: an MP4 box can't start so), at most LIVE_MAX_BYTES
    unzipped. Any other encoding is refused rather than passed on."""
    import zlib
    enc = (encoding or "").strip().lower()
    if enc in ("", "identity") and data[:2] != b"\x1f\x8b":
        return data
    if enc not in ("", "identity", "gzip", "x-gzip") or data[:2] != b"\x1f\x8b":
        raise ValueError(f"unexpected encoding {enc or 'unknown'}")
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data, LIVE_MAX_BYTES + 1)
    if len(out) > LIVE_MAX_BYTES or d.unconsumed_tail:
        raise ValueError("too big unzipped")
    return out


# Low-latency HLS (HA's stream serves it): parts, preload hints and blocking reloads need a server that holds requests
# open, which a request-and-response relay isn't. titan serves plain HLS: full segments only, a few seconds behind live
# (Zach's second live view stalled on the parts, 10-04). The `_HLS_*` query of a blocking reload never reaches HA: the
# route reads the path alone, so it's answered at once with the playlist as it is.
_LL_TAGS = ("#EXT-X-PART-INF", "#EXT-X-SERVER-CONTROL", "#EXT-X-PART:", "#EXT-X-PRELOAD-HINT", "#EXT-X-RENDITION-REPORT",
            "#EXT-X-SKIP", "#EXT-X-START")


def plain_hls(data: bytes) -> bytes:
    """A playlist without low-latency HLS: no parts, preload hints, server control, rendition reports, skips or a
    part-based start. #EXTINF with its full segment, #EXT-X-MAP and #EXT-X-PROGRAM-DATE-TIME stay."""
    lines = data.decode("utf-8").splitlines()
    kept = [l for l in lines if not l.strip().startswith(_LL_TAGS)]
    return ("\n".join(kept) + "\n").encode("utf-8")


def relative_playlist(data: bytes, base: str) -> bytes:
    """A playlist whose URIs resolve under titan's /v1/home/live/<session>/: HA's own paths (/api/hls/<token>/…, with or
    without its address) become relative. HA writes them relative already; this keeps it so if it ever doesn't."""
    import re
    text = data.decode("utf-8")
    prefix = re.escape(base.rstrip("/") + "/")
    text = re.sub(r"(?:https?://[^/\s\"]+)?" + prefix, "", text)
    return text.encode("utf-8")


def _count_live(camera: str, now: float) -> None:
    day = local_time.day(now)
    with _lock:
        con = _db()
        with con:
            con.execute("CREATE TABLE IF NOT EXISTS live_counts (day TEXT, camera TEXT, sessions INTEGER,"
                        " PRIMARY KEY (day, camera))")
            con.execute("INSERT INTO live_counts VALUES (?,?,1) ON CONFLICT(day, camera) DO UPDATE SET sessions=sessions+1",
                        (day, camera))
        con.close()
