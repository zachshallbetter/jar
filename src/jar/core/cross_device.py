#!/usr/bin/env python3
"""Cross-device: a turn from a person's iPhone runs Mac tools on that same person's Mac.

The Mac holds a long-poll on titan (GET /v1/device/inbox); a phone turn whose tool is a Mac tool
is queued there and waits for the Mac's result (POST /v1/device/inbox/{id}/result). When no
long-poll is live titan wakes the Mac (Wake-on-LAN magic packets and a TCP connect to the port it
advertises to the Apple sleep proxy) and waits up to WAKE_WAIT_S for it to come back. A Mac result
that needs the user's approval is relayed to the phone as a `confirm` SSE event; the phone answers
on POST /v1/device/confirm/{turn_id} and titan re-queues the request approved.

Runbook › Cross-device has the operator view. Pieces:
  Channel        in-memory queues, live long-polls, pending results (thread-safe: the turn runs in a
                 worker thread, the long-poll in the event loop)
  Waker          WoL + TCP wake; the network calls are injectable (tests never send packets)
  route()        one routed tool call, start to finish; the API's per-turn device hook calls it
  Approvals      "ask once per session" answers from the phone, 30 minutes per (session, domain)
Presence (the Mac's lan_ip, mac_address, wake_port) is stored in HOUSEHOLD_DB by household_identity.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import time
import uuid
from typing import Any, Callable

# Timings (module-level so the tests can shrink them).
LIVE_WINDOW_S = 35.0          # "live": a long-poll in progress, or one that ended less than this ago
WAKE_WAIT_S = 25.0            # after waking, how long to wait for the Mac's long-poll
DELIVERY_GRACE_S = 8.0        # a live-but-between-polls Mac gets this long to pick a request up
RESULT_TIMEOUT_S = 120.0      # delivered → result (the Mac is running the tool)
CONFIRM_TIMEOUT_S = 110.0     # the phone's confirmation sheet; no answer = declined
SESSION_APPROVAL_S = 30 * 60  # "ask once per session" approvals from the phone
MAX_POLL_WAIT_S = 30.0        # the longest long-poll titan holds open

NO_WAKE = "Your Mac didn't wake up — it may be away from home or shut down."
DECLINED = "You declined this action."
NO_ANSWER = "Your Mac didn't answer in time."
STATUSES = ("done", "needs_confirmation", "refused", "failed")

_MAC_RX = re.compile(r"^[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(\1[0-9A-Fa-f]{2}){4}$")
_KEY_RX = re.compile(r"^[A-Za-z0-9._:-]{4,80}$")


# ── presence ─────────────────────────────────────────────────────────────────────────────────

class PresenceError(ValueError):
    pass


def _private_ipv4(value: Any) -> str | None:
    """A home-network IPv4 address, or None. titan only ever sends wake packets to its own LAN."""
    if value in (None, ""):
        return None
    try:
        ip = ipaddress.IPv4Address(str(value).strip())
    except ValueError as exc:
        raise PresenceError("lan_ip must be an IPv4 address") from exc
    if not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise PresenceError("lan_ip must be a private home-network address")
    return str(ip)


def clean_device_key(value: Any) -> str:
    key = str(value or "").strip()
    if not _KEY_RX.fullmatch(key):
        raise PresenceError("device_key must be 4-80 characters: letters, digits, . _ : -")
    return key


_VERSION_RX = re.compile(r"[A-Za-z0-9 ._+()/-]{1,40}")


def clean_version(value: Any) -> str | None:
    """An app's version string ("0.12.9 (412)"), or None: short, plain characters only."""
    v = str(value or "").strip()
    return v if v and _VERSION_RX.fullmatch(v) else None


_TOOL_NAME_RX = re.compile(r"[a-z][a-z0-9_]{1,40}")


def clean_tool_list(value: Any) -> list[str] | None:
    """A device's tool names (sorted, at most 200), or None when it sent no list."""
    if not isinstance(value, list):
        return None
    return sorted({str(v) for v in value if isinstance(v, str) and _TOOL_NAME_RX.fullmatch(v)})[:200]


