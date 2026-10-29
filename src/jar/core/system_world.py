#!/usr/bin/env python3
"""The system bubble from health checks (titan-node#355): what titan runs, computed in code, never read from notes.

Runbook prose drifts from what runs (#912's cite misses, #947's runtime count). Here each runtime and broker in
runtime-registry.json, and titan's own model servers and agent API (SERVICES below), is a `service` entity in the
system bubble, with claims checked just now:
  port, health_url        the registry's (or SERVICES')
  status                  up | down: a read-only GET of the health URL (no auth, 3 s)
  unit_active             systemctl is-active <unit>   (by name only: never a unit file, its env or a command line)
  unit_enabled            systemctl is-enabled <unit>
  group                   runtime | broker | model server | api
Evidence kind `health` (checked), audience `system`: the admin and engineering agents read it; nobody else
(audience.visible). A changed value supersedes the old one through the world model's own rules, history kept.

drift(): the runbook's stated port for each service, against the claims; counts and names for the scorecard (a
mismatch is a doc ticket, never an auto-edit).

capability_observations(): the same check, as every adult may read it (Zach, 10-09, #355: a plain capability summary
for each adult; the technical details are his only). One "Titan" entity in the household bubble, audience adults: one
claim per capability, "working", "partly working" or "not working right now". No port, service name, unit or URL ever
reaches it. Only capabilities whose services are known are listed (CAPABILITIES); chat answers cover titan's own side
(the cloud model isn't probed here).

  python3 scripts/system_world.py            dry run: what it would write (counts), and the drift
  python3 scripts/system_world.py --apply    write the system bubble (the timer)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ops_ports import AGENT_API_PORT, CODE_MODEL_PORT  # noqa: E402

REGISTRY = Path(__file__).resolve().parent / "runtime-registry.json"
RUNBOOK = Path(__file__).resolve().parent.parent / "docs" / "runbook.md"
SOURCE = "system-health"
LOG = Path(__import__("os").environ.get("SYSTEM_WORLD_LOG", "/ai/logs/system-world.jsonl"))   # one line a run
# titan's own services that aren't in the registry: name, port, unit, health path, group
SERVICES = [("agent-api", AGENT_API_PORT, "titan-agent-api", "/health", "api"),
            ("local-agent", 8010, "titan-local-agent", "/health", "model server"),
            ("code-model", CODE_MODEL_PORT, "titan-code-model", "/health", "model server"),
            ("embeddings", 8015, "titan-embeddings", "/health", "model server"),
            ("decider", 8031, "titan-decider", "/health", "model server"),
            # no reranker: titan-reranker is disabled on purpose since 10-15 (#1622)
            ("kokoro", 8040, "titan-kokoro", "/health", "model server")]


# What each adult can ask about, and the services it rests on. Plain words only; a service missing from the inventory
# drops its capability rather than guessing.
CAPABILITIES = [("chat answers", ("agent-api",)),
                ("searching what's saved", ("retrieval-runtime", "embeddings")),
                ("calendar, bills and records", ("record-runtime",)),
                ("the household timeline", ("event-runtime",)),
                ("reading web pages", ("fetch-runtime",)),
                ("spoken replies", ("kokoro",)),
                ("background help (suggestions and sorting)", ("local-agent", "decider"))]
CAPABILITY_SOURCE = "system-capabilities"


def inventory(registry: Path | None = None) -> list[dict[str, Any]]:
    d = json.loads(Path(registry or REGISTRY).read_text())
    out = [{"name": r["name"], "port": r.get("port"), "health_url": r.get("health_url"), "unit": r["name"],
            "group": "runtime"} for r in d.get("runtimes", [])]
    out += [{"name": b["name"], "port": b.get("port"), "health_url": b.get("health_url"), "unit": b["name"],
             "group": "broker"} for b in d.get("brokers", [])]
    out += [{"name": n, "port": p, "health_url": f"http://127.0.0.1:{p}{path}", "unit": u, "group": g}
            for n, p, u, path, g in SERVICES]
    return out


def probe_health(url: str | None, timeout: float = 3.0) -> str:
    """up when the health URL answers below 500 (a 401 is a live, guarded service); down otherwise."""
    if not url:
        return "unknown"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as r:
            return "up" if r.status < 500 else "down"
    except urllib.error.HTTPError as e:
        return "up" if e.code < 500 else "down"
    except Exception:  # noqa: BLE001
        return "down"


def probe_unit(unit: str, verb: str) -> str:
    """systemctl is-active / is-enabled by unit name only: its one-word answer, never its file, env or command."""
    if not re.fullmatch(r"[A-Za-z0-9@._-]+", unit or "") or verb not in ("is-active", "is-enabled"):
        return "unknown"
    try:
        r = subprocess.run(["systemctl", verb, f"{unit}.service"], capture_output=True, text=True, timeout=5)
        return (r.stdout.strip().split() or ["unknown"])[0]
    except Exception:  # noqa: BLE001
        return "unknown"


def observations(inv: list[dict[str, Any]], health: Callable[[str | None], str] = probe_health,
                 unit: Callable[[str, str], str] = probe_unit, now: datetime | None = None) -> list[Any]:
    import world
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    out = []
    for s in inv:
        eid = "svc-" + re.sub(r"[^a-z0-9]+", "-", s["name"].lower()).strip("-")
        vals = {"port": s["port"], "health_url": s["health_url"], "group": s["group"],
                "status": health(s["health_url"]), "unit_active": unit(s["unit"], "is-active"),
                "unit_enabled": unit(s["unit"], "is-enabled")}
        for attr, v in vals.items():
            if v in (None, ""):
                continue
            out.append(world.Obs(entity=eid, etype="service", name=s["name"], attr=attr, value=str(v), observed_at=at,
                                 kind="health", ref=f"health:{s['name']}:{attr}", origin="code:health",
                                 label="titan's health check", owner=None, audience="system", entity_owner=None,
                                 entity_audience="system", bubble="system",
                                 # its name and its spoken form ("fetch runtime"); never the bare first word ("fetch",
                                 # "event", "record", "text"): "fetch Leo from school" must not pull a service
                                 aliases=tuple({(n, "") for n in (s["name"], s["name"].replace("-", " "))})))
    return out


def capability_observations(obs: list[Any], now: datetime | None = None) -> list[Any]:
    """From observations()' status claims → the adults' plain summary (#355). Never a technical value."""
    import world
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    status = {o.name: o.value for o in obs if o.attr == "status"}
    out = []
    for cap, needs in CAPABILITIES:
        got = [status[n] for n in needs if n in status]
        if len(got) != len(needs):
            continue                      # a service we don't know about: say nothing rather than guess
        up = sum(1 for v in got if v == "up")
        value = "working" if up == len(got) else ("not working right now" if up == 0 else "partly working")
        out.append(world.Obs(entity="titan", etype="assistant", name="Titan", attr="capability", value=f"{cap}: {value}",
                             observed_at=at,
                             kind="health", ref=f"capability:{cap}", origin="code:health", label="titan's health check",
                             owner=None, audience="adults", entity_owner=None, entity_audience="adults",
                             bubble="household", facet="home", aliases=(("Jellybean", ""),)))
    return out


def drift(inv: list[dict[str, Any]], runbook_text: str) -> dict[str, Any]:
    """Each service the runbook names on a line with a port: is the claim's port among them? → counts and the
    names that differ (system names, never household data)."""
    lines = runbook_text.splitlines()
    checked, wrong = 0, []
    for s in inv:
        if not s.get("port"):
            continue
        name = re.escape(s["name"])
        stated = set()
        for ln in lines:
            if re.search(rf"(?<![\w-]){name}(?![\w-])", ln, re.I):
                stated |= {int(p) for p in re.findall(r"(?<![\d.]):(\d{4,5})\b", ln)}
        if stated:
            checked += 1
            if int(s["port"]) not in stated:
                wrong.append(s["name"])
    return {"services": len(inv), "named_with_a_port": checked, "port_differs": len(wrong), "differs": sorted(wrong)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    inv = inventory()
    obs = observations(inv)
    status = {}
    for o in obs:
        if o.attr == "status":
            status[o.value] = status.get(o.value, 0) + 1
    caps = capability_observations(obs)
    out: dict[str, Any] = {"apply": a.apply, "services": len(inv), "claims": len(obs), "status": status,
                           "capabilities": [c.value for c in caps],
                           "drift": drift(inv, RUNBOOK.read_text() if RUNBOOK.exists() else "")}
    if a.apply:
        import time
        import world
        out["sync"] = world.get().sync(SOURCE, obs)
        out["capabilities_sync"] = world.get().sync(CAPABILITY_SOURCE, caps)
        down = sorted(o.name for o in obs if o.attr == "status" and o.value == "down")
        try:   # the scorecard's line: counts and system names only
            LOG.parent.mkdir(parents=True, exist_ok=True)
            with LOG.open("a") as f:
                f.write(json.dumps({"ts": time.time(), "services": len(inv), "down": down,
                                    "port_differs": out["drift"]["differs"]}) + "\n")
        except OSError as exc:
            print(f"(couldn't append to {LOG}: {exc})", file=sys.stderr)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
