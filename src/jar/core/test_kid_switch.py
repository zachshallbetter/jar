#!/usr/bin/env python3
# gate: hermetic   (a fake Home Assistant; no network, no stores)
"""Security-shaped devices in harmless domains, and a Watch token's own tool list (10-07 review W3). A camera's privacy
switch, a motion detector's enable switch, a garage door wired as a switch: a kid or a shared household screen never
sees or changes them, by a word of the name or a named list (HA_RESTRICTED_ENTITIES), whatever the domain; an adult
still can. And a Watch token (via "watch") runs only household_identity.WATCH_TOOLS, enforced where every tool call
runs, not only in the prompt. Fictional devices.
Run: python3 scripts/test_kid_switch.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ["HA_TOKEN_FILE"] = str(Path(tempfile.mkdtemp(prefix="titan-kidswitch-")) / "none")
os.environ["HA_RESTRICTED_ENTITIES"] = "switch.relay_3,switch.garage_opener"
os.environ["HA_KID_ALLOWED_ENTITIES"] = "light.garage_light,switch.garage_opener"
import home_assistant as ha  # noqa: E402
import household_identity as hi  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def st(eid, name, state="off"):
    return {"entity_id": eid, "state": state, "attributes": {"friendly_name": name}}


STATES = [st("switch.porch", "Porch Light"), st("light.indoor_lamp", "Indoor Lamp"),
          st("switch.front_cam_privacy", "Front Camera Privacy", "on"), st("switch.yard_motion", "Yard Motion Detection", "on"),
          st("switch.garage_opener", "Garage Opener"), st("switch.relay_3", "Relay 3"),
          st("switch.side_gates", "Side Gates"), st("light.doorbell_ring", "Doorbell Ring Light"),
          st("light.garage_light", "Garage Light")]
posts = []


def fake(method, path, body):
    if path == "/api/states":
        return STATES
    if path == "/api/template":
        return ""
    posts.append((path, body))
    return []


ha._AREAS["at"] = 0
NO = "That's not something I can do for you."

print("── which devices are security-shaped ──")
by = {s["entity_id"]: ha.sensitive(s) for s in STATES}
check("by a word of the name or id; the named list; plurals; never a word inside a word (indoor); a light only lights", by, {
      "switch.porch": False, "light.indoor_lamp": False, "switch.front_cam_privacy": True, "switch.yard_motion": True,
      "switch.garage_opener": True, "switch.relay_3": True, "switch.side_gates": True, "light.doorbell_ring": False,
      "light.garage_light": False})
check("…the allow list let the harmless Garage Light back in; it never beats the restricted list (the opener stays out)",
      (by["light.garage_light"], by["switch.garage_opener"]), (False, True))
posts.clear()
out = ha.home_control({"device": "Garage Light", "action": "on"}, http=fake, domains=hi.home_domains_for("kid"), refusal=NO)
check("…so a kid can turn the Garage Light on", (out.startswith("Done"), len(posts)), (True, 1))

print("── a kid ──")
for dev in ("Front Camera Privacy", "Yard Motion Detection", "Garage Opener", "Relay 3", "switch.side_gates"):
    posts.clear()
    out = ha.home_control({"device": dev, "action": "off"}, http=fake, domains=hi.home_domains_for("kid"), refusal=NO)
    check(f"'{dev}' off: refused with the plain refusal, nothing sent", (out, posts), (NO, []))
posts.clear()
out = ha.home_control({"device": "Porch Light", "action": "on"}, http=fake, domains=hi.home_domains_for("kid"), refusal=NO)
check("…the porch light still works", (out.startswith("Done"), len(posts)), (True, 1))
out = ha.home_control({"device": "front", "action": "off"}, http=fake, domains=hi.home_domains_for("kid"), refusal=NO)
check("a vague name never offers the camera switch among the choices", "Privacy" in out, False)
status = ha.home_status({}, http=fake, hide_security=True)
check("home_status hides them all", [n for n in ("Privacy", "Motion", "Garage Opener", "Relay 3", "Gates") if n in status], [])
check("…and still lists the porch light", "Porch Light" in status, True)

print("── a shared household screen (all domains, restricted) ──")
posts.clear()
out = ha.home_control({"device": "Garage Opener", "action": "on"}, http=fake, refusal=NO, restricted=True)
check("the garage-as-switch: refused, nothing sent", (out, posts), (NO, []))

print("── an adult ──")
posts.clear()
out = ha.home_control({"device": "Front Camera Privacy", "action": "off"}, http=fake, refusal=NO)
check("an adult can still change the camera's privacy switch", (out.startswith("Done"), len(posts)), (True, 1))
check("…and sees it in home_status", "Front Camera Privacy" in ha.home_status({}, http=fake), True)

print("── a Watch token's own tool list ──")
check("every Watch tool is one a kid may run anyway (the list only narrows)",
      sorted(t for t in hi.WATCH_TOOLS if not hi.role_allows_tool("kid", t)), [])
import agent_runtime_lib as lib  # noqa: E402
io = lib._TURN_IO
try:
    io.role, io.via = "kid", "watch"
    check("on the Watch: home_control yes; web_search, view_image, schedule_action, search_contacts no",
          [lib._role_allows(t) for t in ("home_control", "web_search", "view_image", "schedule_action", "search_contacts")],
          [True, False, False, False, False])
    check("…and running one anyway is refused where every call runs", lib._execute_tool("web_search", {"query": "x"}),
          lib._ROLE_REFUSAL)
    check("…what's advertised is within the Watch list", sorted(set(lib._turn_tool_specs()) - hi.WATCH_TOOLS), [])
    io.via = "device"
    check("the same kid on an iPhone keeps the kid list", lib._role_allows("web_search"), True)
finally:
    io.role = io.via = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
