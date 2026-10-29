#!/usr/bin/env python3
"""
Tests for the eval hooks SkillOpt needs (scripts/skillopt): eval turns may bring a candidate skill
text (skill_overrides, admin + source "eval" only), and tools with real effects are simulated on
eval turns (Home Assistant, durable stores, paid generation). Hermetic: nothing is written or sent.

Run: python3 scripts/test_skillopt_hooks.py
"""
import os
import sys
import tempfile
from pathlib import Path
# Hermetic: never titan's real Home Assistant token (an eval home_control dry-runs against it).
os.environ["HA_TOKEN_FILE"] = str(Path(tempfile.mkdtemp()) / "no-ha-token")
sys.path.insert(0, os.path.dirname(__file__))

import agent_runtime_lib as lib


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


CANDIDATE = """---
name: calendar
description: Candidate calendar skill used only by this test.
---

## Rules
- A candidate rule the test can find.
"""


def run():
    ok = True

    print("\n── Eval turns never perform real effects ──")
    io = lib._TURN_IO
    try:
        io.source = "eval"
        ok &= check("extract_bill simulated", lib.execute_tool("extract_bill", {"path": "/tmp/bill.pdf"})
                    .startswith("Done (eval: not sent). extract_bill"), True)
        for tool, args in (("log_event", {"event": "Heading out to get Leo"}),
                           ("set_preference", {"name": "ask_first", "value": "off"})):
            ok &= check(f"{tool} answers eval itself", lib.execute_tool(tool, args).startswith("Test turn"), True)
        out = lib.execute_tool("home_control", {"device": "Kitchen light", "action": "on"})
        ok &= check("home_control simulated (no Home Assistant)", out.startswith("Done (eval: not sent). home_control"), True)
        import home_assistant as ha
        posts, saved_http = [], ha._http

        def fake_ha(method, path, body=None):
            if method == "POST":
                posts.append(path)
                return []
            return [{"entity_id": "climate.hall", "state": "heat_cool", "attributes": {
                "friendly_name": "Hall Thermostat", "target_temp_low": 69, "target_temp_high": 73,
                "hvac_modes": ["heat", "cool", "heat_cool", "off"], "temperature_unit": "°F"}}]
        try:
            ha._http = fake_ha
            out = lib.execute_tool("home_control", {"device": "Hall Thermostat", "action": "set_temperature", "value": 72})
            ok &= check("with Home Assistant: a dry run of the real plan, nothing sent",
                        (out.startswith("Done (eval: not sent): Hall Thermostat"), "70–74°" in out, posts), (True, True, []))
        finally:
            ha._http = saved_http
        for name in ("save_routine", "update_record", "save_contact", "ingest_text", "kb_forget",
                     "close_task", "generate_image", "speak", "file_write", "file_edit"):
            ok &= check(f"{name} simulated", lib.execute_tool(name, {"x": 1}).startswith("Done (eval: not sent)"), True)
        import weather
        seen = {}
        orig = weather.get_weather
        weather.get_weather = lambda a: seen.update(a) or "sunny"
        try:
            lib.execute_tool("get_weather", {"place": "Spokane, WA", "remember": True})
        finally:
            weather.get_weather = orig
        ok &= check("get_weather never saves a home town on eval", "remember" in seen, False)
        ok &= check("... but still answers", seen.get("place"), "Spokane, WA")
        # the guard keys on the turn's source only
        io.source = None
        ok &= check("a person's turn is not simulated", lib.turn_source() == "eval", False)
    finally:
        io.source = None

    print("\n── skill_overrides: admin eval turns only ──")
    try:
        import agent_api_server as api
        import household_identity as hh
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] agent_api_server not importable here: {exc}")
        return 0 if ok else 1
    if not api._SKILLS:
        print("  [SKIP] no skills loaded")
        return 0 if ok else 1
    admin = hh.owner_identity()
    adult = hh.Identity({"id": "p_x", "name": "Robin", "role": "adult"}, None, "adult", "device")
    body = {"skill_overrides": {"calendar": CANDIDATE}}
    ok &= check("not an eval turn → ignored", api._parse_skill_overrides(body, None, admin), None)
    ok &= check("eval turn by a non-admin → ignored", api._parse_skill_overrides(body, "eval", adult), None)
    got = api._parse_skill_overrides(body, "eval", admin)
    ok &= check("admin eval turn → parsed", sorted(got or {}), ["calendar"])
    ok &= check("... with the candidate body", "A candidate rule" in got["calendar"].body, True)
    ok &= check("unknown skill → ignored",
                api._parse_skill_overrides({"skill_overrides": {"nope": CANDIDATE}}, "eval", admin), None)
    ok &= check("name mismatch → ignored",
                api._parse_skill_overrides({"skill_overrides": {"media": CANDIDATE}}, "eval", admin), None)
    ok &= check("too long → ignored",
                api._parse_skill_overrides({"skill_overrides": {"calendar": CANDIDATE + "x" * 9000}}, "eval", admin), None)
    ok &= check("not a SKILL.md → ignored",
                api._parse_skill_overrides({"skill_overrides": {"calendar": "just text"}}, "eval", admin), None)

    api._turns["t-skillopt"] = {"skill_overrides": got}
    try:
        swapped = {s.name: s for s in api._turn_skills("t-skillopt")}
        ok &= check("the turn sees the candidate", "A candidate rule" in swapped["calendar"].body, True)
        ok &= check("other skills unchanged", sorted(swapped) == sorted(s.name for s in api._SKILLS), True)
        ok &= check("other turns see the loaded skill",
                    "A candidate rule" in {s.name: s for s in api._turn_skills("other")}["calendar"].body, False)
    finally:
        api._turns.pop("t-skillopt", None)

    print("\n── the turn records which skill's full text it saw ──")
    saved_match, saved_key = api._skill_match, os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY") or "test-key"
    try:
        for label, picked, want in (("picked → body shown", (True, {"calendar"}), {"picked": ["calendar"], "body": True}),
                                    ("nothing picked → descriptions only", (False, None), {"picked": None, "body": False})):
            api._skill_match = lambda q, sk, _r=picked: _r
            api._turns["t-skills"] = {"identity": hh.owner_identity(), "source": "eval"}
            try:
                api._build_messages("What's on tomorrow?", [], True, None, turn_id="t-skills")
                ok &= check(label, api._turns["t-skills"].get("skills"), want)
            finally:
                api._turns.pop("t-skills", None)
        api._turns["t-kid"] = {"identity": hh.Identity({"id": "k", "name": "Kid", "role": "kid"}, None, "kid", "device"),
                               "source": "eval"}
        try:
            api._build_messages("What's on tomorrow?", [], True, None, turn_id="t-kid")
            ok &= check("a kid's turn gets no skills", api._turns["t-kid"].get("skills"), {"picked": None, "body": False})
        finally:
            api._turns.pop("t-kid", None)
    finally:
        api._skill_match = saved_match
        if saved_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = saved_key

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
