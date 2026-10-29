#!/usr/bin/env python3
"""
Tests for the cached prompt prefix and on-demand skill guides.

The first system message of a turn is the prefix llama-server's prompt cache (and Gemini's implicit
cache) reuses, so it must be the same for every turn of one shape: identity, tools, workspace and
the skill index, nothing that changes turn to turn. The picked skill's guide, memory, reply style,
voice, device policy and who is asking follow it. Any other skill's guide is read with skill_guide,
so the skills work without Gemini or Jev (a local brain alone). Each model call's fresh and cached
prompt tokens are summed into the turn log. Hermetic: nothing is sent anywhere.

Run: python3 scripts/test_prompt_prefix.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-prefix-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ.pop("GEMINI_API_KEY", None)   # the local brain alone: skills must still be there
sys.path.insert(0, os.path.dirname(__file__))

import agent_runtime_lib as lib


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def run():
    ok = True
    try:
        import agent_api_server as api
        import household_identity as hh
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] agent_api_server not importable here: {exc}")
        return 0
    if not api._SKILLS:
        print("  [SKIP] no skills loaded")
        return 0
    names = sorted(s.name for s in api._SKILLS)
    admin = hh.owner_identity()
    adult = hh.Identity({"id": "p_m", "name": "Robin", "role": "adult"}, None, "adult", "device")
    kid = hh.Identity({"id": "p_k", "name": "Emma", "role": "kid"}, None, "kid", "device")
    saved_match = api._skill_match

    def build(question, ident=admin, picked=(False, None), detail=None, persona=None, tid="t-prefix"):
        api._skill_match = lambda q, sk, _r=picked: _r
        api._turns[tid] = {"identity": ident, "source": "eval", **({"persona": persona} if persona else {})}
        lib._TURN_IO.role, lib._TURN_IO.person = ident.role, {"id": ident.person_id, "name": ident.person["name"]}
        try:
            msgs, _ = api._build_messages(question, [], True, None, turn_id=tid, detail=detail)
            return msgs, dict(api._turns[tid].get("skills") or {})
        finally:
            api._turns.pop(tid, None)
            api._skill_match = saved_match
            lib._TURN_IO.role = lib._TURN_IO.person = None

    try:
        print("\n── the first system message is the same for every turn of a shape ──")
        a, _ = build("hi")
        b, seen_b = build("What's on my calendar tomorrow?", picked=(True, {"calendar"}), detail="brief", persona="warm")
        c, _ = build("Play some jazz in the kitchen", picked=(True, {"media"}), detail="full")
        ok &= check("same prefix across questions, picked skills, style and voice",
                    (a[0]["content"] == b[0]["content"], a[0]["content"] == c[0]["content"]), (True, True))
        ok &= check("identity leads it", a[0]["content"].startswith("## Who you are"), True)
        ok &= check("every skill is listed in it, description only",
                    all(f"- **{n}**:" in a[0]["content"] for n in names) and "[SKILL GUIDE: " not in a[0]["content"], True)
        ok &= check("the prefix offers skill_guide", ("**skill_guide**" in a[0]["content"],
                                                    "call skill_guide with the skill's name" in a[0]["content"]), (True, True))
        per_turn = b[1]["content"]
        ok &= check("the picked guide, style, voice and who follow it",
                    ("[SKILL GUIDE: calendar]" in per_turn, "[REPLY STYLE: brief]" in per_turn, "[VOICE: warm]" in per_turn,
                     "[WHO IS ASKING] Zach (admin)." in per_turn), (True, True, True, True))
        cal = next(s for s in api._SKILLS if s.name == "calendar")
        ok &= check("… with the skill's whole body", cal.body[:200] in per_turn, True)
        ok &= check("… and only the picked one", "[SKILL GUIDE: media]" in per_turn, False)
        ok &= check("the turn records what it preloaded", seen_b, {"picked": ["calendar"], "body": True})

        print("\n── the local model's view: the format hint stays in the prefix ──")
        la, lb = lib._local_messages(a), lib._local_messages(b)
        ok &= check("same first message for the local model", la[0]["content"] == lb[0]["content"], True)
        ok &= check("… ending with the format hint", la[0]["content"].endswith(lib._LOCAL_FORMAT_HINT), True)

        print("\n── warm_local caches exactly the prefix real turns share, for every shape it warms ──")
        import warm_local as wl
        ok &= check("chat is warmed last (the slot #277 checks holds it)", wl.SHAPES[-1][0], "chat")
        seen_turn: dict = {}
        real_turn = api._lib._run_agent_turn

        def capture(messages, *a, **k):
            seen_turn["first"] = [m for m in messages if m.get("role") == "system"][:1]
            seen_turn["id"] = lib.prefix_id(messages)
            return "ok"
        api._lib._run_agent_turn = capture
        try:
            for label, client, device in wl.SHAPES:
                pid, warm_msgs = wl.shape_prefix(api, lib, client, device)
                tid = f"t-warm-{label}"
                api._init_turn(tid)
                api._turns[tid].update({"identity": admin, "client": client, "source": "eval"})
                api._run_turn(tid, "What's on my calendar tomorrow?", [{"role": "user", "content": "earlier"},
                                                                      {"role": "assistant", "content": "sure"}],
                              True, None, device=device, detail="full")
                api._turns.pop(tid, None)
                real_local = lib._local_messages(seen_turn["first"] + [{"role": "user", "content": "x"}])
                ok &= check(f"{label}: warm-up prefix == a real turn's, and the same prefix id",
                            (warm_msgs[0]["content"] == real_local[0]["content"], pid == seen_turn["id"]), (True, True))
            # A Mac that sends client "mac" (jellybean#392) is the Mac shape: the warmed prefix still serves it.
            mac_pid = wl.shape_prefix(api, lib, None, True)[0]
            api._init_turn("t-warm-macclient")
            api._turns["t-warm-macclient"].update({"identity": admin, "client": "mac", "source": "eval"})
            api._run_turn("t-warm-macclient", "What's on my calendar tomorrow?", [], True, None, device=True, detail="full")
            api._turns.pop("t-warm-macclient", None)
            ok &= check("a client \"mac\" turn has the warmed Mac prefix", seen_turn["id"], mac_pid)
            ok &= check("the shapes are distinct prefixes",
                        len({wl.shape_prefix(api, lib, c, d)[0] for _, c, d in wl.SHAPES}), len(wl.SHAPES))
            ok &= check("building a shape leaves no turn state behind",
                        (lib._TURN_IO.role, lib._TURN_IO.client, lib._TURN_IO.device, lib._TURN_IO.remote), (None,) * 4)
        finally:
            api._lib._run_agent_turn = real_turn

        print("\n── roles: skills for the admin and adults only ──")
        m_adult, _ = build("What's on tomorrow?", ident=adult)
        ok &= check("an adult's prefix lists the skills", "## Agent Skills" in m_adult[0]["content"], True)
        m_kid, seen_kid = build("What's on tomorrow?", ident=kid, picked=(True, {"calendar"}))
        kid_sys = "\n".join(m["content"] for m in m_kid if m["role"] == "system")
        ok &= check("a kid's turn: no skills, no guide, no skill_guide tool",
                    ("## Agent Skills" in kid_sys, "[SKILL GUIDE: " in kid_sys, "**skill_guide**" in kid_sys), (False, False, False))
        ok &= check("… and records none", seen_kid, {"picked": None, "body": False})

        print("\n── skill_guide ──")
        lib._TURN_INFO.info = {}
        lib._TURN_IO.skills = {s.name: s for s in api._SKILLS}
        lib._TURN_IO.role = "admin"
        try:
            out = lib.execute_tool("skill_guide", {"name": "Calendar"})
            ok &= check("returns the guide (name case-insensitive)", out.startswith("[SKILL GUIDE: calendar]"), True)
            lib.execute_tool("skill_guide", {"name": "calendar"})
            lib.execute_tool("skill_guide", {"name": "media"})
            ok &= check("names read are recorded once each", lib._TURN_INFO.info.get("skills_read"), ["calendar", "media"])
            miss = lib.execute_tool("skill_guide", {"name": "cooking"})
            ok &= check("an unknown name lists the skills", (miss.startswith("No skill named 'cooking'"),
                                                            all(n in miss for n in names)), (True, True))
            cand = api._parse_skill_text("---\nname: calendar\ndescription: Candidate.\n---\n\nA candidate rule.\n")
            lib._TURN_IO.skills = {**lib._TURN_IO.skills, "calendar": cand}
            ok &= check("an eval turn's candidate is what it reads",
                        "A candidate rule." in lib.execute_tool("skill_guide", {"name": "calendar"}), True)
            lib._TURN_IO.skills = None
            ok &= check("outside the API it reads scripts/skills",
                        lib.execute_tool("skill_guide", {"name": "calendar"}).startswith("[SKILL GUIDE: calendar]"), True)
            lib._TURN_IO.role = "kid"
            ok &= check("a kid is refused", lib.execute_tool("skill_guide", {"name": "calendar"}), lib._ROLE_REFUSAL)
        finally:
            lib._TURN_IO.skills = None
            lib._TURN_IO.role = None

        print("\n── prompt cache use, per turn ──")
        lib._TURN_INFO.info = {}
        lib._note_prompt_usage({"timings": {"prompt_n": 900, "cache_n": 7600, "prompt_ms": 4812.5}}, "local")
        lib._note_prompt_usage({"timings": {"prompt_n": 120, "cache_n": 8700, "prompt_ms": 640.0}}, "local")
        info = lib._TURN_INFO.info
        ok &= check("llama-server timings summed across the turn's calls",
                    (info.get("prompt_fresh"), info.get("prompt_cached"), info.get("prefill_ms")), (1020, 16300, 5452))
        lib._TURN_INFO.info = {}
        lib._note_prompt_usage({"usage": {"prompt_tokens": 500, "prompt_tokens_details": {"cached_tokens": 384}}}, "local")
        ok &= check("OpenAI-style usage when there are no timings",
                    (lib._TURN_INFO.info.get("prompt_fresh"), lib._TURN_INFO.info.get("prompt_cached")), (116, 384))
        lib._TURN_INFO.info = {}
        lib._note_prompt_usage({"usageMetadata": {"promptTokenCount": 6000, "cachedContentTokenCount": 4096}}, "gemini")
        lib._note_prompt_usage({"usageMetadata": {"promptTokenCount": 300}}, "gemini")
        ok &= check("Gemini: cached is part of the prompt count",
                    (lib._TURN_INFO.info.get("prompt_fresh"), lib._TURN_INFO.info.get("prompt_cached"),
                     "prefill_ms" in lib._TURN_INFO.info), (2204, 4096, False))
        lib._TURN_INFO.info = {}
        lib._note_prompt_usage({"choices": []}, "local")
        lib._note_prompt_usage("not a dict", "local")
        ok &= check("nothing reported → nothing recorded", lib._TURN_INFO.info, {})

        log = Path(os.environ["AGENT_TURN_LOG"])
        lib._TURN_INFO.info = {"brain": "local", "prompt_fresh": 900, "prompt_cached": 7600, "prefill_ms": 4812,
                               "skills_read": ["calendar"]}
        lib._TURN_INFO.route = {}
        lib._log_turn(0.0, 5, None)
        rec = json.loads(log.read_text().splitlines()[-1])
        ok &= check("the turn log carries them",
                    {k: rec.get(k) for k in ("prompt_fresh", "prompt_cached", "prefill_ms", "skills_read")},
                    {"prompt_fresh": 900, "prompt_cached": 7600, "prefill_ms": 4812, "skills_read": ["calendar"]})

        print("\n── each turn logs its prefix id and the asker's role ──")
        real_inner = lib._run_agent_turn_inner
        lib._run_agent_turn_inner = lambda messages, *a, **k: "A real answer for the test."
        lib._TURN_IO.role = "adult"
        try:
            lib._run_agent_turn(b)
            rec = json.loads(log.read_text().splitlines()[-1])
            ok &= check("prefix id and role in the log",
                        (rec.get("prefix"), rec.get("role")), (lib.prefix_id(b), "adult"))
            ok &= check("the id is the first system message's (per-turn parts don't change it)",
                        lib.prefix_id(b) == lib.prefix_id(c) and len(lib.prefix_id(b)) == 8, True)
        finally:
            lib._run_agent_turn_inner = real_inner
            lib._TURN_IO.role = None
    finally:
        lib._TURN_INFO.info = {}
        lib._TURN_INFO.route = {}

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
