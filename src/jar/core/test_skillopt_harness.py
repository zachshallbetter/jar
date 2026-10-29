#!/usr/bin/env python3
"""
Tests for the SkillOpt harness (scripts/skillopt/harness.py): the check language, relative-date
rendering, the fake Mac, tool detection from status lines, and that every task bank is valid and
names only tools that exist. Hermetic: no turns are run.

Run: python3 scripts/test_skillopt_harness.py
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "skillopt"))

import harness as h  # noqa: E402


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def run():
    ok = True
    print("\n── check language ──")
    t = h.Turn(text="Tomorrow: Piano lesson at 3 PM and Team dinner at 7 PM.",
               device=[("list_calendar_events", {"start": "x"}), ("create_reminder", {"title": "Call the plumber"})],
               status=["Searching the web…"])
    ok &= check("titan tools come from status lines", "web_search" in t.tools(), True)
    ok &= check("device tools come from device calls", {"list_calendar_events", "create_reminder"} <= t.tools(), True)
    task = {"expect": {"tools_any": ["list_calendar_events"], "tools_none": ["get_screen"],
                       "reply_all": ["piano", "dinner"], "reply_none": ["standup"],
                       "args": [{"tool": "create_reminder", "key": "title", "re": "plumb"}]}}
    ok &= check("all pass → 1.0", h.score(task, t), (1.0, []))
    bad = {"expect": {"tools_all": ["delete_calendar_event"], "reply_any": ["dentist"], "tools_none": ["web_search"]}}
    s, failed = h.score(bad, t)
    ok &= check("none pass → 0.0", s, 0.0)
    ok &= check("one line per failed check", len(failed), 3)
    ok &= check("partial credit", h.score({"expect": {"reply_all": ["piano", "dentist"]}}, t)[0], 0.5)
    ok &= check("a failed turn scores 0", h.score(task, h.Turn(error="timeout"))[0], 0.0)
    link = h.Turn(text="Here: https://docs.python.org/3.13/whatsnew/3.13.html",
                  device=[("delete_calendar_event", {}), ("delete_calendar_event", {})])
    ok &= check("reply_re matches", h.score({"expect": {"reply_re": [r"docs\.python\.org/3(\.13)?/whatsnew"]}}, link)[0], 1.0)
    ok &= check("reply_re misses", h.score({"expect": {"reply_re": [r"example\.org/page"]}}, link)[0], 0.0)
    ok &= check("calls_max within", h.score({"expect": {"calls_max": {"delete_calendar_event": 2}}}, link)[0], 1.0)
    ok &= check("calls_max over", h.score({"expect": {"calls_max": {"delete_calendar_event": 1}}}, link)[0], 0.0)
    ok &= check("history is rendered", "{weekday" in str(h.render("{weekday+5}")), False)
    ok &= check("no checks → 1.0", h.score({"expect": {}}, t)[0], 1.0)

    print("\n── relative dates and the fake Mac ──")
    today = h.te._today()
    ok &= check("{weekday+2}", h.render("on {weekday+2}"), "on " + (today + h.timedelta(days=2)).strftime("%A"))
    ok &= check("{date+10}", h.render("{date+10}"), (today + h.timedelta(days=10)).strftime("%Y-%m-%d"))
    ok &= check("reminders fixture", "Renew passport" in h.fake_mac("list_reminders", {}), True)
    import agent_runtime_lib as _lib
    ok &= check("no Plex tool in the registry, so no Plex fixture (#1625)", "get_plex_state" in _lib._DEVICE_TOOLS, False)
    ok &= check("calendar delegates to turn_eval", "Piano lesson" in h.fake_mac(
        "list_calendar_events", {"start": (today + h.timedelta(days=1)).strftime("%Y-%m-%d"),
                                 "end": (today + h.timedelta(days=1)).strftime("%Y-%m-%d")}), True)
    st = {}
    ok &= check("a delete answers like the Mac", h.fake_mac("delete_calendar_event", {"title": "team dinner", "on": "d"}, st),
                "Cancelled “Team dinner” on d")
    day = (today + h.timedelta(days=1)).strftime("%Y-%m-%d")
    ok &= check("... and a re-read no longer shows it",
                "Team dinner" in h.fake_mac("list_calendar_events", {"start": day, "end": day}, st), False)
    ok &= check("an unknown title changes nothing", "Nothing changed" in h.fake_mac("delete_calendar_event", {"title": "zzz"}, st), True)

    print("\n── creates and moves answer like the Mac, so titan's check passes ──")
    import agent_runtime_lib as lib
    st2, saved = {}, (lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed)
    try:
        lib._device_request = lambda n, a: h.fake_mac(n, a, st2)
        lib._device_bridge_enabled = lambda: True
        lib._device_tool_allowed = lambda n: True
        out = lib.execute_tool("create_calendar_event", {"title": "Haircut", "start": f"{day}T11:00", "calendar": "Family"})
        ok &= check("a create is verified", ("Verified:" in out, "WARNING" in out), (True, False))
        out = lib.execute_tool("update_calendar_event", {"title": "Piano lesson", "on": day, "start": f"{day}T16:00"})
        ok &= check("a move is verified", ("Verified:" in out, "WARNING" in out), (True, False))
    finally:
        lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed = saved

    print("\n── skill-pick scoring (pick_eval) ──")
    import pick_eval as pe
    qs = dict((q, w) for w, q in pe.questions())
    ok &= check("a negative task names its expected pick", qs.get("What's 17% of 240?"), "none")
    ok &= check("alternatives allowed", qs.get("Will it rain tomorrow?"), "none|web-research")
    ok &= check("a plain task expects its bank", qs.get(h.render("Anything on {weekday+2}?")), "calendar")
    fake = {"q1": ("media", 0.9), "q2": ("media", 0.55), "q3": ("home-automation", 0.8), "q4": ("none", 0.9),
            "q5": ("media", 0.9)}
    orig_v = pe.lib.jev_skill_verdict
    try:
        pe.lib.jev_skill_verdict = lambda q, sk: fake[q]
        rows = pe.verdicts([("media", "q1"), ("media", "q2"), ("media", "q3"), ("none|web-research", "q4"),
                            ("none", "q5")], {"media": "m", "home-automation": "h"}, workers=1)
        ok &= check("hits: picked, unsure, other skill, allowed none, a false load",
                    [r["hit"] for r in rows], [True, False, False, True, False])
        s = pe.summary(rows)
        ok &= check("media recall and misses", (s["media"]["recall"], s["media"]["misses"]),
                    (0.333, {"unsure:media": 1, "home-automation": 1}))
        ok &= check("a none question loading media is a false load", s["media"]["false_loads"], 1)
    finally:
        pe.lib.jev_skill_verdict = orig_v

    print("\n── travel_time answers in the iPhone's words ──")
    ok &= check("an event title, on the real clock", h.fake_mac("travel_time", {"destination": "the dentist"}).endswith(
        h.render("Leave by {time+101} to arrive by {time+120}.")), True)
    which = h.fake_mac("travel_time", {"destination": "appointment"})
    ok &= check("two matches ask which, each with its on=", (which.startswith("Which one: "), which.count(", on=")), (True, 2))
    dated = h.Turn(device=[("travel_time", {"destination": "appointment", "on": h.render("{date+1}T09:00")})])
    ok &= check("an args check may use {date+N}", h.score({"expect": {"args": [{"tool": "travel_time", "key": "on",
                                                                                "re": "{date+1}"}]}}, dated)[0], 1.0)
    ok &= check("the follow-up picks by on=", h.fake_mac("travel_time", {"destination": "appointment",
                "on": h.render("{date+1}T09:00")}).endswith("arrive by tomorrow 9:00 AM."), True)
    ok &= check("no place", h.fake_mac("travel_time", {"destination": "standup"}), h.render("“Standup” (today {time+60}) has no place on it."))
    ok &= check("a task's own device answer", h.fake_mac("list_calendar_events", {}, {"overrides": {
        "list_calendar_events": "• {day+0}, {time+60} — Standup"}}), h.render("• {day+0}, {time+60} — Standup"))
    ok &= check("transit", "by transit" in h.fake_mac("travel_time", {"destination": "pearl", "mode": "transit"}), True)
    ok &= check("nothing to go on", h.fake_mac("travel_time", {}), "Say where to: a place, an address, or an event's title.")
    import agent_runtime_lib as lib
    ok &= check("a phone eval reports the phone's tools, travel_time included",
                (set(h.te.phone_tools()) == set(lib.PHONE_DEVICE_TOOLS), "travel_time" in h.te.phone_tools()), (True, True))
    seen = {}
    orig_once = h._ask_once
    try:
        h._ask_once = lambda q, hist, so, to, client=None, mac=None, **k: seen.update(client=client, mac=mac) or h.Turn(text="ok")
        h.run_task({"id": "p", "question": "q", "client": "iphone", "mac": "never", "expect": {}}, "calendar")
        ok &= check("a phone task asks as the iPhone, with a simulated Mac", seen, {"client": "iphone", "mac": "never"})
    finally:
        h._ask_once = orig_once

    import re as _re
    src = (HERE / "turn_eval.py").read_text()
    fb = _re.search(r"no agent deps here[^\n]*\n        return \[(.*?)\]", src, _re.S)
    ok &= check("turn_eval's fallback phone tools match the registry (a new phone tool updates both)",
                sorted(_re.findall(r'"([a-z_]+)"', fb.group(1))) if fb else None, sorted(lib.PHONE_DEVICE_TOOLS))

    print("\n── the iPhone's Music app answers in its own words ──")
    ms = {}
    ok &= check("an album", h.fake_mac("play_music", {"query": "Kind of Blue"}, ms),
                "Playing the album “Kind of Blue” by Miles Davis on this iPhone.")
    ok &= check("what's playing", h.fake_mac("now_playing", {}, ms).startswith("Playing on this iPhone: “So What”"), True)
    ok &= check("pause, then what's playing", (h.fake_mac("music_control", {"action": "pause"}, ms), h.fake_mac("now_playing", {}, ms)),
                ("Paused.", "Paused on this iPhone: “So What” by Miles Davis."))
    ok &= check("not found", h.fake_mac("play_music", {"query": "Zzyzx Road"}, {}),
                "Couldn't find “Zzyzx Road” in your music or Apple Music.")

    print("\n── API restarts are not skill failures ──")
    calls = []
    orig_once, orig_up = h._ask_once, h._api_up
    try:
        seq = [h.Turn(error="URLError: <urlopen error [Errno 111] Connection refused>"), h.Turn(text="ok")]
        h._ask_once = lambda *a, **k: calls.append(1) or seq.pop(0)
        h._api_up = lambda wait_s: True
        ok &= check("a restart is waited out and retried", h.ask("q").text, "ok")
        ok &= check("... one retry", len(calls), 2)
        h._ask_once = lambda *a, **k: h.Turn(error="URLError: Connection refused")
        h._api_up = lambda wait_s: False
        try:
            h.ask("q")
            raised = False
        except h.InfraError:
            raised = True
        ok &= check("an API that stays down stops the run", raised, True)
        h._ask_once = lambda *a, **k: h.Turn(error="HTTP 422 something the skill caused")
        ok &= check("other errors are the turn's", h.ask("q").error.startswith("HTTP 422"), True)
    finally:
        h._ask_once, h._api_up = orig_once, orig_up

    print("\n── whether the skill's text was shown (preloaded) or read (skill_guide) ──")
    orig_ask = h.ask
    try:
        for label, seen, want in (("body shown", {"picked": ["media"], "body": True}, (True, ["media"], False)),
                                  ("another skill picked", {"picked": ["calendar"], "body": True}, (False, ["calendar"], False)),
                                  ("descriptions only", {"picked": None, "body": False}, (False, None, False)),
                                  ("pick missed, the model read it: shown stays False",
                                   {"picked": ["calendar"], "body": True, "read": ["media"]}, (False, ["calendar"], True)),
                                  ("preloaded and read again", {"picked": ["media"], "body": True, "read": ["media"]},
                                   (True, ["media"], True)),
                                  ("an API before skill_guide (no read)", {"picked": None, "body": False}, (False, None, False)),
                                  ("an older API says nothing", None, (None, None, None))):
            h.ask = lambda *a, _s=seen, **k: h.Turn(text="ok", skills=_s)
            r = h.run_task({"id": "x", "question": "q", "expect": {}}, "media")
            ok &= check(label, (r["skill_shown"], r["skill_picked"], r["skill_read"]), want)
    finally:
        h.ask = orig_ask

    print("\n── stratified split ──")
    import tempfile
    import run as runner
    tasks = [{"id": f"t{i}", "question": "q", "expect": {}} for i in range(12)]
    fail = {"t3", "t7", "t9"}
    orig_run = h.run_task
    h.run_task = lambda t, skill, text=None: {"id": t["id"], "score": 0.0 if t["id"] in fail else 1.0,
                                               "failed": [], "tools": [], "device_calls": [], "status": [],
                                               "reply": "", "ms": 1}
    try:
        with tempfile.TemporaryDirectory() as d:
            sizes = runner.stratified_splits(tasks, "calendar", "x", Path(d), repeats=2)
            got = {n: json.loads((Path(d) / "splits" / n / "items.json").read_text()) for n in ("train", "val", "test")}
            base = {n: {x["id"].split("#")[0] for x in v} for n, v in got.items()}
            ok &= check("every split has a failing task", all(base[n] & fail for n in base), True)
            ok &= check("no task in two splits", len(base["train"] | base["val"] | base["test"]), 12)
            ok &= check("val and test run twice", (sizes["val"], sizes["test"]), (len(base["val"]) * 2, len(base["test"]) * 2))
            ok &= check("repeat ids are distinct", len({x["id"] for x in got["val"]}), sizes["val"])
            ok &= check("baseline saved", (Path(d) / "baseline.json").exists(), True)
    finally:
        h.run_task = orig_run

    print("\n── task banks ──")
    import agent_runtime_lib as lib
    known = set(lib._TOOL_REGISTRY) | set(lib._DEVICE_TOOLS)
    ok &= check("status lines parsed from source", len(h.STATUS_TOOLS) > 20, True)
    for path in sorted((HERE / "skillopt" / "tasks").glob("*.jsonl")):
        skill = path.stem
        tasks = h.load_tasks(skill)
        ids = [x["id"] for x in tasks]
        named = set()
        for x in tasks:
            e = x.get("expect", {})
            for key in ("tools_all", "tools_any", "tools_none"):
                named |= set(e.get(key, []))
            named |= {a["tool"] for a in e.get("args", [])}
        unknown = sorted(named - known)
        ok &= check(f"{skill}: {len(tasks)} tasks, unique ids", len(ids) == len(set(ids)), True)
        ok &= check(f"{skill}: a skill of that name exists (or a regression bank)",
                    (HERE / "skills" / skill / "SKILL.md").exists() or skill in h.REGRESSION_BANKS, True)
        ok &= check(f"{skill}: every named tool exists", unknown, [])

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
