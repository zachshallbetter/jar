#!/usr/bin/env python3
# gate: hermetic
"""One decision for titan's own tools (#1628 slice 3, governance.py): a tool titan doesn't know is refused; the kill
switch turns everything off but talking; a switched-off domain and a person's own Never refuse with a message that
says which; device tools pass through to the Mac's own gate; the per-turn check (agent_runtime_lib._role_allows) and
tool execution both use it; delete-my-data covers the person's tiers. Fictional people (Alex, Robin); temporary stores.
Run: python3 scripts/test_governance.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-governance-test-"))
os.environ["SPACES_DB"] = str(_TMP / "spaces.db")
os.environ["CAPABILITY_SWITCHES"] = str(_TMP / "switches.json")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import governance as G  # noqa: E402
import me_delete  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}" + ("" if good else f": got={got!r:.300} expected={expected!r:.300}"))


def switch(**kw):
    p = Path(os.environ["CAPABILITY_SWITCHES"])
    p.write_text(json.dumps(kw))
    t = time.time() + len(json.dumps(kw)) % 7 + 1   # a distinct mtime per write, so the cache sees each change
    os.utime(p, (t, t))


def as_person(pid, role="adult", via=None):
    lib._TURN_IO.person = {"id": pid, "name": pid}
    lib._TURN_IO.role = role
    lib._TURN_IO.via = via
    lib._TURN_IO.capability_tiers = None


print("── decide ──")
check("nothing switched off: a titan tool runs", G.decide("web_search").run, True)
check("a device tool passes through to the Mac's gate", G.decide("run_shell", device_tools={"run_shell"}).run, True)
unknown = G.decide("mystery_tool")
check("a tool titan doesn't know is refused, not asked about", (unknown.run, unknown.why), (False, "no_descriptor"))
check("a person's Never refuses that domain only",
      [G.decide(t, person_tiers={"web": "never"}).run for t in ("web_search", "kb_retrieve")], [False, True])
check("the person's refusal says what to do", "turn it back on in Settings" in G.decide("fetch_url", person_tiers={"web": "never"}).message, True)

print("── kill switches ──")
switch(off=["mail", "not_a_domain"])
check("a switched-off domain is refused", (G.decide("search_mail").run, G.decide("search_mail").why), (False, "domain_off"))
check("other domains still run", G.decide("kb_retrieve").run, True)
check("unknown domain names in the file are ignored", sorted(G.switches()["off"]), ["mail"])
switch(all=True)
check("everything off: only the assistant's own tools run",
      [G.decide(t).run for t in ("get_datetime", "offer_choices", "kb_retrieve", "home_control", "host_status")],
      [True, True, False, False, False])
check("everything off says so", "switched off" in G.decide("kb_retrieve").message, True)
Path(os.environ["CAPABILITY_SWITCHES"]).write_text("{not json")
os.utime(os.environ["CAPABILITY_SWITCHES"], (time.time() + 30, time.time() + 30))
check("an unreadable switches file switches nothing off", G.decide("kb_retrieve").run, True)
os.remove(os.environ["CAPABILITY_SWITCHES"])
check("no switches file switches nothing off", G.switches(), {"all": False, "off": frozenset()})

print("── a person's own tiers ──")
check("none set: empty", G.tiers("p_alex"), {})
check("set Never", G.set_tier("p_alex", "web", "never"), {"web": "never"})
check("another person is untouched", G.tiers("p_robin"), {})
check("Always removes the row (on is the default)", G.set_tier("p_alex", "web", "always"), {})
for bad in (("p_alex", "nope", "never"), ("p_alex", "web", "askAlways")):
    try:
        G.set_tier(*bad)
        check(f"refuses {bad[1:]}", False, True)
    except ValueError:
        check(f"refuses {bad[1:]}", True, True)

print("── in a turn ──")
G.set_tier("p_robin", "web", "never")
as_person("p_robin")
check("Robin's Never: web tools aren't offered", [lib._role_allows(t) for t in ("web_search", "fetch_url", "kb_retrieve")],
      [False, False, True])
said = lib._execute_tool("web_search", {"query": "weather"})
check("calling one anyway is refused with the reason", said.startswith("Not run:") and "turned off" in said, True)
check("an unknown tool isn't offered, even to the admin", (as_person("p_alex", "admin"), lib._role_allows("mystery_tool"))[1], False)
check("…and calling it says so", lib._execute_tool("mystery_tool", {}).startswith("Not run: mystery_tool"), True)
as_person("p_alex", "admin")
check("Alex (nothing off) still gets web search", lib._role_allows("web_search"), True)
check("a device tool still reaches the Mac's gate", lib._role_allows("run_shell"), True)
as_person("p_robin")
G.set_tier("p_robin", "web", "always")
lib._TURN_IO.capability_tiers = ("p_robin", time.monotonic() - 11, {"web": "never"})   # an old cached read
check("a change shows once the 10 s cache is stale", lib._role_allows("web_search"), True)

print("── delete-my-data ──")
stores = me_delete.Parts(mem=None, spaces_db=_TMP / "spaces.db")._stores()
check("delete-my-data covers the person's tiers", [(p, t, c) for p, t, _, c in stores if t == "capability_tiers"],
      [("capabilities", "capability_tiers", "person")])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