SYNC_SOURCES = ("mail", "calendar", "downloads", "browsing", "messages", "notes", "contacts", "reminders")
SOURCE_LEVELS = ("granted", "waiting", "needsRequest", "denied", "off")


def clean_sources(value: Any) -> dict[str, str] | None:
    """jellybean#413: each sync source's state on the Mac ({"mail": "waiting", …}): known sources and levels only,
    never a message. None when the Mac sent none (an older one)."""
    if not isinstance(value, dict):
        return None
    out = {str(k): str(v) for k, v in value.items() if str(k) in SYNC_SOURCES and str(v) in SOURCE_LEVELS}
    return dict(sorted(out.items()))


def clean_name(value: Any, default: str = "Mac") -> str:
    return " ".join(str(value or "").split())[:80] or default


def clean_presence(body: Any) -> dict[str, Any]:
    """POST /v1/device/presence's body → the stored fields. Raises PresenceError (→ 400)."""
    if not isinstance(body, dict):
        raise PresenceError("expected a JSON object")
    platform = str(body.get("platform") or "mac").strip().lower()
    if platform != "mac":
        raise PresenceError("platform must be mac")
    mac = body.get("mac_address")
    if mac not in (None, ""):
        mac = str(mac).strip()
        if not _MAC_RX.fullmatch(mac):
            raise PresenceError("mac_address must look like aa:bb:cc:dd:ee:ff")
        mac = mac.replace("-", ":").lower()
    else:
        mac = None
    port = body.get("wake_port")
    if port in (None, ""):
        port = None
    else:
        try:
            port = int(port)
        except (TypeError, ValueError) as exc:
            raise PresenceError("wake_port must be a port number") from exc
        if not 1 <= port <= 65535:
            raise PresenceError("wake_port must be a port number")
    def _num(key: str, lo: float, hi: float) -> float | None:
        v = body.get(key)
        if v in (None, ""):
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None   # optional health fields never fail the heartbeat
        return v if lo <= v <= hi else None
    battery = _num("battery_pct", 0, 100)
    on_ac = body.get("on_ac")
    return {"device_key": clean_device_key(body.get("device_key")), "name": clean_name(body.get("name")),
            "lan_ip": _private_ipv4(body.get("lan_ip")), "mac_address": mac, "wake_port": port,
            "on_tailnet": bool(body.get("on_tailnet")), "awake": body.get("awake", True) is not False,
            # Device health v2 (Jellybean 0.12.6+): optional; an older Mac sends none of them.
            "battery_pct": int(round(battery)) if battery is not None else None,
            "on_ac": None if on_ac is None else bool(on_ac),
            "disk_free_gb": _num("disk_free_gb", 0, 1_000_000),
            # Self-knowledge (10-01): which Jellybean the Mac runs and the tools it handles; an older
            # Mac sends neither (titan then assumes every Mac tool).
            "app_version": clean_version(body.get("app_version")),
            "device_tools": clean_tool_list(body.get("device_tools")),
            "sources": clean_sources(body.get("sources"))}


def wakeable(mac: dict[str, Any]) -> bool:
    return bool(mac.get("mac_address") or (mac.get("lan_ip") and mac.get("wake_port")))


# ── the channel ──────────────────────────────────────────────────────────────────────────────

Key = tuple[str, str]   # (person_id, device_key): a Mac is only ever its own person's


