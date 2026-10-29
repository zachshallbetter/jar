#!/usr/bin/env python3
"""Cross-device: a phone turn runs Mac tools on the same person's Mac (cross_device.py and its
routes and device hook in agent_api_server.py, the "(on your Mac)" tools in agent_runtime_lib).

Hermetic: every store is a temp file, the model is a fake that calls the tools, the Mac is a thread
long-polling through the real API, and the waker's network calls are fakes (no packet is sent).
Run: python3 scripts/test_cross_device.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-xdev-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "test"
os.environ["TITAN_PAIR_URLS"] = "http://10.0.0.170:7770"
os.environ.pop("TITAN_DISCOVERY", None)
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("TITAN_WOL_BROADCAST", None)
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import cross_device as xd  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.260} expected={expected!r:.260}")
    return good


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ── presence, magic packets, the waker, approvals, the live window ───────────────────────────
print("── presence and waking (no network) ──")
good = {"device_key": "mac-7f3a9c", "name": "Zach's Mac", "platform": "mac", "lan_ip": "10.0.0.42",
        "mac_address": "A4-83-E7-12-34-56", "wake_port": 7771, "on_tailnet": True, "awake": True}
p = xd.clean_presence(good)
check("a good presence is kept, the MAC address normalised",
      (p["device_key"], p["name"], p["lan_ip"], p["mac_address"], p["wake_port"], p["on_tailnet"]),
      ("mac-7f3a9c", "Zach's Mac", "10.0.0.42", "a4:83:e7:12:34:56", 7771, True))
for label, bad in (("a public lan_ip", {"lan_ip": "8.8.8.8"}), ("loopback", {"lan_ip": "127.0.0.1"}),
                   ("not an IP", {"lan_ip": "titan.local"}), ("a bad MAC", {"mac_address": "a4:83:e7:12:34"}),
                   ("a bad port", {"wake_port": 70000}), ("not a Mac", {"platform": "ios"}),
                   ("no device_key", {"device_key": ""}), ("a device_key with slashes", {"device_key": "../../x"})):
    try:
        xd.clean_presence({**good, **bad})
        check(f"presence refuses {label}", "accepted", "PresenceError")
    except xd.PresenceError:
        check(f"presence refuses {label}", "PresenceError", "PresenceError")
pkt = xd.magic_packet("a4:83:e7:12:34:56")
check("magic packet: 6×FF then the MAC 16 times", (len(pkt), pkt[:6], pkt[6:12], pkt[-6:]),
      (102, b"\xff" * 6, bytes.fromhex("a483e7123456"), bytes.fromhex("a483e7123456")))

sent: list = []
fake_waker = xd.Waker(send_udp=lambda pk, h, port: sent.append(("udp", h, port, len(pk))),
                      tcp_touch=lambda h, port: sent.append(("tcp", h, port)), background_tcp=False)
check("wake: WoL to the LAN broadcast and the last address, then a TCP connect",
      (fake_waker.wake(p), sent),
      (["wol 10.0.0.255:9", "wol 10.0.0.42:9", "tcp 10.0.0.42:7771"],
       [("udp", "10.0.0.255", 9, 102), ("udp", "10.0.0.42", 9, 102), ("tcp", "10.0.0.42", 7771)]))
sent.clear()
check("wake without a MAC address: the TCP connect only", fake_waker.wake({**p, "mac_address": None}),
      ["tcp 10.0.0.42:7771"])
check("wakeable needs a MAC address, or an address and a wake port",
      (xd.wakeable(p), xd.wakeable({"lan_ip": "10.0.0.42"}), xd.wakeable({"mac_address": None})), (True, False, False))
check("a test process never sends real packets, even without fakes", xd.Waker().wake(p), [])

ac = Clock()
ap = xd.Approvals(now=ac)
ap.grant("device:d_1", "shell")
check("a session approval covers that session and domain only",
      (ap.granted("device:d_1", "shell"), ap.granted("device:d_1", "filesystem"), ap.granted("device:d_2", "shell")),
      (True, False, False))
ac.t += 29 * 60
check("… for 30 minutes", ap.granted("device:d_1", "shell"), True)
ac.t += 61
check("… then it's gone", ap.granted("device:d_1", "shell"), False)

cc = Clock()
ch = xd.Channel(now=cc)
k = ("zach", "mac-1")
check("never polled: not live", ch.live(k), False)
ch.poll_started(k)
check("a long-poll in progress: live", ch.live(k), True)
ch.poll_ended(k)
cc.t += 34
check("ended 34 s ago: live", ch.live(k), True)
cc.t += 2
check("ended 36 s ago: not live", ch.live(k), False)
check("another person's key is its own channel", ch.live(("robin", "mac-1")), False)
st = ch.poll_started(k)
ch.enqueue(k, {"id": "r1"})
cc.t += 1
ch.mark_asleep(k)
check("the Mac said it's going to sleep: its open long-poll no longer counts or takes",
      (ch.live(k), ch.polling(k), ch.take(k, since=st)), (False, False, []))
ch.poll_ended(k, st)
check("… nor does that poll ending make it live", ch.live(k), False)
cc.t += 1
st2 = ch.poll_started(k)
check("a new long-poll after waking takes the request", ([i["id"] for i in ch.take(k, since=st2)], ch.live(k)),
      (["r1"], True))
ch.poll_ended(k, st2)

# ── the API: people, devices, presence ───────────────────────────────────────────────────────
print("\n── API: presence and the Mac's channel ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402

MASTER = "master-test-token-cross-device-01"
api._AGENT_TOKEN = MASTER
store = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(store)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def tok(t):
    return {"X-Agent-Token": t}


def pair(person, device_name, platform="ios"):
    body = {"person_id": person} if isinstance(person, str) else {"new_person": person}
    code = c.post("/v1/pair/start", json=body, headers=M).json()
    r = c.post("/v1/pair/claim", json={"code": code["code"], "device_name": device_name, "platform": platform}).json()
    return r["token"], r["person"]["id"], r["device"]["id"]


zach_phone, _, zach_phone_id = pair("zach", "Zach's iPhone")
robin_phone, robin_id, _ = pair({"name": "Robin", "role": "adult"}, "Robin's iPhone")
emma_phone, emma_id, _ = pair({"name": "Emma", "role": "kid"}, "Emma's iPhone")
emma_mac, _, _ = pair(emma_id, "Emma's Mac", "mac")
robin_mac, _, robin_mac_id = pair(robin_id, "Robin's old Mac", "mac")
kitchen, _, _ = pair({"name": "Kitchen", "role": "household"}, "Kitchen iPad", "ipados")

ZKEY = "mac-zach-0001"
r = c.post("/v1/device/presence", json={**good, "device_key": ZKEY}, headers=M)
check("the Mac posts its presence (master token = Zach)", (r.status_code, r.json()), (200, {"ok": True, "live": False}))
check("a bad presence → 400", c.post("/v1/device/presence", json={**good, "lan_ip": "1.1.1.1"}, headers=M).status_code, 400)
r = c.get("/v1/device/presence", headers=M).json()["macs"]
check("Zach's Macs: his, wakeable, not live",
      [(m["device_key"], m["name"], m["wakeable"], m["live"]) for m in r], [(ZKEY, "Zach's Mac", True, False)])
r = c.post("/v1/device/presence", json={**good, "device_key": ZKEY, "name": "Not Zach's"}, headers=tok(robin_mac))
check("Robin posting Zach's device_key makes a Mac of hers, not a change to his",
      (r.status_code, [m["name"] for m in store.macs_for("zach")], [m["name"] for m in store.macs_for(robin_id)]),
      (200, ["Zach's Mac"], ["Not Zach's"]))
c.post("/v1/device/presence", json={**good, "device_key": "mac-emma-0001", "name": "Emma's Mac", "lan_ip": "10.0.0.43"},
       headers=tok(emma_mac))
check("a shared household device has no Mac channel",
      (c.get("/v1/device/inbox?wait=0&device_key=mac-kitchen", headers=tok(kitchen)).status_code,
       c.post("/v1/device/presence", json=good, headers=tok(kitchen)).status_code), (403, 403))
check("the inbox needs a device_key", c.get("/v1/device/inbox?wait=0", headers=M).status_code, 400)
t0 = time.monotonic()
r = c.get(f"/v1/device/inbox?wait=0.3&device_key={ZKEY}", headers=M)
check("an empty long-poll returns {requests: []} after `wait`",
      (r.status_code, r.json(), time.monotonic() - t0 >= 0.3), (200, {"requests": []}, True))
check("… and the Mac is now live", xd.CHANNEL.live(("zach", ZKEY)), True)
store.revoke_device(robin_mac_id)
check("a revoked device's Mac is forgotten", store.macs_for(robin_id), [])
r = c.post("/v1/device/presence", json={**good, "device_key": ZKEY, "awake": False}, headers=M)
check("presence awake: false → not live any more", (r.json()["live"], xd.CHANNEL.live(("zach", ZKEY))), (False, False))

# ── routing a phone turn ─────────────────────────────────────────────────────────────────────
print("\n── routing: tools advertised on phone turns ──")
ident_zp, ident_mp, ident_ep = (hh.resolve(t, MASTER) for t in (zach_phone, robin_phone, emma_phone))
rz = api._remote_for_turn(ident_zp, "iphone")
check("Zach's phone: his Mac, the Mac tools, from his iPhone",
      (rz["mac"]["device_key"], {"run_shell", "read_file", "search_notes", "send_message"} <= rz["tools"],
       rz["tools"] & lib.PHONE_DEVICE_TOOLS_LEGACY, rz["from"], rz["platform"]),
      (ZKEY, True, frozenset(), "Zach's iPhone", "ios"))
rz_now = api._remote_for_turn(ident_zp, "iphone", lib.PHONE_DEVICE_TOOLS)
check("a current phone (it reports its tools): the Mac keeps what the phone lacks, and fetch_image for Mac paths",
      (rz_now["tools"] & lib.PHONE_DEVICE_TOOLS, "send_message" in rz_now["tools"], "update_calendar_event" in rz_now["tools"],
       {"run_shell", "read_file", "search_notes"} <= rz_now["tools"]),
      (frozenset({"fetch_image"}), False, False, True))
check("an older phone (reports nothing): edits still go to its Mac",
      {"update_calendar_event", "delete_calendar_event", "send_message", "create_contact"} <= rz["tools"], True)
check("Robin's phone: she has no Mac, so nothing routes", api._remote_for_turn(ident_mp, "iphone"), None)
check("the Mac itself (no client): nothing routes", api._remote_for_turn(hh.owner_identity(), None), None)
re_ = api._remote_for_turn(ident_ep, "iphone")
check("Emma (kid) with her own Mac: only what her role allows there",
      (re_["mac"]["name"], "run_shell" in re_["tools"], "read_file" in re_["tools"], "set_alarm" in re_["tools"],
       "get_plex_state" in re_["tools"]), ("Emma's Mac", False, False, True, False))   # no Plex tool at all (#1625)

seen: dict = {}
SCRIPT: list = []
orig_turn, orig_exec, orig_save = api._lib._run_agent_turn, lib.execute_tool, lib._maybe_save_fact
logged: list = []


def fake_turn(messages, *a, **k):
    seen["system"] = messages[0]["content"]
    seen["decls"] = {d["name"] for d in lib._gemini_function_declarations()}
    seen["remote"] = getattr(lib._TURN_IO, "remote", None)
    outs = []
    for name, args in SCRIPT:
        lib._emit_status(name)
        lib._TURN_IO.deadline = d0 = time.monotonic() + 60
        t0 = time.monotonic()
        outs.append(lib.execute_tool(name, args))
        seen.setdefault("extended", []).append((lib._TURN_IO.deadline - d0, time.monotonic() - t0))
        lib._TURN_IO.deadline = None
    return "\n".join(outs) or "ok"


def exec_wrapper(name, args):
    if name == "log_event":
        logged.append(args.get("event"))
        return "logged"
    return orig_exec(name, args)


api._lib._run_agent_turn = fake_turn
lib.execute_tool = exec_wrapper
lib._maybe_save_fact = lambda q, t, **k: None


def new_turn(ident, client="iphone", source=None, session=None):
    tid = f"t-{time.monotonic_ns()}"
    api._init_turn(tid)
    api._turns[tid].update({"identity": ident, "client": client, "remember": False, "source": source, "session": session})
    return tid


def run_turn(tid, script, question="do it on my mac"):
    SCRIPT[:] = script
    seen.clear()
    return api._run_turn(tid, question, [], True, None, device=True)


tid = new_turn(ident_zp)
run_turn(tid, [])
sys_z = seen["system"]
check("Zach's phone turn: Mac tools advertised, marked (on your Mac); phone tools not marked",
      ("**run_shell**: (on your Mac)" in sys_z, "**search_notes**: (on your Mac)" in sys_z,
       "**create_reminder**: (on your Mac)" in sys_z, "**create_reminder**" in sys_z,
       "- On your Mac, from the iPhone:" in sys_z, "- Inspecting the Mac:" in sys_z,
       "there is no Mac bridge" in sys_z),
      (True, True, False, True, True, True, False))
check("… and Gemini is offered them", {"run_shell", "read_file", "create_reminder"} <= seen["decls"], True)
check("… fetch_image stays internal (not advertised)", "**fetch_image**" in sys_z, False)
tid = new_turn(ident_mp)
run_turn(tid, [])
check("Robin's phone turn (no Mac): the phone's tools only, as before",
      ("**run_shell**" in seen["system"], "there is no Mac bridge" in seen["system"], "run_shell" in seen["decls"],
       seen["remote"]), (False, True, False, None))
tid = new_turn(ident_ep)
run_turn(tid, [])
check("Emma's phone turn: her Mac's allowed tools, never the shell",
      ("**set_alarm**: (on your Mac)" in seen["system"], "**run_shell**" in seen["system"], "run_shell" in seen["decls"]),
      (True, False, False))
check("the turn clears remote afterwards", getattr(lib._TURN_IO, "remote", None), None)


class FakeMac:
    """A Mac long-polling through the real API: answer(req) → the result body to post."""

    def __init__(self, token, key, answer, person="zach"):
        self.token, self.key, self.answer, self.person = token, key, answer, person
        self.got: list = []
        self.stop = threading.Event()
        self.cl = TestClient(api.app)
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            r = self.cl.get(f"/v1/device/inbox?wait=0.5&device_key={self.key}", headers=tok(self.token))
            for req in r.json().get("requests", []):
                self.got.append(req)
                body = self.answer(req)
                if body is not None:
                    self.cl.post(f"/v1/device/inbox/{req['id']}/result", json=body, headers=tok(self.token))

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 5
        while not xd.CHANNEL.polling((self.person, self.key)) and time.monotonic() < deadline:
            time.sleep(0.02)
        return self

    def close(self):
        self.stop.set()
        self.thread.join(3)


waker_calls: list = []


def fresh(wake_wait=3.0, confirm=3.0, result=3.0, on_wake=None):
    xd.reset(waker=xd.Waker(send_udp=lambda pk, h, port: waker_calls.append(("udp", h, port)),
                            tcp_touch=lambda h, port: (waker_calls.append(("tcp", h, port)), on_wake and on_wake()),
                            background_tcp=False))
    waker_calls.clear()
    xd.WAKE_WAIT_S, xd.CONFIRM_TIMEOUT_S, xd.RESULT_TIMEOUT_S, xd.DELIVERY_GRACE_S = wake_wait, confirm, result, 1.0


def events(tid):
    return [(e, d.get("step")) for e, d in api._turns[tid].get("events", [])]


print("\n── routing: the long-poll delivers, results return ──")
fresh()
mac = FakeMac(MASTER, ZKEY, lambda req: {"status": "done", "result": "hello from the Mac"}).start()
try:
    tid = new_turn(ident_zp)
    out = run_turn(tid, [("read_file", {"path": "~/notes.txt"})])
    req = mac.got[0] if mac.got else {}
    import untrusted as _ut
    nonce = req.get("untrusted_nonce") or ""
    check("a phone turn's read_file runs on Zach's Mac and the result comes back, marked as file text (#473)",
          (_ut.strip(out), _ut.spans(out, nonce)), ("hello from the Mac", 1))
    check("the Mac got the contract's request",
          (sorted(req), req.get("turn_id"), req.get("tool"), req.get("args"), req.get("risk"), req.get("origin"),
           req.get("approved"), len(nonce)),
          (["approved", "args", "id", "origin", "risk", "tool", "turn_id", "untrusted_nonce"], tid, "read_file",
           {"path": "~/notes.txt"}, "read", {"device": "Zach's iPhone", "platform": "ios"}, False, 8))
    check("no wake for a live Mac; working then done on the phone's stream",
          (waker_calls, events(tid)), ([], [("remote", "working"), ("remote", "done")]))
    check("the step label says it's on the Mac", api._turns[tid].get("status"), ["On your Mac: Reading the file…"])
    tid = new_turn(ident_zp)
    n = len(mac.got)

    def phone_answers():
        cl = TestClient(api.app)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            req = api._turns.get(tid, {}).get("_device_req")
            if req:
                cl.post(f"/v1/device/result/{tid}", headers=tok(zach_phone),
                        json={"request_id": req["request_id"], "result": "Added reminder 'Milk'."})
                return
            time.sleep(0.02)
    th = threading.Thread(target=phone_answers, daemon=True)
    th.start()
    out = run_turn(tid, [("create_reminder", {"title": "Milk"})])
    th.join(3)
    check("a phone tool still runs on the phone (a `device` event), never on the Mac",
          (out, len(mac.got) - n), ("Added reminder 'Milk'.", 0))

    def last_times():
        info = getattr(lib._TURN_INFO, "info", None) or {}   # what _log_turn writes from
        return info.get("device_times"), info.get("asked_ms")

    check("an app that doesn't send asked_ms: device_times stays [name, ms]",
          (len(last_times()[0][-1]), last_times()[1]), (2, None))
    for body, want in (({"asked_ms": 43000}, 43000), ({"asked_ms": "soon"}, None), ({"asked_ms": True}, None),
                       ({"asked_ms": 10 ** 9}, 120000)):
        tid = new_turn(ident_zp)

        def phone_asked():
            cl = TestClient(api.app)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                req = api._turns.get(tid, {}).get("_device_req")
                if req:
                    cl.post(f"/v1/device/result/{tid}", headers=tok(zach_phone),
                            json={"request_id": req["request_id"], "result": "Added reminder 'Milk'.", **body})
                    return
                time.sleep(0.02)
        th = threading.Thread(target=phone_asked, daemon=True)
        th.start()
        run_turn(tid, [("create_reminder", {"title": "Milk"})])
        th.join(3)
        dt, asked = last_times()
        check(f"asked_ms {body['asked_ms']!r} (#619): logged on the call and the turn as {want!r}",
              (dt[-1][2] if len(dt[-1]) > 2 else None, asked), (want, want))
    tid = new_turn(ident_mp)
    n = len(mac.got)
    out = run_turn(tid, [("run_shell", {"command": "ls"})])
    check("Robin's phone can't reach Zach's Mac", (out, len(mac.got)),
          ("The 'run_shell' device action isn't available on the iPhone.", n))
    tid = new_turn(ident_ep)
    out = run_turn(tid, [("run_shell", {"command": "ls"})])
    check("Emma's phone: the role policy refuses the shell before anything is routed", (out, len(mac.got)),
          (lib._ROLE_REFUSAL, n))

    def phone_answer(turn, result):
        """The phone's side: answer the turn's next device request with `result`; the tool it got."""
        got: list = []
        def run():
            cl = TestClient(api.app)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                req = api._turns.get(turn, {}).get("_device_req")
                if req:
                    got.append((req.get("tool"), req.get("args")))
                    cl.post(f"/v1/device/result/{turn}", headers=tok(zach_phone),
                            json={"request_id": req["request_id"], "result": result})
                    return
                time.sleep(0.02)
        th = threading.Thread(target=run, daemon=True)
        th.start()
        return th, got

    # 09-30: the phone couldn't edit, so "update the event with the school's location" made a duplicate.
    tid = new_turn(ident_zp)
    api._turns[tid]["device_tools"] = sorted(lib.PHONE_DEVICE_TOOLS)
    n = len(mac.got)
    th, got = phone_answer(tid, "Updated “Pickup Leo” (Thu Oct 1, 3:00 PM): location.")
    out = run_turn(tid, [("update_calendar_event", {"title": "Pickup Leo", "on": "2026-10-01", "location": "Lincoln"})])
    th.join(3)
    check("a current phone changes the event itself, never on the Mac",
          (out, [t for t, _ in got], len(mac.got) - n),
          ("Updated “Pickup Leo” (Thu Oct 1, 3:00 PM): location.", ["update_calendar_event"], 0))
    tid = new_turn(ident_zp)
    n = len(mac.got)
    out = run_turn(tid, [("update_calendar_event", {"title": "Pickup Leo", "on": "2026-10-01", "location": "Lincoln"})])
    check("an older phone's edit still goes to the Mac", (out, mac.got[-1]["tool"] if len(mac.got) > n else None),
          ("hello from the Mac", "update_calendar_event"))

    # view_image on a phone turn: an attached photo comes from the phone, a Mac path from the Mac.
    tid = new_turn(ident_zp)
    api._turns[tid]["device_tools"] = sorted(lib.PHONE_DEVICE_TOOLS)
    n = len(mac.got)
    th, got = phone_answer(tid, '{"error": "not decodable"}')
    out = run_turn(tid, [("view_image", {"path": "attachments/photo-1a2b.jpg"})])
    th.join(3)
    check("an attachment's fetch_image runs on the phone",
          (out, got[:1], len(mac.got) - n),
          ("Couldn't load the image: not decodable",
           [("fetch_image", {"path": "attachments/photo-1a2b.jpg", "page": "", "max_px": 1280})], 0))
    tid = new_turn(ident_zp)
    api._turns[tid]["device_tools"] = sorted(lib.PHONE_DEVICE_TOOLS)
    n = len(mac.got)
    out = run_turn(tid, [("view_image", {"path": "~/Desktop/shot.png"})])
    check("a Mac path's fetch_image goes to the Mac", (out, mac.got[-1]["tool"] if len(mac.got) > n else None),
          ("hello from the Mac", "fetch_image"))
    check("phone_runs: attachments on the phone, other paths and other clients not",
          (lib.phone_runs("fetch_image", {"path": "attachments/x.jpg"}, lib.PHONE_DEVICE_TOOLS),
           lib.phone_runs("fetch_image", {"path": "/attachments/x.jpg"}, lib.PHONE_DEVICE_TOOLS),
           lib.phone_runs("fetch_image", {"path": "~/x.jpg"}, lib.PHONE_DEVICE_TOOLS),
           lib.phone_runs("send_message", {}, lib.PHONE_DEVICE_TOOLS),
           lib.phone_runs("send_message", {}, lib.PHONE_DEVICE_TOOLS_LEGACY),
           lib.phone_runs("send_message", {}, None)),
          (True, True, False, True, False, False))
finally:
    mac.close()

print("\n── routing: confirmation relayed to the phone ──")


def confirming(sequence):
    """A Mac that answers needs_confirmation until a request arrives approved."""
    def answer(req):
        sequence.append(req["approved"])
        if not req["approved"]:
            return {"status": "needs_confirmation", "result": "",
                    "confirm": {"summary": "Run a shell command", "detail": "ls ~/Projects", "risk": "dangerous",
                                "domain": "shell"}}
        return {"status": "done", "result": "a b c"}
    return answer


def answer_confirm(tid, approved=True, remember=False, token=zach_phone, delay=0.0, cid=None, expect=200):
    """The phone: wait for the confirm event, then POST the answer."""
    got = {}

    def run():
        cl = TestClient(api.app)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            evs = [d for e, d in api._turns.get(tid, {}).get("events", []) if e == "confirm"]
            if evs:
                got["event"] = evs[-1]
                time.sleep(delay)
                r = cl.post(f"/v1/device/confirm/{tid}", headers=tok(token),
                            json={"confirm_id": cid or evs[-1]["confirm_id"], "approved": approved,
                                  "remember_session": remember})
                got["status"] = r.status_code
                return
            time.sleep(0.02)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, got


fresh()
seq: list = []
mac = FakeMac(MASTER, ZKEY, confirming(seq)).start()
try:
    tid = new_turn(ident_zp, source="user", session="s-1")
    logged.clear()
    th, got = answer_confirm(tid, approved=True, delay=0.4)
    out = run_turn(tid, [("run_shell", {"command": "ls ~/Projects"})])
    th.join(3)
    ev = got.get("event") or {}
    check("needs_confirmation → a confirm event on the phone's stream",
          (sorted(ev), ev.get("device"), ev.get("summary"), ev.get("detail"), ev.get("risk"), ev.get("domain")),
          (["confirm_id", "detail", "device", "domain", "risk", "summary"], "Zach's Mac", "Run a shell command",
           "ls ~/Projects", "dangerous", "shell"))
    check("approve → the same request re-queued approved, then done",
          (got.get("status"), seq, out, mac.got[0]["id"] == mac.got[1]["id"]), (200, [False, True], "a b c", True))
    check("the stream saw working, confirm, working, done",
          events(tid), [("remote", "working"), ("confirm", None), ("remote", "working"), ("remote", "done")])
    ext, took = seen["extended"][0]
    check("the confirmation wait extends the turn's budget", ext >= 0.4 and abs(ext - took) < 0.05, True)
    check("the timeline logs it like a Mac action, from the iPhone",
          logged, ["Ran a shell command on the Mac (from Zach's iPhone)"])

    # remember_session: the next shell action in this session isn't asked again
    seq.clear()
    tid = new_turn(ident_zp, session="s-2")
    th, got = answer_confirm(tid, approved=True, remember=True)
    run_turn(tid, [("run_shell", {"command": "pwd"})])
    th.join(3)
    seq.clear()
    tid = new_turn(ident_zp, session="s-2")
    out = run_turn(tid, [("run_shell", {"command": "whoami"})])
    check("'once per session': the same session's next shell action re-queues approved without asking",
          (out, seq, [e for e, _ in events(tid)].count("confirm")), ("a b c", [False, True], 0))
    seq.clear()
    tid = new_turn(ident_zp, session="s-3")
    th, got = answer_confirm(tid, approved=True)
    run_turn(tid, [("run_shell", {"command": "whoami"})])
    th.join(3)
    check("… another session is asked again", [e for e, _ in events(tid)].count("confirm"), 1)

    # askAlways: a session approval in the same domain never covers it (repeat deletes, secret paths, invitees).
    # (Shell commands here aren't deletions: since #836 titan refuses rm in run_shell before any confirmation.)
    real_answer = mac.answer if hasattr(mac, "answer") else None
    def always(req):
        seq.append(req["approved"])
        if not req["approved"]:
            return {"status": "needs_confirmation", "result": "",
                    "confirm": {"summary": "Delete another event", "detail": "Already deleted this turn: A",
                                "risk": "write", "domain": "shell", "tier": "askAlways"}}
        return {"status": "done", "result": "deleted"}
    mac.answer = always
    seq.clear()
    tid = new_turn(ident_zp, session="s-2")   # s-2 holds a remembered 'shell' approval from above
    th, got = answer_confirm(tid, approved=True, remember=True)
    out = run_turn(tid, [("run_shell", {"command": "chmod 600 x"})])
    th.join(3)
    ev = got.get("event") or {}
    check("askAlways: asked even with a session approval in that domain, and the phone is told",
          ([e for e, _ in events(tid)].count("confirm"), ev.get("tier"), out), (1, "askAlways", "deleted"))
    seq.clear()
    tid = new_turn(ident_zp, session="s-9")
    th, got = answer_confirm(tid, approved=True, remember=True)
    run_turn(tid, [("run_shell", {"command": "chmod 600 y"})])
    th.join(3)
    seq.clear()
    tid = new_turn(ident_zp, session="s-9")
    th, got = answer_confirm(tid, approved=True)
    run_turn(tid, [("run_shell", {"command": "chmod 600 z"})])
    th.join(3)
    check("askAlways: 'remember this session' on it isn't stored", [e for e, _ in events(tid)].count("confirm"), 1)
    mac.answer = real_answer or confirming(seq)

    # decline
    seq.clear()
    logged.clear()
    tid = new_turn(ident_zp, source="user", session="s-4")
    th, got = answer_confirm(tid, approved=False)
    out = run_turn(tid, [("run_shell", {"command": "chmod -R 700 ~/tmp"})])
    th.join(3)
    check("decline → 'You declined this action.', nothing re-queued, nothing on the timeline",
          (out, seq, logged), ("You declined this action.", [False], []))

    # timeout
    xd.CONFIRM_TIMEOUT_S = 0.5
    seq.clear()
    tid = new_turn(ident_zp, session="s-5")
    t0 = time.monotonic()
    out = run_turn(tid, [("run_shell", {"command": "ls"})])
    check("no answer in time → declined", (out, seq, time.monotonic() - t0 < 3), ("You declined this action.", [False], True))
    check("… and the pending confirmation is cleared", api._turns[tid].get("_confirm"), None)
    xd.CONFIRM_TIMEOUT_S = 3.0

    # who may answer
    seq.clear()
    tid = new_turn(ident_zp, session="s-6")
    th_m, got_m = answer_confirm(tid, approved=True, token=robin_phone)
    th_s, got_s = answer_confirm(tid, approved=True, cid="stale", delay=0.1)
    th_z, got_z = answer_confirm(tid, approved=False, delay=0.4)
    out = run_turn(tid, [("run_shell", {"command": "ls"})])
    for t in (th_m, th_s, th_z):
        t.join(3)
    check("another person can't answer Zach's confirmation; a stale id is refused; Zach's answer counts",
          (got_m.get("status"), got_s.get("status"), got_z.get("status"), out), (404, 409, 200, "You declined this action."))
    check("confirm without confirm_id → 400; with nothing waiting → 409",
          (c.post(f"/v1/device/confirm/{tid}", json={"approved": True}, headers=tok(zach_phone)).status_code,
           c.post(f"/v1/device/confirm/{tid}", json={"confirm_id": "x", "approved": True}, headers=tok(zach_phone)).status_code),
          (400, 409))
finally:
    mac.close()

print("\n── routing: refused, failed, no answer ──")
fresh(result=0.6)
answers = {"search_messages": {"status": "refused", "result": "Reading messages is off in Trust on your Mac."},
           "write_file": {"status": "failed", "result": "The folder doesn't exist."},
           "list_files": None}   # taken, never answered
mac = FakeMac(MASTER, ZKEY, lambda req: answers.get(req["tool"], {"status": "done", "result": "ok"})).start()
try:
    logged.clear()
    tid = new_turn(ident_zp, source="user")
    out = run_turn(tid, [("search_messages", {"with": "Mara"})])
    check("refused → the Mac's sentence passes through", out, "Reading messages is off in Trust on your Mac.")
    tid = new_turn(ident_zp, source="user")
    out = run_turn(tid, [("write_file", {"path": "~/x/a.txt", "content": "hi"})])
    check("failed → not completed, with the Mac's reason; nothing on the timeline",
          (out, logged), ("Device action was not completed: The folder doesn't exist.", []))
    tid = new_turn(ident_zp)
    out = run_turn(tid, [("list_files", {"path": "~"})])
    check("delivered but never answered → a bounded wait", out, "Device action was not completed: " + xd.NO_ANSWER)
    check("a result from another person's token is refused (404); a bad status 400",
          (c.post("/v1/device/inbox/nope/result", json={"status": "done", "result": "x"}, headers=tok(robin_phone)).status_code,
           c.post("/v1/device/inbox/nope/result", json={"status": "maybe"}, headers=M).status_code), (404, 400))
finally:
    mac.close()

print("\n── waking ──")
xd.LIVE_WINDOW_S = 0.2
time.sleep(0.3)   # the previous Mac's long-polls are no longer "live"
arrived: dict = {}


def mac_arrives():
    arrived["mac"] = FakeMac(MASTER, ZKEY, lambda req: {"status": "done", "result": "awake now"})
    threading.Timer(0.5, arrived["mac"].start).start()


fresh(wake_wait=4.0, on_wake=mac_arrives)
try:
    tid = new_turn(ident_zp)
    t0 = time.monotonic()
    out = run_turn(tid, [("git_status", {"path": "~/Projects/jellybean"})])
    check("no live Mac: WoL to the broadcast and its address, a TCP connect to its wake port",
          waker_calls, [("udp", "10.0.0.255", 9), ("udp", "10.0.0.42", 9), ("tcp", "10.0.0.42", 7771)])
    check("… it arrives within the wait and the request runs", out, "awake now")
    check("… the phone saw waking, working, done", events(tid), [("remote", "waking"), ("remote", "working"), ("remote", "done")])
finally:
    if arrived.get("mac"):
        arrived["mac"].close()
time.sleep(0.3)
fresh(wake_wait=0.6)
tid = new_turn(ident_zp, source="user")
logged.clear()
out = run_turn(tid, [("run_shortcut", {"name": "Lights"})])
check("the Mac never arrives → the contract's plain sentence, and the turn goes on",
      (out, events(tid), logged), (xd.NO_WAKE, [("remote", "waking"), ("remote", "done")], []))
r = c.get(f"/v1/device/inbox?wait=0&device_key={ZKEY}", headers=M).json()
check("… the request was withdrawn (a late long-poll doesn't run it)", r, {"requests": []})
xd.LIVE_WINDOW_S = 35.0

print("\n── the phone's SSE stream, end to end ──")
# (Starlette's TestClient hands back a streamed body only when it's complete, so the phone's answer
# comes from a watcher that answers whichever turn is waiting on a confirmation.)
fresh()
seq = []
mac = FakeMac(MASTER, ZKEY, confirming(seq)).start()
names: list = []
final: dict = {}
stop_watch = threading.Event()


def phone_watcher():
    cl = TestClient(api.app)
    while not stop_watch.is_set():
        with api._turns_lock:
            waiting = [(t, v["_confirm"]["confirm_id"]) for t, v in api._turns.items() if v.get("_confirm")
                       and v["_confirm"]["answer"] is None]
        for t, cid in waiting:
            cl.post(f"/v1/device/confirm/{t}", headers=tok(zach_phone), json={"confirm_id": cid, "approved": True})
        time.sleep(0.05)


watcher = threading.Thread(target=phone_watcher, daemon=True)
watcher.start()
try:
    SCRIPT[:] = [("run_shell", {"command": "ls"})]
    r = TestClient(api.app).post("/v1/prompt/stream", headers=tok(zach_phone),
                                 json={"prompt": "list my projects folder", "client": "iphone", "device": True,
                                       "remember": False})   # a "test" turn (AGENT_TURN_SOURCE): an eval
                                                             # turn would get a simulated Mac, not this channel
    ev = None
    for line in r.text.splitlines():
        if line.startswith("event: "):
            ev = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
            names.append((ev, data.get("step") or ("…" if ev == "confirm" and data.get("confirm_id") else None)))
            if ev == "done":
                final = data
    check("the step label isn't doubled", [ln for ln in r.text.splitlines() if "On your Mac:" in ln],
          ['data: {"text": "On your Mac: Running a command\\u2026"}'])
    check("the stream: status, working, confirm, working, done, then the reply",
          ([n for n in names if n[0] != "delta"], final.get("text")),
          ([("status", None), ("remote", "working"), ("confirm", "…"), ("remote", "working"), ("remote", "done"),
            ("done", None)], "a b c"))
finally:
    stop_watch.set()
    watcher.join(3)
    mac.close()
    api._lib._run_agent_turn, lib.execute_tool, lib._maybe_save_fact = orig_turn, orig_exec, orig_save

print("\n── heartbeat: battery and disk (device health v2) ──")
import cross_device as _xd2
p = _xd2.clean_presence({"device_key": "mac-abc", "name": "MacBook", "battery_pct": 82.4, "on_ac": True, "disk_free_gb": 123.4})
check("battery, power and disk are kept", (p["battery_pct"], p["on_ac"], p["disk_free_gb"]), (82, True, 123.4))
p = _xd2.clean_presence({"device_key": "mac-abc", "name": "Mac mini", "battery_pct": None, "on_ac": True})
check("a Mac without a battery", (p["battery_pct"], p["on_ac"], p["disk_free_gb"]), (None, True, None))
p = _xd2.clean_presence({"device_key": "mac-abc", "name": "Old Mac"})
check("an older Mac sends none: fine", (p["battery_pct"], p["on_ac"], p["disk_free_gb"]), (None, None, None))
p = _xd2.clean_presence({"device_key": "mac-abc", "name": "x", "battery_pct": "lots", "disk_free_gb": -5})
check("junk is dropped, never a 400", (p["battery_pct"], p["disk_free_gb"]), (None, None))
import tempfile as _tf
_st = hh.HouseholdStore(Path(_tf.mkdtemp()) / "hb.db")
_st.record_presence("zach", None, _xd2.clean_presence({"device_key": "mac-abc", "name": "MacBook", "battery_pct": 15, "on_ac": False, "disk_free_gb": 8.5}))
m = _st.macs_for("zach")[0]
check("stored and returned by macs_for", (m["battery_pct"], m["on_ac"], m["disk_free_gb"]), (15, False, 8.5))

print("\n── a restart ends long-polls cleanly ──")
import asyncio as _aio
import cross_device as _xd3
_xd3.SHUTTING_DOWN.set()
_t0 = time.monotonic()
_got = _aio.run(_xd3.CHANNEL.long_poll(("zach", "mac-restart"), 20))
check("a long-poll returns at once when stopping", (_got, time.monotonic() - _t0 < 1.0), ([], True))
_xd3.SHUTTING_DOWN.clear()

print("\n── what this Mac's Jellybean actually runs (self-knowledge, 10-01) ──")
# A Mac from before move_files and before travel_time (jellybean#547): it reports neither.
OLD_MAC = sorted(n for n in lib._DEVICE_TOOLS if n not in ("move_files", "travel_time") and n not in lib.PHONE_ONLY_TOOLS)
p = _xd2.clean_presence({"device_key": "mac-abc", "name": "MacBook", "device_tools": OLD_MAC + ["Bad Name", 7]})
check("a Mac's tool list is kept plain", (p["device_tools"] == sorted(OLD_MAC), _xd2.clean_presence(
      {"device_key": "mac-abc", "name": "x"})["device_tools"]), (True, None))
_st.record_presence("zach", None, p)
_st.record_presence("zach", None, _xd2.clean_presence({"device_key": "mac-abc", "name": "MacBook"}))
check("an older ping keeps the known list", "read_file" in (_st.macs_for("zach")[0]["device_tools"] or []), True)
check("sources: known names and levels only (jellybean#413)",
      _xd2.clean_presence({"device_key": "mac-abc", "name": "M", "sources": {"mail": "waiting", "calendar": "granted",
                                                                          "secret": "x", "notes": "Mail isn't open"}})["sources"],
      {"calendar": "granted", "mail": "waiting"})
check("…none sent: None", _xd2.clean_presence({"device_key": "mac-abc", "name": "M"})["sources"], None)
_st.record_presence("zach", None, _xd2.clean_presence({"device_key": "mac-abc", "name": "MacBook", "sources": {"mail": "waiting"}}))
first = _st.macs_for("zach")[0]["sources"]["mail"]
_st.record_presence("zach", None, _xd2.clean_presence({"device_key": "mac-abc", "name": "MacBook", "sources": {"mail": "waiting"}}))
check("a level that stays keeps its since", _st.macs_for("zach")[0]["sources"]["mail"], first)
_st.record_presence("zach", None, _xd2.clean_presence({"device_key": "mac-abc", "name": "MacBook"}))
check("an older ping keeps the sources", _st.macs_for("zach")[0]["sources"]["mail"]["level"], "waiting")
_st.record_presence("zach", None, _xd2.clean_presence({"device_key": "mac-abc", "name": "MacBook", "sources": {"mail": "granted"}}))
check("a new level starts its own since", _st.macs_for("zach")[0]["sources"]["mail"]["level"], "granted")
check("mac_runs: a usable list, else None (an older Mac: every tool)",
      (lib.mac_runs(OLD_MAC) == frozenset(OLD_MAC) & frozenset(lib._DEVICE_TOOLS), lib.mac_runs(None),
       lib.mac_runs(["read_file", "nope"])), (True, None, None))
check("a phone-sized list never counts as a Mac's", lib.mac_runs(sorted(lib.PHONE_DEVICE_TOOLS)), None)
mac_turn = lib.device_tools_for_client(None, OLD_MAC)
check("a turn asked on that Mac offers only what it runs", ("move_files" in mac_turn, "read_file" in mac_turn,
      "travel_time" in mac_turn), (False, True, False))
check("a Mac that reports nothing: every tool, as before", lib.device_tools_for_client("mac", None), None)
store.record_presence("zach", None, xd.clean_presence({**good, "device_key": ZKEY, "device_tools": OLD_MAC}))
rz_old = api._remote_for_turn(ident_zp, "iphone", lib.PHONE_DEVICE_TOOLS)
check("a phone turn routes only what Zach's Mac runs", ("move_files" in rz_old["tools"], "read_file" in rz_old["tools"]),
      (False, True))
import capabilities as _cap  # noqa: E402
line = _cap.devices_part("zach", store.people(), store.macs_for, lambda pid, key: True)
check("capabilities says what that Mac lacks", "needs a newer Jellybean for move_files" in line, True)

print("\n── what an inbox item carries (#448) ──")
_it = xd.inbox_item({"tool": "send_message", "args": {"to": "Ava"}, "risk": "dangerous", "grant_id": "gr-abc"},
                    "sa-1", {"device": "titan (scheduled)", "platform": "titan"})
check("a grant approved ahead reaches the Mac", (_it["grant_id"], _it["tool"], _it["approved"]),
      ("gr-abc", "send_message", False))
check("…and is absent when there is none", "grant_id" in xd.inbox_item({"tool": "read_file"}, "t", {}), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
