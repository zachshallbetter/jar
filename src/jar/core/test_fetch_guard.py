#!/usr/bin/env python3
# gate: hermetic
"""The fetch guard (#936's design, its urgent part): fetch_url and ingest_url read the public web only, never titan itself
or the house's network. Checked in Python before any request (fetch_checks.public_url), as a fast first refusal; the
fetch-runtime's own connector guard is the full fix (AcmeProductCo/integration). Every class of address is tested, the
numeric spellings included, and a name that resolves to a private address; a redirect onto the house's network passes
nothing on; the refusal is a plain tool error and is counted, never logged with its URL. No network: a stub resolver
and a stub runtime. Run: python3 scripts/test_fetch_guard.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-fetch-guard-"))
for k in ("HOUSEHOLD_DB", "PROACTIVE_DB", "WORLD_DIR", "LINKS_DB", "LINK_SALT_FILE", "AGENT_MEMORY_DIR", "SITTER_DB"):
    os.environ[k] = str(_TMP / k.lower())
sys.path.insert(0, str(Path(__file__).parent))
import fetch_checks as F  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


DNS = {"harbor-gas.example": "93.184.216.34", "rebind.example": "192.168.1.20", "sneaky.example": "127.0.0.1",
       "v6.example": "2606:4700::1111", "ula.example": "fd12:3456::1", "mapped.example": "::ffff:10.0.0.5"}


def resolve(host, port):
    if host not in DNS:
        raise OSError("no such host")
    ip = DNS[host]
    return [(10 if ":" in ip else 2, 1, 6, "", (ip, port))]


def refused(url):
    return F.public_url(url, resolve=resolve)


print("── the public web is read ──")
for u in ("https://harbor-gas.example/bill", "http://harbor-gas.example:8080/x", "https://93.184.216.34/",
          "https://[2606:4700::1111]/", "https://v6.example/", "https://unknown-name.example/"):
    check(f"{u}: fetched", refused(u), None)

print("── never titan itself or the house's network ──")
for u, why in (("http://127.0.0.1:8123/api/states", "loopback (Home Assistant on titan)"), ("http://localhost:7770/v1/me", "localhost"),
               ("http://10.0.0.5/", "RFC 1918 10/8"), ("http://172.16.4.2/", "RFC 1918 172.16/12"),
               ("http://192.168.1.1/admin", "RFC 1918 192.168/16"), ("http://169.254.169.254/latest/meta-data/", "link-local (cloud metadata)"),
               ("http://100.64.0.7/", "CGNAT"), ("http://224.0.0.1/", "multicast"), ("http://0.0.0.0:8300/", "0.0.0.0"),
               ("http://[::]/", "[::]"), ("http://[::1]:7770/", "IPv6 loopback"), ("http://[fd12:3456::1]/", "IPv6 ULA"),
               ("http://[fe80::1]/", "IPv6 link-local"), ("http://[::ffff:127.0.0.1]/", "IPv4-mapped IPv6 loopback"),
               ("http://2130706433/", "decimal IPv4 (127.0.0.1)"), ("http://0x7f000001/", "hex IPv4"),
               ("http://titan.local:8010/", "*.local"), ("http://printer.lan/", "*.lan"), ("http://router.home.arpa/", "home.arpa"),
               ("http://svc.internal/", "*.internal"), ("http://titan/", "a bare name"),
               ("http://rebind.example/", "a public-looking name resolving to 192.168.x"),
               ("http://sneaky.example/", "a name resolving to loopback"), ("http://ula.example/", "a name resolving to an IPv6 ULA"),
               ("http://mapped.example/", "a name resolving to IPv4-mapped 10.x")):
    check(f"{why}: refused", refused(u), F.NOT_PUBLIC)
check("octal IPv4 (0177.0.0.1): refused (the system resolver reads it as 127.0.0.1)",
      F.public_url("http://0177.0.0.1/", resolve=lambda h, p: [(2, 1, 6, "", ("127.0.0.1", p))]), F.NOT_PUBLIC)
check("short IPv4 (127.1): refused the same way",
      F.public_url("http://127.1/", resolve=lambda h, p: [(2, 1, 6, "", ("127.0.0.1", p))]), F.NOT_PUBLIC)
for u in ("file:///etc/passwd", "ftp://harbor-gas.example/", "gopher://harbor-gas.example/", "javascript:alert(1)", "harbor-gas.example"):
    check(f"{u}: not http(s), refused", refused(u), "Error: not fetched: only http and https web addresses can be read.")
check("the refusal is a plain tool error naming the house network, never the URL",
      (F.NOT_PUBLIC.startswith("Error: "), "inside the house network" in F.NOT_PUBLIC, "127.0.0.1" in F.NOT_PUBLIC), (True, True, False))

print("── through the tools: refused before any request; a redirect inward passes nothing on; counted only ──")
import agent_runtime_lib as L  # noqa: E402

F._socket.getaddrinfo = resolve
calls = []


def runtime(svc, path, method, body, timeout=60):
    calls.append(body["url"])
    if "harbor-gas" in body["url"] and "redirect" in body["url"]:
        return {"url": "http://192.168.1.1/admin", "title": "Router admin", "content": "admin password: hunter2"}
    return {"url": body["url"], "title": "Harbor Gas", "content": "Your bill is $41.20."}


L._runtime_request = runtime
L._TURN_INFO.info = {}
out = L._execute_tool("fetch_url", {"url": "http://127.0.0.1:8123/api/states"})
check("fetch_url to titan: the plain refusal, and the runtime never called", (out, calls), (F.NOT_PUBLIC, []))
out = L._execute_tool("ingest_url", {"url": "http://192.168.1.1/admin"})
check("ingest_url to the LAN: refused, never fetched", (out, calls), (F.NOT_PUBLIC, []))
out = L._execute_tool("fetch_url", {"url": "https://harbor-gas.example/redirect"})
check("a public page that redirects onto the LAN: nothing of it is passed on", (out, "hunter2" in out, "Router" in out),
      (F.NOT_PUBLIC, False, False))
out = L._execute_tool("fetch_url", {"url": "https://harbor-gas.example/bill"})
check("a public page is read as before", "Your bill is $41.20." in out, True)
check("the turn log counts the refusals (3), never a URL", (L._TURN_INFO.info.get("fetch_refused"),
                                                            [v for v in map(str, L._TURN_INFO.info.values()) if "127.0.0.1" in v or "192.168" in v]),
      (3, []))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
