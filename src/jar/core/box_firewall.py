#!/usr/bin/env python3
"""A box's firewall and its listener check (titan-node#407 item 2, docs/box-appliance-ops.md): no inbound ports but the
few the household's own devices need, and a health check that fails when anything else listens beyond loopback.

For a BOX only. titan has its own default-deny firewall (titan-firewall.service, `inet titan_fw`); nothing here is ever
applied to titan. `ruleset` prints rules and never loads them; the only place they're ever loaded is a throwaway network
namespace in the test recipe (`sudo unshare -n sh -c 'nft -f FILE && nft list ruleset'`), which disappears with the
command and can't reach titan's live tables.

The box's ruleset (`inet box_fw`):
  input   policy drop. Allowed: loopback; established/related; ICMP and ICMPv6 (path MTU, neighbour discovery); the DHCP
          client's replies (udp 68); mDNS (udp 5353) and the agent API (tcp API_PORT) from the LAN only (RFC 1918, IPv4
          link-local, IPv6 ULA and link-local), never from anywhere; anything on tailscale0 (the owner's own tailnet,
          already authenticated by Tailscale) and Tailscale's own udp 41641.
  forward policy drop (a box routes nothing).
  output  no restriction for now, by decision (Coordination, 10-09): the box needs Open-Meteo, Tailscale, model and app
          updates, and egress is already counted per turn (#409). Said here so it's a choice, not an oversight.

The listener check (`check`): from `ss -ltnuH`, every TCP listener and bound UDP socket. Loopback is always fine.
Beyond loopback, only the allowlist (the API's TCP port, mDNS, the DHCP client, Tailscale's UDP port) may listen; any
other one, on an address or on a wildcard (0.0.0.0 or ::), is a violation. On a box it checks everything; `--pids`
limits it to given processes (the sandbox's own box), since titan itself runs many LAN services by design.

  python3 scripts/box_firewall.py ruleset [--api-port 7770]
  python3 scripts/box_firewall.py check [--api-port 7770] [--pids 123,456]
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
from typing import Any

from ops_ports import AGENT_API_PORT

LAN4 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
LAN6 = ("fc00::/7", "fe80::/10")
TAILSCALE_IF, TAILSCALE_UDP, MDNS_UDP, DHCP_CLIENT_UDP = "tailscale0", 41641, 5353, 68
API_PORT = AGENT_API_PORT


def ruleset(api_port: int = API_PORT) -> str:
    """The box's nftables ruleset (text). Printed, never applied here."""
    lan4, lan6 = ", ".join(LAN4), ", ".join(LAN6)
    return f"""# A household box's firewall (titan-node#407). Never applied to titan.
table inet box_fw {{
  set lan4 {{ type ipv4_addr; flags interval; elements = {{ {lan4} }} }}
  set lan6 {{ type ipv6_addr; flags interval; elements = {{ {lan6} }} }}

  chain input {{
    type filter hook input priority filter; policy drop;
    iif "lo" accept
    ct state established,related accept
    ct state invalid drop
    meta l4proto icmp accept
    meta l4proto ipv6-icmp accept
    udp dport {DHCP_CLIENT_UDP} accept comment "DHCP client replies"
    ip saddr @lan4 udp dport {MDNS_UDP} accept comment "mDNS, LAN only"
    ip6 saddr @lan6 udp dport {MDNS_UDP} accept comment "mDNS, LAN only"
    ip saddr @lan4 tcp dport {api_port} accept comment "the agent API, LAN only"
    ip6 saddr @lan6 tcp dport {api_port} accept comment "the agent API, LAN only"
    iifname "{TAILSCALE_IF}" accept comment "the owner's tailnet"
    udp dport {TAILSCALE_UDP} accept comment "Tailscale itself"
  }}

  chain forward {{
    type filter hook forward priority filter; policy drop;
  }}

  # output: no restriction for now, by decision (egress is counted per turn, #409); a deliberate choice, not a gap.
  chain output {{
    type filter hook output priority filter; policy accept;
  }}
}}
"""


def allowed(api_port: int = API_PORT) -> set[tuple[str, int]]:
    return {("tcp", api_port), ("udp", MDNS_UDP), ("udp", DHCP_CLIENT_UDP), ("udp", TAILSCALE_UDP)}


_SS_RX = re.compile(r"^(?P<proto>tcp|udp)\s+\S+\s+\d+\s+\d+\s+(?P<local>\S+)\s+\S+(?P<rest>.*)$")


def _split(local: str) -> tuple[str, int]:
    host, _, port = local.rpartition(":")
    return host.strip("[]").split("%", 1)[0], int(port) if port.isdigit() else -1


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


def listeners(ss_text: str) -> list[dict[str, Any]]:
    """[{proto, host, port, pids}] from `ss -ltnupH` (or -ltnuH) text."""
    out = []
    for line in (ss_text or "").splitlines():
        m = _SS_RX.match(line.strip())
        if not m:
            continue
        host, port = _split(m.group("local"))
        pids = {int(p) for p in re.findall(r"pid=(\d+)", m.group("rest"))}
        name = re.search(r'users:\(\("([^"]{1,40})"', m.group("rest"))   # the process NAME only, never its command line
        out.append({"proto": m.group("proto"), "host": host, "port": port, "pids": pids, "name": name.group(1) if name else ""})
    return out


def unexpected(items: list[dict[str, Any]], api_port: int = API_PORT, pids: set[int] | None = None) -> list[dict[str, Any]]:
    """The violating sockets themselves: [{proto, host, port, name}] (name: the process's, as ss shows it)."""
    ok = allowed(api_port)
    out, seen = [], set()
    for it in items:
        if pids is not None and not (it["pids"] & pids):
            continue
        if _loopback(it["host"]) or (it["proto"], it["port"]) in ok:
            continue
        key = (it["proto"], it["host"], it["port"])
        if key not in seen:
            seen.add(key)
            out.append({"proto": it["proto"], "host": it["host"] or "*", "port": it["port"], "name": it.get("name") or ""})
    return sorted(out, key=lambda d: (d["port"], d["proto"]))


def violations(items: list[dict[str, Any]], api_port: int = API_PORT, pids: set[int] | None = None) -> list[str]:
    """Each listener beyond loopback that isn't allowlisted, as "proto host:port". A wildcard (0.0.0.0, ::, *) is
    beyond loopback. `pids`: only those processes' sockets (the sandbox's own box)."""
    ok = allowed(api_port)
    bad = []
    for it in items:
        if pids is not None and not (it["pids"] & pids):
            continue
        if _loopback(it["host"]):
            continue
        if (it["proto"], it["port"]) in ok:
            continue
        bad.append(f"{it['proto']} {it['host'] or '*'}:{it['port']}")
    return sorted(set(bad))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ruleset", "check"])
    ap.add_argument("--api-port", type=int, default=API_PORT)
    ap.add_argument("--pids", default="")
    a = ap.parse_args(argv)
    if a.cmd == "ruleset":
        print(ruleset(a.api_port), end="")
        return 0
    text = subprocess.run(["ss", "-ltnupH"], capture_output=True, text=True).stdout
    pids = {int(p) for p in a.pids.split(",") if p.strip().isdigit()} or None
    bad = violations(listeners(text), a.api_port, pids)
    if bad:
        print("Unexpected listeners beyond loopback: " + "; ".join(bad))
        return 1
    print("Listeners OK: only loopback and the allowlist (" + ", ".join(f"{p} {n}" for p, n in sorted(allowed(a.api_port))) + ").")
    return 0


if __name__ == "__main__":
    sys.exit(main())