class Channel:
    """Per-Mac request queues, which long-polls are open, and the requests awaiting a result."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._cv = threading.Condition()
        self._queues: dict[Key, list[dict[str, Any]]] = {}
        self._polls: dict[Key, list[float]] = {}        # start times of the open long-polls
        self._last_end: dict[Key, float] = {}
        # A Mac that said it's going to sleep (presence awake: false): its open long-polls are dead
        # connections titan can't see close, so polls started before this don't count or take.
        self._asleep_at: dict[Key, float] = {}
        self._pending: dict[str, dict[str, Any]] = {}   # request id → {key, item, state, result}

    # long-polls
    def _fresh(self, key: Key, start: float) -> bool:
        return start > self._asleep_at.get(key, float("-inf"))

    def poll_started(self, key: Key) -> float:
        with self._cv:
            start = self._now()
            self._polls.setdefault(key, []).append(start)
            self._cv.notify_all()
            return start

    def poll_ended(self, key: Key, start: float | None = None) -> None:
        with self._cv:
            polls = self._polls.get(key, [])
            if polls:
                polls.remove(start if start in polls else polls[0])
            if start is None or self._fresh(key, start):
                self._last_end[key] = self._now()
            self._cv.notify_all()

    def mark_asleep(self, key: Key) -> None:
        with self._cv:
            self._asleep_at[key] = self._now()
            self._last_end.pop(key, None)
            self._cv.notify_all()

    def polling(self, key: Key) -> bool:
        with self._cv:
            return any(self._fresh(key, t) for t in self._polls.get(key, []))

    def live(self, key: Key) -> bool:
        with self._cv:
            if any(self._fresh(key, t) for t in self._polls.get(key, [])):
                return True
            end = self._last_end.get(key)
            return end is not None and self._now() - end < LIVE_WINDOW_S

    # requests
    def enqueue(self, key: Key, item: dict[str, Any]) -> str:
        """Queue a request for the Mac (item carries id; a re-queue reuses the id)."""
        with self._cv:
            self._pending[item["id"]] = {"key": key, "item": item, "state": "queued", "result": None}
            self._queues.setdefault(key, []).append(item)
            self._cv.notify_all()
        return item["id"]

    def take(self, key: Key, since: float | None = None) -> list[dict[str, Any]]:
        """Everything queued for this Mac (the long-poll's reply); marks them delivered. A long-poll
        that started before the Mac said it was going to sleep takes nothing."""
        with self._cv:
            if since is not None and not self._fresh(key, since):
                return []
            items = self._queues.pop(key, [])
            for it in items:
                p = self._pending.get(it["id"])
                if p is not None:
                    p["state"] = "delivered"
            if items:
                self._cv.notify_all()
            return items

    def withdraw(self, rid: str) -> bool:
        """Take back a request no Mac picked up. False: it was delivered meanwhile."""
        with self._cv:
            p = self._pending.get(rid)
            if p is None:
                return True
            if p["state"] != "queued":
                return False
            q = self._queues.get(p["key"], [])
            self._queues[p["key"]] = [it for it in q if it["id"] != rid]
            self._pending.pop(rid, None)
            return True

    def forget(self, rid: str) -> None:
        with self._cv:
            p = self._pending.pop(rid, None)
            if p is not None and p["state"] == "queued":
                self._queues[p["key"]] = [it for it in self._queues.get(p["key"], []) if it["id"] != rid]

    def post_result(self, person_id: str, device_key: str | None, rid: str, result: dict[str, Any]) -> str:
        """The Mac's result. "ok", or why not: "unknown" (no such request for this person's Mac, or
        for this device_key when one is given) or "early" (it was never delivered)."""
        with self._cv:
            p = self._pending.get(rid)
            if p is None or p["key"][0] != person_id or (device_key and p["key"][1] != device_key):
                return "unknown"
            if p["state"] == "queued":
                return "early"
            p["state"], p["result"] = "answered", result
            self._cv.notify_all()
            return "ok"

    def _wait(self, pred: Callable[[], bool], timeout: float, cancelled: Callable[[], bool] | None) -> bool:
        end = time.monotonic() + max(0.0, timeout)
        with self._cv:
            while not pred():
                left = end - time.monotonic()
                if left <= 0 or (cancelled is not None and cancelled()):
                    return pred()
                self._cv.wait(min(left, 0.25))
            return True

    def wait_delivered(self, rid: str, timeout: float, cancelled: Callable[[], bool] | None = None) -> bool:
        return self._wait(lambda: (self._pending.get(rid) or {}).get("state") in ("delivered", "answered"),
                          timeout, cancelled)

    def wait_result(self, rid: str, timeout: float, cancelled: Callable[[], bool] | None = None) -> dict | None:
        self._wait(lambda: (self._pending.get(rid) or {}).get("state") == "answered", timeout, cancelled)
        with self._cv:
            p = self._pending.get(rid)
            return p["result"] if p is not None and p["state"] == "answered" else None

    async def long_poll(self, key: Key, wait: float) -> list[dict[str, Any]]:
        """GET /v1/device/inbox: return at once when something is queued, else after `wait` s."""
        import asyncio
        wait = max(0.0, min(float(wait), MAX_POLL_WAIT_S))
        start = self.poll_started(key)
        try:
            end = time.monotonic() + wait
            while True:
                items = self.take(key, since=start)
                if items or time.monotonic() >= end or SHUTTING_DOWN.is_set():
                    return items
                await asyncio.sleep(0.1)
        finally:
            self.poll_ended(key, start)


# Set when the API is stopping (agent_api_server's SIGTERM hook): long-polls end at once, so a
# restart doesn't leave them to be cut off as 500s after the graceful-shutdown timeout.
SHUTTING_DOWN = threading.Event()


# ── waking ───────────────────────────────────────────────────────────────────────────────────

def magic_packet(mac_address: str) -> bytes:
    raw = bytes.fromhex(mac_address.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError("a MAC address is 6 bytes")
    return b"\xff" * 6 + raw * 16


def _send_udp(packet: bytes, host: str, port: int) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (host, port))
    finally:
        s.close()


def _tcp_touch(host: str, port: int) -> None:
    """A SYN to the Mac's advertised wake port: the sleep proxy wakes the Mac for it."""
    try:
        socket.create_connection((host, port), timeout=3).close()
    except OSError:
        pass   # expected while it's asleep: the SYN is what wakes it


