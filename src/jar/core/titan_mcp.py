#!/usr/bin/env python3
"""titan for an engineering agent, as a Model Context Protocol server over stdio (newline-delimited JSON-RPC 2.0),
standard library only. Read-only: nothing here restarts, deploys or enables anything (those stay on titan.sh, behind
preflight and the deploy gate).

Tools:
  titan_reachability   probe each of titan's addresses (/health, no token) and say which paths answer and what to do
  titan_host_summary   titan's health: disks, GPUs, memory, failed units, canary (GET /v1/host/summary)
  titan_boot_check     whether titan's boot units are enabled, from the summary's `boot` (once titan reports it)
  titan_services       services in titan's system bubble, with port, status, unit (titan_system.py services)
  titan_service        one service's facts (titan_system.py service <id>)
  titan_notes          titan's system notes (titan_system.py notes)

Every call but titan_reachability reads with the engineering token (titan_system.py's: ENGINEERING_TOKEN_FILE, else
~/.config/titan/engineering_token), never the master token. Every result starts with titan_system.HEADER and is capped.
Addresses: TITAN_ADDRESSES (space-separated), else DEFAULT_ADDRESSES, in the order Jellybean tries them.

An MCP client, in a project's .mcp.json:
  {"mcpServers": {"titan": {"command": "python3", "args": ["scripts/titan_mcp.py"]}}}
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import titan_system as ts  # noqa: E402
from ops_ports import AGENT_API_PORT  # noqa: E402

VERSION = "0.1.0"
PROTOCOL = "2025-06-18"
PROBE_TIMEOUT = 3.0   # Jellybean's TitanAddresses.probeTimeout
# Jellybean's household order (config/household.yml JB_TITAN_ADDRESSES): home IP, mDNS name, Tailscale IP, tailnet name.
DEFAULT_ADDRESSES = (f"http://10.0.0.170:{AGENT_API_PORT}", f"http://titan-node.local:{AGENT_API_PORT}",
                     f"http://100.77.69.85:{AGENT_API_PORT}", ts.TAILNET_URL)
PATH_WORDS = {"home": "the home network", "mdns": "its name on the home network", "tailscale": "Tailscale",
              "other": "another address"}


# ── reachability ─────────────────────────────────────────────────────────────────────────────

def path_of(url: str) -> str:
    """home | mdns | tailscale | other — Jellybean's RemoteAccess.place, with the .local name split out
    (vectors: docs/titan-path-vectors.json)."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    if not host:
        return "other"
    if host.endswith(".ts.net") or host.startswith("fd7a:115c:a1e0:"):
        return "tailscale"
    if host.endswith(".local"):
        return "mdns"
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 100 and 64 <= b <= 127:
            return "tailscale"
        if a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
            return "home"
    return "other"


