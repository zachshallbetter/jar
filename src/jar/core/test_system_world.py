#!/usr/bin/env python3
# gate: hermetic
"""The system bubble from health checks (#355): titan's services as claims computed in code (never notes), readable
by the admin and engineering agents only. Fake probes and a temporary WORLD_DIR; never titan's.
Run: python3 scripts/test_system_world.py"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-sysworld-test-"))
for k, v in {"WORLD_DIR": "world", "WORLD_DB": "world.db", "HOUSEHOLD_DB": "household.db", "FENCES_DB": "fences.db",
             "KB_ACL_DB": "kb_acl.db", "AGENT_MEMORY_DIR": "memory", "HOUSEHOLD_PREFS": "household.json"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import system_world as SW  # noqa: E402
import world  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


inv = SW.inventory()
names = [s["name"] for s in inv]
print("── the inventory: the registry plus titan's own services ──")
check("every runtime and broker in the registry, plus the agent API and model servers",
      ("fetch-runtime" in names, "decision-broker" in names, "agent-api" in names, "code-model" in names), (True,) * 4)
check("no duplicates", len(names), len(set(names)))

DOWN = {"decision-broker"}
health = lambda url: "down" if any(f":{s['port']}/" in (url or "") for s in inv if s["name"] in DOWN) else "up"  # noqa: E731
unit = lambda u, verb: ("inactive" if u in DOWN else "active") if verb == "is-active" else "enabled"  # noqa: E731
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
obs = SW.observations(inv, health, unit, T0)
check("six claims a service (port, health check, group, status, unit active, unit enabled)", len(obs), 6 * len(inv))
check("all checked in code: evidence kind health, origin code:health, audience system, the system bubble",
      {(o.kind, o.origin, o.audience, o.bubble) for o in obs}, {("health", "code:health", "system", "system")})

print("── written to the system bubble ──")
w = world.get()
r = w.sync(SW.SOURCE, obs)
check("synced", r["observations"], len(obs))
sysw = w.world("system")
ents = sysw.con.execute("SELECT COUNT(*) FROM entities WHERE type='service'").fetchone()[0]
check("one service entity each, nothing in the household bubble", (ents, w.world("household").con.execute(
      "SELECT COUNT(*) FROM entities WHERE type='service'").fetchone()[0]), (len(inv), 0))
check("no claim rests on a note", sysw.con.execute("SELECT COUNT(*) FROM evidence WHERE kind NOT IN ('health')").fetchone()[0], 0)

print("── answers, as the admin ──")
blk, ids = world.context(w, "What port does the fetch runtime use?", "zach", "admin")
check("the fetch runtime's port, from the claim", ("8300" in blk, any("svc-fetch-runtime" in i for i in ids)), (True, True))
blk, _ = world.context(w, "Is the decision broker up?", "zach", "admin")
check("a service the probe found down says down", "down" in blk.lower(), True)

check("household words never pull a service into an admin's turn ('fetch', 'record', 'event', 'text', 'signal')",
      [world.context(w, q, "zach", "admin")[1] for q in ("Can you fetch Leo from school?", "Record that the plumber came",
                                                          "Is there an event this weekend?", "Text Robin I'm late",
                                                          "The signal is weak in the kitchen")], [[], [], [], [], []])

print("── who reads it ──")
for pid, role, want in (("zach", "admin", True), ("eng", "engineering", True), ("robin", "adult", False),
                        ("ava", "kid", False), ("household", "household", False)):
    ids = w.find("fetch runtime", pid, role)
    check(f"{role}: {'sees' if want else 'never sees'} a system service", bool(ids), want)
    blk, _ = world.context(w, "What port does the fetch runtime use?", pid, role)
    check(f"{role}: {'gets' if want else 'never gets'} its port", "8300" in blk, want)

print("── the adults' plain capability summary (Zach, 10-09) ──")
caps = SW.capability_observations(obs, T0)
check("one Titan entity in the household bubble, readable by adults, checked in code",
      {(c.entity, c.bubble, c.audience, c.entity_audience, c.kind, c.origin) for c in caps},
      {("titan", "household", "adults", "adults", "health", "code:health")})
check("every capability is working while only the decision broker is down (no capability rests on it)",
      dict(c.value.split(": ", 1) for c in caps), {cap: "working" for cap, _ in SW.CAPABILITIES})
tech = [str(x["port"]) for x in inv if x.get("port")] + [x["name"] for x in inv] + ["runtime", "unit", "http", "127.0.0.1"]
check("no technical detail in any claim (no port, service name, unit or URL)",
      [c for c in caps if any(t in f"{c.attr} {c.value}".lower() for t in tech)], [])
w.sync(SW.CAPABILITY_SOURCE, caps)
for pid, role, want in (("zach", "admin", True), ("robin", "adult", True), ("ava", "kid", False),
                        ("household", "household", False), ("eng", "engineering", False)):
    blk, _ = world.context(w, "Is Titan's search working?", pid, role)
    check(f"{role}: {'reads' if want else 'never reads'} the summary", "searching what's saved" in blk.lower(), want)
blk, _ = world.context(w, "What can Titan do right now?", "robin", "adult")
check("an adult's summary carries no port or service name", any(t in blk for t in tech[:-1]), False)
check("…and an adult still never sees a system service", world.context(w, "What port does the fetch runtime use?",
                                                                       "robin", "adult")[0].count("8300"), 0)
down = SW.capability_observations([o for o in obs if o.attr != "status"] + [
    world.Obs(entity="x", etype="service", name=n, attr="status", value=v, observed_at="", kind="health", ref="", origin="",
              label="", owner=None, audience="system") for n, v in
    (("agent-api", "up"), ("retrieval-runtime", "up"), ("embeddings", "down"), ("record-runtime", "down"))], T0)
check("a capability with one of its services down is partly working; with all down, not working right now",
      dict(c.value.split(": ", 1) for c in down), {"chat answers": "working", "searching what's saved": "partly working",
                                        "calendar, bills and records": "not working right now"})
check("a capability whose services weren't checked is left out, never guessed",
      "spoken replies" in {c.value.split(": ", 1)[0] for c in down}, False)

print("── a change supersedes, history kept ──")
DOWN.add("fetch-runtime")
w.sync(SW.SOURCE, SW.observations(inv, health, unit, datetime(2026, 10, 5, 12, 15, tzinfo=timezone.utc)))
blk, _ = world.context(w, "Is the fetch runtime up?", "zach", "admin")
check("the newer check wins: down now", ("down" in blk.lower()), True)
check("the claim's history logged the change", sysw.con.execute(
      "SELECT COUNT(*) FROM claim_log l JOIN claims c ON c.id=l.claim_id WHERE c.attr='status'").fetchone()[0] > 0, True)

print("── drift: the runbook's stated ports against the claims ──")
text = "fetch-runtime on :8300 handles pages.\ndecision-broker :8099 relays to Jev.\nThe code-model is on :8198."
d = SW.drift(inv, text)
check("counted, and the one that differs named", (d["named_with_a_port"], d["port_differs"], d["differs"]),
      (3, 1, ["decision-broker"]))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
