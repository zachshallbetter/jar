#!/usr/bin/env python3
"""Household identity: pairing, device tokens, roles and per-person memory (household_identity.py,
agent_api_server.py, agent_runtime_lib's role policy, agent_memory's person_id).

Hermetic: every store is a temp file, Home Assistant and the model are fakes, nothing is
advertised on the network. Run: python3 scripts/test_household_identity.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-household-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             # Revoking a test device forgets its push token and place events: in the temp dir, never titan's.
             "PUSH_DB": "push.db", "PLACES_DB": "places.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha-token")
os.environ["TITAN_PAIR_URLS"] = "http://10.0.0.170:7770,http://100.77.69.85:7770,http://titan-node.ladon-betelgeuse.ts.net:7770"
os.environ.pop("TITAN_DISCOVERY", None)
os.environ.pop("GEMINI_API_KEY", None)   # no skills prompt, no hosted calls
sys.path.insert(0, str(Path(__file__).parent))

import agent_memory  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import home_assistant as ha  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")
    return good


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def set_role(role, person=None):
    lib._TURN_IO.role = role
    lib._TURN_IO.person = person


# ── the role table ────────────────────────────────────────────────────────────────────────────
print("── the role policy table ──")
every_tool = set(lib._TOOL_REGISTRY) | set(lib._DEVICE_TOOLS)
listed = set(hh.TOOL_ROLES) | set(hh.ADMIN_ONLY_TOOLS)
check("every registered tool is in the table (or deliberately admin-only)", sorted(every_tool - listed), [])
check("the table names no tool that doesn't exist", sorted(listed - every_tool), [])
check("no tool is both in the table and admin-only", sorted(set(hh.TOOL_ROLES) & hh.ADMIN_ONLY_TOOLS), [])
check("admin (and no role: CLI, background turns) may use anything",
      (hh.role_allows_tool("admin", "run_shell"), hh.role_allows_tool(None, "host_status")), (True, True))
check("an unlisted tool is admin-only", (hh.role_allows_tool("adult", "brand_new_tool"),
                                         hh.role_allows_tool("admin", "brand_new_tool")), (False, True))
for tool in ("send_message", "search_notes", "draft_email", "read_file", "write_file", "run_shell", "run_applescript",
             "run_skill", "create_skill", "file_read", "host_status", "run_check", "self_test", "kb_forget", "jev_decide",
             "get_screen"):
    check(f"kid and household can't use {tool}", (hh.role_allows_tool("kid", tool), hh.role_allows_tool("household", tool)),
          (False, False))
for tool in ("home_control", "home_status", "get_weather", "kb_retrieve", "list_routines", "list_calendar_events",
             "create_reminder", "notify_user"):
    check(f"every role may use {tool}", all(hh.role_allows_tool(r, tool) for r in hh.ROLES), True)
check("adult: messages, notes, files and shell on their own device",
      all(hh.role_allows_tool("adult", t) for t in ("send_message", "search_notes", "read_file", "run_shell")), True)
check("find_my_locate (where someone is): the admin and adults, never kids or household devices",
      tuple(hh.role_allows_tool(r, "find_my_locate") for r in ("admin", "adult", "kid", "household")), (True, True, False, False))
check("/v1/me's location capability follows the table",
      tuple(hh.capabilities(r)["location"] for r in ("admin", "adult", "kid", "household")), (True, True, False, False))
check("adult: not titan's own files or ops", any(hh.role_allows_tool("adult", t) for t in hh.ADMIN_ONLY_TOOLS), False)

# ── pairing, auth and people over the API ────────────────────────────────────────────────────
print("\n── API: pairing, /v1/me, people, revoke ──")
try:
    from fastapi.testclient import TestClient
    import agent_api_server as api
except Exception as exc:  # the API needs fastapi/httpx; fail loudly: this suite is about the API
    check(f"agent_api_server importable ({exc})", False, True)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1)

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
clock = Clock()
limiter_clock = Clock(1000.0)
store = hh.HouseholdStore(_TMP / "api-household.db", now=clock, limiter=hh.ClaimLimiter(now=limiter_clock))
hh.reset_store(store)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def tok(t):
    return {"X-Agent-Token": t}


check("Zach is seeded as admin", store.person(hh.ADMIN_PERSON_ID), {"id": "zach", "name": "Zach", "role": "admin"})
check("no token → 401", c.get("/v1/me").status_code, 401)
check("unknown token → 401", c.get("/v1/me", headers=tok("x" * 43)).status_code, 401)
r = c.get("/v1/me", headers=M).json()
check("master token = Zach, admin, no device", (r["person"]["name"], r["role"], r["device"], r["can"]["manage_household"]),
      ("Zach", "admin", None, True))
check("/v1/me names the household's admin (jellybean#438)", r["household"], {"admin_name": "Zach"})
check("/health stays public", c.get("/health").status_code, 200)

r = c.post("/v1/pair/start", json={"new_person": {"name": "Robin", "role": "adult"}, "label": "Robin's iPhone"}, headers=M)
start = r.json()
check("pair/start with a new person", (r.status_code, start["person"]["name"], start["person"]["role"],
                                       len(start["code"]), start["code"].isdigit()), (200, "Robin", "adult", 6, True))
check("the code expires in 10 minutes", start["expires_at"], hh._iso(clock.t + 600))
check("the QR carries the code, the name and the URLs in order",
      start["qr"], f"titan://pair?c={start['code']}&n=titan&u=http%3A%2F%2F10.0.0.170%3A7770"
                   "&u=http%3A%2F%2F100.77.69.85%3A7770&u=http%3A%2F%2Ftitan-node.ladon-betelgeuse.ts.net%3A7770")
robin_id = start["person"]["id"]
check("pair/start is admin-only (no token → 401)", c.post("/v1/pair/start", json={"person_id": robin_id}).status_code, 401)

r = c.post("/v1/pair/claim", json={"code": start["code"], "device_name": "Robin's iPhone", "platform": "ios"})
claim = r.json()
check("claim without auth → token, device, person, servers",
      (r.status_code, len(claim["token"]), claim["device"]["name"], claim["person"]["id"], claim["servers"][0]),
      (200, 43, "Robin's iPhone", robin_id, "http://10.0.0.170:7770"))
m_tok, m_dev = claim["token"], claim["device"]["id"]
raw = (_TMP / "api-household.db").read_bytes() + ((_TMP / "api-household.db-wal").read_bytes()
                                                  if (_TMP / "api-household.db-wal").exists() else b"")
check("titan stores the token's sha256, never the token", (m_tok.encode() in raw, hh.token_sha256(m_tok).encode() in raw),
      (False, True))
check("single use: the same code again → 410",
      c.post("/v1/pair/claim", json={"code": start["code"], "device_name": "x", "platform": "ios"}).status_code, 410)
me = c.get("/v1/me", headers=tok(m_tok)).json()
check("/v1/me with the device token", (me["person"]["name"], me["role"], me["device"]["id"], me["device"]["platform"]),
      ("Robin", "adult", m_dev, "ios"))
check("an adult can: messages, personal memory; can't: manage the household or ops",
      (me["can"]["messages"], me["can"]["personal_memory"], me["can"]["manage_household"], me["can"]["host_ops"]),
      (True, True, False, False))

check("claim: unknown code → 404", c.post("/v1/pair/claim", json={"code": "000000", "platform": "ios"}).status_code
      if start["code"] != "000000" else 404, 404)
check("claim: malformed code → 400", c.post("/v1/pair/claim", json={"code": "12ab"}).status_code, 400)
check("claim: bad platform → 400", c.post("/v1/pair/claim", json={"code": "123456", "platform": "watch"}).status_code, 400)
check("claim: not JSON → 400", c.post("/v1/pair/claim", content=b"nope").status_code, 400)

# A kid and a kitchen iPad.
limiter_clock.t += 61   # a new minute for the test client's claims (10 a minute per IP)
kid_start = c.post("/v1/pair/start", json={"new_person": {"name": "Emma", "role": "kid"}}, headers=M).json()
kid = c.post("/v1/pair/claim", json={"code": kid_start["code"], "device_name": "Emma's iPad", "platform": "ipados"}).json()
k_tok, emma_id = kid["token"], kid["person"]["id"]
hh_start = c.post("/v1/pair/start", json={"new_person": {"name": "Kitchen", "role": "household"}, "label": "Kitchen iPad"},
                  headers=M).json()
kitchen = c.post("/v1/pair/claim", json={"code": hh_start["code"], "platform": "ipados"}).json()
check("no device_name: the admin's label names the device", kitchen["device"]["name"], "Kitchen iPad")
h_tok = kitchen["token"]
kme = c.get("/v1/me", headers=tok(k_tok)).json()
check("kid's can: house yes, security/messages/files no, own memory yes",
      (kme["can"]["house_control"], kme["can"]["house_security"], kme["can"]["messages"], kme["can"]["files"],
       kme["can"]["personal_memory"]), (True, False, False, False, True))
check("every member (and the shared device) reads the household calendars (jellybean#206)",
      (isinstance(kme["settings"].get("household_calendars"), list),
       isinstance(c.get("/v1/me", headers=tok(h_tok)).json()["settings"].get("household_calendars"), list)), (True, True))
check("household's can: no personal memory, routines editable",
      (c.get("/v1/me", headers=tok(h_tok)).json()["can"]["personal_memory"],
       c.get("/v1/me", headers=tok(h_tok)).json()["can"]["routines_edit"]), (False, True))

print("\n── routes a non-admin token can't reach ──")
for method, path in (("GET", "/v1/people"), ("POST", "/v1/pair/start"), ("GET", "/v1/host/summary"),
                     ("POST", "/v1/workspaces"), ("POST", "/v1/tools/run_shell"), ("GET", "/v1/triggers"),
                     ("GET", "/v1/letter"), ("POST", "/v1/kb"), ("POST", "/v1/connectors/mail/items")):
    check(f"kid {method} {path} → 403", c.request(method, path, headers=tok(k_tok), json={}).status_code, 403)
check("adult can't list people either", c.get("/v1/people", headers=tok(m_tok)).status_code, 403)
check("…or read titan's host summary (network, boot units)", c.get("/v1/host/summary", headers=tok(m_tok)).status_code, 403)
_ppl = [{"id": "zach", "role": "admin", "facets": {"a": 1}, "places": ["gym"], "calendars_ignored": ["x"], "calendars": ["Z"]},
        {"id": "robin", "role": "adult", "facets": {"vendor:Avista": "bills"}, "places": ["work"], "calendars_ignored": ["y"],
         "calendars": ["M"], "devices": []},
        {"id": "ava", "role": "kid", "facets": {"cal:School": "school"}, "places": ["school"], "calendars_ignored": []}]
_seen = {p["id"]: p for p in api._people_for_admin(_ppl, "zach")}
check("the admin's list: another adult's facets, places and hidden calendars are theirs",
      [k in _seen["robin"] for k in ("facets", "places", "calendars_ignored", "calendars", "devices")],
      [False, False, False, True, True])
check("…the admin's own and a kid's stay", ("facets" in _seen["zach"], "places" in _seen["ava"], "facets" in _seen["ava"]),
      (True, True, True))
check("household can't read personal memory", c.get("/v1/memory/facts", headers=tok(h_tok)).status_code, 403)
check("kid can list routines", c.get("/v1/routines", headers=tok(k_tok)).status_code, 200)
check("kid can't edit routines; the kitchen can",
      (c.post("/v1/routines", headers=tok(k_tok), json={"name": "x", "steps": ["y"]}).status_code,
       c.post("/v1/routines", headers=tok(h_tok), json={"name": "Movie night", "steps": ["Dim the lights"]}).status_code),
      (403, 200))
check("the 403 says it plainly", c.get("/v1/people", headers=tok(k_tok)).json()["detail"], hh.REFUSAL)

print("\n── people ──")
people = c.get("/v1/people", headers=M).json()["people"]
check("people with their devices", [(p["name"], p["role"], [d["name"] for d in p["devices"]]) for p in people],
      [("Zach", "admin", []), ("Robin", "adult", ["Robin's iPhone"]), ("Emma", "kid", ["Emma's iPad"]),
       ("Kitchen", "household", ["Kitchen iPad"])])
dev = people[1]["devices"][0]
check("a device's fields", (sorted(dev), dev["platform"], dev["revoked"], dev["last_seen"] is not None),
      (["app_version", "created_at", "holds", "id", "last_seen", "name", "platform", "revoked", "room"], "ios", False, True))
check("POST /v1/people", (lambda r: (r.status_code, r.json()["role"]))(
    c.post("/v1/people", json={"name": "Grandma", "role": "adult"}, headers=M)), (201, "adult"))
check("same name again → 409", c.post("/v1/people", json={"name": "grandma", "role": "kid"}, headers=M).status_code, 409)
check("bad role → 400", c.post("/v1/people", json={"name": "Rex", "role": "dog"}, headers=M).status_code, 400)
check("PATCH renames", c.patch(f"/v1/people/{emma_id}", json={"name": "Emma Rose"}, headers=M).json()["name"], "Emma Rose")
check("a role change applies to the next request",
      (c.patch(f"/v1/people/{emma_id}", json={"role": "adult"}, headers=M).status_code,
       c.get("/v1/me", headers=tok(k_tok)).json()["role"],
       c.patch(f"/v1/people/{emma_id}", json={"role": "kid"}, headers=M).json()["role"]), (200, "adult", "kid"))
check("Zach's admin role can't change", c.patch("/v1/people/zach", json={"role": "adult"}, headers=M).status_code, 400)
check("PATCH unknown person → 404", c.patch("/v1/people/p_nope", json={"name": "X"}, headers=M).status_code, 404)

print("\n── revoke ──")
check("a device can't remove another device", c.delete(f"/v1/devices/{m_dev}", headers=tok(k_tok)).status_code, 403)
check("admin: unknown device → 404", c.delete("/v1/devices/d_nope", headers=M).status_code, 404)
extra = c.post("/v1/pair/start", json={"person_id": robin_id}, headers=M).json()
m2 = c.post("/v1/pair/claim", json={"code": extra["code"], "device_name": "Robin's Mac", "platform": "mac"}).json()
check("a device can leave the household itself", c.delete(f"/v1/devices/{m2['device']['id']}", headers=tok(m2["token"])).json(),
      {"id": m2["device"]["id"], "revoked": True})
check("… and its token is refused at once", c.get("/v1/me", headers=tok(m2["token"])).status_code, 401)
check("admin revokes Robin's iPhone", c.delete(f"/v1/devices/{m_dev}", headers=M).status_code, 200)
check("revoked → 401", c.get("/v1/me", headers=tok(m_tok)).status_code, 401)
check("people shows it revoked", [d["revoked"] for p in c.get("/v1/people", headers=M).json()["people"]
                                  if p["id"] == robin_id for d in p["devices"]], [True, True])
# Robin gets a fresh device for the rest of the tests.
limiter_clock.t += 61   # a new minute for the test client's claims (10 a minute per IP)
again = c.post("/v1/pair/start", json={"person_id": robin_id}, headers=M).json()
m_tok = c.post("/v1/pair/claim", json={"code": again["code"], "platform": "ios"}).json()["token"]

# ── codes: expiry, the open-code cap, rate limits ───────────────────────────────────────────
print("\n── codes: expiry, cap, rate limits ──")
lc = Clock(5000.0)
s2 = hh.HouseholdStore(_TMP / "codes.db", now=clock, limiter=hh.ClaimLimiter(now=lc))
p = s2.add_person("Sam", "kid")
code = s2.start_pairing(p["id"])["code"]
clock.t += 601
try:
    s2.claim({"code": code, "platform": "ios"}, "10.0.0.5")
    got = 200
except hh.PairingError as exc:
    got = exc.status
check("a code 10 minutes old → 410", got, 410)
codes = [s2.start_pairing(p["id"])["code"] for _ in range(hh.MAX_OPEN_CODES)]
try:
    s2.start_pairing(p["id"])
    got = 200
except hh.PairingError as exc:
    got = exc.status
check(f"at most {hh.MAX_OPEN_CODES} open codes (the next → 429)", got, 429)
check("codes are unique", len(set(codes)), hh.MAX_OPEN_CODES)


def claim_status(st, body, ip):
    try:
        st.claim(body, ip)
        return 200
    except hh.PairingError as exc:
        return exc.status


s3 = hh.HouseholdStore(_TMP / "limits.db", now=clock, limiter=hh.ClaimLimiter(now=lc))
p3 = s3.add_person("Sam", "kid")
valid = s3.start_pairing(p3["id"])["code"]
wrong = "999999" if valid != "999999" else "999998"
statuses = [claim_status(s3, {"code": wrong, "platform": "ios"}, "10.0.0.9") for _ in range(10)]
check("10 claims a minute from one IP get answers", statuses, [404] * 10)
check("the 11th → 429", claim_status(s3, {"code": valid, "platform": "ios"}, "10.0.0.9"), 429)
lc.t += 61
check("a minute later that IP may try again", claim_status(s3, {"code": wrong, "platform": "ios"}, "10.0.0.9"), 404)
# 11 failures so far; 19 more from other IPs reach 30 in the hour → lockout.
for i in range(19):
    claim_status(s3, {"code": wrong, "platform": "ios"}, f"10.0.1.{i}")
check("30 failures in an hour lock every claim (even a valid code, new IP) → 429",
      claim_status(s3, {"code": valid, "platform": "ios"}, "10.0.2.1"), 429)
lc.t += 14 * 60
check("still locked after 14 minutes", claim_status(s3, {"code": valid, "platform": "ios"}, "10.0.2.2"), 429)
lc.t += 61
check("unlocked after 15 minutes; the valid code works", claim_status(s3, {"code": valid, "platform": "ios"}, "10.0.2.3"), 200)
check("the API answers 429 when rate limited",
      (lambda st: [c.post("/v1/pair/claim", json={"code": wrong, "platform": "ios"}).status_code for _ in range(11)][-1])(None),
      429)

print("\n── last_seen at most once a minute ──")
s4 = hh.HouseholdStore(_TMP / "seen.db", now=clock)
p4 = s4.add_person("Ann", "adult")
t4 = s4.claim({"code": s4.start_pairing(p4["id"])["code"], "platform": "ios"}, "10.0.0.1")["token"]
seen = lambda: s4._db.execute("SELECT last_seen FROM devices").fetchone()[0]   # noqa: E731
first = seen()
clock.t += 30
s4.resolve_device(t4)
check("a request 30 s after pairing doesn't write", seen(), first)
clock.t += 31
s4.resolve_device(t4)
check("after a minute it does", seen(), clock.t)

# ── role policy in turns ───────────────────────────────────────────────────────────────────
print("\n── role policy: what a turn is shown and may run ──")
STATES = [
    {"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Kitchen Lights"}},
    {"entity_id": "climate.thermostat", "state": "heat", "attributes": {"friendly_name": "Thermostat", "temperature": 70,
     "current_temperature": 68, "hvac_modes": ["off", "heat"], "temperature_unit": "°F"}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
    {"entity_id": "camera.porch", "state": "idle", "attributes": {"friendly_name": "Porch Camera"}},
    {"entity_id": "fan.bedroom", "state": "off", "attributes": {"friendly_name": "Bedroom Fan"}},
    {"entity_id": "light.front_door", "state": "off", "attributes": {"friendly_name": "Front Door Light"}},
]
posts = []


def fake_ha(method, path, body):
    if method == "POST":
        posts.append(path)
    return STATES if path == "/api/states" else ("" if path == "/api/template" else [])


saved_kw = (dict(ha.home_status.__kwdefaults__), dict(ha.home_control.__kwdefaults__))
ha.home_status.__kwdefaults__["http"] = fake_ha
ha.home_control.__kwdefaults__["http"] = fake_ha
ha._AREAS["at"] = 0
try:
    set_role("kid", {"id": emma_id, "name": "Emma"})
    for tool, args in (("send_message", {"to": "555", "text": "hi"}), ("search_notes", {"query": "x"}),
                       ("run_shell", {"command": "ls"}), ("file_read", {"path": "/etc/hosts"}),
                       ("host_status", {}), ("kb_forget", {"record_id": "x"}), ("jev_decide", {})):
        check(f"kid: {tool} refused in plain words", lib.execute_tool(tool, args), "That's not something I can do for you.")
    check("kid: the front door lock is refused, nothing sent",
          (lib.execute_tool("home_control", {"device": "Front Door", "action": "off"}), posts), (hh.REFUSAL, []))
    check("kid: a fan isn't on a kid's list", lib.execute_tool("home_control", {"device": "Bedroom Fan", "action": "on"}),
          hh.REFUSAL)
    check("kid: lights are fine", lib.execute_tool("home_control", {"device": "Kitchen Lights", "action": "on"}),
          "Done: Kitchen Lights → turn on.")
    check("kid: 'door' matches the lock and a light; only the light is hers",
          lib.execute_tool("home_control", {"device": "door", "action": "on"}), "Done: Front Door Light → turn on.")
    check("kid: the thermostat within the safe range",
          lib.execute_tool("home_control", {"device": "Thermostat", "action": "set_temperature", "value": 72}).startswith(
              "Done: Thermostat → set temperature temperature 72"), True)
    check("kid: … and not outside it", lib.execute_tool("home_control", {"device": "Thermostat", "action": "set_temperature",
                                                                          "value": 95}), "95° is outside the safe range (50–85°); not changed.")
    status = lib.execute_tool("home_status", {})
    check("kid: home_status hides the lock and the camera", ("Front Door:" in status, "Porch Camera" in status,
                                                              "Kitchen Lights" in status), (False, False, True))
    prompt = lib.build_tool_system_prompt()
    check("kid's tool prompt: the house yes; messages, shell, host files, forgetting no",
          ("**home_control**" in prompt, "**get_weather**" in prompt, "**send_message**" in prompt,
           "**file_read**" in prompt, "**kb_forget**" in prompt, '"tool": "kb_forget"' in prompt,
           "- For host file edits" in prompt), (True, True, False, False, False, False, False))
    # lib._role_allows is the role table plus feature flags (the world model's `about` exists only while
    # AGENT_WORLD_MODEL is on), so this holds with the flag either way.
    check("every tool a kid may use is in her prompt",
          [n for n in lib._TOOL_REGISTRY if hh.role_allows_tool("kid", n) and lib._role_allows(n)
           and f"**{n}**" not in prompt], [])
    lib._TURN_IO.device = lambda req: {"result": "ok"}
    prompt = lib.build_tool_system_prompt()
    decls = {d["name"] for d in lib._gemini_function_declarations()}
    check("kid's device tools: reminders yes, messages/shell/screen no (prompt and Gemini)",
          ("**create_reminder**" in prompt, "**send_message**" in prompt, "**run_shell**" in prompt,
           "create_reminder" in decls, "send_message" in decls, "run_shell" in decls, "get_screen" in decls,
           "host_status" in decls), (True, False, False, True, False, False, False, False))
    check("kid: a device tool round-trips when allowed", lib.execute_tool("create_reminder", {"title": "Homework"}), "ok")
    check("kid: send_message never reaches the device", lib.execute_tool("send_message", {"to": "x", "text": "y"}), hh.REFUSAL)

    set_role("household", {"id": "p_kitchen", "name": "Kitchen"})
    check("household: contacts and notes refused", (lib.execute_tool("search_contacts", {"query": "a"}),
                                                    lib.execute_tool("search_notes", {"query": "a"})), (hh.REFUSAL, hh.REFUSAL))
    check("household: the lock is still hands-off", lib.execute_tool("home_control", {"device": "lock.front_door", "action": "off"}),
          "Front Door is a lock: titan doesn't change those yet. Use Home Assistant for it.")
    check("household: the fan is fine", lib.execute_tool("home_control", {"device": "Bedroom Fan", "action": "on"}),
          "Done: Bedroom Fan → turn on.")
    lib._TURN_IO.device = None

    set_role("adult", {"id": robin_id, "name": "Robin"})
    check("adult: titan's own files and ops refused", (lib.execute_tool("file_read", {"path": "/etc/hosts"}),
                                                       lib.execute_tool("host_status", {})), (hh.REFUSAL, hh.REFUSAL))
    check("adult: sees the lock's state", "Front Door: locked" in lib.execute_tool("home_status", {}), True)

    set_role(None)
    lib._TURN_IO.device = None
    check("admin's prompt is exactly the memoized one (the local model's cached prefix)",
          lib.build_tool_system_prompt() == api._TOOL_SYSTEM_PROMPT, True)
    set_role("admin")
    check("… with the role set too", lib.build_tool_system_prompt() == api._TOOL_SYSTEM_PROMPT, True)
finally:
    set_role(None)
    lib._TURN_IO.device = None
    ha.home_status.__kwdefaults__.update(saved_kw[0])
    ha.home_control.__kwdefaults__.update(saved_kw[1])

# ── memory per person ────────────────────────────────────────────────────────────────────────
print("\n── memory per person ──")
mem = agent_memory.get_memory()
mem.add_fact("chat", "My phone number is 555-0101.", person_id=agent_memory.OWNER)                                    # Zach (default)
mem.add_fact(f"chat:{robin_id}", "My phone number is 555-0199.", person_id=robin_id)
mem.add_fact(f"chat:{robin_id}", "Robin's favorite restaurant is Luigi's.", person_id=robin_id)
mem.add_fact("chat", "The router lives in the hall closet.", person_id=agent_memory.OWNER)
mem.add_fact(f"chat:{robin_id}", "The router lives in the hall closet.", person_id=robin_id)
check("the same number sentence with another value doesn't overwrite someone else's",
      (mem.get_facts(person_id="zach"), sorted(mem.get_facts(person_id=robin_id))),
      (["The router lives in the hall closet.", "My phone number is 555-0101."],
       ["My phone number is 555-0199.", "Robin's favorite restaurant is Luigi's.", "The router lives in the hall closet."]))
check("recall is the person's own", ("555-0199" in mem.recall("phone number", person_id=robin_id).prependContext,
                                     "555-0101" in mem.recall("phone number", person_id=robin_id).prependContext,
                                     "555-0101" in mem.recall("phone number", person_id=agent_memory.OWNER).prependContext,
                                     "555-0199" in mem.recall("phone number", person_id=agent_memory.OWNER).prependContext), (True, False, True, False))
check("a kid with no facts recalls nothing", mem.recall("phone number restaurant", person_id=emma_id).prependContext, "")
zach_fact = next(f for f in mem.list_facts(person_id="zach") if "555-0101" in f["text"])
check("nobody can pin, forget or restore another person's fact",
      (mem.set_pinned(zach_fact["id"], True, person_id=robin_id), mem.forget_fact(zach_fact["id"], person_id=robin_id)),
      (False, False))
check("forget and restore your own", (mem.forget_fact(zach_fact["id"], person_id=agent_memory.OWNER), mem.restore_fact(zach_fact["id"], person_id=robin_id),
                                      mem.restore_fact(zach_fact["id"], person_id=agent_memory.OWNER)), (True, False, True))

got = c.get("/v1/memory/facts?detail=1", headers=tok(m_tok)).json()["facts"]
check("GET /v1/memory/facts: Robin sees only hers", sorted(f["text"] for f in got),
      ["My phone number is 555-0199.", "Robin's favorite restaurant is Luigi's.", "The router lives in the hall closet."])
check("… the master token sees Zach's", sorted(c.get("/v1/memory/facts", headers=M).json()["facts"]),
      ["My phone number is 555-0101.", "The router lives in the hall closet."])
check("… the kid sees none of them", c.get("/v1/memory/facts", headers=tok(k_tok)).json()["facts"], [])
check("Robin can't forget Zach's fact over the API", c.delete(f"/v1/memory/facts/{zach_fact['id']}", headers=tok(m_tok)).status_code, 404)
check("the kid's recall route finds nothing of Robin's",
      "Luigi" in c.post("/v1/memory/recall", json={"query": "favorite restaurant"}, headers=tok(k_tok)).json()["prependContext"], False)
r = c.post("/v1/memory/facts", json={"text": "Emma's favorite color is teal."}, headers=tok(k_tok))
check("a kid's fact is hers, in her own conversation", (r.json()["conv_id"], mem.get_facts(person_id=emma_id)),
      (f"chat:{emma_id}", ["Emma's favorite color is teal."]))

print("\n── turns: who is asking, memory, the prompt ──")


def ident_for(t):
    return hh.resolve(t, MASTER)


kid_ident, hh_ident, m_ident = ident_for(k_tok), ident_for(h_tok), ident_for(m_tok)
for tid, ident in (("t-kid", kid_ident), ("t-house", hh_ident), ("t-adult", m_ident), ("t-zach", hh.owner_identity())):
    api._init_turn(tid)
    api._turns[tid]["identity"] = ident
msgs, _ = api._build_messages("what's my favorite restaurant and phone number", [], True, None, turn_id="t-adult")
sys_text = "\n".join(m["content"] for m in msgs if m["role"] == "system")
check("adult's turn: her memory, named in the prompt", ("Luigi" in sys_text, "555-0101" in sys_text,
                                                        "[WHO IS ASKING] Robin (adult) on" in sys_text), (True, False, True))
msgs, _ = api._build_messages("what's the favorite restaurant, phone number, router", [], True, None, turn_id="t-kid")
sys_text = "\n".join(m["content"] for m in msgs if m["role"] == "system")
check("kid's turn: no one else's memory, named, no shell in the prompt",
      ("Luigi" in sys_text, "555-01" in sys_text, "[WHO IS ASKING] Emma (kid)" in sys_text, "**run_shell**" in sys_text,
       hh.REFUSAL in sys_text), (False, False, True, False, True))
msgs, _ = api._build_messages("where is the router, phone number", [], True, None, turn_id="t-house")
sys_text = "\n".join(m["content"] for m in msgs if m["role"] == "system")
check("household turn: no personal memory at all", ("[MEMORY" in sys_text, "hall closet" in sys_text,
                                                     "shared household device" in sys_text), (False, False, True))
msgs, _ = api._build_messages("where is the router", [], True, None, turn_id="t-zach")
check("Zach's turn names him, after the cached prefix", ("[WHO IS ASKING] Zach (admin)." in msgs[1]["content"],
                                                        "[WHO IS ASKING]" in msgs[0]["content"]), (True, False))
msgs, _ = api._build_messages("hello", [], False, None)
check("no turn (CLI-style callers): Zach, identity still leads", (msgs[0]["content"].startswith("## Who you are"),
                                                                 "Zach (admin)" in msgs[1]["content"]), (True, True))

seen = {}
saves = []
orig = (api._lib._run_agent_turn, lib._maybe_save_fact)
try:
    def fake_turn(messages, *a, **k):
        seen["role"], seen["person"] = lib.turn_role(), dict(lib._TURN_IO.person or {})
        seen["system"] = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        return "Sure thing, happy to help with that one."
    api._lib._run_agent_turn = fake_turn
    lib._maybe_save_fact = lambda q, t, **k: saves.append(q)
    before_chat = mem.turn_count("chat")
    api._run_turn("t-kid", "remember that my bike is blue", [], True, None)
    check("a kid's turn runs as the kid", (seen["role"], seen["person"]["name"]), ("kid", "Emma Rose"))
    check("… recorded in her conversation, not Zach's; no family-KB save",
          (mem.turn_count(f"chat:{emma_id}"), mem.turn_count("chat") - before_chat, saves), (1, 0, []))
    check("the role is cleared after the turn", (getattr(lib._TURN_IO, "role", None), getattr(lib._TURN_IO, "person", None)),
          (None, None))
    api._run_turn("t-house", "remember the dog's name is Rex", [], True, None)
    check("household turn: nothing remembered anywhere",
          (mem.turn_count(f"chat:{hh_ident.person_id}"), mem.turn_count("chat") - before_chat, saves), (0, 0, []))
    api._run_turn("t-zach", "remember the trash goes out Tuesday", [], True, None)
    check("Zach's turn: his 'chat' conversation and the family-KB save",
          (mem.turn_count("chat") - before_chat, saves), (1, ["remember the trash goes out Tuesday"]))

    r = c.post("/v1/prompt", json={"prompt": "turn on the lights", "remember": False}, headers=tok(k_tok))
    tid = r.json()["turn_id"]
    check("/v1/prompt with a kid's token runs as the kid", (r.status_code, seen["role"], "Emma" in seen["system"]),
          (200, "kid", True))
    check("her turn is hers: the kitchen iPad can't poll it, she and the admin can",
          (c.get(f"/v1/turns/{tid}", headers=tok(h_tok)).status_code, c.get(f"/v1/turns/{tid}", headers=tok(k_tok)).status_code,
           c.get(f"/v1/turns/{tid}", headers=M).status_code), (404, 200, 200))
    r = c.post("/v1/prompt", json={"prompt": "hi", "workspace_id": "ws1", "remember": False}, headers=tok(m_tok))
    check("a non-admin turn never gets a workspace", "[WORKSPACE]" in seen["system"], False)
finally:
    api._lib._run_agent_turn, lib._maybe_save_fact = orig

# ── the fact migration ───────────────────────────────────────────────────────────────────────
print("\n── migration: existing facts become Zach's ──")
old_dir = _TMP / "old-memory"
old_dir.mkdir()
con = sqlite3.connect(old_dir / "titan_memory.db")
con.executescript("""
CREATE TABLE facts (id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, text TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1.0,
                    ts REAL NOT NULL, pinned INTEGER NOT NULL DEFAULT 0);