class Waker:
    """Wake-on-LAN (UDP 9 to the LAN broadcast and to the Mac's last address) plus a TCP connect to
    lan_ip:wake_port. send_udp / tcp_touch are injectable; tests pass fakes."""

    def __init__(self, send_udp: Callable[[bytes, str, int], None] | None = None,
                 tcp_touch: Callable[[str, int], None] | None = None, background_tcp: bool = True) -> None:
        self.send_udp = send_udp or _send_udp
        self.tcp_touch = tcp_touch or _tcp_touch
        self.background_tcp = background_tcp

    @staticmethod
    def broadcast() -> str:
        return os.environ.get("TITAN_WOL_BROADCAST", "10.0.0.255").strip() or "10.0.0.255"

    def wake(self, mac: dict[str, Any]) -> list[str]:
        """Send what we can; returns what was sent (for logs and tests). Never raises."""
        if os.environ.get("AGENT_TURN_SOURCE") == "test" and (self.send_udp is _send_udp or self.tcp_touch is _tcp_touch):
            return []   # a test process never sends real packets, even if a test forgot its fakes
        sent: list[str] = []
        ip = mac.get("lan_ip")
        if mac.get("mac_address"):
            try:
                pkt = magic_packet(mac["mac_address"])
            except ValueError:
                pkt = None
            for host in dict.fromkeys([self.broadcast()] + ([ip] if ip else [])):
                if pkt is None:
                    break
                try:
                    self.send_udp(pkt, host, 9)
                    sent.append(f"wol {host}:9")
                except OSError:
                    pass
        if ip and mac.get("wake_port"):
            port = int(mac["wake_port"])
            if self.background_tcp:
                threading.Thread(target=self.tcp_touch, args=(ip, port), daemon=True).start()
            else:
                self.tcp_touch(ip, port)
            sent.append(f"tcp {ip}:{port}")
        return sent


# ── session approvals ("ask once per session" from the phone) ────────────────────────────────

class Approvals:
    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._until: dict[tuple[str, str], float] = {}

    def grant(self, session: str, domain: str) -> None:
        with self._lock:
            now = self._now()
            # a granted() never asked again kept its entry forever: the expired ones go with each new grant
            for k in [k for k, until in self._until.items() if now >= until]:
                del self._until[k]
            self._until[(session, domain)] = now + SESSION_APPROVAL_S

    def granted(self, session: str, domain: str) -> bool:
        with self._lock:
            until = self._until.get((session, domain))
            if until is None:
                return False
            if self._now() >= until:
                self._until.pop((session, domain), None)
                return False
            return True


CHANNEL = Channel()
WAKER = Waker()
APPROVALS = Approvals()


