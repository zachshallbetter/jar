#!/usr/bin/env python3
"""titan as an MCP host (#1629; governance in docs/design/governance-chokepoint.md): an MCP server's tools become
titan tools, each with a descriptor, so the one decision (governance.decide), the role check and the person's record
cover them like titan's own.

Servers are listed by an admin in a small JSON file (MCP_SERVERS, default /ai/config/mcp_servers.json):

    {"servers": {"ha": {"url": "http://127.0.0.1:8123/api/mcp", "token_file": "/ai/secrets/ha-mcp.token",
                        "domain": "home", "roles": ["adult"], "tools": {"get_state": "read", "set_light": "write"}}}}

  url         Streamable HTTP at a literal address on this host or the house's network (127.0.0.1, ::1, a private or
              link-local address; "localhost" means 127.0.0.1). Never a name: a name can resolve somewhere else later.
              A server anywhere else would be egress, and egress has one list (egress.py), so it's refused until it's
              added there. Redirects aren't followed and proxies aren't used.
  token_file  optional; sent as a bearer token. The token stays in the file, never in a prompt or a log.
  domain      the capability domain its tools belong to (tool_descriptors.DOMAINS), so a kill switch or a person's
              Never covers them.
  roles       who besides the admin may use them (default: the admin only).
  tools       optional, the admin's own list: {server's tool name: read|write|dangerous}. With it, only those tools are
              offered, at the risk the admin gave. Without it, only the tools the server itself marks read-only
              (readOnlyHint) are offered, as reads: a server's word never makes a write available.
  enabled     false leaves a server listed but unused.

A server's tool `get_state` is offered as `mcp_ha_get_state`. Everything a server says is someone else's text: its
results are wrapped as untrusted data by the runtime, and its tool descriptions are cut to one plain line. A server's
first tool list is read inline (once); after that a due re-read (every 10 minutes, a minute after a failure) runs in
the background, and a server that stops answering keeps its last list for 30 minutes.

CLI (an admin on titan): python3 mcp_host.py status | tools
"""
from __future__ import annotations

import ipaddress
import itertools
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PREFIX = "mcp_"
PROTOCOL = "2025-06-18"
LIST_TTL_S, RETRY_S, STALE_S = 600.0, 60.0, 1800.0
LIST_TIMEOUT_S, CALL_TIMEOUT_S = 5.0, 20.0
RESULT_CHARS, DESC_CHARS = 6000, 300
RISKS = ("read", "write", "dangerous")
_DEFAULT = "/ai/config/mcp_servers.json"
_KEY_RX = re.compile(r"^[a-z0-9]{1,16}$")
_lock = threading.Lock()
_ids = itertools.count(1)
_config_cache: dict[str, Any] = {"path": None, "mtime": None, "problems": {}}
_state: dict[str, "_Server"] = {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:   # a 3xx is an error: never followed, the token never resent
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class _Failed(RuntimeError):
    """A request that didn't work; the message is titan's own words."""


_FORGOT = "forgot the session"


@dataclass
class _Server:
    key: str
    url: str
    domain: str
    roles: frozenset[str]
    allow: dict[str, str] | None   # the admin's {tool: risk}, or None (read-only tools only)
    token_file: str | None
    session: str | None = None
    ready: bool = False            # initialized (and told so): requests may go
    version: str = PROTOCOL
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)   # titan name -> {name, risk, schema, spec}
    listed_at: float = 0.0
    failed_at: float = 0.0
    error: str | None = None
    busy: bool = False
    lock: Any = field(default_factory=threading.RLock)


def _path() -> Path:
    return Path(os.environ.get("MCP_SERVERS") or _DEFAULT)