CREATE TABLE forgotten_facts (id TEXT PRIMARY KEY, conv_id TEXT, text TEXT NOT NULL, confidence REAL, ts REAL,
                              forgotten_at REAL NOT NULL);
INSERT INTO facts VALUES ('f1', 'chat', 'The vet is Dr. Patel.', 1.0, 1.0, 0), ('f2', 'ide-shared', 'Zach likes tea.', 1.0, 2.0, 1);
INSERT INTO forgotten_facts VALUES ('f3', 'chat', 'The old wifi is gone.', 1.0, 3.0, 4.0);
""")
con.commit()
con.close()
saved_paths = (agent_memory._MEMORY_DIR, agent_memory._DB_PATH, agent_memory._REFS_DIR, agent_memory._PERSONA_MD)
try:
    agent_memory._MEMORY_DIR, agent_memory._DB_PATH = old_dir, old_dir / "titan_memory.db"
    agent_memory._REFS_DIR, agent_memory._PERSONA_MD = old_dir / "refs", old_dir / "persona.md"
    m_old = agent_memory.AgentMemory()
    check("old facts are Zach's", sorted(f["text"] for f in m_old.list_facts(person_id=agent_memory.OWNER)), ["The vet is Dr. Patel.", "Zach likes tea."])
    check("… and nobody else's", m_old.list_facts(person_id=robin_id), [])
    check("his pin survives", [f["pinned"] for f in m_old.list_facts(person_id=agent_memory.OWNER) if f["id"] == "f2"], [True])
    check("a forgotten fact is his to restore", (m_old.restore_fact("f3", person_id=robin_id), m_old.restore_fact("f3", person_id=agent_memory.OWNER)),
          (False, True))
    m_again = agent_memory.AgentMemory()
    check("running the migration again changes nothing", len(m_again.list_facts(person_id=agent_memory.OWNER)), 3)
    unassigned = sqlite3.connect(old_dir / "titan_memory.db").execute(
        "SELECT COUNT(*) FROM facts WHERE person_id IS NULL").fetchone()[0]
    check("no fact is left without a person", unassigned, 0)
finally:
    (agent_memory._MEMORY_DIR, agent_memory._DB_PATH, agent_memory._REFS_DIR, agent_memory._PERSONA_MD) = saved_paths

# ── discovery ────────────────────────────────────────────────────────────────────────────────
print("\n── discovery (fake zeroconf; nothing is advertised) ──")
kw = hh.service_info_kwargs(7770, ["10.0.0.170"], "titan-node")
check("what titan advertises", (kw["type_"], kw["name"], kw["port"], kw["properties"], kw["server"], kw["addresses"]),
      ("_titan._tcp.local.", "titan._titan._tcp.local.", 7770, {"v": "1"}, "titan-node.local.", [bytes([10, 0, 0, 170])]))
events = []


class FakeZC:
    def __init__(self, interfaces=None):
        events.append(("open", tuple(interfaces or ())))

    def register_service(self, info):
        events.append(("register", info.kw["name"]))

    def unregister_service(self, info):
        events.append(("unregister", info.kw["name"]))

    def close(self):
        events.append(("close",))


class FakeInfo:
    def __init__(self, **kw):
        self.kw = kw


fake_mod = types.ModuleType("zeroconf")
fake_mod.Zeroconf, fake_mod.ServiceInfo = FakeZC, FakeInfo
saved_mod, saved_lan = sys.modules.get("zeroconf"), hh.lan_addresses
try:
    sys.modules["zeroconf"] = fake_mod
    hh.lan_addresses = lambda: ["10.0.0.170"]
    adv = hh.Advertiser(port=7770)
    import socket as _socket
    saved_host, saved_run, saved_env = _socket.gethostname, hh._run, os.environ.pop("TITAN_PAIR_URLS", None)
    _socket.gethostname = lambda: "titan-node"
    hh._run = lambda cmd: "100.64.0.9" if cmd[:2] == ["tailscale", "ip"] else '{"Self": {"DNSName": "titan.tail-example.ts.net."}}'
    hh._urls_cache["urls"] = None
    check("before it advertises: no .local name", hh.server_urls(),
          ["http://10.0.0.170:7770", "http://100.64.0.9:7770", "http://titan.tail-example.ts.net:7770"])
    status = adv.start()
    check("while advertising (jellybean#764): the name zeroconf published, after the LAN IPs, the rest unchanged",
          hh.server_urls(), ["http://10.0.0.170:7770", "http://titan-node.local:7770", "http://100.64.0.9:7770",
                             "http://titan.tail-example.ts.net:7770"])
    check("…and only that name: the one in the service's own record", hh.ADVERTISED_HOST,
          hh.service_info_kwargs(7770, ["10.0.0.170"], "titan-node")["server"].rstrip("."))
    adv.stop()
    check("stopped: the name goes", ("titan-node.local" in " ".join(hh.server_urls()), hh.ADVERTISED_HOST), (False, None))
    _socket.gethostname, hh._run = saved_host, saved_run
    if saved_env is not None:
        os.environ["TITAN_PAIR_URLS"] = saved_env
    hh._urls_cache["urls"] = None
    check("start registers on the LAN address; stop unregisters and closes",
          (status, events), ("advertising _titan._tcp.local. on 10.0.0.170:7770",
                             [("open", ("10.0.0.170",)), ("register", "titan._titan._tcp.local."),
                              ("unregister", "titan._titan._tcp.local."), ("close",)]))
    hh.lan_addresses = lambda: []
    check("no LAN address: nothing advertised", hh.Advertiser(port=7770).start(), "off: no LAN address to advertise on")
    sys.modules["zeroconf"] = None   # import fails
    check("without python-zeroconf: says what's needed",
          hh.Advertiser(port=7770).start().startswith("unavailable: python-zeroconf is not installed"), True)
finally:
    hh.lan_addresses = saved_lan
    if saved_mod is None:
        sys.modules.pop("zeroconf", None)
    else:
        sys.modules["zeroconf"] = saved_mod
with TestClient(api.app) as c2:
    check("the app never advertises when imported (tests, TITAN_DISCOVERY unset)",
          (c2.get("/health").status_code, api._advertiser), (200, None))
os.environ["TITAN_PAIR_URLS"] = " http://a:1/ , http://b:2 "
check("TITAN_PAIR_URLS overrides the computed URLs", hh.server_urls(), ["http://a:1", "http://b:2"])

print("\n── household facts: birth year, contact card, calendars ──")
fx = hh.HouseholdStore(_TMP / "facts.db")
leo = fx.add_person("Leo", "kid")
rob = fx.add_person("Robin", "adult")
p = fx.update_person(leo["id"], birth_year=2020, calendars=["Leo", "Skate club"])
check("birth year and calendars saved; age derived", (p["birth_year"], p["age"] >= 5, p["calendars"]), (2020, True, ["Leo", "Skate club"]))
check("name and role unchanged when only facts change", (p["name"], p["role"]), ("Leo", "kid"))
p = fx.update_person(rob["id"], contact={"id": "ABC-123:ABPerson", "name": "Robin Hale"}, calendars=["Robin", " robin "])
check("contact saved; duplicate calendar titles folded", (p["contact"], p["calendars"]),
      ({"id": "ABC-123:ABPerson", "name": "Robin Hale"}, ["Robin"]))
check("a calendar belongs to its person (case-insensitive)", (fx.person_for_calendar("robin")["id"], fx.person_for_calendar("SKATE CLUB")["name"]),
      (rob["id"], "Leo"))
check("an unclaimed calendar → None", fx.person_for_calendar("Family"), None)
try:
    fx.update_person(leo["id"], calendars=["Robin"])
    check("a calendar can't be two people's", False, True)
except hh.PairingError as e:
    check("a calendar can't be two people's", (e.status, "already Robin's" in str(e)), (409, True))
for bad in (1850, 3000, "soon"):
    try:
        fx.update_person(leo["id"], birth_year=bad)
        check(f"birth_year {bad!r} refused", False, True)
    except hh.PairingError as e:
        check(f"birth_year {bad!r} refused", e.status, 400)
zz = fx.add_person("Zeke", "adult")
p = fx.update_person(leo["id"], parents=[rob["id"], zz["id"], rob["id"]])
check("parents saved, duplicates folded", p["parents"], [rob["id"], zz["id"]])
check("parents_of reads them back", fx.parents_of(leo["id"]), [rob["id"], zz["id"]])
for bad, why in (([leo["id"]], "own parent"), (["p_nobody"], "no such person")):
    try:
        fx.update_person(leo["id"], parents=bad)
        check(f"parents {why} refused", False, True)
    except hh.PairingError as e:
        check(f"parents {why} refused", e.status in (400, 404), True)
kid2 = fx.add_person("Kit", "kid")
try:
    fx.update_person(leo["id"], parents=[kid2["id"]])
    check("a kid can't be a parent", False, True)
except hh.PairingError as e:
    check("a kid can't be a parent", e.status, 400)
check("null clears parents", fx.update_person(leo["id"], parents=None)["parents"], [])
p = fx.update_person(rob["id"], contact=None)
check("null clears a fact, others stay", (p["contact"], p["calendars"]), (None, ["Robin"]))
check("people() carries the facts", [(x["name"], x["age"] is not None, x["calendars"]) for x in fx.people() if x["name"] == "Leo"],
      [("Leo", True, ["Leo", "Skate club"])])
check("identity's person stays {id, name, role}", sorted(fx.person(leo["id"])), ["id", "name", "role"])
r = c.patch(f"/v1/people/{robin_id}", json={"birth_year": 1990, "calendars": ["Robin"], "contact": {"id": "X1", "name": "Robin"}}, headers=M)
check("PATCH /v1/people sets facts", (r.status_code, r.json()["birth_year"], r.json()["calendars"], r.json()["contact"]["id"]),
      (200, 1990, ["Robin"], "X1"))
check("GET /v1/people returns them", [x["birth_year"] for x in c.get("/v1/people", headers=M).json()["people"] if x["id"] == robin_id], [1990])
check("PATCH facts is admin-only", c.patch(f"/v1/people/{robin_id}", json={"birth_year": 1991}, headers=tok(m_tok)).status_code, 403)
check("a bad fact → 400", c.patch(f"/v1/people/{robin_id}", json={"calendars": "Robin"}, headers=M).status_code, 400)

print("\n── preferences: asking first and offers ──")
ps = hh.HouseholdStore(_TMP / "prefs.db")
hh.reset_store(ps)
check("defaults: off and off", ps.prefs("zach"), {"ask_first": "off", "offers": "off", "live_activity_detail": "off"})
check("set one", ps.set_pref("zach", "offers", "on"), {"ask_first": "off", "offers": "on", "live_activity_detail": "off"})
for bad in (("zach", "tone", "on"), ("zach", "offers", "maybe"), ("nobody", "offers", "on")):
    try:
        ps.set_pref(*bad)
        check(f"refused {bad}", False, True)
    except hh.PairingError as e:
        check(f"refused {bad}", e.status in (400, 404), True)
os.environ["AGENT_TURN_SOURCE"] = "user"
set_role("adult", {"id": "zach", "name": "Zach"})
r = lib.execute_tool("set_preference", {"name": "ask_first", "value": "yes"})
check("the tool saves the asker's own", (r.startswith("Saved: ask_first is on for Zach"), ps.prefs("zach")["ask_first"]), (True, "on"))
set_role("household", None)
check("a household device has none", lib.execute_tool("set_preference", {"name": "offers", "value": "off"}).startswith("A shared household device"), True)
set_role(None, None)
check("the prompt says act, don't offer", ("Acting, not asking" in lib.AGENT_IDENTITY, "call set_preference" in lib.AGENT_IDENTITY), (True, True))
check("a 'never ask' note points to set_preference", "set_preference" in lib.execute_tool("ingest_text", {"title": "Rule", "content": "Never ask for permission before acting."}), True)
who = api._who_is_asking(hh.owner_identity())
check("[PREFERENCES] lists what's on", "[PREFERENCES] ask_first: ask in words" in who and "offers:" in who, True)
ps.set_pref("zach", "ask_first", "off"); ps.set_pref("zach", "offers", "off")
check("…and the defaults when nothing is", "[PREFERENCES] defaults: do clear requests without asking first" in api._who_is_asking(hh.owner_identity()), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