def reset(channel: Channel | None = None, waker: Waker | None = None, approvals: Approvals | None = None) -> None:
    """Tests: swap the process-wide channel, waker and approvals."""
    global CHANNEL, WAKER, APPROVALS
    CHANNEL, WAKER, APPROVALS = channel or Channel(), waker or Waker(), approvals or Approvals()


# ── which Mac, and routing one call ──────────────────────────────────────────────────────────

def mac_for(person_id: str, store: Any = None) -> dict[str, Any] | None:
    """The person's Mac a phone turn can use: a live one first, else the most recently heard-from
    one that can be woken. None: they have no Mac titan knows how to reach."""
    if store is None:
        import household_identity as hh
        store = hh.get_store()
    try:
        macs = store.macs_for(person_id)
    except Exception:  # noqa: BLE001 - no store, no routing
        return None
    for m in macs:
        if CHANNEL.live((person_id, m["device_key"])):
            return m
    return next((m for m in macs if wakeable(m)), None)


# What a device request carries to the Mac besides the call itself: a grant approved ahead (#448: the
# Mac checks its own record; 10-01 it was dropped here, so a scheduled action always asked) and the
# turn's nonce for marking others' text (#473).
CARRIED = ("grant_id", "untrusted_nonce")


def inbox_item(req: dict[str, Any], turn_id: str, origin: dict[str, str]) -> dict[str, Any]:
    """The Mac's inbox item for one device request."""
    item = {"id": uuid.uuid4().hex[:16], "turn_id": turn_id, "tool": req.get("tool"), "args": req.get("args") or {},
            "risk": req.get("risk") or "dangerous", "origin": origin, "approved": False}
    item.update({k: req[k] for k in CARRIED if req.get(k)})
    return item


EXECUTOR_RESULT_S = 20.0      # a paired executor's typed read (#1631): it's live or it isn't, and it answers quickly


