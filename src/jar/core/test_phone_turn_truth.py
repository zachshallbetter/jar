#!/usr/bin/env python3
"""
Tests for what a turn says about things it can't see (09-30, a phone session with the Mac app
closed): an iPhone turn gets a "Your Mac" line built from presence and the channel; run_check says
when the Mac isn't picking checks up instead of "running"; the prompt forbids promised follow-ups
and "the tool results" talk; the turn log names the tools a turn ran (names only). Hermetic.

Run: python3 scripts/test_phone_turn_truth.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
_TMP = Path(tempfile.mkdtemp(prefix="phone-truth-"))
os.environ["PROACTIVE_DB"] = str(_TMP / "proactive.db")

import agent_runtime_lib as lib  # noqa: E402
import cross_device as xd  # noqa: E402
import proactive  # noqa: E402


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


class FakeStore:
    def __init__(self, macs):
        self.macs = macs

    def macs_for(self, person_id):
        return [m for m in self.macs if m["person_id"] == person_id]


def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def run():
    ok = True
    import agent_api_server as api
    import household_identity as hh
    zach = hh.owner_identity()

    print("\n── how long ago ──")
    ok &= check("seconds", lib._ago(40), "40 s")
    ok &= check("minutes", lib._ago(12 * 60), "12 min")
    ok &= check("hours", lib._ago(3 * 3600), "3 h")
    ok &= check("days", lib._ago(2 * 86400), "2 days")

    print("\n── an iPhone turn says what titan knows about the Mac ──")
    saved_store = api._store
    xd.reset(channel=xd.Channel())
    mac = {"person_id": zach.person_id, "device_key": "mac-zach-0001", "name": "Zach's MacBook Pro",
           "lan_ip": None, "mac_address": None, "wake_port": None, "awake": True,
           "updated_at": iso(time.time() - 2 * 3600)}
    try:
        api._store = lambda: FakeStore([])
        ok &= check("not a phone turn: nothing", api._mac_status_line(zach, None, None), None)
        line = api._mac_status_line(zach, "iphone", None)
        ok &= check("never checked in: says so, and that Jellybean's state is unknown",
                    ("never heard from it" in line, "unknown" in line), (True, True))
        ok &= check("… and names what needs the Mac (Find My first)", ("Find My" in line, "needs their Mac" in line),
                    (True, True))

        api._store = lambda: FakeStore([mac])
        line = api._mac_status_line(zach, "iphone", None)
        ok &= check("known but unreachable: name, when, can't wake",
                    ("Zach's MacBook Pro" in line, "last checked in 2 h ago" in line, "can't wake it" in line),
                    (True, True, True))

        wake = {**mac, "mac_address": "aa:bb:cc:dd:ee:ff"}
        api._store = lambda: FakeStore([wake])
        remote = {"mac": wake, "tools": frozenset({"get_screen"})}
        line = api._mac_status_line(zach, "iphone", remote)
        ok &= check("wakeable: titan will try to wake it", "will try to wake it" in line, True)

        xd.CHANNEL.poll_started((zach.person_id, "mac-zach-0001"))
        line = api._mac_status_line(zach, "iphone", remote)
        ok &= check("live: connected now", line.startswith("Your Mac (Zach's MacBook Pro): connected to titan now"), True)

        asleep = {**mac, "awake": False}
        api._store = lambda: FakeStore([asleep])
        xd.reset(channel=xd.Channel())
        ok &= check("a Mac that said it was going to sleep", "going to sleep" in api._mac_status_line(zach, "iphone", None), True)

        def broken():
            raise RuntimeError("no db")
        api._store = broken
        ok &= check("no store: say nothing rather than guess", api._mac_status_line(zach, "iphone", None), None)
    finally:
        api._store = saved_store
        xd.reset()

    print("\n── eval turns: a simulated Mac, never the real one ──")
    ok &= check("not an eval turn: no simulated Mac", api._parse_eval_mac({"eval_mac": "live"}, "user"), None)
    ok &= check("an eval turn defaults to no Mac", api._parse_eval_mac({}, "eval"), "never")
    ok &= check("… or names one", api._parse_eval_mac({"eval_mac": "Wakeable"}, "eval"), "wakeable")
    ok &= check("… nothing else", api._parse_eval_mac({"eval_mac": "real"}, "eval"), "never")
    ok &= check("no simulated Mac for a Mac-client eval", api._eval_remote(zach, None, "live"), None)
    ok &= check("an unreachable Mac offers no tools", api._eval_remote(zach, "iphone", "unreachable"), None)
    er = api._eval_remote(zach, "iphone", "live")
    ok &= check("a live simulated Mac: its tools, answered by the eval", (er["eval"], "get_frontmost_app" in er["tools"]),
                (True, True))
    for state, want in (("never", "never heard from it"), ("unreachable", "can't wake it"),
                        ("wakeable", "will try to wake it"), ("live", "connected to titan now")):
        line = api._mac_status_line(zach, "iphone", er if state in ("live", "wakeable") else None, state)
        ok &= check(f"simulated {state}: the line says so", want in (line or ""), True)
    io = lib._TURN_IO
    con = proactive._jobs_db()
    before = con.execute("SELECT COUNT(*) FROM device_jobs").fetchone()[0]
    con.close()
    try:
        io.source, io.eval_mac = "eval", "unreachable"
        out = lib._execute_tool("run_check", {"name": "jellybean-selftests"})
        ok &= check("eval run_check with the Mac away: not running", "NOT running yet" in out, True)
        io.eval_mac = "live"
        ok &= check("eval run_check with a live Mac: runs soon", "within ~30 s" in lib._execute_tool("run_check", {"name": "x"}), True)
    finally:
        io.source, io.eval_mac = None, None
    con = proactive._jobs_db()
    ok &= check("… and no real job was queued", con.execute("SELECT COUNT(*) FROM device_jobs").fetchone()[0], before)
    con.close()

    print("\n── the line reaches the prompt context ──")
    saved = (lib._device_bridge_enabled, getattr(lib, "_kb_context", None))
    lib._TURN_IO.mac_status = "Your Mac (Test Mac): not connected."
    try:
        lib._device_bridge_enabled = lambda: False
        ctx = lib._situational_context_inner(None)
        ok &= check("in the situational context", "Your Mac (Test Mac): not connected." in ctx, True)
    finally:
        lib._TURN_IO.mac_status = None
        lib._device_bridge_enabled = saved[0]
    ok &= check("prompt: no promised follow-ups", "never promise to check back" in lib.AGENT_IDENTITY, True)
    ok &= check("prompt: not 'the tool results'", 'not "the tool results do not contain X"' in lib.AGENT_IDENTITY, True)

    print("\n── run_check when the Mac isn't picking checks up ──")
    proactive.set_catalog([{"name": "jellybean-selftests", "description": "self-tests"},
                           {"name": "git-status", "description": "git status"}])
    out = lib._execute_tool("run_check", {"name": "jellybean-selftests"})
    ok &= check("never claimed: not running yet", ("NOT running yet" in out, "never picked up" in out), (True, True))
    proactive.claim_jobs()
    out = lib._execute_tool("run_check", {"name": "git-status"})
    ok &= check("claimed just now: the ~30 s promise stands", "runs it within ~30 s" in out, True)
    con = proactive._jobs_db()
    con.execute("UPDATE device_jobs_seen SET at=? WHERE k='claim'", (time.time() - 2 * 3600,))
    con.commit()
    con.close()
    out = lib._execute_tool("run_check", {"name": "git-status"})
    ok &= check("claimed two hours ago: says when", ("NOT running yet" in out, "2 h ago" in out), (True, True))
    out = lib._execute_tool("run_check", {"name": "jellybean-git-history"})
    ok &= check("an unknown check still lists the real ones", "Available checks: git-status, jellybean-selftests" in out, True)

    print("\n── finished checks are told on the next turn ──")
    jid = out = None
    con = proactive._jobs_db()
    jid = con.execute("SELECT id FROM device_jobs WHERE check_name='jellybean-selftests'").fetchone()[0]
    con.close()
    proactive.post_result(jid, False, 1, "3 failures", 4000)
    io = lib._TURN_IO
    try:
        io.source, io.role = "eval", None
        ok &= check("not on eval turns", lib._finished_checks_line(), None)
        io.source = "user"
        line = lib._finished_checks_line()
        ok &= check("the user's turn hears the failure", ("jellybean-selftests FAILED (exit 1)" in (line or ""), jid in (line or "")),
                    (True, True))
        ok &= check("… once", lib._finished_checks_line(), None)
        io.role = "kid"
        ok &= check("not on someone else's turn", lib._finished_checks_line(), None)
    finally:
        io.source, io.role = None, None

    print("\n── a turn always says something ──")
    lib._TURN_INFO.info = {"tool_names": ["file_list", "file_read", "file_read", "kb_retrieve"]}
    ok &= check("empty → says it didn't get there", lib._never_empty("  "),
                "I didn't get to an answer on that one: I tried 4 steps but my reply came back empty. Ask again, or put it another way.")
    ok &= check("a reply is untouched", lib._never_empty("Hi."), "Hi.")
    ok &= check("only punctuation is empty too (10-01: a calendar read, then \".\")",
                lib._never_empty(" . ").startswith("I didn't get to an answer"), True)
    ok &= check("a bare number is an answer", lib._never_empty("42"), "42")
    lib._TURN_INFO.info = {}
    import inspect
    src = inspect.getsource(lib)
    ok &= check("the forced final answer no longer says 'the tool results above'",
                ("using only the tool results above" in src, 'never \\"the tool results\\"' in src), (False, True))

    print("\n── the turn log names the tools (never their arguments) ──")
    log = _TMP / "turns.jsonl"
    saved_log = os.environ.get("AGENT_TURN_LOG")
    os.environ["AGENT_TURN_LOG"] = str(log)
    try:
        lib._TURN_INFO.info = {}
        lib._TURN_INFO.route = {}
        for name in ("get_time", "get_time", "run_check"):
            lib._note_tool_name(name)
        lib._log_turn(time.monotonic(), 8, None)
        rec = json.loads(log.read_text().splitlines()[-1])
        ok &= check("names in call order", rec.get("tool_names"), ["get_time", "get_time", "run_check"])
        ok &= check("a Mac or CLI turn has no client field", "client" in rec, False)
        lib._TURN_IO.client = "iphone"
        lib._log_turn(time.monotonic(), 8, None)
        lib._TURN_IO.client = None
        ok &= check("an iPhone turn says so", json.loads(log.read_text().splitlines()[-1]).get("client"), "iphone")
        lib._TURN_INFO.info = {}
        for i in range(lib.TURN_LOG_MAX_TOOLS + 5):
            lib._note_tool_name("get_time")
        ok &= check("capped", len(lib._TURN_INFO.info["tool_names"]), lib.TURN_LOG_MAX_TOOLS)
        lib._TURN_INFO.info = {}
        lib.execute_tool("get_time", {})
        ok &= check("execute_tool records its name", lib._TURN_INFO.info.get("tool_names"), ["get_time"])
    finally:
        lib._TURN_INFO.info = {}
        if saved_log is None:
            os.environ.pop("AGENT_TURN_LOG", None)
        else:
            os.environ["AGENT_TURN_LOG"] = saved_log

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