def _local_address(host: str) -> bool:
    """A literal address on this machine or the house's network. Names (but localhost) are refused: they can change."""
    host = (host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        a = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return not a.is_unspecified and (a.is_loopback or a.is_private or a.is_link_local)


def _servers() -> dict[str, _Server]:
    """The configured servers, re-read when the file changes; problems (a bad entry) are kept for `status`."""
    import tool_descriptors as T
    p = _path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        with _lock:
            _state.clear()
            _config_cache.update(path=str(p), mtime=None, problems={})
        return {}
    with _lock:
        if _config_cache["path"] == str(p) and _config_cache["mtime"] == mtime:
            return dict(_state)
        try:
            raw = json.loads(p.read_text()).get("servers") or {}
        except (OSError, ValueError, AttributeError):
            raw = {}
        servers: dict[str, _Server] = {}
        problems: dict[str, str] = {}
        for key, cfg in (raw.items() if isinstance(raw, dict) else ()):
            if not isinstance(cfg, dict) or cfg.get("enabled") is False:
                continue
            u = urllib.parse.urlsplit(str(cfg.get("url") or ""))
            allow = cfg.get("tools")
            if not _KEY_RX.match(str(key)):
                problems[str(key)[:20]] = "name must be 1-16 lowercase letters or digits"
            elif u.scheme not in ("http", "https") or not u.hostname:
                problems[key] = "url must be http(s)"
            elif not _local_address(u.hostname):
                problems[key] = ("url must be a literal address on this host or the house's network (egress isn't "
                                 "allowed for MCP servers yet)")
            elif cfg.get("domain") not in T.DOMAINS:
                problems[key] = "domain must be one of tool_descriptors.DOMAINS"
            elif allow is not None and not (isinstance(allow, dict) and all(
                    isinstance(n, str) and r in RISKS for n, r in allow.items())):
                problems[key] = "tools must be {tool name: read|write|dangerous}"
            else:
                url = u.geturl()
                if u.hostname == "localhost":
                    url = u._replace(netloc=u.netloc.replace("localhost", "127.0.0.1", 1)).geturl()
                srv = _Server(key, url, cfg["domain"],
                              frozenset(r for r in (cfg.get("roles") or []) if isinstance(r, str)),
                              dict(allow) if allow is not None else None,
                              str(cfg["token_file"]) if cfg.get("token_file") else None)
                old = _state.get(key)
                if old and (old.url, old.token_file, old.allow, old.domain) == (srv.url, srv.token_file, srv.allow, srv.domain):
                    old.roles = srv.roles
                    srv = old   # the same server: keep its session and tools
                servers[key] = srv
        _state.clear()
        _state.update(servers)
        _config_cache.update(path=str(p), mtime=mtime, problems=problems)
        return dict(servers)


def _events(resp: Any, deadline: float):
    """An event stream's data payloads, read line by line until the deadline (CRLF or LF)."""
    data: list[str] = []
    while time.monotonic() < deadline:
        raw = resp.readline(1_000_000)
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line and data:
            yield "\n".join(data)
            data = []
    if data:
        yield "\n".join(data)


def _response(m: Any, want: int) -> bool:
    return isinstance(m, dict) and m.get("id") == want and "method" not in m and ("result" in m or "error" in m)


def _read_reply(resp: Any, want: int, deadline: float) -> Any:
    """The JSON-RPC response with id `want`, from a JSON body or an event stream read until it arrives."""
    found = None
    if "text/event-stream" in resp.headers.get("Content-Type", ""):
        for ev in _events(resp, deadline):
            try:
                m = json.loads(ev)
            except ValueError:
                continue
            if _response(m, want):
                found = m
                break
    else:
        try:
            msgs = json.loads(resp.read(4_000_000).decode("utf-8", "replace"))
        except ValueError as exc:
            raise _Failed("answered with something that isn't JSON") from exc
        found = next((m for m in (msgs if isinstance(msgs, list) else [msgs]) if _response(m, want)), None)
    if found is None:
        raise _Failed("didn't reply to the request")
    if found.get("error") is not None:
        code = found["error"].get("code") if isinstance(found["error"], dict) else None
        raise _Failed(f"refused it (error {code})" if isinstance(code, int) else "refused it")
    return found.get("result")


def _rpc(srv: _Server, method: str, params: dict[str, Any] | None, timeout: float, *, notify: bool = False) -> Any:
    """One JSON-RPC message over Streamable HTTP; the result, or raises _Failed with a short reason in titan's words."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    want = next(_ids)
    if not notify:
        msg["id"] = want
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if method != "initialize":
        headers["MCP-Protocol-Version"] = srv.version
    if srv.session:
        headers["Mcp-Session-Id"] = srv.session
    if srv.token_file:
        try:
            headers["Authorization"] = "Bearer " + Path(srv.token_file).read_text().strip()
        except OSError as exc:
            raise _Failed("has a token file titan can't read") from exc
    req = urllib.request.Request(srv.url, data=json.dumps(msg).encode(), headers=headers, method="POST")
    deadline = time.monotonic() + timeout
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid and method == "initialize":
                srv.session = sid[:200]
            if notify:
                return None
            return _read_reply(resp, want, deadline)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and srv.session and method != "initialize":
            raise _Failed(_FORGOT) from exc
        raise _Failed(f"answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise _Failed("didn't answer") from exc


def _ensure_session(srv: _Server) -> None:
    """Initialize once, then tell the server so; nothing else is sent until both are done (srv.ready)."""
    with srv.lock:
        if srv.ready:
            return
        srv.session = None
        res = _rpc(srv, "initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                       "clientInfo": {"name": "titan", "version": "1"}}, LIST_TIMEOUT_S)
        v = res.get("protocolVersion") if isinstance(res, dict) else None
        srv.version = v if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else PROTOCOL
        _rpc(srv, "notifications/initialized", None, LIST_TIMEOUT_S, notify=True)
        srv.ready = True


def _with_session(srv: _Server, method: str, params: dict[str, Any], timeout: float) -> Any:
    """A request in the server's session, starting it again once if the server forgot it."""
    for attempt in (1, 2):
        _ensure_session(srv)
        try:
            return _rpc(srv, method, params, timeout)
        except _Failed as exc:
            if str(exc) != _FORGOT or attempt == 2:
                raise
            with srv.lock:
                srv.ready = False
    return None


def _titan_name(key: str, tool: str) -> str:
    return (PREFIX + key + "_" + re.sub(r"[^a-z0-9_]+", "_", tool.lower()).strip("_"))[:64]


def _plain(text: Any, limit: int) -> str:
    """A server's words for the prompt: one line, no markers, cut short."""
    import untrusted
    return untrusted.clean_name(str(text or ""), limit)


def _spec(tool: dict[str, Any], key: str) -> dict[str, Any]:
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    required = set(schema.get("required") or [])
    params = {}
    for pname, p in list((schema.get("properties") or {}).items())[:20]:
        p = p if isinstance(p, dict) else {}
        words = _plain(p.get("description") or p.get("title") or pname, 120)
        kind = p.get("type") if isinstance(p.get("type"), str) else None
        if kind and kind != "string":
            words += f" ({kind}{', JSON' if kind in ('object', 'array') else ''})"
        params[_plain(pname, 40)] = words + ("" if pname in required else " (optional)")
    desc = _plain(tool.get("description") or tool.get("title") or tool.get("name"), DESC_CHARS)
    return {"description": f"ON THE {key.upper()} SERVER (MCP; described by the server). {desc}", "parameters": params}


def _list(srv: _Server) -> dict[str, dict[str, Any]]:
    tools: dict[str, dict[str, Any]] = {}
    cursor = None
    for _ in range(10):
        res = _with_session(srv, "tools/list", {"cursor": cursor} if cursor else {}, LIST_TIMEOUT_S)
        res = res if isinstance(res, dict) else {}
        for t in res.get("tools") or []:
            if not isinstance(t, dict) or not isinstance(t.get("name"), str):
                continue
            if srv.allow is not None:
                risk = srv.allow.get(t["name"])
                if risk is None:
                    continue
            elif isinstance(t.get("annotations"), dict) and t["annotations"].get("readOnlyHint") is True:
                risk = "read"
            else:
                continue
            name = _titan_name(srv.key, t["name"])
            if name in tools:
                continue   # two of its tools with one titan name: the first stays
            tools[name] = {"name": t["name"], "risk": risk, "schema": t.get("inputSchema") or {}, "spec": _spec(t, srv.key)}
        cursor = res.get("nextCursor")
        if not isinstance(cursor, str) or not cursor:
            break
    return tools


def _refresh(srv: _Server) -> None:
    with srv.lock:
        try:
            tools = _list(srv)
            srv.tools, srv.listed_at, srv.failed_at, srv.error = tools, time.time(), 0.0, None
        except _Failed as exc:
            srv.failed_at, srv.error = time.time(), str(exc)
            if time.time() - srv.listed_at > STALE_S:
                srv.tools = {}
        finally:
            srv.busy = False


def _due(srv: _Server, now: float) -> bool:
    if srv.failed_at:
        return now - srv.failed_at >= RETRY_S
    return now - srv.listed_at >= LIST_TTL_S


def _all() -> dict[str, tuple[_Server, dict[str, Any]]]:
    """Every offered MCP tool. A server's first list is read inline (once); after that a due re-read runs in the
    background and the turn uses what's already known."""
    now = time.time()
    out: dict[str, tuple[_Server, dict[str, Any]]] = {}
    for srv in _servers().values():
        start = False
        with _lock:
            if _due(srv, now) and not srv.busy:
                srv.busy = start = True
        if start:
            if not srv.listed_at and not srv.failed_at:
                _refresh(srv)
            else:
                threading.Thread(target=_refresh, args=(srv,), name=f"mcp-list-{srv.key}", daemon=True).start()
        for name, t in srv.tools.items():
            out[name] = (srv, t)
    return out


def specs() -> dict[str, dict[str, Any]]:
    """{titan name: {"description", "parameters"}} for every offered MCP tool (roles are checked by the caller)."""
    return {n: t["spec"] for n, (_, t) in _all().items()}


def descriptor(name: str) -> Any:
    """The tool's Descriptor (its server's domain, the tool's risk), or None for a name that isn't one."""
    if not name.startswith(PREFIX):
        return None
    import tool_descriptors as T
    hit = _all().get(name)
    if not hit:
        return None
    srv, t = hit
    return T.Descriptor(srv.domain, t["risk"], (), None if t["risk"] == "read" else "none", t["risk"] == "dangerous")


def role_allows(name: str, role: str | None) -> bool:
    hit = _all().get(name)
    return bool(hit) and (not role or role == "admin" or role in hit[0].roles)


def _coerce(args: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """The model sends every parameter as a string; give the server the types its schema asks for."""
    props = schema.get("properties") or {}
    out: dict[str, Any] = {}
    for k, v in args.items():
        kind = (props.get(k) or {}).get("type") if isinstance(props.get(k), dict) else None
        if isinstance(v, str) and kind in ("integer", "number", "boolean", "object", "array"):
            s = v.strip()
            try:
                if kind == "integer":
                    v = int(s)
                elif kind == "number":
                    v = float(s)
                elif kind == "boolean":
                    v = {"true": True, "false": False, "yes": True, "no": False}.get(s.lower(), v)
                else:
                    v = json.loads(s)
            except ValueError:
                pass
        out[k] = v
    return out


def call(name: str, args: dict[str, Any]) -> tuple[bool, str, str, bool]:
    """Run one MCP tool: (ok, text, server key, titan's own words). When the last is False the text is the server's,
    and the caller wraps it as untrusted."""
    hit = _all().get(name)
    if not hit:
        return False, f"Not run: {name} isn't connected right now.", "", True
    srv, t = hit
    try:
        res = _with_session(srv, "tools/call", {"name": t["name"], "arguments": _coerce(args, t["schema"])},
                            CALL_TIMEOUT_S)
    except _Failed as exc:
        return False, f"Error: the {srv.key} server {exc}.", srv.key, True
    if not isinstance(res, dict):
        return False, f"Error: the {srv.key} server sent a reply titan can't read.", srv.key, True
    parts = []
    for c in res.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(str(c.get("text") or ""))
        elif isinstance(c, dict):
            parts.append(f"[{_plain(c.get('type', 'item'), 20)} not shown]")
    if not parts and res.get("structuredContent") is not None:
        parts.append(json.dumps(res["structuredContent"])[:RESULT_CHARS])
    text = "\n".join(parts).strip() or "(no content)"
    if len(text) > RESULT_CHARS:
        text = text[:RESULT_CHARS] + " …(cut)"
    return res.get("isError") is not True, text, srv.key, False


def status() -> dict[str, Any]:
    servers = _servers()
    out = {"file": str(_path()), "servers": {}, "refused": dict(_config_cache.get("problems") or {})}
    for key, srv in servers.items():
        if not srv.listed_at and not srv.failed_at:
            _refresh(srv)
        out["servers"][key] = {"domain": srv.domain, "roles": sorted(srv.roles | {"admin"}),
                               "allowlist": srv.allow is not None, "tools": len(srv.tools), "error": srv.error}
    return out


def _main(argv: list[str]) -> int:
    if argv[:1] == ["status"]:
        print(json.dumps(status(), indent=2))
        return 0
    if argv[:1] == ["tools"]:
        for n, (srv, t) in sorted(_all().items()):
            print(f"{n}\t{srv.domain}\t{t['risk']}")
        return 0
    print("usage: mcp_host.py status | tools", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