def addresses(environ: dict[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    raw = (env.get("TITAN_ADDRESSES") or "").split() or list(DEFAULT_ADDRESSES)
    out: list[str] = []
    for u in raw:
        u = u.rstrip("/")
        if u and u not in out:
            out.append(u)
    return out


def health_request(base: str) -> urllib.request.Request:
    """GET /health with no token: titan answers anyone who can reach it."""
    return urllib.request.Request(base.rstrip("/") + "/health", method="GET")


def _why(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    reason = getattr(exc, "reason", exc)
    text = str(reason).lower()
    if isinstance(reason, TimeoutError) or "timed out" in text:
        return "no answer (timed out)"
    if "refused" in text:
        return "refused (the machine is up, titan's API isn't)"
    if "nodename" in text or "name or service" in text or "not known" in text or "getaddrinfo" in text:
        return "name not found"
    if "unreachable" in text:
        return "network unreachable"
    return type(reason).__name__


def probe(base: str, timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(health_request(base), timeout=timeout) as r:
            return (r.status == 200, "" if r.status == 200 else f"HTTP {r.status}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, _why(exc)


def reachability(urls: list[str], probe_fn: Callable[[str], tuple[bool, str]] = probe) -> list[dict[str, Any]]:
    """Every address probed at once; rows in the given order."""
    def one(u: str) -> dict[str, Any]:
        t0 = time.monotonic()
        ok, err = probe_fn(u)
        row: dict[str, Any] = {"url": u, "path": path_of(u), "ok": ok, "ms": int((time.monotonic() - t0) * 1000)}
        if not ok:
            row["error"] = err or "no answer"
        return row
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        return list(pool.map(one, urls))


def verdict(rows: list[dict[str, Any]]) -> str:
    """One line: what answered, and the next step."""
    if not rows:
        return "No addresses to try: set TITAN_ADDRESSES."
    ok = {r["path"] for r in rows if r["ok"]}
    tried = {r["path"] for r in rows}
    if not ok:
        paths = ", ".join(PATH_WORDS[p] for p in ("home", "mdns", "tailscale", "other") if p in tried)
        return (f"Nothing answered ({paths}). titan is off or off the network: check it has power and its network "
                f"cable; after a power cut it starts again only if the BIOS restores AC power. If another machine on "
                f"the home network can't reach it either, it's titan, not this machine.")
    if "mdns" in ok and "home" in tried and "home" not in ok:
        return ("titan answers by name but not at its home IP: its home address changed. Find titan again "
                "(Jellybean: Settings › titan › Find titan) and update the configured address.")
    if ok == {"tailscale"} and tried & {"home", "mdns"}:
        return ("titan answers over Tailscale only: this machine is away from home, or titan's home address changed "
                "(if you're home, find titan again).")
    if "tailscale" in tried and "tailscale" not in ok:
        return ("titan answers on the home network but not over Tailscale: Tailscale is off on this machine or on "
                "titan, so it won't work away from home.")
    return "titan answers on every path tried."


def reachability_text(urls: list[str] | None = None,
                      probe_fn: Callable[[str], tuple[bool, str]] = probe) -> str:
    rows = reachability(addresses() if urls is None else urls, probe_fn)
    lines = [f"{'ok' if r['ok'] else '--'}  {r['path']:<9} {r['url']}  {r['ms']} ms"
             + ("" if r["ok"] else f"  {r['error']}") for r in rows]
    return "\n".join([*lines, "", verdict(rows)])


# ── the host summary ─────────────────────────────────────────────────────────────────────────

def _summary() -> dict[str, Any]:
    out = ts.get("/v1/host/summary")
    if not isinstance(out, dict):
        raise ValueError("titan's summary wasn't an object")
    return out


def host_summary_text(summary: dict[str, Any]) -> str:
    lines = ["ok" if summary.get("ok") else "needs attention"]
    lines += [f"  problem: {ts._one_line(p, 200)}" for p in summary.get("problems") or []]
    if summary.get("memory_free_gib") is not None:
        lines.append(f"memory free: {summary['memory_free_gib']} GiB")
    for d in summary.get("disks") or []:
        lines.append(f"disk {d.get('mount')}: {d.get('used_pct')}% used, {d.get('free_gib')} GiB free")
    for g in summary.get("gpus") or []:
        lines.append(f"GPU{g.get('index')}: {g.get('temp_c')} °C, {g.get('mem_used_mib')}/{g.get('mem_total_mib')} MiB")
    failed = summary.get("failed_units") or []
    lines.append("failed units: " + (", ".join(str(u) for u in failed) if failed else "none"))
    can = summary.get("canary") or {}
    if can:
        lines.append(f"agent canary: {can.get('status')} at {can.get('ts')}")
    net = summary.get("network")
    if isinstance(net, dict):
        lines.append(f"network: {net.get('hostname')} " + " ".join(str(a) for a in net.get("ipv4") or [])
                     + (f", Tailscale {net['tailscale_ipv4']}" if net.get("tailscale_ipv4") else ""))
    if isinstance(summary.get("boot"), dict):
        lines.append(boot_text(summary))
    return "\n".join(lines)


def boot_text(summary: dict[str, Any]) -> str:
    boot = summary.get("boot")
    if not isinstance(boot, dict):
        return ("titan's summary doesn't report boot units yet (the boot-resilience check isn't on titan). "
                "On titan: python3 scripts/boot_resilience.py check")
    lines = [f"boot: default target {boot.get('default_target', 'unknown')}; {boot.get('ok', 0)} ok, "
             f"{boot.get('not_enabled', 0)} not enabled, {boot.get('missing', 0)} missing"
             + (f", {boot['platform_fail']} platform checks failing" if boot.get("platform_fail") else "")]
    lines += [f"  {ts._one_line(b, 200)}" for b in boot.get("bios") or []]
    return "\n".join(lines)


# ── the server ───────────────────────────────────────────────────────────────────────────────

_Q = {"type": "string", "description": "Words to match (optional)"}
TOOLS: list[dict[str, Any]] = [
    {"name": "titan_reachability",
     "description": "Probe each of titan's addresses (home IP, its .local name, Tailscale IP and name) on /health and "
                    "say which paths answer, how fast, and the next step if none do. Works when titan is down; no token.",
     "inputSchema": {"type": "object", "properties": {
         "addresses": {"type": "array", "items": {"type": "string"},
                       "description": "Base URLs to try instead of the configured ones, e.g. "
                                      + DEFAULT_ADDRESSES[0]}}}},
    {"name": "titan_host_summary",
     "description": "titan's health right now: problems, disks, GPUs, memory, failed systemd units, the agent canary "
                    "(and network identity and boot units when titan reports them).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "titan_boot_check",
     "description": "Whether titan's boot units are enabled so it comes back by itself after a power cut, and the BIOS "
                    "AC-power note, from titan's summary.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "titan_services",
     "description": "Services in titan's system bubble with port, status, unit and when each was last checked.",
     "inputSchema": {"type": "object", "properties": {"q": _Q}}},
    {"name": "titan_service",
     "description": "One service's facts, by the id titan_services prints (e.g. system:fetch-runtime).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "titan_notes",
     "description": "titan's system notes (runbook, decisions): title and first line.",
     "inputSchema": {"type": "object", "properties": {"q": _Q}}},
]


def _cap(body: str) -> str:
    text = f"{ts.HEADER}\n{body}"
    return text if len(text) <= ts.MAX_CHARS else text[: ts.MAX_CHARS - 1] + "…"


class Server:
    def __init__(self, probe_fn: Callable[[str], tuple[bool, str]] = probe):
        self.probe_fn = probe_fn

    def call(self, name: str, args: dict[str, Any]) -> str:
        if name == "titan_reachability":
            urls = args.get("addresses")
            if urls is not None and not (isinstance(urls, list) and all(isinstance(u, str) for u in urls)):
                raise ValueError("addresses: a list of base URLs")
            for u in urls or []:
                if urllib.parse.urlsplit(u).scheme not in ("http", "https"):
                    raise ValueError(f"not an http(s) address: {u!r}")
            return _cap(reachability_text([u.rstrip("/") for u in urls] if urls else None, self.probe_fn))
        if name == "titan_host_summary":
            return _cap(host_summary_text(_summary()))
        if name == "titan_boot_check":
            return _cap(boot_text(_summary()))
        if name == "titan_services":
            return _cap("\n".join(ts.services(str(args.get("q") or ""))))
        if name == "titan_service":
            if not str(args.get("id") or "").strip():
                raise ValueError("id is required")
            return _cap("\n".join(ts.service(str(args["id"]).strip())))
        if name == "titan_notes":
            return _cap("\n".join(ts.notes(str(args.get("q") or ""))))
        raise ValueError(f"unknown tool {name!r}")

    def _call_text(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            return self.call(name, args), False
        except ts.NoToken as exc:
            return f"error: {exc}", True
        except urllib.error.HTTPError as exc:
            hint = " (the engineering token isn't allowed here)" if exc.code in (401, 403) else ""
            return f"error: titan answered HTTP {exc.code}{hint}", True
        except (urllib.error.URLError, OSError) as exc:
            return f"error: titan didn't answer ({_why(exc)}); try titan_reachability", True
        except (ValueError, KeyError) as exc:
            return f"error: {exc}", True

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:          # a notification (notifications/initialized and the like): no reply
            return None
        try:
            if method == "initialize":
                result: Any = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", PROTOCOL),
                               "capabilities": {"tools": {}},
                               "serverInfo": {"name": "titan", "version": VERSION}}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                p = msg.get("params") or {}
                text, is_error = self._call_text(str(p.get("name") or ""), p.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}]}
                if is_error:
                    result["isError"] = True
            elif method == "ping":
                result = {}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as exc:  # noqa: BLE001 - one bad request must not end the server
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)[:500]}}


def main() -> int:
    server = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None,
                                            "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
