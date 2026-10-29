#!/usr/bin/env python3
# gate: hermetic
"""A box's firewall ruleset and listener check (titan-node#407): the ruleset is input-drop with only the LAN-limited
API and mDNS, the DHCP client, ICMP and Tailscale (tailscale0 and its UDP port), forward drop, and an explicit
no-restriction output policy; the check fails on any listener beyond loopback that isn't allowlisted, on an address or a
wildcard (0.0.0.0 and ::). Pure text in, text out: nothing is applied anywhere.
Run: python3 scripts/test_box_firewall.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import box_firewall as fw  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


print("── the ruleset ──")
r = fw.ruleset(7770)
lines = [l.strip() for l in r.splitlines()]
check("input and forward drop; output an explicit accept with its reason",
      ("type filter hook input priority filter; policy drop;" in lines, "type filter hook forward priority filter; policy drop;" in lines,
       "type filter hook output priority filter; policy accept;" in lines, "no restriction for now, by decision" in r), (True, True, True, True))
api = [l for l in lines if "tcp dport 7770" in l]
mdns = [l for l in lines if "udp dport 5353" in l]
check("the API and mDNS only from the LAN sets (never from anywhere)",
      (len(api), len(mdns), all("saddr @lan" in l for l in api + mdns)), (2, 2, True))
check("the LAN sets are RFC 1918 + link-local, and IPv6 ULA + link-local", ("10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16" in r,
      "fc00::/7, fe80::/10" in r), (True, True))
check("Tailscale: the tailscale0 interface and its own UDP port only", ('iifname "tailscale0" accept' in r, "udp dport 41641 accept" in r),
      (True, True))
accepts = [l for l in lines if l.endswith("accept") or "accept comment" in l]
check("nothing accepted from anywhere on any other port", sorted(set(
      __import__("re").findall(r"dport (\d+)", "\n".join(accepts)))), ["41641", "5353", "68", "7770"])
check("another API port moves with it", "tcp dport 8443" in fw.ruleset(8443) and "7770" not in fw.ruleset(8443), True)

print("── the listener check ──")
SS = """tcp   LISTEN 0      4096         127.0.0.1:8010       0.0.0.0:*    users:(("llama-server",pid=11,fd=3))
tcp   LISTEN 0      2048           0.0.0.0:7770       0.0.0.0:*    users:(("python3",pid=12,fd=5))
udp   UNCONN 0      0              0.0.0.0:5353       0.0.0.0:*    users:(("python3",pid=12,fd=9))
udp   UNCONN 0      0       10.0.0.170%enp9s0:68       0.0.0.0:*    users:(("systemd-network",pid=13,fd=20))
tcp   LISTEN 0      4096             [::1]:8123          [::]:*    users:(("hass",pid=14,fd=7))
udp   UNCONN 0      0                    *:41641            *:*    users:(("tailscaled",pid=15,fd=8))"""
items = fw.listeners(SS)
check("parsed every socket, with its pids", [(i["proto"], i["port"], sorted(i["pids"])) for i in items],
      [("tcp", 8010, [11]), ("tcp", 7770, [12]), ("udp", 5353, [12]), ("udp", 68, [13]), ("tcp", 8123, [14]), ("udp", 41641, [15])])
check("loopback and the allowlist only: no violation", fw.violations(items), [])
for extra, label in (("tcp   LISTEN 0 128 0.0.0.0:8040 0.0.0.0:* users:((\"kokoro\",pid=16,fd=3))", "0.0.0.0 (IPv4 any)"),
                     ("tcp   LISTEN 0 128 [::]:9100 [::]:* users:((\"retrieval\",pid=17,fd=3))", ":: (IPv6 any)"),
                     ("tcp   LISTEN 0 128 *:8300 *:* users:((\"fetch\",pid=18,fd=3))", "* (any)"),
                     ("tcp   LISTEN 0 128 192.168.1.20:22 0.0.0.0:* users:((\"sshd\",pid=19,fd=3))", "a LAN address"),
                     ("udp   UNCONN 0 0 0.0.0.0:1900 0.0.0.0:* users:((\"minissdpd\",pid=20,fd=3))", "a UDP socket (SSDP)")):
    bad = fw.violations(fw.listeners(SS + "\n" + extra))
    check(f"an unexpected listener on {label}: flagged", len(bad), 1)
check("…named by protocol, address and port", fw.violations(fw.listeners("tcp LISTEN 0 1 [::]:9100 [::]:* users:((\"x\",pid=1,fd=1))")),
      ["tcp :::9100"])
check("the API on a different port than configured is flagged", fw.violations(items, api_port=8443), ["tcp 0.0.0.0:7770"])
stray = "tcp   LISTEN 0 128 0.0.0.0:4444 0.0.0.0:* users:((\"nc\",pid=99,fd=3))"
check("--pids: only those processes are judged (the sandbox's own box)",
      (fw.violations(fw.listeners(SS + "\n" + stray), pids={12, 14}), fw.violations(fw.listeners(SS + "\n" + stray), pids={99})),
      ([], ["tcp 0.0.0.0:4444"]))
check("a scope id on an address is ignored (10.0.0.170%enp9s0 is a LAN address, DHCP is allowed)",
      fw._split("10.0.0.170%enp9s0:68"), ("10.0.0.170", 68))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
