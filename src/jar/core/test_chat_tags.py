#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Chat @-tags (jellybean#715): a leading @code / @home / @research pins the turn's tools strictly (never a tool the
role lacks), its skill and note, and @research's deeper turn; anything else is plain text; GET /v1/me lists only the
tags the caller's role can use today. Fictional household.
Run: python3 scripts/test_chat_tags.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-chattags-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "PUSH_DB": "push.db", "KB_ACL_DB": "kb_acl.db", "PROACTIVE_DB": "proactive.db", "SITTER_DB": "sitter.db",
             "TV_UNLOCK_DB": "tv_unlock.db", "HANDOFF_DB": "handoff.db", "PLACES_DB": "places.db",
             "CONTINUITY_DB": "continuity.db", "LOCK_UP_DB": "lock_up.db", "TODOS_DB": "todos.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import chat_tags as T  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── parsing ──")
check("a leading tag, any case, punctuation after it",
      [T.parse(x) for x in ("@home turn off the porch light", "  @Research: best e-bikes under $2k", "@code", "@HOME, lights")],
      [("home", "turn off the porch light"), ("research", "best e-bikes under $2k"), ("code", ""), ("home", "lights")])
check("anything else is plain text", [T.parse(x)[0] for x in ("ask @home later", "@homework is due", "@robin hi",
                                                               "email me@home.example", "@codes please")],
      [None, None, None, None, None])

print("\n── a tag never grants a tool ──")
for role in ("admin", "adult", "kid", "household"):
    want = [t for t, d in T.TAGS.items() if role == "admin" or hh.role_allows_tool(role, d["needs"])]
    check(f"{role}: only the tags whose main tool the role has today", [x["tag"] for x in T.available(role)], want)
check("…which today means", {r: [x["tag"] for x in T.available(r)] for r in ("admin", "adult", "kid", "household")},
      {"admin": ["code", "home", "research"], "adult": ["code", "home", "research"],
       "kid": ["home", "research"] if hh.role_allows_tool("kid", "web_search") else ["home"],
       "household": [t for t in ("home", "research") if hh.role_allows_tool("household", T.TAGS[t]["needs"])]})
check("@code says where it works", next(x for x in T.available("adult") if x["tag"] == "code").get("where"), "mac")
core = {"get_datetime", "kb_retrieve"}
check("a kid's @code: refused", T.tools_for("code", "kid", core), None)
kid_research = T.tools_for("research", "kid", core) or set()
check("a kid's @research never gets a tool their role lacks (fetch_url)",
      ("fetch_url" in kid_research) == hh.role_allows_tool("kid", "fetch_url"), True)
home = T.tools_for("home", "adult", core)
check("strict: @home is its tools and the core, nothing else", sorted(home),
      sorted({"get_datetime", "kb_retrieve", "home_status", "home_control", "home_why", "lock_up"} & (home or set())))
check("…never the shell or run_agent", ("run_shell" in home, "run_agent" in home), (False, False))

print("\n── in a turn ──")
import agent_runtime_lib as lib  # noqa: E402
lib._TURN_IO.role = "adult"
lib._TURN_INFO.info = {}
msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "[MEMORY]\nx\n\n@research which e-bike?"}]
got = lib._apply_chat_tag(msgs, None, 5)
check("the tag leaves the model's text (after the memory block)", got["messages"][-1]["content"], "[MEMORY]\nx\n\nwhich e-bike?")
check("…a note (with the web-research guide) is added before the conversation",
      (got["messages"][1]["role"], "addressed @research" in got["messages"][1]["content"],
       "web-research" in (lib._TURN_INFO.info.get("skills_read") or [])), ("system", True, True))
check("@research: 8 rounds, effort high, its tools", (got["rounds"], lib._TURN_IO.effort, "web_search" in got["allowed"],
                                                      "home_control" in got["allowed"]), (8, "high", True, False))
check("the turn log keeps the tag's name", lib._TURN_INFO.info.get("tag"), "research")
lib._TURN_IO.effort = None
check("an untagged message: nothing changes", lib._apply_chat_tag([{"role": "user", "content": "lights off"}], None, 5), None)
lib._TURN_IO.role = "kid"
called = []
real = lib._run_gemini_agent
lib._run_gemini_agent = lambda *a, **k: called.append(1) or "model"
try:
    out = lib._run_agent_turn_inner([{"role": "user", "content": "@code fix the build"}])
finally:
    lib._run_gemini_agent = real
check("a kid's @code: the refusal, and no model call", (out, called), (lib._ROLE_REFUSAL, []))
lib._TURN_IO.role = None
check("the turn log's fields include tag", '"tag"' in Path(lib.__file__).read_text().split("def _log_turn", 1)[1][:4000]
      or "\"skills_read\", \"tag\"" in Path(lib.__file__).read_text(), True)

print("\n── GET /v1/me ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    return {"X-Agent-Token": c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device,
                                                             "platform": platform}).json()["token"]}


for name, role, dev, plat in (("Robin", "adult", "Robin's iPhone", "ios"), ("Ava", "kid", "Ava's iPhone", "ios"),
                              ("Den", "household", "Den TV", "tvos")):
    me = c.get("/v1/me", headers=pair(name, role, dev, plat)).json()
    check(f"{role}: /v1/me tags are the role's", [x["tag"] for x in me.get("tags", [])], [x["tag"] for x in T.available(role)])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