def executor_call(person_id: str, executor: dict[str, Any], tool: str, *, turn_id: str, nonce: str | None = None,
                  wait_s: float = EXECUTOR_RESULT_S, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """One typed read on a person's own paired executor (#1631 slice 4): only while it's live (an executor isn't woken),
    queued on its inbox, its result within wait_s. → {"result": text} or {"error": text}. Never a Mac: the caller passes
    one of macs_for(person, kind="executor")."""
    key = (person_id, executor["device_key"])
    # never the machine's own name here: it chose it, so it's untrusted text (the caller wraps it with the answer)
    if not CHANNEL.live(key):
        return {"error": "That machine isn't connected to titan right now."}
    req = {"tool": tool, "args": dict(args or {}), "risk": "read"}
    if nonce:
        req["untrusted_nonce"] = nonce
    item = inbox_item(req, turn_id, {"kind": "titan"})
    CHANNEL.enqueue(key, item)
    try:   # every way out forgets the request, so no result (or the machine's text) stays in the channel
        if not CHANNEL.wait_delivered(item["id"], DELIVERY_GRACE_S) and CHANNEL.withdraw(item["id"]):
            return {"error": "That machine didn't pick the request up."}
        res = CHANNEL.wait_result(item["id"], wait_s)
        if res is None:
            return {"error": "That machine didn't answer in time."}
        if res.get("status") != "done":
            return {"error": f"That machine couldn't do that ({str(res.get('status'))[:20]}).",
                    "status": str(res.get("status"))[:20]}
        return {"result": str(res.get("result") or "")[:4000]}
    finally:
        CHANNEL.forget(item["id"])


PHONE_PLATFORMS = ("ios", "ipados")
PHONE_WAKE_WAIT_S = 30.0      # an iPhone woken by a background push; iOS may hold or coalesce those (#776)


def _push_wake(dev: dict[str, Any]) -> list[str]:
    """Never raises: a phone that can't be pushed is a phone that doesn't wake. A test process never sends one."""
    if os.environ.get("AGENT_TURN_SOURCE") == "test":
        return []
    try:
        import push
        return push.nudge_inbox(str(dev.get("device_id") or dev.get("device_key") or ""))
    except Exception:  # noqa: BLE001
        return []


PUSH_WAKE: Callable[[dict[str, Any]], list[str]] = _push_wake


def route(req: dict[str, Any], *, person_id: str, mac: dict[str, Any], turn_id: str, origin: dict[str, str],
          session: str, emit: Callable[[str, dict], None],
          ask_confirm: Callable[[dict[str, Any]], dict[str, Any]],
          cancelled: Callable[[], bool] | None = None, approved: bool = False) -> dict[str, Any]:
    """Run one device tool on the person's Mac for a phone turn. Returns the device hook's shape:
    {"result": text} or {"error": text}; "completed": False marks a sentence that isn't the action
    having happened (declined, refused, the Mac didn't wake) so it stays off the timeline.

    emit(event, data) puts `remote` / `confirm` events on the phone's stream; ask_confirm(payload)
    shows the confirmation on the phone and returns {"approved", "remember_session"} (blocking,
    CONFIRM_TIMEOUT_S at most)."""
    ch, key = CHANNEL, (person_id, mac["device_key"])
    device = mac.get("name") or "your Mac"
    item = inbox_item(req, turn_id, origin)
    if approved:   # approved before it was queued (#776: the owner's yes for someone else's read comes first)
        item["approved"] = True
    domain_guess = str(req.get("tool") or "")

    def deliver() -> bool:
        """Queue the item; a live Mac picks it up at once (or within DELIVERY_GRACE_S, between two
        polls). Otherwise wake it and wait WAKE_WAIT_S for its long-poll."""
        ch.enqueue(key, item)
        if ch.live(key) and ch.wait_delivered(item["id"], DELIVERY_GRACE_S, cancelled):
            return True
        emit("remote", {"step": "waking", "device": device})
        phone = str(mac.get("platform") or "") in PHONE_PLATFORMS
        if phone:
            PUSH_WAKE(mac)       # an iPhone drains its inbox on a content-available push (#776)
        else:
            WAKER.wake(mac)
        if ch.wait_delivered(item["id"], PHONE_WAKE_WAIT_S if phone else WAKE_WAIT_S, cancelled):
            return True
        return not ch.withdraw(item["id"])   # withdrawn: nobody came (False: it went just now)

    try:
        while True:
            if not deliver():
                emit("remote", {"step": "done", "device": device})
                return {"result": NO_WAKE, "completed": False}
            emit("remote", {"step": "working", "device": device})
            res = ch.wait_result(item["id"], RESULT_TIMEOUT_S, cancelled)
            if res is None:
                emit("remote", {"step": "done", "device": device})
                return {"error": NO_ANSWER}
            status, text = res.get("status"), str(res.get("result") or "")
            if status == "needs_confirmation" and not item["approved"]:
                conf = res.get("confirm") if isinstance(res.get("confirm"), dict) else {}
                domain = str(conf.get("domain") or domain_guess)
                # The Mac marks confirmations that must be asked every time (a second delete in one
                # request, a file that may hold secrets, an event with other people on it) with
                # tier "askAlways": a "don't ask again this session" yes never covers those.
                always_ask = str(conf.get("tier") or "") == "askAlways"
                if not always_ask and APPROVALS.granted(session, domain):
                    item = {**item, "approved": True}
                    continue
                payload = {"confirm_id": uuid.uuid4().hex[:16], "device": device,
                           "summary": str(conf.get("summary") or ""), "detail": str(conf.get("detail") or ""),
                           "risk": str(conf.get("risk") or item["risk"]), "domain": domain}
                if conf.get("invitees"):
                    payload["invitees"] = conf["invitees"]
                if always_ask:
                    payload["tier"] = "askAlways"   # the phone hides "don't ask again this session"
                answer = ask_confirm(payload) or {}
                if answer.get("approved") is not True:
                    emit("remote", {"step": "done", "device": device})
                    return {"result": DECLINED, "completed": False}
                if answer.get("remember_session") is True and not always_ask:
                    APPROVALS.grant(session, domain)
                item = {**item, "approved": True}
                continue
            emit("remote", {"step": "done", "device": device})
            if status == "done":
                return {"result": text or "Done."}
            if status == "refused" or status == "needs_confirmation":
                # (needs_confirmation after approval: the Mac still won't; treat it as its refusal)
                return {"result": text or "Your Mac refused that action.", "completed": False}
            return {"error": text or "It failed on your Mac."}
    finally:
        ch.forget(item["id"])
