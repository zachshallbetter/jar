#!/usr/bin/env python3
"""
Titan Agent HTTP API
====================
FastAPI server that wraps agent_runtime_lib._run_agent_turn() so any local
application (Jellybean, web, scripts, ai-component-creator) can call the
household agent over HTTP with streaming and optional workspace sandboxing.

Endpoints
---------
GET  /health                              — liveness / readiness
(Every route, generated from this file: docs/api-routes.md; python3 scripts/api_routes.py.)

# Household identity (scripts/household_identity.py; runbook › Household identity and pairing)
POST   /v1/pair/start                     — (admin) {person_id | new_person:{name,role}, label?} → {code, expires_at, person, qr}
POST   /v1/pair/claim                     — (no auth) {code, device_name, platform} → {token, device, person, servers}
GET    /v1/me                             — {person, device, role, can}
GET    /v1/people                         — (admin) people with their devices
POST   /v1/people                         — (admin) {name, role}
PATCH  /v1/people/{id}                    — (admin) {name?, role?}
DELETE /v1/devices/{id}                   — (admin, or the device itself) revoke
Every other route needs X-Agent-Token (the master token = Zach/admin, or a paired device's
token = its person); non-admin roles reach only the routes household_identity.ROUTE_ROLES lists.

# Knowledge base
GET  /v1/kb                               — list the KB entries the caller may see (kb_privacy.py)
GET  /v1/kb/{entry_id}                    — one entry; 200 {} when it's gone or the caller can't see it (not 404)
POST /v1/kb                               — create  {title, body, domain?, share?}  (private to the caller
                                             unless share: household | adults | names)
DELETE /v1/kb/{entry_id}                  — delete  (the owner or the admin; 404 if not visible)
PUT    /v1/kb/{entry_id}                  — update  {title?, body?, domain?}  (the owner or the admin)
POST   /v1/kb/{entry_id}/share            — {to: "Robin" | "adults" | "household"} → {audience, visible_to}
POST   /v1/kb/{entry_id}/unshare          — {from?: "Robin"}; no from: private again
# Tasks and tools
GET  /v1/tasks                            — the caller's durable tasks (own, shared, their kids'; the admin the same, #1239; none for a household device), with owner, moved_at, last_step, autonomy (jellybean#228)
POST /v1/tools/{name}                     — run one tool  {args: {...}} | {argv: [k, v, ...]}

# Workspaces  (a git-init folder under .ai-workspaces)
POST   /v1/workspaces                     — create  {driver:"worktree", label?}; any other driver is 400
GET    /v1/workspaces/{ws_id}             — status + process list
DELETE /v1/workspaces/{ws_id}             — discard (wipe the worktree / kill sandbox)
POST   /v1/workspaces/{ws_id}/apply       — stop its processes and commit its changes in its own repo (merges nothing)
GET    /v1/workspaces/{ws_id}/files       — list directory  ?path=.
GET    /v1/workspaces/{ws_id}/files/read  — read file       ?path=src/foo.ts
POST   /v1/workspaces/{ws_id}/files       — write file      {path, content}
POST   /v1/workspaces/{ws_id}/run         — run command     {kind: install|build|test|lint|typecheck|gitStatus|gitDiff}
POST   /v1/workspaces/{ws_id}/processes   — start process   {process_id, command, args:[]}
DELETE /v1/workspaces/{ws_id}/processes/{pid} — stop process

# Agent
POST /v1/prompt                           — one-shot agent turn  {prompt, history?, tools?, workspace_id?, device?,
                                            client?}  (client "iphone": only the phone's device tools —
                                            calendar, reminders, contacts — and no Mac field state)
POST /v1/prompt/stream                    — SSE streaming turn   (same body)
POST /v1/device/result/{turn_id}          — outcome of the pending device action
                                            {request_id, result} | {request_id, error}
                                            (/v1/device/reply/{turn_id} is an alias)
GET  /v1/turns/{turn_id}                  — poll turn state

# Cross-device (scripts/cross_device.py; runbook › Cross-device): a phone turn runs Mac tools on the
# same person's Mac, waking it when needed.
GET  /v1/device/inbox?wait=25&device_key=&name=   — the Mac's long-poll → {"requests": [{id, turn_id, tool,
                                            args, risk, origin: {device, platform}, approved}]}
POST /v1/device/inbox/{id}/result         — the Mac's result {result, status: done|needs_confirmation|refused|
                                            failed, confirm?: {summary, detail, risk, domain, invitees?}}
POST /v1/device/presence                  — the Mac's heartbeat {device_key, name, platform: "mac", lan_ip,
                                            mac_address, wake_port, on_tailnet, awake}
GET  /v1/device/presence                  — the asking person's Macs, with live/wakeable
POST /v1/device/confirm/{turn_id}         — the phone answers a `confirm` event {confirm_id, approved,
                                            remember_session}

SSE event types
---------------
  event: status   data: {"text": "Searching the knowledge base…"}      (device turns: running tool)
  event: delta    data: {"text": "..."}
  event: device   data: {"turn_id","request_id","tool","args","risk"}  (device=true turns: the
                  app runs the action on the Mac, then POSTs /v1/device/result/{turn_id})
  event: remote   data: {"step": "waking"|"working"|"done", "device": "Zach's Mac"}  (phone turns: a Mac tool)
  event: confirm  data: {"confirm_id", "device", "summary", "detail", "risk", "domain", "invitees"?}
                  (the Mac needs the user's OK: answer on POST /v1/device/confirm/{turn_id}; 110 s → declined)
  event: done     data: {"text": "<full reply>", "truncated": bool,
                         "sources": [{"source_id","title","snippet"}]}   (KB entries the reply drew on)
  event: error    data: {"message": "..."}

Workspace drivers
-----------------
  worktree  — git worktree in .ai-workspaces/ws_<id>/  on the titan host (the only driver; e2b and cube were removed,
              titan-node#1624: no key, no unit, nothing used them)

Memory (L0-L3 pyramid)
-----------------------
  agent_memory.AgentMemory is loaded at startup (SQLite, pure-Python BM25).
  Each turn: recall() → inject context → run → add_message() → extract_facts().

Skills
------
  SKILL.md files in scripts/skills/*/SKILL.md are loaded at startup. Every adult's and the
  admin's turn lists each skill's name and description in the static prompt prefix; the full
  guide of a skill picked for the turn follows the prefix, and any other is read on demand with
  the skill_guide tool.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import date, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import agent_runtime_lib as _lib  # noqa: E402
import household_identity as _hh  # noqa: E402
import cross_device as _xd  # noqa: E402
from ops_ports import AGENT_API_PORT  # noqa: E402
import reply_stream  # noqa: E402

# ── Optional: agent memory (L0-L3 pyramid) ───────────────────────────────────
try:
    from agent_memory import get_memory as _get_memory, TokenCounts as _TokenCounts
    _MEMORY_AVAILABLE = True
except Exception:
    _MEMORY_AVAILABLE = False
    def _get_memory():  # type: ignore
        return None

    class _TokenCounts:  # type: ignore — stub when memory module absent
        def __init__(self, **kw): self.__dict__.update(kw)
        estimated_savings_tokens = 0

# ── Optional: agent skills (SKILL.md loader) ─────────────────────────────────
try:
    from agent_skills import load_skills as _load_skills, skills_index_prompt as _skills_index
    from agent_skills import skill_guide_prompt as _skill_guide_prompt
    from agent_skills import pick_by_triggers as _pick_by_triggers
    from agent_skills import parse_skill_text as _parse_skill_text
    _SKILLS = _load_skills()
    _SKILLS_AVAILABLE = True
except Exception:
    _SKILLS = []
    _SKILLS_AVAILABLE = False

STARTED_AT = time.time()
DEVICE_TIMEOUT_SECONDS = 120

# Memoize at import time — 14 927 chars, never changes at runtime.
# Calling build_tool_system_prompt() per turn is pure waste and breaks
# the static/dynamic prompt split needed for future prompt-cache prefixes.
_TOOL_SYSTEM_PROMPT: str = _lib.build_tool_system_prompt()

# Keyed by turn_id; accumulated across the lifetime of each conversation turn.
_session_token_stats: dict[str, "_TokenCounts"] = {}   # a running turn's; _run_turn takes it at the end
_token_savings_done = 0   # the finished turns' estimated savings (/health)


def _skill_match(question: str, skills: list) -> tuple[bool, "set[str] | None"]:
    """Return (needs_full_body, active_skill_names | None).
    True + matched set → inject only matched skills at full body.
    False + None → inject all skills descriptions-only (lower token cost)."""
    picked = _lib.jev_pick_skill(question, {s.name: s.description for s in skills})
    if picked:
        return True, {picked}
    if picked == "":   # Jev is sure no skill fits — don't let "social media" load the media skill
        return False, None
    # No verdict from Jev (off, as on a box; no key; an error; unsure): each SKILL.md's triggers decide,
    # at most one guide (agent_skills.pick_by_triggers, #402).
    local = _pick_by_triggers(question, skills)
    return (True, {local}) if local else (False, None)

# ---------------------------------------------------------------------------
# Workspace management
# ---------------------------------------------------------------------------

_WORKSPACES_ROOT = _HERE.parent / ".ai-workspaces"
# mkdir deferred to startup hook — don't side-effect at import time

_workspaces: dict[str, dict] = {}  # ws_id -> metadata
_processes: dict[str, dict] = {}   # "ws_id:proc_id" -> state


def _safe_id(raw: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", raw)


def _worktree_path(ws_id: str) -> Path:
    return _WORKSPACES_ROOT / f"ws_{_safe_id(ws_id)}"


def _guard_path(ws_id: str, rel: str) -> Path:
    """Resolve rel inside the worktree and raise if it escapes the sandbox."""
    root = _worktree_path(ws_id).resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal denied")
    return target


def _run_sync(cmd: list[str], cwd: Path, timeout: int = 60) -> dict:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return {"success": r.returncode == 0, "stdout": r.stdout, "stderr": r.stderr}
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": f"timed out after {timeout}s"}
    except Exception as exc:
        return {"success": False, "stdout": "", "stderr": str(exc)}


async def _create_worktree_workspace(ws_id: str) -> dict:
    path = _worktree_path(ws_id)
    path.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_run_sync, ["git", "init"], path)
    meta = {"ws_id": ws_id, "driver": "worktree", "path": str(path), "created_at": time.time()}
    _workspaces[ws_id] = meta
    return meta


# ---------------------------------------------------------------------------
# In-memory turn registry
# ---------------------------------------------------------------------------
_turns: dict[str, dict] = {}
_turns_lock = threading.Lock()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Titan Agent API", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Optional shared-secret auth. Set AGENT_API_TOKEN (or AGENT_API_TOKEN_FILE —
# preferred: an inline secret on the process argv is visible to any local user
# via `ps aux`/`/proc/<pid>/cmdline`, unlike a 0600 file only the owner can
# read) to enable. /health is always public so load-balancers can probe it
# without a token.
def _load_agent_token() -> str:
    kf = os.environ.get("AGENT_API_TOKEN_FILE", "").strip()
    if kf:
        try:
            return Path(kf).read_text().strip()
        except OSError:
            pass
    return os.environ.get("AGENT_API_TOKEN", "").strip()


_AGENT_TOKEN: str = _load_agent_token()

# ── Optional: Ed25519-JWT verification for external callers (e.g. ASE) ──────
# Distinct from the fleet's internal COORDINATOR_PUBLIC_KEY trust — external
# surfaces get their own registered identity/keypair rather than the shared
# coordinator key, so a compromised or over-broad external caller can't
# impersonate the coordinator across the rest of the fleet. Off (no tokens
# accepted via this path) until ASE_PUBLIC_KEY_FILE/ASE_PUBLIC_KEY is set;
# the static X-Agent-Token path above keeps working unconditionally either way.
import base64 as _base64
import binascii as _binascii


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return _base64.urlsafe_b64decode(s + pad)


def _load_ase_public_key():
    pem = None
    kf = os.environ.get("ASE_PUBLIC_KEY_FILE", "").strip()
    if kf:
        try:
            with open(kf, "rb") as f:
                pem = f.read()
        except Exception:
            pem = None
    if pem is None:
        inline = os.environ.get("ASE_PUBLIC_KEY", "").strip()
        if inline:
            pem = inline.replace("\\n", "\n").encode()
    if not pem:
        return None
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        return load_pem_public_key(pem)
    except Exception:
        return None


_ASE_PUBLIC_KEY = _load_ase_public_key()
_ASE_ALLOWED_AUDS = {"memory", "fleet"}  # surfaces this identity is trusted for


def _verify_ase_jwt(token: str) -> bool:
    """Verify an Ed25519-signed JWT from the registered 'ase' identity.

    Checks: well-formed compact JWT, alg=EdDSA, signature against the
    registered ASE public key, exp not elapsed, iss/sub == "ase", and
    aud in _ASE_ALLOWED_AUDS (a token scoped to a surface this identity
    isn't trusted for should not work here).
    """
    if _ASE_PUBLIC_KEY is None:
        return False
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        if header.get("alg") != "EdDSA":
            return False
        payload = json.loads(_b64url_decode(payload_b64))
        sig = _b64url_decode(sig_b64)
        signing_input = (header_b64 + "." + payload_b64).encode()
        _ASE_PUBLIC_KEY.verify(sig, signing_input)  # raises on bad signature
        now = int(time.time())
        if int(payload.get("exp", 0)) < now:
            return False
        if payload.get("iss") != "ase" or payload.get("sub") != "ase":
            return False
        if payload.get("aud") not in _ASE_ALLOWED_AUDS:
            return False
        return True
    except (ValueError, KeyError, TypeError, _binascii.Error, json.JSONDecodeError):
        return False
    except Exception:
        # Includes cryptography's InvalidSignature — any verification failure
        # here means "reject", never "let it through".
        return False


# Paths anyone may call: the load-balancer probe and a new device claiming its pairing code
# (rate limited in household_identity).
_PUBLIC_PATHS = frozenset({"/health", "/v1/pair/claim", "/v1/watch/pair/request", "/v1/watch/hello",
                           "/v1/screen/pair/request",   # an Apple TV asks to be paired (#411); the admin confirms
                           "/v1/public/now",   # public by construction (#1043): no token needed, nothing private in it
                           # A box's first run (#411): the page holds nothing; every /v1/setup/* call checks the box's
                           # one-time setup key itself, and all of them are 404 on titan and on a box that's set up.
                           "/setup", "/v1/setup/state", "/v1/setup/owner", "/v1/setup/people", "/v1/setup/pair",
                           "/v1/setup/calendars", "/v1/setup/claim", "/v1/setup/finish", "/v1/setup/recovery",
                           "/v1/setup/home"})
# A kid's Watch polling for its token (titan-node#755): no token yet; its own secret (a header) is the check.
_PUBLIC_PREFIXES = ("/v1/watch/pair/request/",
                    "/v1/screen/pair/request/",   # …and a TV polling for its token with its own secret (#411)
                    # A live camera's playlist and segments (titan-node#413): the 128-bit session id in the path is the
                    # credential (AVPlayer's segment requests), home network only, minutes long; a token sent along
                    # must be the starting device's (home_live_fetch).
                    "/v1/home/live/")


_LIVE_SESSION_RX = re.compile(r"(/v1/home/live/)[0-9a-f]{32}")


class _HideLiveSessions:
    """uvicorn's access log names every path; a live camera session's id is the credential for its fetches (#413), so
    it never reaches the log: /v1/home/live/<id>/… is written /v1/home/live/-/…."""
    def filter(self, record: Any) -> bool:
        args = record.args
        if isinstance(args, tuple) and any(isinstance(a, str) and "/v1/home/live/" in a for a in args):
            record.args = tuple(_LIVE_SESSION_RX.sub(r"\1-", a) if isinstance(a, str) else a for a in args)
        elif isinstance(record.msg, str) and "/v1/home/live/" in record.msg:
            record.msg = _LIVE_SESSION_RX.sub(r"\1-", record.msg)
        return True


import logging as _logging  # noqa: E402
_logging.getLogger("uvicorn.access").addFilter(_HideLiveSessions())


def _resolve_identity(request: Request) -> "_hh.Identity | None":
    """The one place a request's credential becomes (person, device, role):
      - the master token (AGENT_API_TOKEN) → Zach, admin
      - a paired device's token → that device's person and their role (revoked/unknown → None)
      - the ASE's Ed25519 JWT → Zach, admin (Zach's IDE)
      - no credential configured at all (local/dev) → Zach, admin, unless a device token is sent."""
    presented = request.headers.get("X-Agent-Token", "")
    ident = _hh.resolve(presented, _AGENT_TOKEN)
    if ident is not None:
        if ident.device and request.headers.get("X-Client-Version"):
            try:   # which Jellybean this device runs (capabilities: "Zach's iPhone (Jellybean 0.12.9, …)")
                _store().set_device_version(ident.device["id"], _xd.clean_version(request.headers["X-Client-Version"]))
            except Exception:  # noqa: BLE001 - never fails a request
                pass
        return ident
    # Not the master token or a device's — it may be a JWT instead (ASE sends its minted token in
    # both X-Agent-Token and Authorization).
    jwt_candidate = presented or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if jwt_candidate and _verify_ase_jwt(jwt_candidate):
        return _hh.owner_identity("ase")
    if not _AGENT_TOKEN and _ASE_PUBLIC_KEY is None:
        return _hh.owner_identity("open")   # no credential type configured: auth disabled
    return None


def _identity(request: Request) -> "_hh.Identity":
    return getattr(request.state, "identity", None) or _hh.owner_identity("open")


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_PATHS or (request.method == "GET" and path.startswith(_PUBLIC_PREFIXES)):
        return await call_next(request)
    ident = _resolve_identity(request)
    if ident is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # A kid's Watch (titan-node#755) reaches its own few routes only, whatever its role may call elsewhere.
    if ident.via == "watch" and not _hh.watch_route_allowed(request.method, path):
        return JSONResponse({"error": "forbidden", "detail": _hh.REFUSAL}, status_code=403)
    # A paired executor (#1631) reaches its presence and its own inbox only.
    if ident.via == "executor" and not _hh.executor_route_allowed(request.method, path):
        return JSONResponse({"error": "forbidden", "detail": _hh.REFUSAL}, status_code=403)
    # Roles are enforced here, not just hidden in the clients: a kid's or a shared device's token
    # reaches only the routes household_identity.ROUTE_ROLES lists for its role.
    if not _hh.route_allowed(ident.role, request.method, path):
        return JSONResponse({"error": "forbidden", "detail": _hh.REFUSAL}, status_code=403)
    # A TV in sitter mode (jellybean#428) reaches only the sitter screen: leaving it shows nothing of the household.
    if ident.role == "household" and (ident.device or {}).get("id"):
        import sitter
        if sitter.blocked((ident.device or {}).get("id"), request.method, path):
            return JSONResponse({"error": "This TV is in sitter mode.", "code": "sitter_mode"}, status_code=403)
    request.state.identity = ident
    return await call_next(request)


def _install_shutdown_hook() -> None:
    """On SIGTERM/SIGINT, end every long-poll at once (a clean 503 the apps retry), then hand the
    signal to uvicorn. Without this, each deploy cut open long-polls off as 500 "Task cancelled,
    timeout graceful shutdown exceeded" (09-30: 28 on /v1/cards/wait, 22 on /v1/device/inbox)."""
    import signal
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prev = signal.getsignal(sig)
        except (ValueError, OSError):
            continue

        def handler(signum, frame, _prev=prev):
            _xd.SHUTTING_DOWN.set()
            if callable(_prev):
                _prev(signum, frame)
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):   # not the main thread (tests)
            pass


def _stopping_response() -> JSONResponse:
    return JSONResponse({"error": "titan is restarting", "retry": True}, status_code=503, headers={"Retry-After": "5"})


def _warm_items() -> dict[str, Any]:
    """#1181: every items reader once for each live person and once as a shared screen, at start, so the first
    request after a deploy doesn't pay for the cold caches (kb_privacy's per-source metadata, the record nodes, the
    stores' connections). Reads only; counts and time printed, never content. → {"people", "ms"}."""
    t0 = time.monotonic()
    who = []
    try:
        for p in _store().people():
            role = str(p.get("role") or "")
            if role in ("admin", "adult", "kid"):
                who.append(_hh.Identity({"id": p["id"], "name": p.get("name"), "role": role}, None, role, "warm"))
        who.append(_hh.Identity({"id": "household", "name": "household", "role": "household"}, None, "household", "warm"))
    except Exception as exc:  # noqa: BLE001 - never block startup
        print(f"[items] warm at start skipped: {type(exc).__name__}", file=sys.stderr)
        return {"people": 0, "ms": 0}
    for ident in who:
        try:
            for read in _item_readers(ident, _lib._local_now()).values():
                try:
                    read()
                except Exception:  # noqa: BLE001 - a store down at start is the request's to report
                    pass
        except Exception:  # noqa: BLE001
            pass
    ms = round((time.monotonic() - t0) * 1000)
    print(f"[items] warm at start: {len(who)} viewers, {ms} ms", file=sys.stderr)
    return {"people": len(who), "ms": ms}


def _box_setup_at_start() -> None:
    """A box out of the carton (#411): its setup key exists from boot (the sticker, `box_setup.py url`); never logged."""
    try:
        import box_setup
        if box_setup.needs_setup(_hh.get_store()):
            box_setup.ensure_key()
            box_setup.rearm()   # a power-cycle gives the key a fresh 30 minutes
            print("[setup] this box isn't set up yet: open /setup with its key (python3 box_setup.py url)", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - never block startup
        print(f"[setup] skipped: {type(exc).__name__}", file=sys.stderr)


_MEETING_NOTES_KICK = threading.Event()
MEETING_NOTES_SWEEP_S = 600.0


def _meeting_notes_worker() -> None:
    """#1364: meeting notes read the transcript from its Mac through cross_device, whose channel lives in this process
    (the Macs long-poll it), so the pass runs here: kicked when a meeting_summary arrives (its Mac is awake then) and
    swept every 10 min for the retries. One pass at a time; a failed pass costs those notes, never the server."""
    def busy() -> bool:   # a chat turn in flight: the derive waits for it (the brain has one slot)
        with _turns_lock:
            return any(t.get("state") in ("running", "device_pending") for t in _turns.values())

    first = True
    while True:   # never exits: a pass that fails is logged by its exception's name and the next one runs
        try:
            _MEETING_NOTES_KICK.wait(60.0 if first else MEETING_NOTES_SWEEP_S)   # after a restart: the pending rows soon
            first = False
            _MEETING_NOTES_KICK.clear()
            import meeting_notes
            st = meeting_notes.run_pass(busy=busy)
            if st.get("summaries"):
                print(f"[meeting_notes] {st['outcomes']}", file=sys.stderr)   # counts by outcome only
        except Exception as exc:  # noqa: BLE001
            print(f"[meeting_notes] pass skipped: {type(exc).__name__}", file=sys.stderr)
            time.sleep(30)


@app.on_event("startup")
async def _startup() -> None:
    _install_shutdown_hook()

    def _warm_links() -> None:   # the scored link graph, built once at start so no request pays for it (#1115)
        try:
            import hooks
            import linker
            with contextlib.closing(hooks.connect()) as con:
                linker.scored(con)
        except Exception as exc:  # noqa: BLE001 - never block startup
            print(f"[linker] warm at start skipped: {type(exc).__name__}", file=sys.stderr)
        _warm_items()           # then the items surface's readers (#1181), once the graph they read is built
    threading.Thread(target=_warm_links, name="linker-warm", daemon=True).start()
    threading.Thread(target=_meeting_notes_worker, name="meeting-notes", daemon=True).start()
    _box_setup_at_start()
    # A switch silently off because its env-file line carried a comment (titan-node#940): say so once, names only.
    try:
        import switch_check
        line = switch_check.warn()
        if line:
            print(line, file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - never block startup
        print(f"[switches] check skipped: {exc}", file=sys.stderr)
    _WORKSPACES_ROOT.mkdir(exist_ok=True)
    # Triggers cut off by the previous process (every deploy restarts this API) get one retry.
    try:
        import proactive
        r = proactive.recover_interrupted()
        if any(r.values()):
            print(f"[proactive] after restart: {r}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - never block startup
        print(f"[proactive] recovery skipped: {exc}", file=sys.stderr)
    # Uploads past their time (titan-node#713) go now; every upload sweeps too.
    try:
        import uploads
        await asyncio.to_thread(uploads.sweep)
    except Exception as exc:  # noqa: BLE001 - never block startup
        print(f"[uploads] sweep skipped: {exc}", file=sys.stderr)
    # Generated pictures past their 30 days go now too; every new picture sweeps as well.
    try:
        n = await asyncio.to_thread(_lib.sweep_generated)
        if n:
            print(f"[images] swept {n} generated pictures past their time", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - never block startup
        print(f"[images] sweep skipped: {type(exc).__name__}", file=sys.stderr)
    # Departures held for the jitter window when the previous process stopped (jellybean#666) pick up where they were.
    try:
        r = await _resume_held_departures()
        if any(r.values()):
            print(f"[places] held departures after restart: {r}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - never block startup
        print(f"[places] held departures skipped: {exc}", file=sys.stderr)
    asyncio.get_running_loop().create_task(_routine_nudger())
    asyncio.get_running_loop().create_task(_scheduled_runner())
    # Bonjour (_titan._tcp.local.) so a new iPhone finds titan on the Wi-Fi. Only when the server
    # is run for real (__main__ sets TITAN_DISCOVERY=1); tests that import the app never advertise.
    if os.environ.get("TITAN_DISCOVERY") == "1":
        global _advertiser
        _advertiser = _hh.Advertiser(port=int(os.environ.get("AGENT_API_PORT", str(AGENT_API_PORT)) or AGENT_API_PORT))
        status = await asyncio.to_thread(_advertiser.start)
        print(f"[discovery] {status}", file=sys.stderr)


_advertiser: "_hh.Advertiser | None" = None


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _advertiser is not None:
        await asyncio.to_thread(_advertiser.stop)
        print(f"[discovery] {_advertiser.status}", file=sys.stderr)


async def _routine_nudger() -> None:
    """Every 30 s: a routine whose time has come gets a "Time for …" card (once a day)."""
    import routines
    while True:
        try:
            n = await asyncio.to_thread(routines.nudge_due)
            if n:
                print(f"[routines] nudged {n}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[routines] nudge failed: {exc}", file=sys.stderr)
        try:
            if _lib.world_on():
                import world
                n = await asyncio.to_thread(world.send_asks)   # a kid's statement held through quiet hours (#358)
                n += await asyncio.to_thread(world.daily_questions)   # one a day per adult, from 09:00 (#484)
                if n:
                    print(f"[world] asked {n}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[world] asks failed: {exc}", file=sys.stderr)
        try:
            import push
            for line in await asyncio.to_thread(push.send_trip_starts):   # trips started with the app closed (jellybean#753)
                print(f"[push] {line}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[push] trip starts failed: {type(exc).__name__}", file=sys.stderr)
        await asyncio.sleep(30)


# What titan is doing right now, for the apps' idle motes (jellybean#329 Phase 5): short labels, per viewer, never
# content. Steps this process runs register here; titan's own timer jobs are read from systemd.
_BACKGROUND: dict[str, dict] = {}
_BACKGROUND_LOCK = threading.Lock()
# unit → (label, who sees it: "admin" | "adults")
_BACKGROUND_UNITS = {
    "titan-reflection-dream.service": ("Reflecting on yesterday", "adults"),
    "titan-world-notes.service": ("Reading the day's notes", "adults"),
    "titan-reflection-letter.service": ("Writing the evening letter", "admin"),
    "titan-household-eval.service": ("Checking its own answers", "admin"),
}
_UNITS_CACHE: dict[str, Any] = {"at": 0.0, "active": set()}


def _background_note(key: str, label: str | None, owner: str | None = None) -> None:
    """Register (label) or clear (None) one running step, seen by its owner only."""
    with _BACKGROUND_LOCK:
        if label is None:
            _BACKGROUND.pop(key, None)
        else:
            _BACKGROUND[key] = {"label": label[:60], "owner": (owner or "").lower()}


def _active_units() -> set[str]:
    now = time.time()
    if now - _UNITS_CACHE["at"] > 15:
        try:
            out = subprocess.run(["systemctl", "is-active", *_BACKGROUND_UNITS], capture_output=True, text=True,
                                 timeout=3).stdout.split()
            _UNITS_CACHE["active"] = {u for u, st in zip(_BACKGROUND_UNITS, out) if st in ("active", "activating")}
        except Exception:  # noqa: BLE001
            _UNITS_CACHE["active"] = set()
        _UNITS_CACHE["at"] = now
    return _UNITS_CACHE["active"]


def background_for(person: str | None, role: str | None) -> list[dict[str, str]]:
    """[{id, label}] of what titan is doing now that this viewer may know about: their own running task step or
    scheduled action, and titan's own jobs (the admin sees all; adults the household ones). A kid or a shared
    device sees only their own steps. Empty when idle."""
    out: list[dict[str, str]] = []
    pid = (person or "").lower()
    with _BACKGROUND_LOCK:
        mine = [(k, v["label"]) for k, v in _BACKGROUND.items() if pid and v["owner"] == pid]
    out += [{"id": k, "label": lab} for k, lab in mine]
    try:
        import scheduled
        for r in scheduled.list_for(person, role):
            if r.get("state") == "running" and str(r.get("owner") or "").lower() == pid:
                out.append({"id": f"scheduled-{r['id']}", "label": f"Doing “{str(r.get('what') or '')[:40]}”"})
    except Exception:  # noqa: BLE001
        pass
    if role in ("admin", "adult"):
        for unit in sorted(_active_units()):
            label, who = _BACKGROUND_UNITS[unit]
            if who == "adults" or role == "admin":
                out.append({"id": unit.split(".")[0], "label": label})
    return out


def _run_due_tasks() -> list[dict]:
    """Tasks advancing on their own (#343, behind AGENT_TASK_AUTONOMY): at most one step each, as the owner."""
    import scenarios
    if not scenarios.enabled():
        return []
    import changes as chg
    import grants
    import proactive
    import scheduled
    import task_runner
    import world
    store = scheduled._store(None)

    def person_of(owner: str) -> dict | None:
        return store.person(owner) if store is not None else None

    def execute_step(s: dict, ctx: str, person: dict, chained: bool = False, reads: dict | None = None) -> str:
        # The owner's own role, always (person_of found it, or the runner never got here): never a default.
        # chained (#343, TASK_CHAIN): a read-only step after the run's first one (run_task's chained tool set)
        io = _lib._TURN_IO
        io.task_context, io.task_autonomous, io.task_chained = ctx, True, bool(chained)
        io.task_reads = reads or None   # #395: the owner's own Mac, only for the reads the task's grant names
        _background_note(f"task-{s['task_id']}", f"{str(s.get('goal') or 'A task')[:40]}: taking a step", s["owner"])
        try:
            ok, text = scheduled._execute_as({"id": f"task-{s['task_id']}", "owner": s["owner"], "tool": "run_task",
                                              "args": {"task_id": s["task_id"], "max_steps": 1}},
                                             person["role"], person.get("name"),
                                             scheduled.mac_hook if reads else None)
            return text
        finally:
            io.task_context, io.task_autonomous, io.task_chained, io.task_reads = None, False, False, None
            _background_note(f"task-{s['task_id']}", None)

    def card(title: str, detail: str, *, owner: str, key: str, urgency: int = 1, actions=None) -> None:
        proactive.add_card(title, detail, kind="update", urgency=urgency, source="tasks", to=[owner], key=key,
                           actions=actions)

    def current_step(tid: str) -> tuple[str, str] | None:
        # the step the task would take next (the workflow runtime's current step), for a cap to approve (#707)
        try:
            inst = _lib._runtime_request("workflow", f"/v1/instances/{tid}", "GET")
            return (str(inst.get("current_step_key") or ""), "") if inst.get("current_step_key") else None
        except Exception:  # noqa: BLE001
            return None

    def cap_card(title: str, detail: str, **kw: Any) -> None:
        proactive.add_card(title, detail, **kw)

    return task_runner.run_due(
        current_step=current_step, cap_card=cap_card,
        execute_step=execute_step, card=card, changes_fn=chg.changes,
        world_fn=lambda ids, since, person, role: world.get().changes(ids, since, person, role),
        describe=chg.describe,
        grant_use=lambda gid, owner, tid: grants.use(gid, owner, "run_task", {"task_id": tid}, classify=_lib.effect_class),
        person_of=person_of,
        reads_ok=lambda gid, owner: grants.check(gid, owner, "read_file", {}, classify=_lib.effect_class) is None)


def _mac_connected(person_id: str) -> bool:
    """Whether one of this person's Macs holds a live long-poll on titan now (never wakes one)."""
    try:
        macs = _store().macs_for(person_id)
    except Exception:  # noqa: BLE001
        return False
    return any(_xd.CHANNEL.live((person_id, m["device_key"])) for m in macs)


def _send_calendar_hold(row: dict) -> tuple[str, str]:
    """One waiting calendar write (#1612) to its owner's Mac, as a scheduled action goes: execute_tool as the owner,
    with their role now, the Mac's own trust settings deciding whether it asks."""
    import calendar_hold
    import scheduled
    person = None
    try:
        person = _store().person(row["owner"])
    except Exception:  # noqa: BLE001
        person = None
    if person is None:
        return "failed", "whoever asked for it is no longer in the household."
    ok, text = scheduled._execute_as({"id": row["id"], "owner": row["owner"], "tool": row["tool"], "args": row["args"]},
                                     person.get("role") or "adult", person.get("name"), scheduled.mac_hook)
    return calendar_hold.classify(ok, text), text


def _read_calendar_day(row: dict, day: str) -> tuple[str, str]:
    """A check (#1612): the owner's calendar for the day a maybe-done write was on, read on their Mac. Never the write."""
    return _send_calendar_hold({**row, "tool": "list_calendar_events", "args": {"start": day, "end": day}})


def _drain_calendar_holds() -> list[dict]:
    import calendar_hold
    return calendar_hold.drain(live=_mac_connected, send=_send_calendar_hold, read=_read_calendar_day)


async def _scheduled_runner() -> None:
    """Every 20 s: scheduled actions whose time has come run, once each (scheduled.py). It runs here,
    not only from the timer, because a Mac action goes through this process's device channel."""
    import scheduled
    while True:
        try:
            for x in await asyncio.to_thread(scheduled.run_due, device=scheduled.mac_hook):
                print(f"[scheduled] {x['id']} {x['state']}: {x['result'][:120]}", file=sys.stderr)
            for x in await asyncio.to_thread(_run_due_tasks):
                print(f"[tasks] {x['task_id']} {x['status']}", file=sys.stderr)
            for x in await asyncio.to_thread(_drain_calendar_holds):   # #1612: a Mac back takes its waiting writes
                print(f"[calendar-hold] {x['id']} {x['status']}", file=sys.stderr)
            import proactive
            await asyncio.to_thread(proactive.wake_snoozed)   # #1039: a snoozed card back, pushed once to its person
            import push
            for line in await asyncio.to_thread(push.clear_expired):   # an expired card leaves the phones, once
                print(f"[push] expired {line}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduled] run failed: {exc}", file=sys.stderr)
        await asyncio.sleep(20)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

# /health's memory_turns: a COUNT(*) over every chat message, which ran on the event loop on each probe (the canary,
# the apps, the box). Counted off the loop and kept HEALTH_TURNS_TTL_S: a turn count a few seconds old is fine there.
HEALTH_TURNS_TTL_S = 30.0
_health_turns: dict[str, float] = {"at": float("-inf"), "n": 0}


async def _memory_turns() -> int:
    now = time.monotonic()
    if now - _health_turns["at"] < HEALTH_TURNS_TTL_S:
        return int(_health_turns["n"])
    try:
        n = await asyncio.to_thread(_get_memory().turn_count)
    except Exception:
        return 0   # unreadable: 0, and asked again next probe (only a count is kept)
    _health_turns.update(at=now, n=n)
    return n


@app.get("/health")
async def health() -> JSONResponse:
    mem_turns = 0
    if _MEMORY_AVAILABLE:
        mem_turns = await _memory_turns()
    total_savings = _token_savings_done + sum(
        getattr(tc, "estimated_savings_tokens", 0)
        for tc in list(_session_token_stats.values())
    )
    return JSONResponse({
        "status": "ok",
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "model": os.environ.get("AGENT_TOOL_BRAIN", "gemini"),
        "workspaces": len(_workspaces),
        "skills": len(_SKILLS),
        "memory_available": _MEMORY_AVAILABLE,
        "memory_turns": mem_turns,
        "session_token_savings": total_savings,
        "quiet_failures": dict(_lib.QUIET_FAILURES),   # counts per place only (memory recall, fact save, turn log)
        "speech": _lib.speech_stats(),                  # POST /v1/speech since start: by voice, by kind of device, refusals; ms per voice
    })


# ---------------------------------------------------------------------------
# Household identity: people, their devices, pairing (scripts/household_identity.py)
# ---------------------------------------------------------------------------

def _store() -> "_hh.HouseholdStore":
    try:
        return _hh.get_store()
    except Exception as exc:  # noqa: BLE001 - e.g. HOUSEHOLD_DB's folder isn't writable
        raise HTTPException(status_code=503, detail=f"household store unavailable: {exc}") from exc


def _pairing_error(exc: "_hh.PairingError") -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status)


def _require_admin(request: Request) -> "_hh.Identity":
    ident = _identity(request)
    if ident.role != "admin":
        raise HTTPException(status_code=403, detail=_hh.REFUSAL)
    return ident


async def _json_body(request: Request) -> Any:
    try:
        return await request.json() if (await request.body()) else {}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="body must be JSON") from exc


@app.post("/v1/pair/start")
async def pair_start(request: Request) -> JSONResponse:
    """(admin) {"person_id": "...", "label"?, "kind"?} or {"new_person": {"name", "role"}, "label"?} →
    {code, expires_at, person, qr}. Codes: 6 digits, single use, 10 minutes, at most 5 open. kind "executor" (#1631):
    the device that claims it is a paired executor (platform other), started by its owner for themselves only."""
    _require_admin(request)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    store = _store()
    if body.get("kind") == "executor" and not body.get("person_id"):
        return _pairing_error(_hh.PairingError(403, "an executor is paired by its owner, for themselves"))
    try:
        if isinstance(body.get("new_person"), dict):
            person = await asyncio.to_thread(store.add_person, body["new_person"].get("name"), body["new_person"].get("role"))
            pid = person["id"]
        elif body.get("person_id"):
            pid = str(body["person_id"])
        else:
            raise _hh.PairingError(400, "person_id or new_person is required")
        kind = body.get("kind")
        if kind == "executor" and pid != _identity(request).person_id:
            # #1631: an executor is paired by its owner, on their own device (adults' own executors come later)
            raise _hh.PairingError(403, "an executor is paired by its owner, for themselves")
        return JSONResponse(await asyncio.to_thread(store.start_pairing, pid, body.get("label"), kind))
    except _hh.PairingError as exc:
        return _pairing_error(exc)


@app.post("/v1/me/executor-pairing")
async def me_executor_pairing(request: Request) -> JSONResponse:
    """{"label"?} → {code, expires_at, person, qr}: a code to pair one of the caller's OWN other machines as an executor
    (#1631), started from their own device, for themselves only. Adults and the admin (route table); an executor's own
    token can't start one (its routes are its presence and inbox only)."""
    body = await _json_body(request) if (request.headers.get("content-length") or "0") != "0" else {}
    ident = _identity(request)
    if not (ident.device or {}).get("id") and ident.via != "master":
        return _pairing_error(_hh.PairingError(403, "start it from your own device"))
    try:
        return JSONResponse(await asyncio.to_thread(_store().start_pairing, ident.person_id,
                                                    (body or {}).get("label") if isinstance(body, dict) else None,
                                                    "executor"))
    except _hh.PairingError as exc:
        return _pairing_error(exc)


@app.post("/v1/pair/claim")
async def pair_claim(request: Request) -> JSONResponse:
    """(no auth) {"code", "device_name", "platform"} → {token, device, person, servers}. The token
    is shown once; titan keeps only its sha256. 400/404/410/429 per the pairing contract."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    ip = request.client.host if request.client else ""
    try:
        return JSONResponse(await asyncio.to_thread(_store().claim, body, ip))
    except _hh.PairingError as exc:
        return _pairing_error(exc)


# ── a box's first run (titan-node#411; box_setup.py) ────────────────────────────────────────

_SETUP_PAGE = Path(__file__).with_name("box_setup_page.html")


async def _setup_call(request: Request, fn: Any) -> JSONResponse:
    import box_setup
    store = _store()
    ip = request.client.host if request.client else ""
    try:
        body = await _json_body(request) if request.method == "POST" else {}
        if not isinstance(body, dict):
            raise box_setup.SetupError(400, "expected a JSON object")
        # the key check and the action under one lock (two racing owner claims can't both win)
        return JSONResponse(await asyncio.to_thread(box_setup.guarded, store, request.headers.get("X-Setup-Key"), ip,
                                                    lambda: fn(box_setup, store, body)))
    except box_setup.SetupError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    except _hh.PairingError as exc:
        return _pairing_error(exc)


@app.get("/setup")
async def setup_page() -> Response:
    """(no auth) A box's setup page: static, holds nothing; 404 unless the box is waiting to be set up."""
    import box_setup
    if not await asyncio.to_thread(box_setup.needs_setup, _store()):
        return JSONResponse({"error": "not found"}, status_code=404)
    return Response(_SETUP_PAGE.read_text(), media_type="text/html",
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/v1/setup/state")
async def setup_state(request: Request) -> JSONResponse:
    """(setup key) {needs_setup, owner, people [{id, name, role, calendars, devices}], steps}."""
    return await _setup_call(request, lambda bs, st, b: bs.state(st))


@app.post("/v1/setup/owner")
async def setup_owner(request: Request) -> JSONResponse:
    """(setup key) {name} → the owner (the box's admin) with that name."""
    return await _setup_call(request, lambda bs, st, b: bs.name_owner(st, b.get("name")))


@app.post("/v1/setup/people")
async def setup_people(request: Request) -> JSONResponse:
    """(setup key) {name, role: adult|kid|household} → {id, name, role}."""
    return await _setup_call(request, lambda bs, st, b: bs.add_person(st, b.get("name"), b.get("role")))


@app.post("/v1/setup/pair")
async def setup_pair(request: Request) -> JSONResponse:
    """(setup key) {person_id, label?} → {code, expires_at, person, qr}: #157's pairing code and titan://pair link."""
    return await _setup_call(request, lambda bs, st, b: bs.pair(st, b.get("person_id"), b.get("label")))


@app.post("/v1/setup/calendars")
async def setup_calendars(request: Request) -> JSONResponse:
    """(setup key) {person_id, calendars: [title]} → that person's facts (whose calendar is whose)."""
    return await _setup_call(request, lambda bs, st, b: bs.set_calendars(st, b.get("person_id"), b.get("calendars")))


@app.post("/v1/setup/home")
async def setup_home(request: Request) -> JSONResponse:
    """(setup key) Connect the house: the box onboards its own Home Assistant (box_ha.py) and says what it found:
    {connected, devices: {kind: count}, hands_off: [{entity_id, name, kind}]}. Locks, alarms, garage doors and cameras
    are only listed (#413). 503 when HA isn't answering; 409 when HA already has an owner the box didn't make."""
    return await _setup_call(request, lambda bs, st, b: bs.home(st))


@app.post("/v1/setup/recovery")
async def setup_recovery(request: Request) -> JSONResponse:
    """(setup key) The box's backup recovery phrase, made now and shown once (#411, #407): {phrase, groups, key, sender}.
    409 once it exists: the box keeps only the public key, never the phrase. Not cached, never logged."""
    resp = await _setup_call(request, lambda bs, st, b: bs.recovery(st))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/v1/setup/claim")
async def setup_claim(request: Request) -> JSONResponse:
    """(setup key) The app's owner claim: {name?, device_name, platform} → {token, device, person, servers} for the
    owner (#157's claim answer). Works once: the owner's first device closes setup."""
    ip = request.client.host if request.client else ""
    return await _setup_call(request, lambda bs, st, b: bs.claim(st, b, ip))


@app.post("/v1/setup/finish")
async def setup_finish(request: Request) -> JSONResponse:
    """(setup key) Ends setup: the key is deleted and every setup route is 404 from then on."""
    return await _setup_call(request, lambda bs, st, b: bs.finish(st))


@app.get("/v1/capabilities")
async def capabilities_snapshot(request: Request) -> JSONResponse:
    """What titan is and can do right now: the same live snapshot the capabilities tool reads (its
    version and models, sync, home, the caller's devices, the household, abilities, build routes,
    services, what's new). Admin only, like the tool: it carries the admin's sync activity, titan's
    dev log and service state (plan item 9: his system information never reaches anyone else)."""
    import capabilities
    ident = _identity(request)
    text = await asyncio.to_thread(capabilities.snapshot, ident.person_id)
    return JSONResponse({"text": text})


@app.get("/v1/conversations/search")
async def conversations_search(request: Request, q: str = "", limit: int = 30) -> JSONResponse:
    """Search the asker's own past conversations (jellybean#827): {query, hits: [{conversation, title, last_at, role,
    at, snippet}]}, every word of q in a message, newest first. Only ever the caller's own stream (as the list); a
    household device has none (403). 400 when q is under 2 or over 100 characters."""
    ident = _identity(request)
    query = " ".join(str(q or "").split())
    import conversations
    if not conversations.SEARCH_MIN <= len(query) <= conversations.SEARCH_MAX:
        return JSONResponse({"error": f"q must be {conversations.SEARCH_MIN} to {conversations.SEARCH_MAX} characters"},
                            status_code=400)
    if not _MEMORY_AVAILABLE:
        return JSONResponse({"query": query, "hits": []})
    conv = _hh.conv_id_for(ident.person_id)
    hits = await asyncio.to_thread(lambda: conversations.search(_get_memory()._db, conv, query, limit))
    return JSONResponse({"query": query, "hits": hits})


@app.get("/v1/conversations")
async def conversations_list(request: Request, limit: int = 50, view: str = "recent", kind: str = "", person: str = "",
                             device: str = "", project: str = "", place: str = "", since: str = "", room: str = "",
                             q: str = "", as_: str | None = Query(None, alias="as")) -> JSONResponse:
    """The asker's own past conversations, newest first (jellybean#117: topic cards on the phone and the
    Mac): {conversations: [{id, title, started_at, last_at, count, preview, device, platform}]}. platform (#1287):
    mac, iphone, ipad, watch or tv, as the device paired; null when unknown. Only ever the caller's own stream; a
    household device has none. view=threads (#1016): {view, threads, recent, facets,
    precision, filters, since}, filtered by kind/person/device/project/place; docs/chat-threads-api.md. view=threads&as=surface (#1060
    step 3): the same view as a jb-1 `collection` ResolvedSurface (docs/surfaces.md §9)."""
    ident = _identity(request)
    if view == "threads":
        import chat_threads
        body = {"view": "threads", "threads": [], "recent": [], "facets": {}, "precision": {}, "filters": [],
                "since": None}
        if _MEMORY_AVAILABLE:
            try:
                body = await asyncio.to_thread(lambda: chat_threads.build(
                    ident.person_id, ident.role, _get_memory()._db, limit=limit, kind=kind, person=person,
                    device=device, project=project, place=place, since=since, room=room, q=q))
            except chat_threads.Refused as exc:
                return JSONResponse({"error": exc.detail}, status_code=exc.status)
        if as_ == "surface":
            import surfaces
            return JSONResponse(surfaces.threads(body, ident.person_id, ident.role, time.time()))
        return JSONResponse(body)
    if view != "recent":
        return JSONResponse({"error": "view is recent or threads"}, status_code=400)
    if not _MEMORY_AVAILABLE:
        return JSONResponse({"conversations": []})
    import chat_links
    import conversations
    conv = _hh.conv_id_for(ident.person_id)
    items = await asyncio.to_thread(lambda: conversations.list_for(_get_memory()._db, conv, limit))
    import continuity
    try:
        marks = await asyncio.to_thread(continuity.reads, ident.person_id)
    except Exception:  # noqa: BLE001 - the list is there without read marks
        marks = {}
    import spaces
    try:   # which project or group the person filed it under (jellybean#240)
        tags = await asyncio.to_thread(spaces.tags_for, ident.person_id)
    except Exception:  # noqa: BLE001 - the list is there without them
        tags = {}
    try:   # where a branch came from (titan-node#895)
        lineage = await asyncio.to_thread(spaces.lineage_for, ident.person_id)
    except Exception:  # noqa: BLE001 - the list is there without it
        lineage = {}
    for c in items:   # how far this person has read it, on any device (jellybean#138)
        c["read_upto"] = marks.get(c["id"])
        c["project"] = (tags.get(c["id"]) or {}).get("project")
        c["group"] = (tags.get(c["id"]) or {}).get("group")
        c["branched_from"] = lineage.get(c["id"])   # {thread_id, upto_message_id}, or null
        c["item_id"] = chat_links.item_id(c["id"])   # its linker item (#1016): what /v1/threads/relate takes
    return JSONResponse({"conversations": items})


@app.patch("/v1/conversations/{cid}")
async def conversation_file(cid: str, request: Request) -> JSONResponse:
    """{project?, group?}: file the asker's own conversation under a project or group they can see (null clears it).
    404 when the conversation isn't theirs, 403 for a project or group they can't see (jellybean#240). Only the fields
    the body names change. project null also undoes an automatic project (#1016): kept as the person's correction,
    so it isn't offered again."""
    import conversations
    import spaces
    ident = _identity(request)
    if ident.role == "household" or not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=404, detail="no such conversation")
    body = await _json_body(request)
    if not isinstance(body, dict) or not ({"project", "group"} & set(body)):
        raise HTTPException(status_code=400, detail="expected {project?, group?}")
    conv = _hh.conv_id_for(ident.person_id)
    mine = await asyncio.to_thread(lambda: conversations.messages(_get_memory()._db, conv, cid, limit=1))
    if mine is None:
        raise HTTPException(status_code=404, detail="no such conversation")
    # Only the fields the body names change (10-05: an Undo of an automatic project, {project: null}, dropped a
    # manual group filing; {group: x} alone dropped a manual project). A new project brings its own group unless the
    # body names one.
    was = (await asyncio.to_thread(spaces.tags_for, ident.person_id)).get(cid) or {}
    want_project = body["project"] if "project" in body else was.get("project")
    want_group = body["group"] if "group" in body else (None if body.get("project") else was.get("group"))
    try:
        project, group = spaces.check_target(ident.person_id, ident.role, want_project, want_group)
    except spaces.SpaceError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    rejected = None
    if "project" in body and body.get("project") is None:   # Undo on an automatic project (#1016): the person's correction
        import chat_threads
        try:
            rejected = await asyncio.to_thread(chat_threads.reject_auto, ident.person_id, ident.role, _get_memory()._db, cid)
        except Exception as exc:  # noqa: BLE001 - the manual tag is still cleared
            print(f"[chat_threads] auto project not undone: {type(exc).__name__}", file=sys.stderr)
    await asyncio.to_thread(spaces.tag, ident.person_id, cid, project, group)
    return JSONResponse({"id": cid, "project": project, "group": group,
                         **({"auto_project_rejected": rejected} if rejected else {})})


def _space_error(exc: Exception) -> JSONResponse:
    return JSONResponse({"error": getattr(exc, "message", str(exc))}, status_code=getattr(exc, "status", 400))


@app.get("/v1/projects")
async def projects_list(request: Request) -> JSONResponse:
    """The projects the caller may see: their own, ones shared with them, and their groups' (jellybean#240)."""
    import spaces
    ident = _identity(request)
    return JSONResponse({"projects": await asyncio.to_thread(spaces.projects_for, ident.person_id, ident.role)})


@app.post("/v1/projects")
async def projects_create(request: Request) -> JSONResponse:
    """{name, icon?, color?, audience?, group?} → the project, private unless audience says otherwise (Zach, 10-02)."""
    import spaces
    ident = _identity(request)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    try:
        p = await asyncio.to_thread(lambda: spaces.create_project(ident.person_id, ident.role, body.get("name"),
                                                                  body.get("icon"), body.get("color"),
                                                                  body.get("audience"), body.get("group")))
    except spaces.SpaceError as exc:
        return _space_error(exc)
    return JSONResponse(p, status_code=201)


@app.patch("/v1/projects/{pid}")
async def projects_update(pid: str, request: Request) -> JSONResponse:
    """{name?, icon?, color?, audience?, group?, archived?}: its owner (or the admin) changes it."""
    import spaces
    ident = _identity(request)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    keep = {k: body[k] for k in ("name", "icon", "color", "audience", "group", "archived") if k in body}
    try:
        p = await asyncio.to_thread(lambda: spaces.update_project(pid, ident.person_id, ident.role, **keep))
        import projects
        if "name" in keep:   # #1085: a name a person gave is theirs from now on ("Named by you")
            await asyncio.to_thread(projects.named_by_person, pid)
        elif "audience" in keep:   # shared (or re-shared): a titan-made name, rebuilt from what all of them can see
            people = await asyncio.to_thread(_store().people)
            p = await asyncio.to_thread(projects.reshare, pid, people) or p
    except spaces.SpaceError as exc:
        return _space_error(exc)
    return JSONResponse(p)


def _projects_call(fn: Callable[[], Any]) -> JSONResponse:
    import projects
    try:
        return JSONResponse(fn())
    except projects.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.get("/v1/projects/proposals")
async def project_proposals(request: Request) -> JSONResponse:
    """#1085: threads of the caller's own visible items that hold together, offered as projects (titan never
    creates one silently). Kids and shared screens: none."""
    import projects
    ident = _identity(request)
    got = await asyncio.to_thread(projects.proposals, ident.person_id, ident.role)
    # #1177: each with what this viewer may do (Keep, Not a project), so the apps render from operations
    return JSONResponse({"proposals": [{**p, "operations": projects.proposal_ops(p, ident.person_id, ident.role)}
                                       for p in got]})


@app.post("/v1/projects/proposals/{prop}/keep")
async def project_keep(prop: str, request: Request) -> JSONResponse:
    """{name?, audience?} → the new project (private by default; suggested_audience offered: the other adults its
    items involve), its items filed, and one undo token."""
    import projects
    ident = _identity(request)
    body = await _json_body(request)
    body = body if isinstance(body, dict) else {}
    people = await asyncio.to_thread(_store().people)
    return await asyncio.to_thread(_projects_call, lambda: projects.keep(
        ident.person_id, ident.role, prop, name=body.get("name"), audience=body.get("audience"), people=people))


@app.post("/v1/projects/proposals/{prop}/dismiss")
async def project_dismiss(prop: str, request: Request) -> JSONResponse:
    """Not a project: this cluster stays dismissed, for this person, as it grows."""
    import projects
    ident = _identity(request)
    return await asyncio.to_thread(_projects_call, lambda: projects.dismiss(ident.person_id, ident.role, prop))


@app.post("/v1/projects/{pid}/split")
async def project_split(pid: str, request: Request) -> JSONResponse:
    """{items: [ids], name} → a new project with those items (same owner and audience); one undo token."""
    import projects
    ident = _identity(request)
    body = await _json_body(request)
    body = body if isinstance(body, dict) else {}
    items_ = [str(x) for x in (body.get("items") or []) if x]
    return await asyncio.to_thread(_projects_call, lambda: projects.split(ident.person_id, ident.role, pid, items_,
                                                                          body.get("name")))


@app.post("/v1/projects/{pid}/merge")
async def project_merge(pid: str, request: Request) -> JSONResponse:
    """{into} → this project's items move into another; this one is archived; one undo token."""
    import projects
    ident = _identity(request)
    body = await _json_body(request)
    into = str((body or {}).get("into") or "") if isinstance(body, dict) else ""
    return await asyncio.to_thread(_projects_call, lambda: projects.merge(ident.person_id, ident.role, pid, into))


@app.post("/v1/projects/{pid}/related/{thread}")
async def project_related(pid: str, thread: str, request: Request) -> JSONResponse:
    """#1109 {link: true|false}: Link files the related thread's items under the project (one undo token); Not related
    keeps it off this project's page. Only a thread the caller is offered on that page."""
    import projects
    ident = _identity(request)
    body = await _json_body(request)
    if not isinstance(body, dict) or not isinstance(body.get("link"), bool):
        return JSONResponse({"error": "expected {link: true|false}"}, status_code=400)
    return await asyncio.to_thread(_projects_call, lambda: projects.relate(pid, thread, ident.person_id, ident.role,
                                                                           body["link"]))


@app.post("/v1/projects/undo")
async def project_undo(request: Request) -> JSONResponse:
    """{token}: put back exactly what one keep, file, unfile, split or merge changed."""
    import projects
    ident = _identity(request)
    body = await _json_body(request)
    tok = str((body or {}).get("token") or "") if isinstance(body, dict) else ""
    return await asyncio.to_thread(_projects_call, lambda: projects.undo(ident.person_id, tok))


def _file_turn_made(t: dict, ident: "_hh.Identity") -> int:
    """#1173: a turn asked from a project page (file_under) files what its tools made (todo:…, task:…, scheduled:…)
    under that project too, as their maker. → how many. Not filed is never lost: the item is made either way."""
    target = (t.get("file_under") or {}).get("project") or (t.get("file_under") or {}).get("group")
    if not target or not t.get("created") or not ident.person_id or ident.role == "household":
        return 0
    import projects
    n = 0
    for item in t["created"]:
        try:
            projects.file(ident.person_id, ident.role, item, target)
            n += 1
        except Exception as exc:  # noqa: BLE001 - a project they can't see files nothing
            print(f"[projects] made but not filed: {type(exc).__name__}", file=sys.stderr)
    return n


# which reader holds an item, by its id's kind prefix (#1169); anything else is a linker item
_ITEM_READER = {"todo": "todos", "task": "tasks", "scheduled": "scheduled", "routine": "routines", "event": "events",
                "ref": "references", "mail": "mail", "bill": "bills", "note": "notes", "chat": "chats"}


UNSORTED_SUGGESTED_CAP = 20


_SUGGESTED_MEMO: dict[tuple, tuple] = {}   # (viewer, role, which) → (content key, made at, items)
SUGGESTED_MEMO_MAX_S = 300.0                # titles can change without the graph: never older than this


def _linked_suggestions(pid: str | None, role: str | None, keep: Callable[[str, dict, Any], bool],
                        cap: int, which: str = "") -> list[dict]:
    """File-it suggestions on the house link graph projected to this person (threadview's visibility), with the
    projects they may see (#1175, #1180): the unfiled items `keep` accepts, strongest first, at most `cap`, each an
    items-surface item carrying `suggested`. Never an item without a suggestion; kids and shared screens: none."""
    if not pid or role not in ("admin", "adult"):
        return []
    import file_suggest
    import hooks
    import items as it
    import linker
    import projects
    import threadview
    con = hooks.connect()
    try:
        items_, edges = linker.scored(con)
    finally:
        con.close()
    filed = projects.filings()
    names = file_suggest.visible_projects(pid, role)
    # #1181: one entry per viewer, kept while the graph, the fences, the filings and the projects they may see are
    # unchanged (a content key; who may see what is in it) and at most SUGGESTED_MEMO_MAX_S old
    import fences
    who = (str(pid), str(role), which)
    key = (linker._SCORED.get("key"), _lib._fences_key(fences.get()), tuple(sorted(filed.items())),
           tuple(sorted(names.items())), cap)
    held = _SUGGESTED_MEMO.get(who)
    if held and held[0] == key and time.monotonic() - held[1] < SUGGESTED_MEMO_MAX_S:
        return [dict(x) for x in held[2]]
    visible = threadview._visible_fn(pid, role)
    vis = {i: x for i, x in items_.items() if visible(x)}
    projs = [{"id": p_, "name": n, "members": [i for i, pr in filed.items() if pr == p_ and i in vis]}
             for p_, n in names.items()]
    got = file_suggest.suggest(vis, [e for e in edges if e.a in vis and e.b in vis], lambda _x: True, projs, role=role)
    pick = [(i, g) for i, g in got.items() if i not in filed and keep(i, g, vis[i])]
    pick.sort(key=lambda t: (-file_suggest.RANK.get(t[1]["state"], 0), t[0]))
    pick = pick[:cap]
    titles = linker.titles_for([i for i, _g in pick])   # these items' titles alone (10-10: the whole house's)
    out = []
    for i, g in pick:
        x = it.from_linked(vis[i], titles.get(i), [])
        x["suggested"] = {"project": g["project"], "reason": g["reason"], "state": g["state"], "auto": g["auto"]}
        out.append(x)
    _SUGGESTED_MEMO[who] = (key, time.monotonic(), [dict(x) for x in out])
    return out


def _suggested_reader(ident: "_hh.Identity") -> Callable[[], list]:
    """#1175: the caller's own unfiled linker items (scans, mail, messages, notes) that already carry a File-it
    suggestion: Unsorted's non-chat members."""
    pid, role = ident.person_id, ident.role
    return lambda: _linked_suggestions(pid, role, lambda _i, _g, x: str(x.owner or "").lower() == str(pid).lower(),
                                       UNSORTED_SUGGESTED_CAP, which="unsorted")


PROJECT_LINKED_CAP = 20


def project_linked_on() -> bool:
    return os.environ.get("PROJECT_LINKED", "").strip().lower() in ("1", "on", "true", "yes")


def _project_linked_reader(ident: "_hh.Identity", project: str) -> Callable[[], list]:
    """#1180: a project page's "linked to this project": unfiled items the caller can see (anyone's they may see,
    not only their own) that the link graph suggests for this project. Shown as suggestions, never filed; File it
    appears only where the caller may file the item (items.may_file). Behind PROJECT_LINKED until measured."""
    pid, role = ident.person_id, ident.role
    return lambda: _linked_suggestions(pid, role, lambda _i, g, _x: g["project"] == project, PROJECT_LINKED_CAP,
                                       which=f"project:{project}")


def _may_file_item(ident: "_hh.Identity", item_id: str) -> bool:
    """#1169: the item as this caller sees it (their own reader: access projection first), and whether they may file
    it. Not one they can see, or not theirs to file: False, and the route says 404 (nobody learns it exists)."""
    import items as it
    if ident.role == "household" or not ident.person_id:
        return False
    readers = _item_readers(ident, _lib._local_now())
    name = _ITEM_READER.get(str(item_id).split(":", 1)[0], "linked")
    try:
        found = next((x for x in readers[name]() if x["id"] == item_id), None)
        if found is None and name == "linked":   # an unfiled linker item Unsorted offers (#1175)
            found = next((x for x in _suggested_reader(ident)() if x["id"] == item_id), None)
    except Exception:  # noqa: BLE001 - a store down: refuse, never guess
        return False
    return bool(found) and it.may_file(found, ident.person_id, ident.role)


@app.post("/v1/items/{item_id}/file")
async def item_file(item_id: str, request: Request) -> JSONResponse:
    """{project} (a project or group id) → files one item under it; one undo token. Only an item the caller may file
    (#1169: its owner; an adult a household or adults item; a kid their own), else 404."""
    import projects
    ident = _identity(request)
    body = await _json_body(request)
    proj = str((body or {}).get("project") or "") if isinstance(body, dict) else ""
    if not proj:
        return JSONResponse({"error": "expected {project}"}, status_code=400)
    if ident.role != "household" and not await asyncio.to_thread(_may_file_item, ident, item_id):
        return JSONResponse({"error": "no such item"}, status_code=404)
    return await asyncio.to_thread(_projects_call, lambda: projects.file(ident.person_id, ident.role, item_id, proj))


@app.post("/v1/items/{item_id}/unfile")
async def item_unfile(item_id: str, request: Request) -> JSONResponse:
    """Takes one item out of its project; one undo token. The same rule as filing it (#1169), else 404."""
    import projects
    ident = _identity(request)
    if ident.role != "household" and not await asyncio.to_thread(_may_file_item, ident, item_id):
        return JSONResponse({"error": "no such item"}, status_code=404)
    return await asyncio.to_thread(_projects_call, lambda: projects.unfile(ident.person_id, ident.role, item_id))


def _later_item(ident: "_hh.Identity", item_id: str) -> dict | None:
    """#1233: the to-do, scheduled action or task as this caller sees it (their own reader), else None."""
    readers = _item_readers(ident, _lib._local_now())
    name = _ITEM_READER.get(str(item_id).split(":", 1)[0])
    if name not in ("todos", "scheduled", "tasks"):
        return None
    return next((x for x in readers[name]() if x["id"] == item_id), None)


@app.post("/v1/items/{item_id}/later")
async def item_later(item_id: str, request: Request) -> JSONResponse:
    """Later on a Now item (#1233): {"until": epoch seconds | ISO 8601}, at most 30 days out → {id, snoozed_until}.
    Hidden from the caller's own Now until then (never anyone else's), and back after if it's still current. A
    to-do, scheduled action or task the caller can see; never a safety item (403), never from a shared screen (403)."""
    import items as it
    import proactive
    ident = _identity(request)
    if ident.role == "household" or not ident.person_id:
        return JSONResponse({"error": "cannot_snooze"}, status_code=403)
    if str(item_id).split(":", 1)[0] not in it.LATER_KINDS:
        return JSONResponse({"error": "not_snoozable", "detail": "Later is for to-dos, scheduled actions and tasks"},
                            status_code=400)
    body = await _json_body(request)
    now = _lib._local_now().timestamp()
    try:
        until = _snooze_until((body or {}).get("until") if isinstance(body, dict) else None)
        if until != until or until <= now or until > now + proactive.SNOOZE_MAX:
            raise ValueError("until")
    except (TypeError, ValueError, OverflowError):
        return JSONResponse({"error": "bad_until", "detail": "until must be a time in the next 30 days"}, status_code=400)
    try:
        found = await asyncio.to_thread(_later_item, ident, item_id)
    except Exception:  # noqa: BLE001 - a store down: refuse, never guess
        return JSONResponse({"error": "unavailable"}, status_code=503)
    if found is None or found["state"]["is"] == "done":
        return JSONResponse({"error": "no such item"}, status_code=404)
    if not it.may_later(found, ident.person_id, ident.role):
        return JSONResponse({"error": "cannot_snooze"}, status_code=403)   # a safety item
    return JSONResponse(await asyncio.to_thread(lambda: proactive.snooze_item(
        item_id, until, person=ident.person_id, role=ident.role, now=now)))


@app.get("/v1/groups")
async def groups_list(request: Request) -> JSONResponse:
    """The groups the caller is in (a household device: none), each with its members' names (jellybean#240)."""
    import spaces
    ident = _identity(request)
    gs = await asyncio.to_thread(spaces.groups_for, ident.person_id, ident.role)
    try:
        folks = _store().people()
    except Exception:  # noqa: BLE001
        folks = []
    for g in gs:   # who's in it, by name: the avatars on the sidebar and the People tab
        g["people"] = [{"id": p["id"], "name": p.get("name")} for p in folks
                       if p.get("role") != "household" and spaces.in_group(g, p["id"], p.get("role"))]
    return JSONResponse({"groups": gs})


@app.post("/v1/groups")
async def groups_create(request: Request) -> JSONResponse:
    """{name, members: "household"|"adults"|[person ids], icon?, color?}: an adult makes a group (with themselves in it)."""
    import spaces
    ident = _identity(request)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    try:
        g = await asyncio.to_thread(lambda: spaces.create_group(ident.person_id, ident.role, body.get("name"),
                                                                body.get("members"), body.get("icon"), body.get("color")))
    except spaces.SpaceError as exc:
        return _space_error(exc)
    return JSONResponse(g, status_code=201)


@app.get("/v1/groups/{gid}/home")
async def group_home(gid: str, request: Request) -> JSONResponse:
    """A group's page (jellybean#240, WorkspaceHome): {group, projects, chats (the caller's own filed there), todos (the
    group's list, open), people, calendar (the title the Mac reads)}. 404 for anyone not in the group."""
    import conversations
    import spaces
    import todos
    ident = _identity(request)
    chats = []
    if _MEMORY_AVAILABLE and ident.role != "household":
        conv = _hh.conv_id_for(ident.person_id)
        chats = await asyncio.to_thread(lambda: conversations.list_for(_get_memory()._db, conv, 200))
    try:
        folks = _store().people()
    except Exception:  # noqa: BLE001
        folks = []
    try:
        page = await asyncio.to_thread(lambda: spaces.home(
            gid, ident.person_id, ident.role, chats=chats, todo_rows=todos.list_for([gid]),
            people=[{"id": p["id"], "name": p.get("name"), "role": p.get("role")} for p in folks]))
    except spaces.SpaceError as exc:
        return _space_error(exc)
    return JSONResponse(page)


@app.patch("/v1/groups/{gid}")
async def groups_update(gid: str, request: Request) -> JSONResponse:
    """{name?, icon?, color?, members?, calendar?}: an adult member (or the admin) changes it. calendar: the title of
    the calendar its page shows (the Mac reads it)."""
    import spaces
    ident = _identity(request)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    keep = {k: body[k] for k in ("name", "icon", "color", "members", "calendar") if k in body}
    try:
        g = await asyncio.to_thread(lambda: spaces.update_group(gid, ident.person_id, ident.role, **keep))
    except spaces.SpaceError as exc:
        return _space_error(exc)
    return JSONResponse(g)


@app.post("/v1/conversations/{cid}/read")
async def conversation_read(cid: str, request: Request) -> JSONResponse:
    """{upto: <epoch seconds>}: the asker has read their conversation up to there (never moves back);
    their other devices hear it through /v1/sync. 404 when it isn't theirs."""
    ident = _identity(request)
    if ident.role == "household" or not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=404, detail="no such conversation")
    body = await _json_body(request)
    try:
        upto = float((body if isinstance(body, dict) else {}).get("upto"))
        if not __import__("math").isfinite(upto):
            raise ValueError
    except (TypeError, ValueError):
        return JSONResponse({"error": "upto (epoch seconds) is required"}, status_code=400)
    import continuity
    import conversations
    conv = _hh.conv_id_for(ident.person_id)
    if await asyncio.to_thread(lambda: conversations.messages(_get_memory()._db, conv, cid, None, 1)) is None:
        raise HTTPException(status_code=404, detail="no such conversation")
    await asyncio.to_thread(_note_active, ident)
    mark = await asyncio.to_thread(lambda: continuity.set_read(ident.person_id, cid, upto,
                                                               device=(ident.device or {}).get("id")))
    return JSONResponse({"id": cid, "read_upto": mark})


def _sync_owners(ident: "_hh.Identity") -> list[str]:
    """Whose changes this person's feed carries: their own, and the todo lists they may see."""
    import todos
    return sorted(set(todos.lists_visible(ident.person_id, ident.role)) | {ident.person_id})


@app.get("/v1/sync")
async def sync_feed(request: Request, since: str = "", wait: float = 0.0) -> JSONResponse:
    """The asker's continuity feed (jellybean#132 + #138): {cursor, changes: [{kind, at, mine, …}]} after
    `since`, kinds message | thread | todo | task. Long-polls up to `wait` s (≤ 55) for something new,
    like /v1/cards/wait; pass `cursor` back as the next `since`. The cursor is opaque and only works for
    the person it was made for (#420). `since` empty: from the start of the feed (a week); "now": from
    here on, nothing before. An unknown cursor (another person's, from a reset store, altered) answers
    at once with `reset: true` and a cursor from now: refetch what's shown, then carry on. `mine`: the
    change came from the asking device (its own echo). Only the asker's own (and the todo lists they
    may see); a shared household device has no feed (403)."""
    ident = _identity(request)
    if ident.role == "household":
        raise HTTPException(status_code=403, detail=_hh.REFUSAL)
    import continuity
    owners = _sync_owners(ident)
    me, device = ident.person_id, (ident.device or {}).get("id")
    latest = await asyncio.to_thread(continuity.latest, owners)
    raw = (since or "").strip()
    pos = 0 if raw in ("", "0") else latest if raw == "now" else await asyncio.to_thread(continuity.decode_cursor, me, raw)
    if pos is None or pos > latest:   # not this person's, or from before the store was reset
        cur = await asyncio.to_thread(continuity.encode_cursor, me, latest)
        return JSONResponse({"cursor": cur, "changes": [], "reset": True})
    if device:
        import push
        await asyncio.to_thread(push.mark_sync_live, device)   # its changes reach it here: no nudge (#419)
    deadline = time.monotonic() + max(0.0, min(float(wait), 55.0))
    while True:
        if latest > pos:
            rows = await asyncio.to_thread(continuity.since, owners, pos)
            last = rows[-1]["seq"] if rows else latest
            changes = [{**{k: v for k, v in r.items() if k not in ("seq", "from_device")},
                        "mine": bool(device) and r.get("from_device") == device} for r in rows]
            return JSONResponse({"cursor": await asyncio.to_thread(continuity.encode_cursor, me, last), "changes": changes})
        if _xd.SHUTTING_DOWN.is_set():
            return _stopping_response()
        if time.monotonic() >= deadline or await request.is_disconnected():
            return JSONResponse({"cursor": await asyncio.to_thread(continuity.encode_cursor, me, pos), "changes": []})
        await asyncio.sleep(1.5)
        latest = await asyncio.to_thread(continuity.latest, owners)


@app.post("/v1/presence/context")
async def presence_context_put(request: Request) -> JSONResponse:
    """What this device is on now (jellybean#132): {kind: web|document|media, title, url?, path?, app?,
    position?}. One per device, kept a day, only ever shown back to the same person; never in the
    knowledge base, memory or a turn's prompt. A page's address keeps no query or fragment."""
    ident = _identity(request)
    if ident.role == "household" or not ident.device:
        raise HTTPException(status_code=403, detail=_hh.REFUSAL)
    import continuity
    body = await _json_body(request)
    try:
        row = await asyncio.to_thread(continuity.put_context, ident.person_id, ident.device["id"],
                                      ident.device.get("name"), body if isinstance(body, dict) else {})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(row)


@app.delete("/v1/presence/context/{device_id}")
async def presence_context_take(device_id: str, request: Request) -> JSONResponse:
    """Another of the asker's devices picked up (or dismissed) what `device_id` was on: titan forgets it.
    Only the asker's own; 404 when there's nothing of theirs."""
    ident = _identity(request)
    if ident.role == "household" or not ident.device:
        raise HTTPException(status_code=403, detail=_hh.REFUSAL)
    import continuity
    if not await asyncio.to_thread(continuity.take_context, ident.person_id, device_id):
        raise HTTPException(status_code=404, detail="nothing to pick up")
    return JSONResponse({"device": device_id, "taken": True})


@app.get("/v1/presence/context")
async def presence_context_get(request: Request) -> JSONResponse:
    """What the asker's other devices were last on (the last day), newest first: {contexts: [...]}."""
    ident = _identity(request)
    if ident.role == "household":
        raise HTTPException(status_code=403, detail=_hh.REFUSAL)
    import continuity
    rows = await asyncio.to_thread(continuity.get_context, ident.person_id, (ident.device or {}).get("id"))
    return JSONResponse({"contexts": rows})


@app.get("/v1/conversations/{cid}/messages")
async def conversation_messages(cid: str, request: Request, before: str | None = None, limit: int = 100,
                                before_id: str | None = None) -> JSONResponse:
    """One of the asker's conversations, oldest first: {messages: [{id, role, text, at, platform}]} (platform as in the
    list, #1287). 404 when it isn't theirs. To continue it, the app sends these as `history` with `thread_id` = cid. Paging: `before` = the oldest
    loaded message's at, and `before_id` = its id (#1290: then a message sharing that at isn't lost)."""
    ident = _identity(request)
    if not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=404, detail="no such conversation")
    import conversations
    conv = _hh.conv_id_for(ident.person_id)
    msgs = await asyncio.to_thread(lambda: conversations.messages(_get_memory()._db, conv, cid, before, limit,
                                                                  before_id=before_id))
    if msgs is None:
        raise HTTPException(status_code=404, detail="no such conversation")
    return JSONResponse({"id": cid, "messages": msgs})


@app.get("/v1/conversations/{cid}/trace")
async def conversation_trace(cid: str, request: Request, limit: int = 50) -> JSONResponse:
    """(admin) The asker's own conversation turn by turn, for the developer inspector (jellybean#1267): {id, turns, truncated}.
    Each turn: {turn_id, ts, at, source, client, route, jev, brain, mode, pin, fallback, effort, effort_why, failed,
    budget_hit, empty_reply, tools: [{name, ok, reason?, ms?, device_ms?, asked_ms?, device?}], device_calls,
    device_waits: [{step, tool, ended, ms, asked_ms}], checks: {self_check, postanswer, postanswer_skipped, retries},
    egress, ms: {total, build, context, model, tools, device, confirm, ...}}. Read from the turn log: names, counts, durations and statuses only, never a message, a tool's arguments or its
    result. Only the asker's own turns, whoever else's line names the same id; 404 when the conversation isn't theirs
    and none of their turns name it. Newest `limit` turns (1-200), oldest first."""
    ident = _require_admin(request)
    cid = str(cid or "").strip()[:80]
    import turn_trace
    path = os.environ.get("AGENT_TURN_LOG", "/ai/logs/agent-turns.jsonl")
    turns, truncated = await asyncio.to_thread(turn_trace.for_conversation, path, ident.person_id, cid, limit)
    if not turns:
        owned = False
        if _MEMORY_AVAILABLE:
            import conversations
            conv = _hh.conv_id_for(ident.person_id)
            owned = await asyncio.to_thread(
                lambda: conversations.messages(_get_memory()._db, conv, cid, limit=1) is not None)
        if not owned:
            raise HTTPException(status_code=404, detail="no such conversation")
    return JSONResponse({"id": cid, "turns": turns, "truncated": truncated})


@app.get("/v1/me")
async def me(request: Request) -> JSONResponse:
    ident = _identity(request)
    settings = {}
    if ident.role != "household":
        settings["calendars_ignored"] = await asyncio.to_thread(_store().calendars_ignored, ident.person_id)
        settings["places"] = await asyncio.to_thread(_store().places, ident.person_id)
        settings["facets"] = await asyncio.to_thread(_store().facets, ident.person_id)
        prefs = await asyncio.to_thread(_store().prefs, ident.person_id)
        settings["live_activity_detail"] = prefs.get("live_activity_detail") == "on"
        settings["call_me"] = await asyncio.to_thread(_store().call_me, ident.person_id)   # #1080; null: the first name
        try:   # sources this person forgot (they stay forgotten): their devices stop syncing them
            import connectors_runner as cx
            settings["connectors_off"] = await asyncio.to_thread(cx.sources_off, ident.person_id)
        except Exception:  # noqa: BLE001
            settings["connectors_off"] = []
    # The calendars the adults marked as the household's (#335): every member's devices treat them as
    # everyone's (jellybean#206: cards never skip them). Titles only; lowercased, as titan compares them.
    try:
        import world as _world
        settings["household_calendars"] = sorted(await asyncio.to_thread(_world.household_calendars))
    except Exception:  # noqa: BLE001
        pass
    # Who runs the household, by name, so the apps can say "Ask Zach" rather than "the household's admin"
    # (jellybean#438). Only the admin's display name; nothing else about them.
    admin = await asyncio.to_thread(_store().person, _hh.ADMIN_PERSON_ID)
    return JSONResponse({"person": ident.person, "device": ident.device, "role": ident.role,
                         "can": _hh.capabilities(ident.role), "settings": settings,
                         # the chat @-tags this caller's role can use (jellybean#715): only tags whose tools it has today
                         "tags": __import__("chat_tags").available(ident.role),
                         "household": {"admin_name": (admin or {}).get("name") or None}})


@app.patch("/v1/me")
async def me_update(request: Request) -> JSONResponse:
    """A person's own settings, each replaced whole when present: {calendars_ignored: [titles]}
    (Jellybean's calendar picker; titan ignores those calendars too), {places: [names]} (the places
    they named on their iPhone, names only, for "when I get home…") and, for an adult, {facets:
    {"calendar:<title>" | "vendor:<name>": personal|social|work|school}} (#354). Every one of their devices reads
    them back from GET /v1/me. {live_activity_detail: true|false}: a task's Live Activity shows its current step's
    title and what it waits on, beside the goal it always shows (#725; off by default)."""
    ident = _identity(request)
    if ident.role == "household":
        return JSONResponse({"error": "a shared device has no personal settings"}, status_code=403)
    body = await _json_body(request)
    if not isinstance(body, dict) or not ({"calendars_ignored", "places", "connectors_off", "facets",
                                           "live_activity_detail", "call_me"} & set(body)):
        return JSONResponse({"error": "expected {calendars_ignored: [...]}, {places: [...]}, {facets: {...}} "
                                      "and/or {connectors_off: [...]}"}, status_code=400)
    out: dict[str, Any] = {}
    try:
        if "call_me" in body:     # #1080: what titan calls them; their own, 1-30 characters, null clears it
            out["call_me"] = await asyncio.to_thread(_store().set_call_me, ident.person_id, body["call_me"])
        if "facets" in body:      # #354: their calendars and vendors → work, personal…; set by each adult
            if ident.role not in ("admin", "adult"):
                return JSONResponse({"error": "a parent sets a kid's facets"}, status_code=403)
            out["facets"] = await asyncio.to_thread(_store().set_facets, ident.person_id, body["facets"])
        if "live_activity_detail" in body:
            if not isinstance(body["live_activity_detail"], bool):
                return JSONResponse({"error": "live_activity_detail must be true or false"}, status_code=400)
            prefs = await asyncio.to_thread(_store().set_pref, ident.person_id, "live_activity_detail",
                                            "on" if body["live_activity_detail"] else "off")
            out["live_activity_detail"] = prefs["live_activity_detail"] == "on"
        if "calendars_ignored" in body:
            out["calendars_ignored"] = await asyncio.to_thread(_store().set_calendars_ignored, ident.person_id,
                                                               body["calendars_ignored"])
        if "places" in body:
            out["places"] = await asyncio.to_thread(_store().set_places, ident.person_id, body["places"])
        if "connectors_off" in body:      # turning a source back on; forgotten items stay forgotten
            import connectors_runner as cx
            if not isinstance(body["connectors_off"], list):
                return JSONResponse({"error": "connectors_off must be a list of sources"}, status_code=400)
            out["connectors_off"] = await asyncio.to_thread(cx.set_sources_off, ident.person_id,
                                                            [str(x) for x in body["connectors_off"]])
    except _hh.PairingError as exc:
        return _pairing_error(exc)
    return JSONResponse({"settings": out})


def _engineering_read(route: str, kind: str, items: list) -> list:
    """An engineering agent's read (#355): counted in its audit log, and anything not titan's own system item
    dropped (engineering_audit.py; a second check behind kb_privacy and audience)."""
    import engineering_audit
    engineering_audit.record(route, kind, items)
    return engineering_audit.only_system(kind, items)


def _world_viewer(request: Request) -> tuple[str | None, str, dict[str, str]]:
    """(person id or None for a shared household device, role, bubble labels) for the world API."""
    ident = _identity(request)
    pid = None if ident.role == "household" else ident.person_id
    try:
        labels = {p["id"]: p["name"] for p in _store().people()}
    except Exception:  # noqa: BLE001
        labels = {}
    return pid, ident.role, labels


@app.get("/v1/schedule")
async def schedule(request: Request, start: str | None = None, end: str | None = None,
                   people: str | None = None, as_: str | None = Query(None, alias="as")) -> JSONResponse:
    """The household's events this caller may see, grouped by whose they are (docs/schedule-api.md,
    jellybean#219): start/end as YYYY-MM-DD (default: today and the next 6 days, at most 62 days);
    people=ava,household to filter. ?as=surface (#1060 step 4): the same view as a jb-1 `temporal`
    ResolvedSurface, one group per household day (docs/surfaces.md §9)."""
    import schedule_view
    from datetime import date as _date, timedelta as _td
    pid, role, _ = _world_viewer(request)
    try:
        today = _lib._local_now().date()
        s = _date.fromisoformat(start) if start else today
        e = _date.fromisoformat(end) if end else s + _td(days=6)
    except ValueError:
        return JSONResponse({"error": "start and end are YYYY-MM-DD"}, status_code=400)
    if e < s or (e - s).days > 62:
        return JSONResponse({"error": "end must be on or after start, at most 62 days later"}, status_code=400)
    only = {x.strip() for x in people.split(",") if x.strip()} if people else None
    try:
        out = await asyncio.to_thread(schedule_view.for_viewer, s, e, pid, role, only)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"schedule unavailable: {str(exc)[:120]}"}, status_code=503)
    if as_ == "surface":
        import local_time
        import surfaces
        return JSONResponse(surfaces.schedule(out, pid, role, time.time(), today, local_time.zone()))
    return JSONResponse(out)


@app.get("/v1/box/status")
async def box_status_route(request: Request) -> JSONResponse:
    """A box's status light (#407): green, amber or red from device health's last run. The admin also gets the one
    sentence (the worst problem's card title); anyone else, a shared screen included, gets the colour only."""
    import box_status
    ident = _identity(request)
    return JSONResponse(await asyncio.to_thread(box_status.status, ident.role))


@app.get("/v1/box/identity")
async def box_identity_route(request: Request) -> JSONResponse:
    """A box's identity (#407): the public half of its Ed25519 key (server_identity, the key a kid's Watch already
    pins), to register with the coordinator, and whether the private half is sealed to the TPM ("tpm") or a 0600 file
    ("file"). Admin only; the private key never leaves the box."""
    import server_identity
    return JSONResponse({**await asyncio.to_thread(server_identity.identity), "storage": server_identity.storage()})


@app.get("/v1/brief")
async def brief_surface(request: Request) -> JSONResponse:
    """The daily brief as a jb-1 `temporal` ResolvedSurface (#1060 step 4; docs/surfaces.md §9), resolved once on
    titan: this caller's schedule (today and tomorrow), items (view=now) and cards, each through its own route for
    this same request, so it shows nothing those don't. Titan's sources only: the Mac's Reminders never leave it."""
    import surfaces
    from datetime import timedelta as _td
    today = _lib._local_now().date()

    def body(r: JSONResponse) -> dict:
        return json.loads(r.body) if r.status_code == 200 else {}
    sched = body(await schedule(request, start=today.isoformat(), end=(today + _td(days=1)).isoformat(),
                                people=None, as_="surface"))
    items_now = body(await _items_view(request, "now", None, None))
    deck = body(await proactive_cards(request, since=0.0, include_acked=False, as_="surface"))
    try:
        import weather
        wx = await asyncio.to_thread(weather.today_summary)
    except Exception:  # noqa: BLE001 - the brief is there without its weather line
        wx = None
    ident = _identity(request)
    return JSONResponse(surfaces.brief(sched, items_now, deck, None if ident.role == "household" else ident.person_id,
                                       ident.role, time.time(), today, weather=wx))


@app.get("/v1/public/now")
async def public_now() -> JSONResponse:
    """(no auth) The public /now feed for zachshallbetter.com (titan-node#1043): allowlisted public repos read from
    GitHub with no token, and titan's one hand-written line. Never household data. Cached 6 h."""
    import now_feed
    try:
        return JSONResponse(await asyncio.to_thread(now_feed.get))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"feed unavailable: {type(exc).__name__}"}, status_code=503)


@app.get("/v1/documents")
async def documents_list(request: Request, audience: str = "household",
                         as_: str | None = Query(None, alias="as")) -> JSONResponse:
    """The household's documents for the iPhone's read-only File Provider (jellybean#320): household-audience KB notes
    and documents, and household references' originals (on their owner's device: available false). Household-audience
    only, for every caller, the admin included. ?as=surface (#1060 step 4): the same list as a jb-1 `collection`
    ResolvedSurface (docs/surfaces.md §9)."""
    import documents
    ident = _identity(request)
    if audience != "household":
        return JSONResponse({"error": "audience is household"}, status_code=400)
    try:
        listing = await asyncio.to_thread(documents.listing, ident.role)
    except documents.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    if as_ == "surface":
        import surfaces
        return JSONResponse(surfaces.documents(listing, ident.person_id, ident.role, time.time()))
    return JSONResponse(listing)


def _fetch_original(ref: dict, requester: str | None = None) -> dict:
    """#1081: a shared file's original from its owner's Mac (the `item_original` device tool through cross_device),
    already approved by the owner's share. → {data: bytes, content_type} or {reason}. The device answers
    {content_type, data_b64} (docs/documents-file-share.md); a device that doesn't know the tool yet refuses.
    #1170: the args carry `requester` (the person id only) for the Mac's audit line; {refused: "changed"} (the file no
    longer matches what was shared) → {changed: true, new_hash}, which file_share turns into the owner's card."""
    import base64
    mac = _xd.mac_for(ref["owner"])
    if not mac:
        return {"reason": "its owner's device isn't connected to titan"}
    req = {"tool": "item_original", "args": {"ref_id": ref["id"], "kind": ref["kind"], "native_id": ref["native_id"],
                                             "content_hash": ref.get("content_hash"), "max_bytes": 25 * 1024 * 1024,
                                             "requester": requester}}
    tid = "fs-" + uuid.uuid4().hex[:12]
    try:
        got = _xd.route(req, person_id=ref["owner"], mac=mac, turn_id=tid, origin={"kind": "file_share"},
                        session=tid, emit=lambda e, d: None, ask_confirm=lambda p: {"approved": False},
                        approved=True)
    except Exception as exc:  # noqa: BLE001
        return {"reason": f"its owner's device couldn't send it ({type(exc).__name__})"}
    try:
        res = json.loads(got.get("result") or "{}")
        if isinstance(res, dict) and res.get("refused") == "changed":
            return {"changed": True, "new_hash": res.get("content_hash"), "reason": "isn't available right now"}
        return {"data": base64.b64decode(res["data_b64"]), "content_type": res.get("content_type")}
    except Exception:  # noqa: BLE001 - an error, a refusal, an older app: not available, said plainly
        return {"reason": "its owner's device couldn't send it just now"}


@app.put("/v1/references/{ref_id}/file_share")
async def references_file_share(ref_id: str, request: Request) -> JSONResponse:
    """#1081: {shared: true|false}: the owner (an adult or the admin) hands the item's audience its original, or takes it
    back at once. Kids' originals stay text-only (403); someone else's is a 404."""
    import file_share
    ident = _identity(request)
    body = await _json_body(request)
    if not isinstance(body, dict) or not isinstance(body.get("shared"), bool):
        return JSONResponse({"error": "expected {shared: true|false}"}, status_code=400)
    try:
        return JSONResponse(await asyncio.to_thread(file_share.share, ref_id, ident.person_id, ident.role, body["shared"]))
    except file_share.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.get("/v1/references/file_reads")
async def references_file_reads(request: Request) -> JSONResponse:
    """#1081: the caller's own audit: who opened which of their shared files, when, from which device."""
    import file_share
    ident = _identity(request)
    if ident.role == "household":
        return JSONResponse({"error": "a shared screen has no files of its own"}, status_code=403)
    return JSONResponse({"reads": await asyncio.to_thread(file_share.reads_of, ident.person_id)})


@app.get("/v1/documents/{doc_id}/content")
async def documents_content(doc_id: str, request: Request) -> Response:
    """A household document's text (text/markdown), or {available: false, reason} for an original on its owner's
    device; 404 for anything not household-visible (jellybean#320)."""
    import documents
    ident = _identity(request)
    names = {p["id"]: p.get("name") or "" for p in _hh.get_store().people()}
    try:
        pid = None if ident.role == "household" else ident.person_id
        status, body, ctype, etag = await asyncio.to_thread(lambda: documents.content(
            doc_id, ident.role, names=names, reader=pid, device=(ident.device or {}).get("id"), fetch=_fetch_original))
    except documents.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    if isinstance(body, dict):
        return JSONResponse(body, status_code=status)
    return Response(content=body, media_type=ctype, status_code=status, headers={"ETag": etag} if etag else {})


@app.get("/v1/retrieval/trace")
async def retrieval_trace_rows(request: Request, days: float = 7, person: str | None = None) -> JSONResponse:
    """This caller's knowledge-base searches (#368): ids, scores, ranks, stages and citations, never words.
    The admin may ask for one person, or everyone."""
    import retrieval_trace
    ident = _identity(request)
    pid = None if ident.role == "household" else ident.person_id
    rows = await asyncio.to_thread(retrieval_trace.rows, pid, ident.role, min(max(days, 0.1), 30), person)
    return JSONResponse({"searches": rows})


@app.get("/v1/world/entities")
async def world_entities(request: Request, group: str | None = None, q: str | None = None,
                         limit: int = 200, bubble: str | None = None, facts: int = 0) -> JSONResponse:
    """The world model's entities this caller may see (docs/world-api.md, #365): People, Places, Things.
    bubble=household: that bubble only; facts=1: each entity's facts inline, each with state, stale and confirmed."""
    import world as _world
    pid, role, labels = _world_viewer(request)
    if group and group not in ("people", "places", "things"):
        return JSONResponse({"error": "group is people, places or things"}, status_code=400)
    out = await asyncio.to_thread(_world.entity_list, _world.get(), pid, role, group, q, limit, labels,
                                  bubble or None, bool(facts))
    if role == _hh.ENGINEERING and isinstance(out, dict):
        out["entities"] = _engineering_read("/v1/world/entities", "world", out.get("entities") or [])
    return JSONResponse(out)


@app.get("/v1/world/entities/{entity_id}")
async def world_entity(entity_id: str, request: Request) -> JSONResponse:
    """One entity's page for this caller, or 404: what they may not see doesn't exist for them (#356)."""
    import world as _world
    pid, role, labels = _world_viewer(request)
    page = await asyncio.to_thread(_world.entity_page, _world.get(), entity_id, pid, role, labels)
    if role == _hh.ENGINEERING and page is not None:
        page = (_engineering_read("/v1/world/entities/{id}", "world", [{**page, "id": entity_id}]) or [None])[0]
    if page is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(page)


@app.post("/v1/world/entities/{entity_id}/facts")
async def world_set_fact(entity_id: str, request: Request) -> JSONResponse:
    """{attribute, value}: the caller states or corrects a fact on this entity (#358), the same as set_fact.
    {attribute, neither: true, qual?}: none of the values the caller sees is right (jellybean#593 D6). Their own
    statements are withdrawn and the rest rejected, so the fact is unknown; "none" is never written.
    → the updated page, or {error} (404 when the entity doesn't exist for them, 403 when it isn't theirs)."""
    import world as _world
    pid, role, labels = _world_viewer(request)
    body = await _json_body(request)
    if isinstance(body, dict) and body.get("neither") is True and str(body.get("attribute") or "").strip():
        w = _world.get()
        if _world.entity_page(w, entity_id, pid, role, labels) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        qual = body.get("qual") if isinstance(body.get("qual"), dict) else {}
        done, said, why = await asyncio.to_thread(_world.neither_fact, w, entity_id, str(body["attribute"]), qual, pid, role)
        if not done:
            return JSONResponse({"error": said}, status_code={"denied": 403, "unknown": 404, "nothing": 409}.get(why, 400))
        return JSONResponse({"said": said, "page": _world.entity_page(w, entity_id, pid, role, labels)})
    if not isinstance(body, dict) or not str(body.get("value") or "").strip() or not str(body.get("attribute") or "").strip():
        return JSONResponse({"error": "attribute and value are required"}, status_code=400)
    w = _world.get()
    if _world.entity_page(w, entity_id, pid, role, labels) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    ent = w.entity(entity_id)
    try:
        folks = _store().people()
    except Exception:  # noqa: BLE001
        folks = []
    done, said, why = await asyncio.to_thread(_world.set_fact, w, ent["name"], str(body["attribute"]), str(body["value"]),
                                         pid, role, None, folks, labels, entity_id)
    if not done:
        return JSONResponse({"error": said}, status_code={"denied": 403, "unknown": 404}.get(why, 400))
    page = _world.entity_page(w, entity_id, pid, role, labels)
    return JSONResponse({"said": said, "page": page})


@app.get("/v1/timeline")
async def timeline_list(request: Request, which: str = "household", limit: int = 50, before: str | None = None,
                        notes: str = "") -> JSONResponse:
    """The timeline this person may see (item 9: their own entries and the household's; titan's dev
    stream for the admin only), newest first: {events: [{id, at, text, person, audience, mine}]}."""
    ident = _identity(request)
    viewer = "household" if ident.role == "household" else ident.person_id
    if which == "dev" and ident.role != "admin":
        return JSONResponse({"error": "forbidden", "detail": "Code activity is the household admin's."}, status_code=403)
    stream = _lib.DEV_STREAM if which == "dev" else "household-timeline"
    events = await asyncio.to_thread(_lib.timeline_for, viewer, ident.role, stream,
                                     max(1, min(int(limit), 200)), before,
                                     notes.strip().lower() in ("1", "true", "yes"))
    return JSONResponse({"events": events})


@app.post("/v1/timeline")
async def timeline_add(request: Request) -> JSONResponse:
    """Add an entry (the same as the log_event tool): {text, for?: "household"}; it's the person's
    own unless it's for the household."""
    ident = _identity(request)
    body = await _json_body(request)
    text = str((body or {}).get("text") or "").strip() if isinstance(body, dict) else ""
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)

    def add() -> str:
        io = _lib._TURN_IO
        io.person, io.role = ident.person, ident.role
        try:
            return _lib.execute_tool("log_event", {"event": text[:500], "for": body.get("for")})
        finally:
            io.person = io.role = None
    out = await asyncio.to_thread(add)
    ok = not out.lower().startswith(("error", "test turn"))
    return JSONResponse({"ok": ok, "result": out}, status_code=200 if ok else 400)



@app.get("/v1/people")
async def people_list(request: Request) -> JSONResponse:
    ident = _require_admin(request)
    return JSONResponse({"people": _people_for_admin(await asyncio.to_thread(_store().people), ident.person_id)})


HOUSEHOLD_PEOPLE_FIELDS = ("id", "name", "role")   # the allowlist (#1164): never devices, contact, birth year, facts


def household_people(people: list[dict]) -> list[dict]:
    """Who's in the household, for anyone in it (#1164): id, the name they go by (call_me, else their name) and role.
    Shared screens aren't people. Someone whose data is being deleted (delete-my-data's 7-day window) still lists:
    the deletion takes their data, not their place in the household."""
    return [{"id": str(p["id"]), "name": str(p.get("call_me") or p.get("name") or ""), "role": str(p["role"])}
            for p in people if p.get("role") in ("admin", "adult", "kid")]


@app.get("/v1/household/people")
async def household_people_list(request: Request) -> JSONResponse:
    """{people: [{id, name, role}]}: the household's members for any person's token (admin, adult, kid; #1164); a
    shared screen gets 403. Only those three fields: devices, contact, birth year, parents, aliases and facts stay on
    the admin's GET /v1/people."""
    return JSONResponse({"people": household_people(await asyncio.to_thread(_store().people))})


# Another adult's own settings (their bill vendors' life areas, their named places, the calendars they hide) are
# theirs, even from the admin's household list (per-person privacy; flagged in jellybean#376's review). Who they are,
# their role, devices and which calendars are theirs stay: those run the household. A kid's stay for the parent.
_ADULT_PRIVATE_FACTS = ("facets", "places", "calendars_ignored")


def _people_for_admin(people: list[dict], viewer: str | None) -> list[dict]:
    return [{k: v for k, v in p.items() if k not in _ADULT_PRIVATE_FACTS}
            if p.get("role") in ("admin", "adult") and p.get("id") != viewer else p for p in people]


@app.post("/v1/people")
async def people_add(request: Request) -> JSONResponse:
    """(admin) {name, role} → 201 {id, name, role}."""
    _require_admin(request)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    try:
        person = await asyncio.to_thread(_store().add_person, body.get("name"), body.get("role"))
    except _hh.PairingError as exc:
        return _pairing_error(exc)
    return JSONResponse(person, status_code=201)


@app.patch("/v1/people/{person_id}")
async def people_update(person_id: str, request: Request) -> JSONResponse:
    """(admin) {name?, role?, birth_year?, contact?, calendars?, parents?, other_parent?, aliases?, facets?} → the person with their
    household facts. other_parent: a kid's parent outside the household, as the household says it ("their dad").
    Zach's admin role can't be changed; a fact left out is unchanged, null clears it."""
    _require_admin(request)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    facts = {k: body[k] for k in ("birth_year", "contact", "calendars", "parents", "other_parent", "aliases") if k in body}
    try:
        person = await asyncio.to_thread(lambda: _store().update_person(
            person_id, body.get("name"), body.get("role"), **facts))
        if "facets" in body:   # #354: a kid's facet map is set here, by the admin for their parents
            person["facets"] = await asyncio.to_thread(_store().set_facets, person_id, body["facets"])
    except _hh.PairingError as exc:
        return _pairing_error(exc)
    return JSONResponse(person)


@app.put("/v1/people/{person_id}/call_me")
async def people_call_me(person_id: str, request: Request) -> JSONResponse:
    """{call_me} → what titan calls a kid (#1080), set by their parent or the admin; a person's own goes through
    PATCH /v1/me. Nobody sets another adult's, the admin included. null clears it."""
    ident = _identity(request)
    body = await _json_body(request)
    if not isinstance(body, dict) or "call_me" not in body:
        return JSONResponse({"error": "expected {call_me}"}, status_code=400)
    if not await asyncio.to_thread(_store().may_set_call_me, ident, person_id):
        return JSONResponse({"error": "only they (or a kid's parent) choose what titan calls them"}, status_code=403)
    try:
        v = await asyncio.to_thread(_store().set_call_me, person_id, body["call_me"])
    except _hh.PairingError as exc:
        return _pairing_error(exc)
    return JSONResponse({"id": person_id, "call_me": v})


@app.get("/v1/me/prefs")
async def me_prefs_get(request: Request) -> JSONResponse:
    """The caller's look across their devices (#1159): {look, accent, follow, updated_at}; nothing set yet: look and
    accent null, follow true. A person's own (kids too); a shared screen gets 403 (route table)."""
    import me_prefs
    return JSONResponse(await asyncio.to_thread(me_prefs.get, _identity(request).person_id))


@app.put("/v1/me/prefs")
async def me_prefs_put(request: Request) -> JSONResponse:
    """Any of {look: cool|warm|graphite|paper|evening|macos, accent: a catalogue name or #RRGGBB, follow: bool}: the
    caller's own, stamped now → the prefs; 400 for anything else."""
    import me_prefs
    try:
        out = await asyncio.to_thread(me_prefs.put, _identity(request).person_id, await _json_body(request))
    except me_prefs.Invalid as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(out)


@app.get("/v1/me/clearing")
async def me_clearing_get(request: Request) -> JSONResponse:
    """What "clear this" means for the caller (#1646): {mode: archive|trash, kinds: {kind: mode}, updated_at}; nothing
    set yet: archive. A person's own; a shared screen gets 403 (route table)."""
    import clear_prefs
    return JSONResponse(await asyncio.to_thread(clear_prefs.get, _identity(request).person_id))


@app.put("/v1/me/clearing")
async def me_clearing_put(request: Request) -> JSONResponse:
    """Either or both of {mode: archive|trash, kinds: {mail|files|photos|notes|reminders|messages: archive|trash|null}}:
    the caller's own → the setting; 400 for anything else, deleting permanently included (not offered yet)."""
    import clear_prefs
    try:
        out = await asyncio.to_thread(clear_prefs.put, _identity(request).person_id, await _json_body(request))
    except clear_prefs.Invalid as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(out)


@app.get("/v1/me/triage-rules")
async def me_triage_rules(request: Request) -> JSONResponse:
    """What the caller taught mail triage (#1644): {rules: [{sender, rule: keep|drop, keeps, drops, updated_at}]}, their own
    only, newest first. A shared screen gets 403 (route table)."""
    import mail_triage
    return JSONResponse({"rules": await asyncio.to_thread(mail_triage.rules, _identity(request).person_id)})


@app.get("/v1/me/triage-score")
async def me_triage_score(request: Request) -> JSONResponse:
    """How well mail triage matches what the caller does with their mail (#1644, triage_scorecard): {days, cleared,
    agreed, triage_kept, untriaged, filed, undone, missed, agreement}, counts only, their own. ?days= (default 30, at
    most 90). A shared screen gets 403 (route table)."""
    import time
    import triage_scorecard
    try:
        days = max(1, min(int(request.query_params.get("days") or 30), 90))
    except ValueError:
        return JSONResponse({"error": "days is a number of days"}, status_code=400)
    got = await asyncio.to_thread(triage_scorecard.score, _identity(request).person_id, since=time.time() - days * 86400)
    return JSONResponse({"days": days, **got})


@app.delete("/v1/me/triage-rules/{sender}")
async def me_triage_rule_forget(sender: str, request: Request) -> JSONResponse:
    """Take back what the caller taught triage about one sender (its address) → {forgotten: bool}; their own only."""
    import mail_triage
    if not mail_triage.sender_key(sender):
        return JSONResponse({"error": "say the sender's address"}, status_code=400)
    return JSONResponse({"forgotten": await asyncio.to_thread(mail_triage.forget_rule, _identity(request).person_id, sender)})


@app.get("/v1/me/capability-log")
async def me_capability_log(request: Request) -> JSONResponse:
    """What titan did for the caller (#1628 slice 4): {entries: [{ts, role, surface, source, tool, domain, risk,
    outcome: ok|failed|refused|declined|timeout|unavailable (#1631), why, ms}], keep_days}, newest first, the caller's
    own rows only, never arguments or results. ?limit= (default 200, at most 1000), ?since= epoch seconds."""
    import capability_audit
    q = request.query_params
    try:
        limit = int(q.get("limit") or 200)
        since = float(q["since"]) if q.get("since") else None
    except ValueError:
        return JSONResponse({"error": "limit and since are numbers"}, status_code=400)
    rows = await asyncio.to_thread(capability_audit.log, _identity(request).person_id, limit=limit, since=since)
    return JSONResponse({"entries": rows, "keep_days": capability_audit.KEEP_DAYS})


@app.get("/v1/me/capabilities")
async def me_capabilities_get(request: Request) -> JSONResponse:
    """The caller's capability tiers (#1628, #1627): {domains: [{domain, words, tier: always|never, switched_off}],
    capabilities: [{id, words, domain, tier, switched_off}], all_off}. Always (on) is the default for each; a
    capability is listed only when the caller's role has a setting for it; switched_off is titan's kill switch for the
    whole house, which only an admin changes on titan."""
    import governance
    import tool_descriptors
    ident = _identity(request)
    mine = await asyncio.to_thread(governance.tiers, ident.person_id)
    sw = governance.switches()
    return JSONResponse({"all_off": sw["all"], "domains": [
        {"domain": d, "words": w, "tier": mine.get(d, "always"), "switched_off": d in sw["off"]}
        for d, w in tool_descriptors.DOMAINS.items()], "capabilities": [
        {"id": c, "words": governance.CAPABILITIES[c].words, "domain": governance.CAPABILITIES[c].domain,
         "tier": mine.get(c, "always"), "switched_off": governance.killed(c)}
        for c in governance.for_role(ident.role)]})


@app.put("/v1/me/capabilities")
async def me_capabilities_put(request: Request) -> JSONResponse:
    """{domain or capability id: always|never, ...}: the caller's own tiers → the same shape as GET; 400 for an unknown
    name or tier, or a capability the caller's role has no setting for (nothing is written then)."""
    import governance
    import tool_descriptors
    body = await _json_body(request)
    if not isinstance(body, dict) or not body:
        return JSONResponse({"error": "send {domain or capability: always|never}"}, status_code=400)
    ident = _identity(request)
    theirs = set(governance.for_role(ident.role))
    for d, t in body.items():
        if (d not in tool_descriptors.DOMAINS and d not in theirs) or t not in governance.TIERS:
            return JSONResponse({"error": f"unknown domain, capability or tier: {d}"}, status_code=400)
    pid = ident.person_id
    for d, t in body.items():
        await asyncio.to_thread(governance.set_tier, pid, d, t)
    return await me_capabilities_get(request)


@app.get("/v1/me/autonomy")
async def me_autonomy_get(request: Request) -> JSONResponse:
    """What titan may do without asking you (#1645): {operations: [{tool, words, level: ask|act_tell|act_quiet,
    explicit_only}]}, the operations titan holds for your OK that your role may use. ask is the default; act_tell runs
    it and a card says so; act_quiet runs it and it shows in your record only."""
    import autonomy
    import agent_runtime_lib as lib
    ident = _identity(request)
    mine = await asyncio.to_thread(autonomy.levels, ident.person_id)
    return JSONResponse({"operations": [
        {"tool": t, "words": lib._ask_what(t), "level": (mine.get(t) or {}).get("level", "ask"),
         "explicit_only": autonomy.explicit_only(t)} for t in autonomy.operations(ident.role)]})


@app.put("/v1/me/autonomy")
async def me_autonomy_put(request: Request) -> JSONResponse:
    """{tool: ask|act_tell|act_quiet, ...}: your own levels (set by you) → the same shape as GET; 400 for an operation
    you have no level for or an unknown level (nothing is written then)."""
    import autonomy
    body = await _json_body(request)
    ident = _identity(request)
    theirs = set(autonomy.operations(ident.role))
    if not isinstance(body, dict) or not body:
        return JSONResponse({"error": "send {tool: ask|act_tell|act_quiet}"}, status_code=400)
    for t, lv in body.items():
        if t not in theirs or lv not in autonomy.LEVELS:
            return JSONResponse({"error": f"unknown operation or level: {t}"}, status_code=400)
    for t, lv in body.items():
        await asyncio.to_thread(autonomy.set_level, ident.person_id, t, lv)
    return await me_autonomy_get(request)


@app.get("/v1/me/places")
async def me_places(request: Request) -> JSONResponse:
    """Settings › You › Your places (#1133): {places: [{place, last_arrived, when}], count, mode}. The caller's own
    arrivals only (named places, not scheduled regions), newest first, names as stored, never coordinates. mode: their
    phone's location permission as it last reported it ({value, precise, device, at}), null until one has. A kid sees
    their own; a shared household screen: 403."""
    import places
    ident = _identity(request)
    if ident.role == "household":
        raise HTTPException(status_code=403, detail=_hh.REFUSAL)
    rows = await asyncio.to_thread(places.my_places, ident.person_id)
    mode = await asyncio.to_thread(places.mode_of, ident.person_id)
    return JSONResponse({"places": rows, "count": len(rows), "mode": mode})


@app.post("/v1/me/places/mode")
async def me_places_mode(request: Request) -> JSONResponse:
    """A person's own phone reports its location permission (#1133): {value: always|while_using|off, precise?}. Stored
    per device; only that person reads it back (GET /v1/me/places)."""
    import places
    ident = _identity(request)
    if ident.role == "household" or not ident.device:
        raise HTTPException(status_code=403, detail=_hh.REFUSAL)
    body = await _json_body(request)
    body = body if isinstance(body, dict) else {}
    try:
        mode = await asyncio.to_thread(places.set_mode, ident.person_id, ident.device["id"], ident.device.get("name"),
                                       body.get("value"), body.get("precise"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"mode": mode})


@app.get("/v1/presence/places")
async def presence_places(request: Request) -> JSONResponse:
    """Where the people this caller may see last were, by named place only: their own and, for a parent, their kids'
    (Zach, 10-02; jellybean#186/#308). {people: [{id, name, event: arrived|left, place, at, when}]} (`when`: a
    household-local phrase, "at 8:12 AM today (3 h ago)"; the latest by the event's own time, jitter skipped); a person with no
    event yet is left out. Never coordinates, never another adult's, nothing on a household device."""
    import places
    ident = _identity(request)
    try:
        folks = _store().people()
    except Exception:  # noqa: BLE001
        folks = []
    names = {p["id"]: p.get("name") for p in folks}
    out = []
    for pid in places.viewable(ident.person_id, ident.role, folks):
        last = await asyncio.to_thread(places.where_now, pid)
        if last:
            out.append({"id": pid, "name": names.get(pid) or pid, "event": last["event"], "place": last["place"],
                        "at": last.get("at"), "when": places.when_text(last)})
    return JSONResponse({"people": out})


@app.get("/v1/home")
async def home_screen(request: Request) -> JSONResponse:
    """Home for a shared screen (titan-node#669; the Apple TV, jellybean#416): {header {date, time, weather, pill},
    up_next, today, chores, house, playing, shelf}. Built in code (home_screen.py); household items only, whoever asks;
    the kids' stays only on people's own devices or with FAMILY_SCREEN_CUSTODY=1."""
    import home_screen
    ident = _identity(request)
    return JSONResponse(await asyncio.to_thread(home_screen.gather, ident.role))


@app.get("/v1/me/references")
async def me_references(request: Request) -> JSONResponse:
    """#1135, for Share the file (jellybean#952): the caller's own references only, newest first, {references: [{id,
    kind, title, native (path|id), file_shared, audience, last_seen}], count}. Never another person's; never the native
    id itself. A shared screen: 403."""
    import references
    ident = _identity(request)
    try:
        rows = await asyncio.to_thread(references.mine, ident.person_id, ident.role)
    except references.RefError as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    return JSONResponse({"references": rows, "count": len(rows)})


def _mail_open(ids: list[str]) -> dict[str, dict[str, str]]:
    """#1497: where each mail item opens: its thread's KB entry (server mail) or its device reference (Mac-held).
    Ids only; a store that can't be read leaves its mail without Open."""
    out: dict[str, dict[str, str]] = {}
    try:
        import mail_server
        out.update({i: {"thread": t} for i, t in mail_server.threads_of(ids).items()})
    except Exception:  # noqa: BLE001
        pass
    try:
        import item_refs
        out.update({i: {"reference": r} for i, r in item_refs.refs_of([i for i in ids if i not in out]).items()})
    except Exception:  # noqa: BLE001
        pass
    return out


@app.get("/v1/references/{ref_id}")
async def reference_one(ref_id: str, request: Request) -> JSONResponse:
    """(#1497) One reference the caller may see, for Open on a Mac-held item: {reference: {id, owner, device_id, kind,
    native_id, …}}; 200 {} when it's gone or the caller can't see it (nobody learns whether it exists)."""
    import references
    ident = _identity(request)
    if not references.REF_ID_RX.match(ref_id or ""):
        return JSONResponse({})
    got = await asyncio.to_thread(references.visible, ident.person_id, ident.role, None, 100000)
    hit = next((r for r in got if r.get("id") == ref_id), None)
    return JSONResponse({"reference": hit} if hit else {})


@app.get("/v1/references")
async def references_list(request: Request, kind: str | None = None) -> JSONResponse:
    """(#446) Pointers to items on people's devices, never their bodies: the caller's own and what's shared with them
    (audience.py). {references: [{id, owner, audience, device_id, kind, native_id, content_hash, title, dates,
    last_seen, …}]}; `kind` filters (mail, scan, file, photo, …)."""
    import references
    ident = _identity(request)
    return JSONResponse({"references": await asyncio.to_thread(references.visible, ident.person_id, ident.role,
                                                               (kind or "").strip().lower() or None)})


@app.post("/v1/references")
async def references_put(request: Request) -> JSONResponse:
    """(#446) A device writes (or re-sends) a reference for its own person: {kind, native_id, content_hash?, title?,
    dates?, audience?, last_seen?}. Never a body (400). Only from a person's own device (403 otherwise)."""
    import references
    ident = _identity(request)
    body = await _json_body(request)
    try:
        rec = await asyncio.to_thread(references.put, ident.person_id, ident.role, (ident.device or {}).get("id"), body)
    except references.RefError as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    return JSONResponse(rec)


@app.get("/v1/threads")
async def threads_get(request: Request, about: str = "", hold: str = "", all: str = "") -> JSONResponse:
    """(#951 (g)) The asker's threads as data, for the apps: the same view the threads tool reads (threadview.build).
    Per viewer; kids and shared screens get none (403). Never an identifier value. docs/threads-api.md."""
    import threadview
    ident = _identity(request)
    if ident.role in ("kid", "household"):
        return JSONResponse({"error": "threads aren't shown here"}, status_code=403)
    view = await asyncio.to_thread(threadview.build, ident.person_id, ident.role, about, hold,
                                   all.strip().lower() in ("yes", "true", "1"), _lib._local_now().date())
    return JSONResponse(view, status_code=400 if view.get("status") == "bad_hold" else 200)


@app.post("/v1/threads/relate")
async def threads_relate(request: Request) -> JSONResponse:
    """(#951 (g)) The person's correction, {a, b, related, source?}: two items are (or aren't) related. A statement that
    outranks the linker; only for items the asker may see (conversations, "chat-…": only their own, #1016). source:
    "baseline" (a labelling pass) or "correction" (the default)."""
    import threadview
    ident = _identity(request)
    body = await _json_body(request)
    body = body if isinstance(body, dict) else {}
    try:
        rec = await asyncio.to_thread(threadview.relate, ident.person_id, ident.role, body.get("a"), body.get("b"),
                                      bool(body.get("related")), body.get("source") or "correction")
    except threadview.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    return JSONResponse(rec)


@app.post("/v1/hooks")
async def hooks_put(request: Request) -> JSONResponse:
    """(#951) A person's own device sends one item's link hooks as raw values over the paired channel; titan keys them
    with its salt on arrival (the salt never leaves titan) and never stores or logs a raw value. Never a body (400).
    {"kind": "event_signal", "signal", "event_ref", "proposed_start", "confidence", "sig_id"}: a text that moves or
    cancels one of the poster's own calendar events, those fields only (event_signals.py); a card asks the owner.
    {"kind": "obligation_signal", "signal", "account_ref", "amount", "on", "confidence", "sig_id"}: a text that
    confirms a payment (or a failed one) on one of the poster's own obligations, by its ref from
    /v1/obligations/watch, those fields only (obligation_signals.py); evidence on the poster's own view only.
    {"kind": "event_proposal", "title", "start", "end", "place", "participants", "others", "confidence", "thread_ref",
    "sig_id"}: a plan agreed over texts (#1363, event_proposals.py); an "Add to calendar?" card to the poster only.
    {"kind": "meeting_summary", "ref_id", "title", "day", "speakers", "turns", "proposals", "sig_id"}: a transcript on
    the poster's Mac, counts only (meeting_notes.py, #1364); titan reads it once, soon after (this process's meeting-notes worker), for the poster's own notes."""
    import hooks
    ident = _identity(request)
    body = await _json_body(request)
    try:
        rec = await asyncio.to_thread(hooks.put_device, ident.person_id, ident.role, (ident.device or {}).get("id"),
                                      body if isinstance(body, dict) else {},
                                      accounts=lambda: _lib._obligation_watch(ident.person_id, ident.role)[1])
    except hooks.HookError as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    if isinstance(body, dict) and body.get("kind") == "meeting_summary":
        _MEETING_NOTES_KICK.set()   # read it now, while the Mac that posted it is awake
    return JSONResponse(rec)


@app.get("/v1/event_proposals/kept")
async def event_proposals_kept(request: Request, since: str = "") -> JSONResponse:
    """(jellybean#1189) The caller's own plans from texts they kept (tapped Add or Update on the card), since an ISO
    day (household zone, inclusive; empty: all): {"kept": [{"sig_id", "thread_ref"}]}, ids only. Their own paired
    device only (403 without one, and for a shared screen); the Mac then posts thread:device hooks for those texts."""
    import event_proposals
    import hooks
    ident = _identity(request)
    if not ident.person_id or not (ident.device or {}).get("id") or ident.role == "household":
        return JSONResponse({"error": "a person's own device only"}, status_code=403)
    try:
        def read() -> list[dict[str, str]]:
            con = hooks.connect()
            try:
                return event_proposals.kept(con, ident.person_id, since or None)
            finally:
                con.close()
        got = await asyncio.to_thread(read)
    except event_proposals.ProposalError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"kept": got})


@app.get("/v1/obligations/watch")
async def obligations_watch(request: Request) -> JSONResponse:
    """(#951 step 4) What the caller's own device may match a text to: {obligations: [{account_ref, vendor, amount,
    due, state}]}, their visible open obligations, newest cycle per account. Adults only (kids and shared screens:
    403). The device posts a match as an obligation_signal (POST /v1/hooks); no text ever comes back."""
    ident = _identity(request)
    if not ident.person_id or ident.role not in ("admin", "adult"):
        return JSONResponse({"error": "bills are an adult's own"}, status_code=403)
    got, _refs = await asyncio.to_thread(_lib._obligation_watch, ident.person_id, ident.role)
    return JSONResponse({"obligations": got})


@app.get("/v1/surfaces/account")
async def surfaces_account(request: Request, ref: str = "") -> JSONResponse:
    """(#951 step 5) One account's page as an `explain` ResolvedSurface (account@1, docs/surfaces.md §9): the current
    cycle's state, then each cycle's dated evidence (mail and records observed, a person's word declared, a text read on
    its owner's device derived) and how each bears on the state; the caller's own word, share and take-back as
    operations. ref: an account_ref (GET /v1/obligations/watch, or `acct-` + the bill item's account). Adults only;
    an account they can't see is a 404 like a missing one."""
    import bill_due
    import surfaces
    ident = _identity(request)
    ts = await asyncio.to_thread(_lib._account_threads, ident.person_id, ident.role, ref)
    if not ts:
        return JSONResponse({"error": "no such account"}, status_code=404)
    word = await asyncio.to_thread(bill_due.my_word, ident.person_id, ts[0].account)
    return JSONResponse(surfaces.account(ts, ref, ident.person_id, ident.role, time.time(), my_word=word))


async def _account_or_404(request: Request, ref: str) -> tuple[Any, list[Any]] | JSONResponse:
    ident = _identity(request)
    ts = await asyncio.to_thread(_lib._account_threads, ident.person_id, ident.role, ref)
    if not ts:
        return JSONResponse({"error": "no such account"}, status_code=404)
    return ident, sorted(ts, key=lambda t: t.cycle, reverse=True)


@app.post("/v1/obligations/{ref}/say")
async def obligations_say(ref: str, request: Request) -> JSONResponse:
    """(#951 step 5) The caller's own word on an account, from its page: {kind: paid | disputed | not_mine | handled_by,
    handled_by?: a household member's name} → {kind, share_ask}. A private claim (owner the caller), as in chat;
    share_ask: the bill is the household's or the adults', so the page may offer Share once."""
    import bill_due
    got = await _account_or_404(request, ref)
    if isinstance(got, JSONResponse):
        return got
    ident, ts = got
    body = await _json_body(request)
    kind = str((body or {}).get("kind") or "") if isinstance(body, dict) else ""
    if kind not in ("paid", "disputed", "not_mine", "handled_by"):
        return JSONResponse({"error": "kind is paid, disputed, not_mine or handled_by"}, status_code=400)
    about = None
    if kind == "handled_by":
        about, _first = await asyncio.to_thread(_lib._household_member, str(body.get("handled_by") or ""))
        if about is None:
            return JSONResponse({"error": "handled_by names one household member"}, status_code=400)
    await asyncio.to_thread(bill_due.say_kind, ident.person_id, ts[0].account, kind, _lib._local_now().date(),
                            about=about, source="app")
    return JSONResponse({"kind": kind, "share_ask": ts[0].audience in bill_due.SHARE_AUDIENCES})


@app.post("/v1/obligations/{ref}/share")
async def obligations_share(ref: str, request: Request) -> JSONResponse:
    """(#951 step 5, decision (b)) The caller's answer to "tell the other adults?": {yes: true|false} → {result:
    shared | kept | not_asked | none}. Only their own latest word, only on a household or adults' bill."""
    import bill_due
    got = await _account_or_404(request, ref)
    if isinstance(got, JSONResponse):
        return got
    ident, ts = got
    body = await _json_body(request)
    yes = bool((body or {}).get("yes")) if isinstance(body, dict) else False
    res = await asyncio.to_thread(bill_due.share, ident.person_id, ident.role, ts[0].account, yes,
                                  account_audience=ts[0].audience)
    return JSONResponse({"result": res})


@app.post("/v1/obligations/{ref}/take-back")
async def obligations_take_back(ref: str, request: Request) -> JSONResponse:
    """(#951 step 5) The caller takes their shared word back: {taken_back: bool}; theirs alone again."""
    import bill_due
    got = await _account_or_404(request, ref)
    if isinstance(got, JSONResponse):
        return got
    ident, ts = got
    return JSONResponse({"taken_back": await asyncio.to_thread(bill_due.take_back, ident.person_id, ts[0].account)})


@app.post("/v1/places/confirm")
async def places_confirm(request: Request) -> JSONResponse:
    """(#1427) The caller's word on one of their places: {id, label?, kind?, share?: true, same_as?} → the place. The
    owner (or a kid's parent) only; titan decides owner and audience; a kid never widens; someone else's is a 404."""
    import places_seed
    ident = _identity(request)
    body = await _json_body(request)
    try:
        got = await asyncio.to_thread(places_seed.confirm, ident.person_id, ident.role, body if isinstance(body, dict) else {})
    except places_seed.ConfirmError as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    return JSONResponse(got)


async def _place_change(request: Request, verb: str, place_id: str | None = None) -> JSONResponse:
    import surfaces
    ident = _identity(request)
    body = await _json_body(request) if verb != "remove" else {}
    try:
        out = await asyncio.to_thread(surfaces.place_change, ident.person_id, ident.role, verb, place_id=place_id,
                                      body=body if isinstance(body, dict) else {})
        return JSONResponse(out, status_code=201 if verb == "add" else 200)
    except surfaces.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.post("/v1/places")
async def place_add(request: Request) -> JSONResponse:
    """{name, kind?, visibility?: private|household} → 201 {place, token} (#1510): a place the caller names; theirs
    alone unless an adult says household; 409 when they already have one by that name. Adults only; the token undoes it."""
    return await _place_change(request, "add")


@app.patch("/v1/places/{place_id}")
async def place_edit(place_id: str, request: Request) -> JSONResponse:
    """{name?, kind?, visibility?} → {place, token}: the caller's word on one of their places (or their kid's); the old
    name stays as an alias and nothing learned overwrites it. Someone else's is a 404. Adults only; the token undoes it."""
    return await _place_change(request, "edit", place_id)


@app.delete("/v1/places/{place_id}")
async def place_remove(place_id: str, request: Request) -> JSONResponse:
    """→ {place, token}: archives the place (hidden everywhere, never relearned, never deleted); the token puts it
    back. Someone else's is a 404. Adults only."""
    return await _place_change(request, "remove", place_id)


@app.patch("/v1/references/{ref_id}")
async def references_patch(ref_id: str, request: Request) -> JSONResponse:
    """(#446) The owner's own device updates a reference's title, dates, hash, audience or last_seen; never what it
    points to. Someone else's is a 404."""
    import references
    ident = _identity(request)
    body = await _json_body(request)
    try:
        rec = await asyncio.to_thread(references.patch, ref_id, ident.person_id, ident.role,
                                      (ident.device or {}).get("id"), body if isinstance(body, dict) else {})
    except references.RefError as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    return JSONResponse(rec)


@app.get("/v1/home/kids")
async def home_kids(request: Request) -> JSONResponse:
    """The kids' screen (jellybean#422): {kids: [{id, name, chores: [{id, text, at, done, ritual?}], next, streak, sleeps?}]}.
    Kids' first names and their household chores only, for every role; sleeps only with custody allowed."""
    import kids_screen
    ident = _identity(request)
    return JSONResponse(await asyncio.to_thread(kids_screen.gather, ident.role))


@app.post("/v1/home/kids/chores/{chore_id}/done")
async def home_kids_done(chore_id: str, request: Request) -> JSONResponse:
    """The one write a household screen gets (jellybean#422): {done: true|false} for today's occurrence of an assigned
    household chore. A household device or the admin for any kid's; a kid for their own. Idempotent; logged with
    device, person and time. 404 when it isn't due today."""
    import kids_screen
    ident = _identity(request)
    body = await _json_body(request)
    done = bool((body or {}).get("done", True)) if isinstance(body, dict) else True
    status, out = await asyncio.to_thread(kids_screen.mark_live, chore_id, done, ident.person_id, ident.role,
                                          (ident.device or {}).get("id"))
    return JSONResponse(out, status_code=status)


@app.get("/v1/our-year")
async def our_year_view(request: Request, year: int | None = None) -> JSONResponse:
    """The household's past year for the TV (jellybean#430): household-calendar moments, trips, milestones, photo
    counts and a year ago today. Always the household's view, whoever asks (our_year.py)."""
    import our_year
    if year is not None and not 2000 <= year <= 2100:
        return JSONResponse({"error": "year must be 2000-2100"}, status_code=400)
    try:
        return JSONResponse(await asyncio.to_thread(our_year.gather, year))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": _lib.plain_failure(exc)}, status_code=500)


@app.post("/v1/our-year/photos")
async def our_year_photos(request: Request) -> JSONResponse:
    """A person's own phone: per-day counts of the photos in albums they've shared with the household,
    {"days": {"YYYY-MM-DD": count}} (opt-in; counts only). Replaces theirs for those days. Never a shared device."""
    import our_year
    ident = _identity(request)
    if not ident.device or ident.role == "household":
        return JSONResponse({"error": "only a person's own phone sends photo counts"}, status_code=400)
    body = await _json_body(request)
    try:
        n = await asyncio.to_thread(our_year.set_photo_days, ident.person_id, (body or {}).get("days"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"days": n})


@app.post("/v1/presence/room")
async def presence_room(request: Request):
    """(#433) A person's own iPhone or Watch: which room they're in, for their own requests only ("lights here").
    {room, confidence 0-1, ts (epoch seconds)} → 204. Kept in memory for 10 minutes, never stored, never logged, never
    read back (no GET). Adults and the admin only; a shared device, a Mac, a TV or a kid's device → 403. 400 for a
    room that isn't the household's (with the list), a stale or future ts, or a bad number."""
    import home_assistant as ha
    import room_hint
    from fastapi.responses import Response
    ident = _identity(request)
    why = room_hint.refusal(ident.role, ident.device, ident.via)
    if why:
        return JSONResponse({"error": why}, status_code=403)
    body = await _json_body(request)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)
    import rooms as _rooms
    try:
        room_list = await asyncio.to_thread(_rooms_synced)
    except Exception:  # noqa: BLE001
        room_list = []
    # (#1511) a room Settings renamed is still its Home Assistant area for "lights in here": kept as the area's name
    area_of = {_rooms.key(r["name"]): r["ha_area"] for r in room_list if r.get("ha_area")}
    room = body.get("room")
    hint_room = area_of.get(_rooms.key(room), room) if isinstance(room, str) else room
    names = {r["name"] for r in room_list} | set(area_of.values())
    try:
        room_hint.put(ident.person_id, ident.device["id"], hint_room, body.get("confidence"), body.get("ts"),
                      names)
    except room_hint.HintError as exc:
        return JSONResponse({"error": str(exc), **({"rooms": exc.rooms} if exc.rooms else {})}, status_code=exc.status)
    return Response(status_code=204)


@app.delete("/v1/presence/room")
async def presence_room_clear(request: Request):
    """(#433) The person's own device stops sensing, or they turned it off: their room hint goes now. 204."""
    import room_hint
    from fastapi.responses import Response
    ident = _identity(request)
    why = room_hint.refusal(ident.role, ident.device, ident.via)
    if why:
        return JSONResponse({"error": why}, status_code=403)
    room_hint.clear(ident.person_id)
    return Response(status_code=204)


@app.post("/v1/capture")
async def capture_save(request: Request) -> JSONResponse:
    """A page the person saved from their own browser (#858; Mac jellybean#691, iPhone jellybean#692): {url, text,
    title?, app?, captured_at?}, kept private in the KB's web zone under the id ingest_url would give the same address
    (saved twice: updated, never duplicated). → 201 {id, title, status: saved|updated|unchanged}. Not from a shared
    device or a kid (they don't write the KB). A link with little or no text is fetched by titan; one it can't read
    (a walled site, a block page, no text) → 422 {error: blocked|unreachable, message, site}."""
    import capture
    ident = _identity(request)
    if ident.role in ("household", "kid") or not ident.person_id:
        return JSONResponse({"error": "only an adult's own device saves pages to their knowledge base"}, status_code=403)
    body = await _json_body(request)
    try:
        out = await asyncio.to_thread(capture.save, ident.person_id, body or {})
    except capture.CaptureNotFetched as exc:
        return JSONResponse(exc.body(), status_code=422)
    except capture.CaptureError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(out, status_code=201 if out["status"] == "saved" else 200)


@app.delete("/v1/capture/{capture_id}")
async def capture_remove(request: Request, capture_id: str) -> JSONResponse:
    """Undo a capture: the person's own only (404 otherwise, never revealing someone else's)."""
    import capture
    ident = _identity(request)
    if not await asyncio.to_thread(capture.remove, ident.person_id or "", capture_id):
        return JSONResponse({"error": "no such capture of yours"}, status_code=404)
    return JSONResponse({"id": capture_id, "removed": True})


@app.get("/v1/home/week")
async def home_week(request: Request, start: str | None = None) -> JSONResponse:
    """The shared screen's week (jellybean#417): {start, days: [{date, label, today, events, chores, state?}],
    series}. Built in code (home_screen.week); household items only, whoever asks. `start`: YYYY-MM-DD (default today)."""
    import home_screen
    ident = _identity(request)
    try:
        d = date.fromisoformat(start) if start else None
    except ValueError:
        return JSONResponse({"error": "start must be YYYY-MM-DD"}, status_code=400)
    return JSONResponse(await asyncio.to_thread(home_screen.gather_week, ident.role, d))


@app.post("/v1/home/media")
async def home_media(request: Request) -> JSONResponse:
    """Play, pause or skip what Home shows playing (jellybean#421): {id: a media_player Home lists now, action:
    play|pause|next|previous} → {ok, text}. Anyone in the household, a shared TV and kids included; nothing but those
    four actions on a player Home is showing (home_screen.media_control), through home_control's checks."""
    import home_screen
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected {id, action}"}, status_code=400)
    ident = _identity(request)
    eid, action = str(body.get("id") or "")[:200], str(body.get("action") or "")[:20]
    code, out = await asyncio.to_thread(home_screen.media_control, eid, action)
    print(f"[home/media] {ident.person_id} ({ident.role}) {action} {eid} → {code}", file=sys.stderr)
    return JSONResponse(out, status_code=code)


@app.get("/v1/home/house")
async def home_house(request: Request) -> JSONResponse:
    """The house, room by room (jellybean#420): {rooms: [{name, lights, climate}], scenes}. Lights, thermostats and
    scenes only (home_screen.house_view); never locks, alarms, garage doors or cameras. A kid gets KID_HOME_DOMAINS."""
    import home_screen
    ident = _identity(request)
    code, out = await asyncio.to_thread(home_screen.gather_house, ident.role)
    return JSONResponse(out, status_code=code)


@app.get("/v1/home/security")
async def home_security_status(request: Request) -> JSONResponse:
    """The house's locks, doors and garage doors, read-only (titan-node#413): {locks, doors, garage, summary, lock_up,
    snapshots}. Admin, adult and household screens; a kid's device gets 403 (ROUTE_ROLES). No control, no history."""
    import home_security
    code, out = await asyncio.to_thread(home_security.status)
    return JSONResponse(out, status_code=code)


@app.get("/v1/home/cameras")
async def home_cameras(request: Request) -> JSONResponse:
    """The cameras a TV can show stills of (titan-node#413): {cameras: [{id, name}], refresh_s}. 403 unless
    HA_CAMERA_SNAPSHOTS=1."""
    import home_security
    code, out = await asyncio.to_thread(home_security.cameras)
    return JSONResponse(out, status_code=code)


@app.get("/v1/home/cameras/{entity_id}/snapshot")
async def home_camera_snapshot(entity_id: str, request: Request):
    """A camera's still, passed through from memory (titan-node#413): never stored, home network only (not Tailscale),
    at most once per 30 s per device and camera (429). 403 unless HA_CAMERA_SNAPSHOTS=1."""
    import home_security
    from fastapi.responses import Response
    ident = _identity(request)
    dev = (ident.device or {}).get("id") or f"person:{ident.person_id}"
    host = request.client.host if request.client else None
    code, body, headers = await asyncio.to_thread(lambda: home_security.snapshot(entity_id, dev, host, role=ident.role))
    if code == 200:
        return Response(content=body, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    return JSONResponse(body, status_code=code, headers=headers)


@app.post("/v1/home/cameras/{entity_id}/live")
async def home_camera_live(entity_id: str, request: Request) -> JSONResponse:
    """Live video on tap (titan-node#413, Zach 10-04): 201 {session, playlist, expires_at, max_s}. titan relays Home
    Assistant's HLS from memory, never recorded; home network only, local cameras only; 5 minutes, 20 s idle ends it; at
    most 2 at once (429). 403 unless HA_CAMERA_LIVE=1."""
    import home_security
    ident = _identity(request)
    dev = (ident.device or {}).get("id") or f"person:{ident.person_id}"
    host = request.client.host if request.client else None
    code, out = await asyncio.to_thread(lambda: home_security.live_start(entity_id, dev, host, role=ident.role))
    return JSONResponse(out, status_code=code)


@app.get("/v1/home/live/{session}/{path:path}")
async def home_live_fetch(session: str, path: str, request: Request):
    """A live session's playlist, init or segment, relayed from memory, no-store (titan-node#413). 410 {ended} once it's
    over (time, idle, closed, off)."""
    import home_security
    from fastapi.responses import Response
    dev = None
    if request.headers.get("x-agent-token") or request.headers.get("authorization"):
        ident = _resolve_identity(request)
        if ident is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if ident.role not in ("admin", "adult", "household"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        dev = (ident.device or {}).get("id") or f"person:{ident.person_id}"
    host = request.client.host if request.client else None
    code, body, kind = await asyncio.to_thread(home_security.live_fetch, session, path, host, dev)
    if code == 200:
        return Response(content=body, media_type=kind, headers={"Cache-Control": "no-store"})
    return JSONResponse(body, status_code=code)


@app.delete("/v1/home/live/{session}")
async def home_live_end(session: str, request: Request):
    """Close a live session now (titan-node#413): 204; only the device that started it."""
    import home_security
    from fastapi.responses import Response
    ident = _identity(request)
    dev = (ident.device or {}).get("id") or f"person:{ident.person_id}"
    code = await asyncio.to_thread(home_security.live_end, session, dev)
    return Response(status_code=code) if code == 204 else JSONResponse({"error": "no such session"}, status_code=code)


@app.post("/v1/home/lock-up")
async def home_lock_up(request: Request) -> JSONResponse:
    """Lock up (titan-node#413): lock only. A card goes to the adult asking (their own iPhone) or, from a household
    screen, to the adults at home; only that tap locks. 202 {request_id, locks, asked, expires_at}; 200 {already} when
    everything's locked; 403 unless HA_LOCK_UP=1."""
    import home_security
    ident = _identity(request)
    asker = {"role": ident.role, "person_id": ident.person_id if ident.role != "household" else None,
             "device_id": (ident.device or {}).get("id") or f"person:{ident.person_id}",
             "device_name": (ident.device or {}).get("name") or ""}
    code, out = await asyncio.to_thread(lambda: home_security.request(asker, _store().people()))
    return JSONResponse(out, status_code=code)


@app.get("/v1/home/lock-up/{request_id}")
async def home_lock_up_result(request_id: str, request: Request) -> JSONResponse:
    """How a Lock Up request went, for the device that asked (titan-node#413): {state: pending|locked|partly|declined|
    expired, results: [{name, state}], said}."""
    import home_security
    ident = _identity(request)
    dev = (ident.device or {}).get("id") or f"person:{ident.person_id}"
    code, out = await asyncio.to_thread(home_security.result, request_id, dev)
    return JSONResponse(out, status_code=code)


@app.get("/v1/home/why")
async def home_why_route(request: Request, target: str = "", hours: int = 12) -> JSONResponse:
    """Why the house is the way it is (jellybean#431): {target, kind, area, now, since, events: [{at, what, by: {kind,
    name}}], answer}, from Home Assistant's own history, built in code. Household-visible devices only: locks, alarms,
    garage doors, cameras and anyone's location answer 403 hands_off. Errors: {error, code} (hands_off, not_found,
    ambiguous with candidates [{target, name, area}], ha_not_connected, bad_request)."""
    import home_why
    _identity(request)
    code, out = await asyncio.to_thread(home_why.why, target, hours)
    return JSONResponse(out, status_code=code)


@app.get("/v1/home/plans")
async def home_plans(request: Request) -> JSONResponse:
    """The plans the admin made for the household (jellybean#429): {plans: [{id, goal, by, progress, steps, updated}]},
    most recent first. Every household screen and person sees the same list; a person's own tasks never appear."""
    _identity(request)
    return JSONResponse(await asyncio.to_thread(_lib.household_plans))


@app.post("/v1/home/house")
async def home_house_control(request: Request) -> JSONResponse:
    """A change the house view offers (jellybean#420): {id: a light, thermostat or scene it lists now, action:
    on|off|toggle|brightness (lights), set_temperature (thermostats), activate (scenes), value?} → {ok, text}.
    Through home_control's checks (home_screen.house_control), held to the role's domains."""
    import home_screen
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected {id, action, value?}"}, status_code=400)
    ident = _identity(request)
    eid, action = str(body.get("id") or "")[:200], str(body.get("action") or "")[:20]
    code, out = await asyncio.to_thread(home_screen.house_control, eid, action, body.get("value"), ident.role)
    print(f"[home/house] {ident.person_id} ({ident.role}) {action} {eid} → {code}", file=sys.stderr)
    return JSONResponse(out, status_code=code)


@app.get("/v1/family-meeting")
async def family_meeting(request: Request, week: str | None = None) -> JSONResponse:
    """The family meeting's agenda (jellybean#425), the household's to see on any device, a shared screen included:
    {week, title, kids, last_week, days: [{date, label, events, handoffs, sort_out}], sort_out}. Built in code
    (family_meeting.py); an event's title only when it's the household's, else "busy". `week`: its Monday."""
    import family_meeting as fm
    ident = _identity(request)
    try:
        start = date.fromisoformat(week) if week else None
    except ValueError:
        return JSONResponse({"error": "week must be YYYY-MM-DD"}, status_code=400)
    return JSONResponse(await asyncio.to_thread(fm.build, start, ident.role))


@app.get("/v1/presence/regions")
async def presence_regions(request: Request):
    """The caller's own pending town and address triggers for their iPhone to watch (#616): {regions: [{id,
    label, kind, lat?, lon?, query?, radius_m, on, expires_at}]}. Never anyone else's (a parent doesn't get a
    kid's); a shared household device has none (403)."""
    import scheduled
    ident = _identity(request)
    if ident.role == "household":
        return JSONResponse({"error": "forbidden", "detail": _hh.REFUSAL}, status_code=403)
    return JSONResponse({"regions": await asyncio.to_thread(scheduled.regions_for, ident.person_id)})


@app.post("/v1/presence/place")
async def presence_place(request: Request):
    """An iPhone's arrival or departure (opt-in): {id, event: arrived|left, place, at}. 204; the
    person's own place routines suggest themselves and their own place-triggered scheduled actions run
    (in the background: a Mac action can take minutes). Shown only to them and, for a kid, their parents
    (GET /v1/presence/places)."""
    import places
    from fastapi.responses import Response
    ident = _identity(request)
    if ident.role == "household":
        return JSONResponse({"error": "forbidden", "detail": _hh.REFUSAL}, status_code=403)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    import scheduled
    region = body.get("region")   # a scheduled town or address's id (#616): "rg_sa-…", the phone's own list
    if region is not None and not scheduled.REGION_ID_RX.match(str(region)):
        return JSONResponse({"error": "region must be an id from GET /v1/presence/regions"}, status_code=400)
    try:
        new = await asyncio.to_thread(places.record, str(body.get("id") or ""), ident.person_id,
                                      str(body.get("event") or ""), body.get("place"), body.get("at"),
                                      None, (ident.device or {}).get("id"), region)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if new:
        place = places.clean_place(body.get("place"))
        event = str(body["event"])
        if event == "arrived" or places.JITTER_S <= 0:
            await _place_effects(ident.person_id, ident.role, event, place, region)
        else:
            # A departure waits places.JITTER_S: an arrival back at the same place cancels it (geofence jitter,
            # jellybean#666), so no routine nudge, TV lock or scheduled "leave" action for a step out and back.
            # Kept in places' store first, so a restart inside the window replays it (_resume_held_departures).
            left_at = places.event_ts({"at": body.get("at"), "received_at": time.time()})
            hid = str(body.get("id") or "").strip()[:64]
            await asyncio.to_thread(places.hold, hid, ident.person_id, ident.role, place, region, left_at,
                                    time.time() + places.JITTER_S)
            _start_held(ident.person_id, ident.role, place, region, left_at, places.JITTER_S, hid)
    return Response(status_code=204)


async def _place_effects(person: str, role: str, event: str, place: str, region: str | None) -> None:
    import routines
    await asyncio.to_thread(routines.on_place_event, person, role, event, place)
    if event == "arrived":
        await asyncio.to_thread(_place_todos_card, person, role, place)
    if event == "left" and place == "home":
        import tv_unlock   # walking away locks any TV showing this adult's things (jellybean#427)
        await asyncio.to_thread(tv_unlock.end_for_person, person, "left_home")
    if event == "left":
        # their next own located event, and how long it'll take (#513; NEXT_STOP_CARDS): in the background, so it
        # never holds up the place event or the routines and actions after it
        nxt = asyncio.get_running_loop().create_task(_next_stop(person, role))
        _place_tasks.add(nxt)
        nxt.add_done_callback(_place_tasks.discard)
    task = asyncio.get_running_loop().create_task(_place_actions(person, role, event, place, region))
    _place_tasks.add(task)
    task.add_done_callback(_place_tasks.discard)


async def _next_stop(person: str, role: str | None) -> None:
    import next_stop
    try:
        await asyncio.to_thread(next_stop.on_left, person, role)
    except Exception as exc:  # noqa: BLE001 - a card never breaks a place event
        print(f"[next-stop] skipped ({type(exc).__name__})", file=sys.stderr)


def _start_held(person: str, role: str | None, place: str, region: str | None, left_at: float, wait: float,
                hold_id: str | None) -> None:
    task = asyncio.get_running_loop().create_task(_held_departure(person, role, place, region, left_at, wait, hold_id))
    _place_tasks.add(task)   # held until done: the loop keeps only a weak reference to a task
    task.add_done_callback(_place_tasks.discard)


async def _held_departure(person: str, role: str, place: str, region: str | None, left_at: float,
                          wait: float | None = None, hold_id: str | None = None) -> bool:
    """A departure's effects after places.JITTER_S, unless they arrived back at that place meanwhile. → ran. With a
    hold_id, only whoever claims the stored hold acts (the live task, or the startup replay after a restart)."""
    import places
    await asyncio.sleep(places.JITTER_S if wait is None else wait)
    if hold_id and not await asyncio.to_thread(places.take_held, hold_id):
        return False
    if not await asyncio.to_thread(places.still_away, person, place, left_at):
        print(f"[places] {person} left {place} and came back within {places.JITTER_S}s: jitter, nothing run",
              file=sys.stderr)
        return False
    await _place_effects(person, role, "left", place, region)
    return True


_place_tasks: set = set()


async def _resume_held_departures(now: float | None = None) -> dict[str, int]:
    """Departures a restart cut off mid-window: each waits out what's left and then runs or is cancelled as usual;
    one whose window ended over places.HELD_STALE_S ago is dropped (late effects would be wrong). → counts."""
    import places
    now = time.time() if now is None else now
    out = {"resumed": 0, "stale": 0}
    for h in await asyncio.to_thread(places.held):
        if now - h["due_at"] > places.HELD_STALE_S:
            await asyncio.to_thread(places.take_held, h["id"])
            out["stale"] += 1
            continue
        _start_held(h["person"], h["role"], h["place"], h["region"], h["left_at"], max(0.0, h["due_at"] - now), h["id"])
        out["resumed"] += 1
    return out


def _place_todos_card(person: str, role: str, place: str) -> None:
    """#284: arriving somewhere, the person's open todos for that place (their own and the household's) as one card,
    to them only. Updated in place on each arrival (one card per person and place); nothing when none wait."""
    import proactive
    import todos
    try:
        waiting = todos.waiting_at(person, role, place)
    except Exception:  # noqa: BLE001
        return
    if not waiting:
        return
    n = len(waiting)
    lines = "\n".join(f"• {t['title']}" + (" (household)" if t.get("for") == todos.HOUSEHOLD else "") for t in waiting[:8])
    proactive.add_card(f"At {place}: {n} thing{'' if n == 1 else 's'} waiting", lines, kind="info", urgency=1,
                       source="places", to=[person], key="place-todos-" + hashlib.sha1(place.lower().encode()).hexdigest()[:12],   # no place name to Apple (10-07)
                       expires_in=6 * 3600,
                       action_prompt=f"What's on my list for {place}?")


async def _place_actions(person: str, role: str, event: str, place: str, region: str | None = None) -> None:
    """Scheduled actions waiting for this arrival or departure (scheduled.on_place_event), each once."""
    import scheduled
    try:
        for x in await asyncio.to_thread(scheduled.on_place_event, person, role, event, place,
                                         device=scheduled.mac_hook, region=region):
            print(f"[scheduled] {x['id']} {x['state']} on {event} {place}: {x['result'][:120]}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduled] place run failed: {exc}", file=sys.stderr)


@app.post("/v1/devices/push")
async def device_push_register(request: Request):
    """The iPhone's APNs token: {token, environment: development|production, bundle[, card_alerts]}. 204;
    replaces this device's token. `card_alerts` false (jellybean#786 row 1: "Important cards" off) mutes titan's card
    banners; the token stays for the silent pushes (sync, clear, the #776 inbox wake). Missing: true. Only a paired
    device can register (push.py)."""
    import push
    from fastapi.responses import Response
    ident = _identity(request)
    if not ident.device:
        return JSONResponse({"error": "only a paired device registers for push"}, status_code=400)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    try:
        await asyncio.to_thread(push.register, ident.device["id"], body.get("token"), body.get("environment"),
                                body.get("bundle"), card_alerts=body.get("card_alerts"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return Response(status_code=204)


@app.post("/v1/devices/self/activities")
async def device_activity_register(request: Request):
    """A Live Activity's own push token (#725, jellybean#350): {kind: task|trip|agent, ref, token, environment[, bundle]};
    or the device's push-to-start token, once: {kind: "task-start"|"agent-start", token, environment[, bundle]} (ref
    ignored). A trip's may carry {trip: {lead, minutes}} (jellybean#753): what titan needs to move it when its event
    moves; kept with the token, gone with it.
    the phone sends it again when iOS rotates it, and token null when the activity ends. 204. A person's own
    device only: never a shared household device or the master token."""
    import push
    from fastapi.responses import Response
    ident = _identity(request)
    if not ident.device or ident.role == "household":
        return JSONResponse({"error": "only a person's own paired device has Live Activities"}, status_code=400)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    try:
        await asyncio.to_thread(push.register_activity, ident.device["id"], ident.person_id, ident.role,
                                body.get("kind"), body.get("ref"), body.get("token"), body.get("environment"),
                                body.get("bundle"), None, body.get("trip"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return Response(status_code=204)


@app.post("/v1/devices/self/trips")
async def device_trip_plan(request: Request):
    """An upcoming trip as the phone worked it out (jellybean#753 push-to-start): {ref, leave_by (Unix s), lead (s),
    minutes, mode: car|transit|walk}. Times only, never the event's title or place. An hour before leave-by titan starts
    the trip's Live Activity on this device if it shows none yet (its trip-start token). Kept until the event starts;
    sent again when the trip changes. 204. A person's own device only."""
    import push
    from fastapi.responses import Response
    ident = _identity(request)
    if not ident.device or ident.role == "household":
        return JSONResponse({"error": "only a person's own paired device has Live Activities"}, status_code=400)
    body = await _json_body(request)
    try:
        await asyncio.to_thread(push.register_trip_plan, ident.device["id"], ident.person_id, ident.role, body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return Response(status_code=204)


@app.delete("/v1/devices/self/trips/{ref}")
async def device_trip_plan_remove(ref: str, request: Request):
    """The trip's event went, or the person turned Trip on Lock Screen off: titan won't start it (jellybean#753). 204."""
    import push
    from fastapi.responses import Response
    ident = _identity(request)
    if not ident.device or ident.role == "household":
        return JSONResponse({"error": "only a person's own paired device has Live Activities"}, status_code=400)
    await asyncio.to_thread(push.remove_trip_plan, ident.device["id"], ref)
    return Response(status_code=204)


def _contacts_caller(request: Request, deleting: bool = False):
    """The person whose own device sends contacts (#1380), or the refusal: 404 while the house has contacts off
    (CONTACTS_CONNECTOR=0 or the switches file: the route isn't there), 403 for the master token, a shared screen, a
    kid (also refused in ingest; checked here too), or a person who turned contacts off for themselves (#1627). A delete
    is never refused for either (jellybean#1216 review): what titan kept while it was on can always go."""
    import contacts_connector as C
    if not C.enabled() and not deleting:
        return None, JSONResponse({"detail": "Not Found"}, status_code=404)
    ident = _identity(request)
    if not ident.device or ident.role == "household":
        return None, JSONResponse({"error": "contacts come from a person's own paired device"}, status_code=403)
    if ident.role not in ("admin", "adult"):
        return None, JSONResponse({"error": "contacts come from an adult's own Mac"}, status_code=403)
    if not deleting and not C.enabled(ident.person_id):
        return None, JSONResponse({"error": "you've turned off contacts in titan; turn it back on in Settings"},
                                  status_code=403)
    return ident, None


@app.post("/v1/contacts/sync")
async def contacts_sync(request: Request):
    """A person's contacts from their own Mac (#1380; on until they turn it off, #1627): {contacts: [{id, given, family,
    nickname, organization, phones, emails}], relations: [{label, name}] (their own card's Related Names), full: bool
    (the listing is every contact, so one it doesn't list is deleted)}. Phones and emails are keyed on arrival and
    kept nowhere raw. A field titan doesn't keep is refused by name (400), never a value. → {contacts, claims,
    deleted}."""
    import contacts_connector as C
    ident, refused = _contacts_caller(request)
    if refused:
        return refused
    body = await _json_body(request)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected {contacts, relations, full}"}, status_code=400)
    extra = sorted(set(body) - {"contacts", "relations", "full"})
    if extra:
        return JSONResponse({"error": "fields titan doesn't keep: " + ", ".join(extra)}, status_code=400)
    contacts, relations, full = body.get("contacts") or [], body.get("relations") or [], body.get("full", False)
    if not isinstance(contacts, list) or not all(isinstance(c, dict) for c in contacts) \
            or not isinstance(relations, list) or not all(isinstance(r, dict) for r in relations) or not isinstance(full, bool):
        return JSONResponse({"error": "expected {contacts: [...], relations: [...], full: true|false}"}, status_code=400)
    try:
        n = await asyncio.to_thread(C.ingest, ident.person_id, ident.role, ident.device["id"], contacts, relations,
                                    full=full)
    except C.ContactError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(n)


@app.delete("/v1/contacts/{native_id}")
async def contacts_delete(native_id: str, request: Request):
    """A contact deleted on the Mac, or the person turned contacts off (#1380): everything titan kept of it goes
    (contacts_connector.delete). → {removed}: whether titan had it. Works with contacts off too (the house's or theirs), so what was
    kept while it was on can always be deleted (the Mac keeps a delete it couldn't pay until titan answers)."""
    import contacts_connector as C
    import references
    ident, refused = _contacts_caller(request, deleting=True)
    if refused:
        return refused
    if not C.enabled(ident.person_id) and native_id not in references.native_ids(ident.person_id, ident.device["id"], C.KIND):
        return JSONResponse({"removed": False})   # off, and nothing kept of it: touch no store
    removed = await asyncio.to_thread(C.delete, ident.person_id, ident.device["id"], native_id)
    return JSONResponse({"removed": bool(removed)})


@app.get("/v1/me/sharing")
async def me_sharing(request: Request) -> JSONResponse:
    """What others see of yours (Settings › Privacy & Sharing): the caller's own notes and world facts counted by
    audience, who sees their place, and their #446 grants. Only ever the caller's own; never titles or text."""
    import me_sharing as ms
    ident = _identity(request)
    people = await asyncio.to_thread(_store().people)
    try:
        return JSONResponse(await asyncio.to_thread(ms.summary, ident.person_id, ident.role, people))
    except ms.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.get("/v1/me/assistant")
async def me_assistant(request: Request) -> JSONResponse:
    """Settings › Assistant (#1076): {asked_for_more_7d, reply_length, persona}, the caller's own."""
    import about_info
    ident = _identity(request)
    if ident.role == "household":
        return JSONResponse({"error": "a shared screen has no assistant settings"}, status_code=403)
    return JSONResponse(await asyncio.to_thread(about_info.assistant, ident.person_id))


@app.get("/v1/about")
async def about_titan(request: Request) -> JSONResponse:
    """Settings › About (#1076): titan's deployed commit and time, the last canary, the caller's own devices' app
    versions, which brain answers chat and that background work is local-only. No hostnames, paths or environment."""
    import about_info
    ident = _identity(request)
    people = await asyncio.to_thread(_store().people)
    return JSONResponse(await asyncio.to_thread(about_info.about, ident.person_id, people))


@app.post("/v1/reports")
async def reports_post(request: Request) -> JSONResponse:
    """About › Report a problem (#1076): {text, device?, app_version?, log_tail?}, cleaned; any other field is refused;
    stored per person, at most 20 a day (429). The reply says who reads it (`goes_to`: the admin's first name). Never filed
    automatically."""
    import about_info
    ident = _identity(request)
    if ident.role == "household":
        return JSONResponse({"error": "reports are from a person"}, status_code=403)
    try:
        people = await asyncio.to_thread(_store().people)
        admin = next((str(p.get("name") or "").split(" ")[0] for p in people if p.get("role") == "admin"), None)
        return JSONResponse(await asyncio.to_thread(about_info.report, ident.person_id, await _json_body(request), None, admin),
                            status_code=201)
    except about_info.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.get("/v1/reports")
async def reports_list(request: Request, limit: int = 50) -> JSONResponse:
    """The reports queue: an admin sees every one, anyone else their own (#1076)."""
    import about_info
    ident = _identity(request)
    return JSONResponse({"reports": await asyncio.to_thread(about_info.reports, ident.role, ident.person_id, None, limit)})
def _my_items(ident: "_hh.Identity") -> dict[str, Any]:
    """Settings › You (#1079): the caller's own chats, memories and KB notes, and the titles of others' items shared with
    them (titles only). Readers for counts and the export; never another person's rows."""
    import conversations
    import kb_privacy
    pid = ident.person_id
    mem = _get_memory() if _MEMORY_AVAILABLE else None
    conv = _hh.conv_id_for(pid)
    groups = conversations._groups(conversations._rows(mem._db, conv)) if mem else []
    srcs = _lib.kb_sources()
    acl = kb_privacy.acl_rows(str(x.get("source_id")) for x in srcs)

    def chats():
        """Every conversation and every message, one at a time (no list or message cap: everything means everything)."""
        for gid, rs in (groups if mem else []):
            yield ({"id": gid, "title": conversations._title(rs), "started_at": conversations._iso(rs[0]["ts"]),
                    "last_at": conversations._iso(rs[-1]["ts"]), "count": len(rs)},
                   [{"role": r["role"], "text": r["content"], "at": conversations._iso(r["ts"])} for r in rs])

    def memories() -> list:
        return mem.list_facts(limit=10**9, person_id=pid) if mem else []

    def forgotten() -> list:
        """The person's forgotten facts (restorable until purged): part of what titan still holds."""
        if not mem:
            return []
        return [{"id": r[0], "text": r[1], "forgotten_at": r[2]} for r in mem._db.execute(
            "SELECT id, text, forgotten_at FROM forgotten_facts WHERE person_id=? ORDER BY forgotten_at", (pid,))]

    def dev_notes() -> list:
        """The person's own dev notes (#1605): wishes about titan, theirs like everything else here."""
        return mem.dev_notes(person_id=pid, limit=10**9) if mem else []

    def mine(sid: str, md: Any) -> bool:
        """The caller's own note, by kb_privacy's rule (a row with no owner at all is titan's own, e.g. the runbook,
        never a person's; an item with no row and no metadata is the legacy owner's, as kb_privacy says)."""
        return str(kb_privacy.acl_of(md, sid, acl)[0] or "").lower() == pid.lower()

    def notes() -> list:
        out = []
        for x in srcs:
            sid = str(x.get("source_id"))
            if mine(sid, x.get("metadata")):
                out.append({"id": sid, "title": x.get("title") or "", "text": kb_privacy.source_text(_lib._runtime_request, sid) or ""})
        return out

    def shared_titles() -> list:
        return [str(x.get("title") or "") for x in kb_privacy.visible_sources(_lib._runtime_request, srcs, pid, ident.role)
                if not mine(str(x.get("source_id")), x.get("metadata"))]
    return {"chats": chats, "chat_count": len(groups), "memories": memories, "forgotten": forgotten, "dev_notes": dev_notes,
            "fact_count": mem.total_fact_count(pid) if mem else 0, "notes": notes, "shared_titles": shared_titles,
            # The same notes the export writes: ones still in the KB (an access row can outlive its note, #1082 check)
            "note_count": sum(1 for x in srcs if mine(str(x.get("source_id")), x.get("metadata")))}


@app.get("/v1/me/data")
async def me_data_counts(request: Request) -> JSONResponse:
    """Settings › You (#1079): {memories, chats, notes, standing_shares}, the caller's own. A shared screen: 403."""
    import me_data
    import me_sharing
    ident = _identity(request)
    if ident.role == "household":
        return JSONResponse({"error": "a shared screen has nothing of its own"}, status_code=403)
    people = await asyncio.to_thread(_store().people)
    items = await asyncio.to_thread(_my_items, ident)
    sharing = await asyncio.to_thread(me_sharing.summary, ident.person_id, ident.role, people)
    try:
        return JSONResponse(me_data.counts(ident.person_id, ident.role, facts=items["fact_count"],
                                           chats=items["chat_count"], sharing=sharing, notes=items["note_count"]))
    except me_data.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.post("/v1/me/export")
async def me_export_start(request: Request) -> JSONResponse:
    """Settings › You › Download everything (#1079): build one archive of the caller's own chats, memories and notes
    (JSON + Markdown), kept 24 h. Kids and shared screens: 403."""
    import me_data
    ident = _identity(request)
    try:
        me_data.state(ident.person_id, ident.role)   # refuses kids and screens before any work
    except me_data.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    items = await asyncio.to_thread(_my_items, ident)
    got = await asyncio.to_thread(lambda: me_data.build(ident.person_id, ident.role, chats=items["chats"],
                                                        memories=items["memories"], notes=items["notes"],
                                                        shared_titles=items["shared_titles"],
                                                        forgotten=items["forgotten"], dev_notes=items["dev_notes"]))
    return JSONResponse(got, status_code=201 if got.get("state") == "ready" else 202)


def _delete_parts() -> Any:
    import me_delete
    return me_delete.Parts(_get_memory(), kb_sources=_lib.kb_sources, call=_lib._runtime_request, kb_index=_lib._kb_index)


@app.get("/v1/me/delete")
async def me_delete_state(request: Request) -> JSONResponse:
    """Settings › You › Delete my data (#1080): {state: none|pending, purge_at, counts} and, while nothing is pending,
    what a delete would move ({would: {...}}). Kids: 403 ("ask a parent"); a shared screen: 403."""
    import me_delete
    ident = _identity(request)
    try:
        st = await asyncio.to_thread(me_delete.state, ident.person_id, ident.role)
    except me_delete.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    if st["state"] == "none":
        items = await asyncio.to_thread(_my_items, ident)
        st["would"] = {"chats": items["chat_count"], "memories": items["fact_count"], "notes": items["note_count"]}
    return JSONResponse(st)


@app.post("/v1/me/delete")
async def me_delete_start(request: Request) -> JSONResponse:
    """{confirm: "delete"}: move the caller's chats, memories and KB notes (and their thread titles, Claude
    conversations, lineage and export) into a 7-day tombstone. Recall and retrieval stop seeing them at once;
    devices stay paired. Cancel with POST /v1/me/delete/cancel; the nightly purge deletes it after 7 days."""
    import me_delete
    ident = _identity(request)
    body = await _json_body(request)
    try:
        st = await asyncio.to_thread(lambda: me_delete.delete(
            ident.person_id, ident.role, _delete_parts(), conv=_hh.conv_id_for(ident.person_id),
            confirm=(body or {}).get("confirm") if isinstance(body, dict) else None))
    except me_delete.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    return JSONResponse(st, status_code=202)


@app.post("/v1/me/delete/cancel")
async def me_delete_cancel(request: Request) -> JSONResponse:
    """Undo a pending delete within its 7 days: everything back exactly as it was."""
    import me_delete
    ident = _identity(request)
    try:
        return JSONResponse(await asyncio.to_thread(me_delete.cancel, ident.person_id, ident.role, _delete_parts()))
    except me_delete.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.get("/v1/me/export")
async def me_export_get(request: Request, download: int = 0) -> Response:
    """The caller's archive: {state: none|building|ready, file, bytes, expires_at}; ?download=1 → the zip itself."""
    import me_data
    ident = _identity(request)
    try:
        st = await asyncio.to_thread(me_data.state, ident.person_id, ident.role)
    except me_data.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    if download:
        f = me_data.path_of(ident.person_id, ident.role) if st.get("state") == "ready" else None
        if f is None:
            return JSONResponse({"error": "no archive ready"}, status_code=404)
        return Response(content=f.read_bytes(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="titan-{f.name}"'})
    return JSONResponse(st)


@app.get("/v1/sharing/grants")
async def sharing_grants(request: Request) -> JSONResponse:
    """The caller's own "Always for <name>" answers (#446 step 3; iPhone Settings › Sharing): {grants: [{id,
    asker_first_name, kind}]}. Only grants they gave; a shared household device has none."""
    import item_open
    ident = _identity(request)
    people = {str(p["id"]).lower(): p for p in await asyncio.to_thread(_store().people)}
    rows = await asyncio.to_thread(item_open.grants_of, ident.person_id)
    return JSONResponse({"grants": [{"id": item_open.grant_id(ident.person_id, g["asker"], g["kind"]),
                                     "asker_first_name": str((people.get(g["asker"]) or {}).get("name") or "someone").split(" ")[0],
                                     "kind": g["kind"]} for g in rows]})


@app.post("/v1/sharing/grants/stop")
async def sharing_grants_stop(request: Request) -> JSONResponse:
    """{asker}: take back every "Always" the caller gave this person (#1060) → {stopped, token}; the token undoes
    it (POST /v1/surfaces/undo)."""
    import surfaces
    ident = _identity(request)
    body = await _json_body(request)
    asker = str((body or {}).get("asker") or "").strip().lower() if isinstance(body, dict) else ""
    if not asker:
        return JSONResponse({"error": "asker is required"}, status_code=400)
    return JSONResponse(await asyncio.to_thread(surfaces.stop_all, ident.person_id, asker))


@app.get("/v1/surfaces/household-facts")
async def surface_household_facts(request: Request, q: str | None = None) -> JSONResponse:
    """Settings › Household as a ResolvedSurface (#1060, docs/surfaces.md jb-1): the household bubble's facts this
    caller sees, a needs-a-look group (stale or disputed), then one group per entity; confirm per fact and per entity.
    A Places group (#1510) lists the places this caller may see, with add, edit and remove (one undo each); a kid sees
    them with every operation unavailable."""
    import surfaces
    import world as _world
    ident = _identity(request)
    try:
        import places_seed
        try:
            rows = await asyncio.to_thread(places_seed.listing, ident.person_id, ident.role)
        except Exception:  # noqa: BLE001 - no registry here: the facts as before #1510, without a Places group
            rows = None
        out = await asyncio.to_thread(surfaces.household_facts, _world.get(), ident.person_id, ident.role, time.time(),
                                      q=q, places=rows)
    except surfaces.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    return JSONResponse(out)


@app.post("/v1/surfaces/household-facts/confirm")
async def surface_household_confirm(request: Request) -> JSONResponse:
    """{entity}: restate every stale fact of one household entity as the caller's own (an adult's) → {confirmed,
    token}; the token undoes exactly that (POST /v1/surfaces/undo)."""
    import surfaces
    import world as _world
    ident = _identity(request)
    body = await _json_body(request)
    cid = str((body or {}).get("entity") or "").strip() if isinstance(body, dict) else ""
    if not cid:
        return JSONResponse({"error": "entity is required"}, status_code=400)
    try:
        return JSONResponse(await asyncio.to_thread(surfaces.confirm_all, _world.get(), ident.person_id, ident.role, cid))
    except surfaces.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.get("/v1/surfaces/always-shared")
async def surface_always_shared(request: Request) -> JSONResponse:
    """Privacy › Always shared as a ResolvedSurface (#1060): the caller's own "Always for <name>" answers, a
    needs-a-look group (unused for 90 days), then one group per asker; stop-all per asker with one undo."""
    import surfaces
    ident = _identity(request)
    people = {str(p["id"]).lower(): p for p in await asyncio.to_thread(_store().people)}
    try:
        return JSONResponse(await asyncio.to_thread(surfaces.always_shared, ident.person_id, ident.role, people,
                                                    time.time()))
    except surfaces.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


def _rooms_synced() -> list[dict[str, Any]]:
    """The household's rooms (#1511), after taking in Home Assistant's areas (read only; none when HA can't be reached)
    and the rooms screens already name."""
    import home_assistant as ha
    import rooms as _rooms
    try:
        areas = sorted(set(ha._areas(ha._http).values()) - {""})
    except Exception:  # noqa: BLE001 - no HA: the rooms we have
        areas = None
    try:
        return _rooms.sync(areas, _store().device_rooms())
    except Exception:  # noqa: BLE001 - the store can't be read or written: HA's areas as they are, as before #1511
        return [{"id": None, "name": a, "ha_area": a} for a in areas or []]


@app.get("/v1/surfaces/rooms")
async def surface_rooms(request: Request) -> JSONResponse:
    """Settings › Rooms as a ResolvedSurface (#1511): the household's rooms (Home Assistant areas, read only, and rooms
    added here), how many household screens each has, add / rename / remove (remove and rename with one undo). Kids see
    them with the operations unavailable; a shared screen gets 403."""
    import surfaces
    ident = _identity(request)
    try:
        room_list = await asyncio.to_thread(_rooms_synced)
        if any(r.get("id") is None for r in room_list):
            return JSONResponse({"error": "the rooms can't be changed right now"}, status_code=503)
        screens = await asyncio.to_thread(surfaces._screens, _store())
        return JSONResponse(surfaces.rooms_surface(ident.person_id, ident.role, room_list, screens, time.time()))
    except surfaces.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


async def _room_change(request: Request, verb: str, room_id: str | None = None) -> JSONResponse:
    import surfaces
    ident = _identity(request)
    body = await _json_body(request) if verb != "remove" else {}
    name = body.get("name") if isinstance(body, dict) else None
    try:
        if verb == "add":
            await asyncio.to_thread(_rooms_synced)   # an area HA has is a room before a same-named one is added
        out = await asyncio.to_thread(surfaces.room_change, ident.person_id, ident.role, _store(), verb,
                                      room_id=room_id, name=name)
        return JSONResponse(out, status_code=201 if verb == "add" else 200)
    except surfaces.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.post("/v1/rooms")
async def room_add(request: Request) -> JSONResponse:
    """{name} → 201 {room}: a room the household names (#1511); 409 when a room has that name. Adults only."""
    return await _room_change(request, "add")


@app.patch("/v1/rooms/{room_id}")
async def room_rename(room_id: str, request: Request) -> JSONResponse:
    """{name} → {room, screens, token}: the screens in it follow; a Home Assistant area keeps its area (titan's name
    only). The token undoes it. Adults only."""
    return await _room_change(request, "rename", room_id)


@app.delete("/v1/rooms/{room_id}")
async def room_remove(room_id: str, request: Request) -> JSONResponse:
    """→ {room, screens, token}: the room goes, and the household screens in it lose their room (named in `screens`);
    a Home Assistant area isn't changed and doesn't come back by itself. The token undoes it. Adults only."""
    return await _room_change(request, "remove", room_id)


def _review_context(ident, batch_id: str | None):
    """(batch, viewer in it) for the review routes (#1507). No batch asked for: (None, None), the empty review. An unknown
    batch (the benchmark is served only with SCAN_REVIEW_BENCH=1): None, None with `known` False."""
    import scan_review as _sr
    if not batch_id:
        return None, None, True
    batch = _sr.get_batch(str(batch_id))
    if batch is None:
        return None, None, False
    return batch, _sr.viewer_in(batch, ident.person.get("name") or ""), True


@app.get("/v1/surfaces/review")
async def surface_review(request: Request, batch: str | None = None) -> JSONResponse:
    """Bulk review of scanned mail as a ResolvedSurface (#1507, review@1): titan's proposed connections for the caller's own
    scans, grouped by kind, each group with approve in one tap and each item with leave-out; what filed itself on a hard key
    (account plus amount) is listed with its undo. A kid sees their own with every operation unavailable; a shared screen gets
    403. No batch → the empty review; the benchmark batch needs SCAN_REVIEW_BENCH=1 (no live scans are read yet)."""
    import scan_review as _sr
    import surfaces
    ident = _identity(request)
    b, viewer, known = _review_context(ident, batch)
    if not known:
        return JSONResponse({"error": "no such batch"}, status_code=404)
    try:
        await asyncio.to_thread(_sr.auto_file, b, viewer, ident.role, ident.person_id, time.time())
        return JSONResponse(await asyncio.to_thread(_sr.review, b, viewer, ident.role, ident.person_id, time.time()))
    except surfaces.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.post("/v1/surfaces/review/approve")
async def surface_review_approve(request: Request) -> JSONResponse:
    """{batch, group[, items]}: file every pending proposal in one group of the review in place (a link, cycle, bundle
    membership, event plan or archive plan; nothing deleted, no body copied) → {filed, token}; `items` files just those, also
    ones left out. The token undoes exactly that (POST /v1/surfaces/undo). Adults only; 404 for a group that is not theirs."""
    import scan_review as _sr
    import surfaces
    ident = _identity(request)
    body = await _json_body(request)
    body = body if isinstance(body, dict) else {}
    items = body.get("items")
    if items is not None and not (isinstance(items, list) and all(isinstance(x, str) for x in items)):
        return JSONResponse({"error": "items must be a list of ids"}, status_code=400)
    b, viewer, known = _review_context(ident, str(body.get("batch") or ""))
    if not known:
        return JSONResponse({"error": "no such batch"}, status_code=404)
    try:
        return JSONResponse(await asyncio.to_thread(_sr.approve, b, viewer, ident.role, ident.person_id,
                                                    str(body.get("group") or ""), items, time.time()))
    except surfaces.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.post("/v1/surfaces/review/exclude")
async def surface_review_exclude(request: Request) -> JSONResponse:
    """{batch, group, item}: leave one document out of its group (the exception) → {excluded, token}; it waits under "Left for
    you" and the token undoes it (POST /v1/surfaces/undo). Adults only."""
    import scan_review as _sr
    import surfaces
    ident = _identity(request)
    body = await _json_body(request)
    body = body if isinstance(body, dict) else {}
    b, viewer, known = _review_context(ident, str(body.get("batch") or ""))
    if not known:
        return JSONResponse({"error": "no such batch"}, status_code=404)
    try:
        return JSONResponse(await asyncio.to_thread(_sr.exclude, b, viewer, ident.role, ident.person_id,
                                                    str(body.get("group") or ""), str(body.get("item") or ""), time.time()))
    except surfaces.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.post("/v1/surfaces/undo")
async def surface_undo(request: Request) -> JSONResponse:
    """{token}: puts back exactly what one batch did (#1060), only the caller's own token, within 7 days; else 404."""
    import surfaces
    ident = _identity(request)
    body = await _json_body(request)
    token = str((body or {}).get("token") or "") if isinstance(body, dict) else ""
    try:
        return JSONResponse(await asyncio.to_thread(surfaces.undo, ident.person_id, token))
    except surfaces.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.delete("/v1/sharing/grants/{grant_id}")
async def sharing_grant_revoke(grant_id: str, request: Request):
    """The owner takes an "Always" back: the next read asks again. 204; 404 for an id that isn't one of theirs."""
    import item_open
    from fastapi.responses import Response
    ident = _identity(request)
    if not await asyncio.to_thread(item_open.revoke_id, ident.person_id, grant_id):
        return JSONResponse({"error": "no such grant"}, status_code=404)
    return Response(status_code=204)


@app.delete("/v1/devices/push")
async def device_push_unregister(request: Request):
    """This phone's token goes: no pushes of any kind. An older app's "Important cards" off; a current one keeps the
    token and posts card_alerts false instead (jellybean#786 row 1), so the silent pushes still come."""
    import push
    from fastapi.responses import Response
    ident = _identity(request)
    if ident.device:
        await asyncio.to_thread(push.unregister, ident.device["id"])
    return Response(status_code=204)


def _device_self_id(request: Request) -> str:
    """The calling device's own id; 401 for a token with no device row (the master token)."""
    dev = (_identity(request).device or {}).get("id")
    if not dev:
        raise HTTPException(status_code=401, detail="a device token is required")
    return dev


@app.get("/v1/devices/self/calendars")
async def device_self_calendars(request: Request) -> JSONResponse:
    """This device's calendar claims (jellybean#1037, titan-node#1234): {calendars: [{id, title, person}]}. 403 on a shared
    household screen."""
    ident = _identity(request)
    dev = _device_self_id(request)
    if ident.role == "household":
        return JSONResponse({"error": "a shared household screen claims no calendars"}, status_code=403)
    return JSONResponse({"calendars": await asyncio.to_thread(_store().device_calendars, dev)})


@app.put("/v1/devices/self/calendars")
async def device_self_calendars_put(request: Request) -> JSONResponse:
    """Replace this device's calendar claims (jellybean#1037): {calendars: [{id, title, person}]} → {calendars: the accepted,
    refused: [{id, why}]}. EventKit's calendar id is device-local, so a claim counts only for reads this device runs.
    Anyone but a shared screen may name any household member; a claim always narrows who sees the calendar and widens
    only for an adult's or the admin's own or their kids' (decided at read time, #1268). 403 on a shared household
    screen, 413 over 100, 401 without a device token."""
    dev = _device_self_id(request)
    body = await _json_body(request)
    if not isinstance(body, dict) or not isinstance(body.get("calendars"), list):
        return JSONResponse({"error": "expected {calendars: [{id, title, person}]}"}, status_code=400)
    try:
        out = await asyncio.to_thread(_store().set_device_calendars, dev, body["calendars"])
    except _hh.PairingError as exc:
        return _pairing_error(exc)
    print(f"[calendars] device claims set: {len(out['calendars'])} kept, {len(out['refused'])} refused", file=sys.stderr)
    return JSONResponse(out)


@app.patch("/v1/devices/self")
async def device_self(request: Request) -> JSONResponse:
    """A device's own settings (jellybean#424): {room} names the room it's in ("Living room"; null clears); {holds}:
    the kinds of referenced item this iPhone answers item_body for (#776: ["scan"]; [] clears); {motion}:
    "driving" while this phone is in a car, null when it stops (jellybean#310: its important-but-not-urgent card pushes
    wait until then; only "driving" or not is kept, for at most push.DRIVING_TTL_S). Only the calling device's own row,
    whatever its role; the master token has no device row (400)."""
    import push
    ident = _identity(request)
    dev = (ident.device or {}).get("id")
    if not dev:
        return JSONResponse({"error": "this token has no device row"}, status_code=400)
    body = await _json_body(request)
    if not isinstance(body, dict) or not ({"room", "motion", "holds"} & set(body)):
        return JSONResponse({"error": "expected {room}, {motion} or {holds}"}, status_code=400)
    out: dict[str, Any] = {"id": dev}
    if "motion" in body:
        if body["motion"] not in ("driving", None):
            return JSONResponse({"error": "motion is \"driving\" or null"}, status_code=400)
        await asyncio.to_thread(push.set_driving, dev, body["motion"] == "driving")
        out["motion"] = body["motion"]
    if "room" in body:
        try:
            out["room"] = await asyncio.to_thread(_store().set_device_room, dev, body["room"])
        except _hh.PairingError as exc:
            return _pairing_error(exc)
    if "holds" in body:   # #776: the kinds this iPhone answers item_body for ("scan"); [] clears
        try:
            out["holds"] = await asyncio.to_thread(_store().set_device_holds, dev, body["holds"])
        except _hh.PairingError as exc:
            return _pairing_error(exc)
    return JSONResponse(out)


@app.delete("/v1/devices/{device_id}")
async def device_revoke(device_id: str, request: Request) -> JSONResponse:
    """(admin, or the device itself: "Leave this household"; or a parent, for their kid's Watch: titan-node#755)
    Revoked at once: its token → 401."""
    ident = _identity(request)
    if not await asyncio.to_thread(_store().may_revoke, ident, device_id):
        raise HTTPException(status_code=403, detail=_hh.REFUSAL)
    owner = next((p["id"] for p in await asyncio.to_thread(_store().people)
                  for d in p.get("devices", []) if d["id"] == device_id), None)
    if not await asyncio.to_thread(_store().revoke_device, device_id):
        raise HTTPException(status_code=404, detail="no such device")
    import push
    await asyncio.to_thread(push.unregister, device_id)   # its push token goes with it
    await asyncio.to_thread(push.forget_activities, device_id)   # and its Live Activities' (#725)
    import home_security
    await asyncio.to_thread(home_security.end_device, device_id)   # its live camera views end now, freeing their slots (#1246)
    if owner:
        # Their place history and continuity: all of it when this was their last device (leaving the
        # household), else only what this device sent (#421: an old iPad removed mustn't wipe the phone's).
        import places
        left = [d for p in await asyncio.to_thread(_store().people) if p["id"] == owner
                for d in p.get("devices", []) if d["id"] != device_id and not d.get("revoked")]
        if left:
            await asyncio.to_thread(places.forget_device, owner, device_id)
        else:
            await asyncio.to_thread(places.forget_person, owner)
        try:
            import continuity
            if left:
                await asyncio.to_thread(continuity.forget_device, owner, device_id)
            else:
                await asyncio.to_thread(continuity.forget_person, owner)
        except Exception:  # noqa: BLE001 - the device is revoked whatever this does
            pass
    return JSONResponse({"id": device_id, "revoked": True})


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

def _kb_cmd(request: Request | None = None) -> list[str]:
    """kb_query.py, acting as the caller: every KB route sees and changes only what that person may
    (kb_privacy.py). A household device passes its role alone."""
    cmd = ["python3", str(_HERE / "kb_query.py")]
    if request is not None:
        ident = _identity(request)
        cmd += ["--person", ident.person_id, "--role", ident.role]
    return cmd


def _kb_run(cmd: list[str], stdin: str = "", timeout: float = 15) -> dict:
    """Run a kb_query/kb_update command; its JSON, or an HTTPException with the status it gave."""
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    try:
        out = json.loads((proc.stdout or "").strip() or "{}")
    except ValueError:
        out = {"error": (proc.stdout or proc.stderr or "failed")[:300]}
    if proc.returncode != 0 or (isinstance(out, dict) and out.get("error")):
        raise HTTPException(status_code=int(out.get("status") or (400 if proc.returncode == 1 else 500)),
                            detail=str(out.get("error") or "failed")[:300])
    return out


@app.get("/v1/kb")
async def list_kb(request: Request) -> JSONResponse:
    data = await asyncio.to_thread(_kb_run, _kb_cmd(request) + ["list"])
    # kb_query.py returns {"items": [...]}; this used to nest it one level too deep.
    entries = data.get("items", []) if isinstance(data, dict) else data
    if _identity(request).role == _hh.ENGINEERING:
        entries = _engineering_read("/v1/kb", "kb", entries or [])
    return JSONResponse({"entries": entries})


@app.get("/v1/kb/{entry_id}")
async def get_kb(entry_id: str, request: Request) -> JSONResponse:
    """One entry; 200 {} when it's gone or the caller can't see it (not 404: nobody learns whether it exists)."""
    data = await asyncio.to_thread(_kb_run, _kb_cmd(request) + ["get", entry_id])
    item = data.get("item") or {}
    if item and _identity(request).role == _hh.ENGINEERING:
        item = (_engineering_read("/v1/kb/{id}", "kb", [item]) or [{}])[0]
    return JSONResponse(item)


@app.post("/v1/kb")
async def create_kb(request: Request) -> JSONResponse:
    body = await request.json()
    title = str(body.get("title") or "").strip()
    text  = str(body.get("body")  or "").strip()
    domain = str(body.get("domain") or "general").strip()
    if not title or not text:
        raise HTTPException(status_code=400, detail="title and body are required")
    # kb_query.py reads {domain, title, content}; "body" here meant create always failed.
    out = await asyncio.to_thread(_kb_run, _kb_cmd(request) + ["create"],
                                  json.dumps({"title": title, "content": text, "domain": domain,
                                              "share": body.get("share")}))
    _lib.feed_sources_changed()   # the new note is in the next feed
    return JSONResponse(out, status_code=201)


@app.delete("/v1/kb/{entry_id}")
async def delete_kb(entry_id: str, request: Request) -> JSONResponse:
    await asyncio.to_thread(_kb_run, _kb_cmd(request) + ["delete", entry_id])
    if entry_id.startswith("cx-"):       # a synced item: it stays forgotten (its device never sends it back)
        import connectors_runner as cx
        stone = await asyncio.to_thread(cx.tombstone_item, entry_id)
        return JSONResponse({"deleted": entry_id, "tombstoned": stone is not None})
    return JSONResponse({"deleted": entry_id})


@app.post("/v1/kb/{entry_id}/share")
async def share_kb(entry_id: str, request: Request) -> JSONResponse:
    """{to: "Robin" | "adults" | "household"} → {audience, visible_to}: one step to make a note
    available (the owner or the admin; audience.widen, so sharing never narrows)."""
    body = await _json_body(request)
    out = await asyncio.to_thread(_kb_run, _kb_cmd(request) + ["share", entry_id],
                                  json.dumps({"to": (body or {}).get("to")}))
    return JSONResponse(out)


@app.post("/v1/kb/{entry_id}/unshare")
async def unshare_kb(entry_id: str, request: Request) -> JSONResponse:
    """{from?: "Robin"}: take someone off a note; no `from`: private again."""
    body = await _json_body(request)
    out = await asyncio.to_thread(_kb_run, _kb_cmd(request) + ["unshare", entry_id],
                                  json.dumps({"from": (body or {}).get("from")}))
    return JSONResponse(out)


@app.put("/v1/kb/{entry_id}")
async def update_kb(entry_id: str, request: Request) -> JSONResponse:
    """Edit a note's title / body / category (kb_update.py, run as the caller with --person/--role;
    it refuses household.yaml notes, whose source of truth is the file). The owner or the admin."""
    body = await request.json()
    payload = {k: body[k] for k in ("title", "body", "domain") if body.get(k) is not None}
    if not payload:
        raise HTTPException(status_code=400, detail="nothing to update (title, body or domain)")
    ident = _identity(request)
    proc = await asyncio.to_thread(
        subprocess.run, [sys.executable, str(_HERE / "kb_update.py"), "--person", ident.person_id,
                         "--role", ident.role, entry_id],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        try:
            status = int(json.loads(out).get("status") or 400)
        except (ValueError, AttributeError):
            status = 400
        raise HTTPException(status_code=status, detail=(out or proc.stderr or "update failed")[:300])
    try:
        return JSONResponse(json.loads(out))
    except ValueError:
        return JSONResponse({"updated": entry_id, "detail": out[:300]})


# ---------------------------------------------------------------------------
# Tasks and tools (the Jellybean plan card and direct tool runs; same output as the CLI)
# ---------------------------------------------------------------------------

def _lib_cli(action: str, *args: str, timeout: float) -> str:
    proc = subprocess.run([sys.executable, str(_HERE / "agent_runtime_lib.py"), action, *args],
                          capture_output=True, text=True, timeout=timeout, cwd=str(_HERE.parent))
    return (proc.stdout or "").strip()


def _item_readers(ident: "_hh.Identity", now_dt: Any,
                  parts: dict[str, int] | None = None) -> dict[str, Callable[[], list]]:
    """#1084: each store's own per-viewer reader, bound to this caller (access projection first). A shared screen
    reads as nobody in particular: only what the household sees. `parts` (#1552): kb_visible's own ms, when it ran,
    for the latency log."""
    import items as it
    pid = None if ident.role == "household" else ident.person_id
    role = ident.role

    def todos_r() -> list:
        import todos
        return [it.from_todo(r) for r in todos.list_for(todos.lists_visible(pid, role))]

    def scheduled_r() -> list:
        import scheduled
        return [it.from_scheduled(r) for r in scheduled.list_for(pid, role)]

    def routines_r() -> list:
        import routines
        out = []
        # as GET /v1/routines reads them (_routine_viewer): a shared device as its own person
        for r in routines.list_all(person=ident.person_id, role=role):
            nxt = routines.next_run(r, now_dt)
            out.append(it.from_routine(r, nxt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if nxt else None))
        return out

    def tasks_r() -> list:
        # in-process (#1181): the same tasks_for the CLI ran, without a Python subprocess on every request (~250 ms,
        # every role, even with no tasks: the common latency miss); a shared screen has no tasks at all
        if not pid:
            return []
        return [it.from_task(t) for t in _lib.tasks_for(pid, role).get("tasks", [])]

    def references_r() -> list:
        import references
        return [it.from_reference(r) for r in references.visible(pid, role)]

    def chats_r() -> list:
        if not pid or not _MEMORY_AVAILABLE:
            return []       # a shared screen has no history
        import conversations
        import spaces
        tags = spaces.tags_for(pid)
        return [it.from_chat(c, pid, tags.get(c["id"]))
                for c in conversations.list_for(_get_memory()._db, _hh.conv_id_for(pid), 200)]

    # #1181: notes and mail read the same viewer-visible KB sources; worked out once per request, shared by both
    # (never across requests, so nothing here is ever stale)
    kb_once: dict[str, Any] = {}
    kb_lock = threading.Lock()

    def kb_visible() -> list:
        with kb_lock:
            if "vis" not in kb_once:
                import kb_privacy
                t = time.perf_counter()
                kb_once["vis"] = kb_privacy.visible_sources(_lib._runtime_request, _lib.kb_sources(), pid, role)
                if parts is not None:
                    parts["kb_visible"] = round((time.perf_counter() - t) * 1000)
            return kb_once["vis"]

    def notes_r() -> list:
        vis = kb_visible()
        return [it.from_note(x, x.get("owner"), str(x.get("audience") or "private")) for x in vis
                if str(x.get("source_type") or "") == "note"]
    def linked_r() -> list:   # #1085: linker items filed under a project, each by its own audience
        import hooks
        import linker
        import projects
        import threadview
        filed = {i: p for i, p in projects.filings().items() if ":" not in i and not i.startswith("chat-")}
        if not filed:
            return []
        con = hooks.connect()
        try:
            # the cached graph (10-10: a whole hooks scan per request): its items are loaded fresh whenever links.db
            # changed, so who owns each and who may see it is current (linker.scored)
            items_, _edges = linker.scored(con)
        finally:
            con.close()
        visible = threadview._visible_fn(pid, role)
        shown = [i for i in sorted(filed) if i in items_ and visible(items_[i])]
        titles = linker.titles_for(shown)   # only the titles shown (10-10: every KB source, record and mail subject)
        mail = _mail_open([i for i in shown if items_[i].source == "mail"])
        return [it.from_linked(items_[i], titles.get(i), [filed[i]], mail.get(i)) for i in shown]

    def bills_r() -> list:   # #1108: adults' own (and shared) bills, as open_obligations reads them; never a kid's view
        if role not in ("admin", "adult") or not pid:
            return []
        recs = [n for t in ("bill", "receipt") for n in _lib.record_nodes(t)[0] if _lib._node_visible(n, pid, role)]
        facets = {pid: _hh.get_store().facets(pid)} if hasattr(_hh.get_store(), "facets") else {}   # the viewer's own map
        return it.bills_now(_lib._obligation_threads(recs, now_dt.date(), pid, role), now_dt.date(), facets)   # one per account

    def mail_r() -> list:    # #1108: mail triage kept, its owner's alone (whatever its audience says), subject only
        if not pid:
            return []
        vis = kb_visible()
        return [it.from_mail(x, pid) for x in vis if str(x.get("source_type") or "") == "connector:mail"
                and str(x.get("owner") or "").lower() == pid.lower()]

    def events_r() -> list:  # #1108: household calendar events titan reads itself (EventKit stays on the devices)
        import schedule_view
        from datetime import timedelta as _td
        today = now_dt.date()
        v = schedule_view.for_viewer(today - _td(days=1), today + _td(days=62), pid, role, {"household"})
        return [it.from_event(e) for e in (v.get("household") or {}).get("events", [])]

    readers = {"todos": todos_r, "scheduled": scheduled_r, "routines": routines_r, "tasks": tasks_r,
               "references": references_r, "chats": chats_r, "notes": notes_r, "linked": linked_r,
               "bills": bills_r, "mail": mail_r, "events": events_r}

    def filed(read: Callable[[], list]) -> Callable[[], list]:
        def run() -> list:   # #1085: every item carries the projects it's filed under
            import projects
            memb = projects.membership(pid)
            out = read()
            for x in out:
                x["projects"] = sorted(set(x["projects"]) | set(memb.get(x["id"], [])))
            return out
        return run
    return {k: filed(v) for k, v in readers.items()}


ITEMS_LATENCY_LOG = Path(os.environ.get("ITEMS_LATENCY_LOG", "/ai/logs/items-latency.jsonl"))
_ITEMS_LATENCY_LOCK = threading.Lock()


def _timed_readers(readers: dict[str, Callable[[], list]], parts: dict[str, int],
                   stats: dict[str, Any] | None) -> dict[str, Callable[[], list]]:
    """#1552: each reader as it was, still lazy (items.resolve calls it, on its own pool), its own run time written to
    parts[name] in ms when it runs, and its cached reads counted into this request's `stats` (a pool worker doesn't
    inherit the request's context). The same result or exception: only the clock is added."""
    def timed(name: str, read: Callable[[], list]) -> Callable[[], list]:
        def run() -> list:
            t = time.perf_counter()
            try:
                with _lib.read_stats(stats):
                    return read()
            finally:
                parts[name] = round((time.perf_counter() - t) * 1000)
        return run
    return {k: timed(k, v) for k, v in readers.items()}


@app.get("/v1/items")
async def items_view(request: Request, view: str = "now", start: str | None = Query(None, alias="from"),
                     end: str | None = Query(None, alias="to")) -> JSONResponse:
    """The main window's one items surface (#1084): view=now|time|unsorted|project:<id>|person:<id>, from/to
    (YYYY-MM-DD) for time. A ResolvedSurface (docs/surfaces.md, jb-1) built only from what this caller can see.
    Device calendar events aren't here: the apps merge EventKit themselves."""
    # #1181's guard: each request's time, by view kind and role (no ids, no content), for the scorecard; #1552: and
    # where it went, per reader (`parts`, reader name -> ms), and whether the read-path caches (#1548) were cold for it
    # (`cold`: any record / timeline / KB-title read went to the runtime). Names, ms and a boolean only.
    t0 = time.monotonic()
    parts: dict[str, int] = {}
    stats = _lib.read_stats_new()
    with _lib.read_stats(stats):
        resp = await _items_view(request, view, start, end, parts=parts, stats=stats)
    try:
        row = {"ts": time.time(), "view": str(view).split(":", 1)[0][:16], "role": _identity(request).role,
               "ms": round((time.monotonic() - t0) * 1000), "status": resp.status_code,
               "parts": {str(k): int(v) for k, v in sorted(dict(parts).items())}, "cold": stats["miss"] > 0}
        with _ITEMS_LATENCY_LOCK, ITEMS_LATENCY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001 - the timing log never fails a request
        pass
    return resp


async def _items_view(request: Request, view: str = "now", start: str | None = None,
                      end: str | None = None, parts: dict[str, int] | None = None,
                      stats: dict[str, Any] | None = None) -> JSONResponse:
    """The main window's one items surface (#1084): view=now|time|unsorted|project:<id>|person:<id>, from/to
    (YYYY-MM-DD) for time. A ResolvedSurface (docs/surfaces.md, jb-1) built only from what this caller can see.
    Device calendar events aren't here: the apps merge EventKit themselves."""
    return await items_view_for(_identity(request), view, start, end, parts=parts, stats=stats)


async def items_view_for(ident: "_hh.Identity", view: str = "now", start: str | None = None, end: str | None = None,
                         parts: dict[str, int] | None = None, stats: dict[str, Any] | None = None) -> JSONResponse:
    """_items_view for an identity (#1646: chat's project_items asks for the turn's own person through the same code,
    so it sees exactly what that person's app would, never more)."""
    import file_suggest
    import items as it
    import projects as projects_mod
    import spaces
    now_dt = _lib._local_now()
    pid = None if ident.role == "household" else ident.person_id
    groups = await asyncio.to_thread(spaces.all_groups)

    def project_ok(p: str) -> bool:   # a project they can see, or a group they're in (#1085: groups are projects too)
        pr = spaces.get_project(p)
        if pr:
            return spaces.project_visible(pr, pid, ident.role, groups)
        g = groups.get(p)
        return bool(g) and spaces.in_group(g, pid, ident.role)
    folks = await asyncio.to_thread(_store().people)
    people = {str(p["id"]) for p in folks}
    names = {str(p["id"]): str(p.get("name") or "").split(" ")[0] for p in folks}   # #1086: names on items you see

    def project_name(p: str) -> str:
        pr = spaces.get_project(p) or groups.get(p)
        return str((pr or {}).get("name") or "")

    def name_source(p: str) -> str:
        return str((spaces.get_project(p) or {}).get("name_source") or "person")   # a group is named by a person

    def project_people(p: str, its: list) -> list:
        """#1142: who's on this project page. Household members who may see the project, by its own audience (or the
        group's members); then people outside the household named in the project items this viewer sees (a world
        entity on a linked item), each one the viewer may see in the world model. Never from anyone else's items."""
        pr = spaces.get_project(p)
        g = groups.get(p)
        out = []
        for f in folks:
            fid, frole = str(f["id"]), str(f.get("role") or "")
            ok = spaces.project_visible(pr, fid, frole, groups) if pr else bool(g) and spaces.in_group(g, fid, frole)
            if ok:
                out.append({"id": fid, "name": names.get(fid, ""), "role": frole})
        eids = sorted({e for x in its for e in (x.get("parties") or [])})
        if not eids:
            return out
        import world as _world
        w = _world.get()
        ours = {str(f.get("name") or "").strip().lower() for f in folks} | {n.lower() for n in names.values() if n}
        for e in eids:
            try:
                row = w.entity(e)
                if row is None or not w.visible_entity(e, pid, ident.role):
                    continue
                row = dict(row)
                name = str(row.get("name") or "").strip()
                if row.get("type") != "person" or not name or name.lower() in ours:
                    continue
                # their relation as this viewer's evidence says it ("coach"), else none
                roles = [v.value for f_ in w.facts(e, pid, ident.role) if f_.attr == "relation" for v in f_.values]
            except Exception:  # noqa: BLE001 - an unreadable entity is left out, never guessed
                continue
            out.append({"id": e, "name": name, "role": str(roles[0]) if roles else None, "outside": True})
        return out
    def suggest(view_items: list) -> dict:
        """#1087: File it suggestions over the caller's own chat edges, into projects (and groups) they may see."""
        import chat_links
        import file_suggest
        import hooks
        if not pid or ident.role in ("kid", "household"):
            return {}
        con = hooks.connect()
        try:
            chat_links.ensure(con)
            edges = [chat_links.Edge(a, b, typ, st, own, json.loads(ty), json.loads(me), why) for a, b, typ, st, own, ty, me, why
                     in con.execute("SELECT a, b, type, state, owner, typed, measured, why FROM chat_links WHERE owner=?",
                                    (pid,))]
        finally:
            con.close()
        names_ = {p["id"]: str(p.get("name") or "") for p in spaces.projects_for(pid, ident.role)}
        names_.update({g: str(v.get("name") or "") for g, v in groups.items() if spaces.in_group(v, pid, ident.role)})
        return file_suggest.for_items(view_items, edges, names_, ident.role)
    try:
        readers = _item_readers(ident, now_dt, parts)
        if view == "unsorted":   # #1175: only Unsorted lists unfiled linker items (elsewhere they'd repeat KB items)
            readers["suggested"] = _suggested_reader(ident)
        elif view.startswith("project:") and project_linked_on():   # #1180: items linked to this project
            readers["linked_here"] = _project_linked_reader(ident, view.split(":", 1)[1])
        if parts is not None:   # #1552: each reader's own time, recorded when resolve runs it
            readers = _timed_readers(readers, parts, stats)
        snoozed: set[str] = set()
        if view == "now" and pid:   # #1233: what this viewer put off with Later, still hidden now
            import proactive
            t_sn = time.perf_counter()
            try:
                snoozed = await asyncio.to_thread(proactive.snoozed_items, pid, now_dt.timestamp())
            except Exception:  # noqa: BLE001 - an unreadable snooze store shows everything, never hides
                snoozed = set()
            if parts is not None:
                parts["snoozed"] = round((time.perf_counter() - t_sn) * 1000)
        t_res = time.perf_counter()
        out = await asyncio.to_thread(lambda: it.resolve(view, pid, ident.role, readers,
                                                         now=now_dt.timestamp(), tz=now_dt.tzinfo or timezone.utc,
                                                         start=start, end=end, project_ok=project_ok, people=people,
                                                         call_me=ident.call_me if pid else None, names=names,
                                                         project_name=project_name,
                                                         related=lambda p: projects_mod.related(p, pid, ident.role),
                                                         suggest=suggest,
                                                         auto_filed=(file_suggest.recent_auto(pid) if view == "now" and pid
                                                                     else None),
                                                         project_people=project_people, name_source=name_source,
                                                         snoozed=snoozed))
        if parts is not None:   # the whole resolve: its readers (in parallel), then projection, ranking, suggestions
            parts["resolve"] = round((time.perf_counter() - t_res) * 1000)
    except it.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    if view.startswith("project:") and pid and ident.role in ("admin", "adult"):   # #1177: Rename / Share, owner's
        p_id = view.split(":", 1)[1]
        pr = spaces.get_project(p_id)
        if pr:
            # Share offers the household's other adults who can't see it yet (never a kid: sharing a project with
            # one is a parent's call made in Settings, not a one-tap offer)
            sees = {x["id"] for x in (out.get("people") or []) if not x.get("outside")}
            offer = sorted(str(p_["id"]) for p_ in folks if p_.get("role") in ("admin", "adult")
                           and str(p_["id"]) != pid and str(p_["id"]) not in sees)
            mops = projects_mod.manage_ops(pr, pid, ident.role, offer)
            out["operations"] = out.get("operations", []) + mops
            for g in out.get("groups", []):
                if g["id"] == "g-project":
                    g["operations"] = (g.get("operations") or []) + [o["id"] for o in mops]
    return JSONResponse(out)


@app.get("/v1/fences")
async def fences_list(request: Request) -> JSONResponse:
    """The caller's fences with what each held back (#357 item 4, content-free): their own, plus the household's
    defaults (money, health) for an adult or the admin. A household device gets none."""
    import fences
    ident = _identity(request)
    if ident.role == "household" or not ident.person_id:
        return JSONResponse({"fences": [], "enabled": fences.enabled()})
    rows = await asyncio.to_thread(fences.get().for_viewer, str(ident.person_id).lower(), ident.role)
    return JSONResponse({"fences": rows, "enabled": fences.enabled()})


@app.delete("/v1/fences/{fence_id}")
async def fences_retire(fence_id: int, request: Request) -> JSONResponse:
    """Retire one of the caller's own fences (what it held back shows again). Defaults can't be retired here."""
    import fences
    ident = _identity(request)
    pid = str(ident.person_id or "").lower()
    fs = fences.get()
    mine = {r["id"] for r in await asyncio.to_thread(fs.owned_by, pid)} if pid else set()
    if fence_id not in mine:
        raise HTTPException(status_code=404, detail="not found")
    ok = await asyncio.to_thread(fs.retire, fence_id, pid)
    return JSONResponse({"retired": bool(ok)})


@app.post("/v1/files")
async def upload_file(request: Request, name: str = "", audience: str = "") -> JSONResponse:
    """A file for a turn (titan-node#713; the Mac's chat attachments, which were copied over with scp): the raw
    bytes as the body, `?name=photo.jpg`, and optionally `?audience=p1,p2` (people whose turns may read it too;
    nobody else's by default). 201 {id, name, path, type, size, audience, expires_at}: the turn names `path`.
    Images, PDFs, text and office documents only (415), at most UPLOADS_MAX_MB (413). A person's, never a household
    device's (route table). Kept UPLOADS_TTL_HOURS; never put into the knowledge base."""
    import uploads
    ident = _identity(request)
    try:
        if int(request.headers.get("content-length") or 0) > uploads.MAX_BYTES:
            return JSONResponse({"error": f"the file is larger than {uploads.MAX_BYTES // (1024 * 1024)} MB"}, status_code=413)
    except ValueError:
        return JSONResponse({"error": "bad Content-Length"}, status_code=400)
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > uploads.MAX_BYTES:
            return JSONResponse({"error": f"the file is larger than {uploads.MAX_BYTES // (1024 * 1024)} MB"}, status_code=413)
    aud = [a for a in audience.split(",") if a.strip()]
    code, out = await asyncio.to_thread(uploads.save, ident.person_id, name or request.headers.get("x-filename", ""),
                                        bytes(buf), aud)
    print(f"[files] {ident.person_id} ({ident.role}) {out.get('name', name)!r} {len(buf)} B → {code}", file=sys.stderr)
    return JSONResponse(out, status_code=code)


@app.get("/v1/files/uploads/{file_id}")
async def uploaded_file(file_id: str, request: Request):
    """A file someone sent (POST /v1/files), back to them and the people they named (a voice note's audience,
    jellybean#313): anyone else, an expired one, and any other id get 404."""
    from fastapi.responses import FileResponse
    import uploads
    ident = _identity(request)
    r = await asyncio.to_thread(uploads.for_viewer, file_id, ident.person_id)
    if not r:
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(r["path"], media_type=r["type"], filename=r["name"])


def _tv_device(ident: "_hh.Identity") -> dict[str, Any] | None:
    """The calling household TV's own device row (its id, name, platform, room), or None for anything else."""
    dev = ident.device or {}
    if ident.role != "household" or not dev.get("id"):
        return None
    row = next((d for p in _store().people() for d in (p.get("devices") or []) if d.get("id") == dev["id"]), {})
    return {"id": dev["id"], "name": row.get("name") or dev.get("name"), "platform": row.get("platform") or dev.get("platform"),
            "room": row.get("room")}


@app.post("/v1/tv-unlocks/request")
async def tv_unlock_request(request: Request) -> JSONResponse:
    """A household TV asks to show an adult's own things for a few minutes (jellybean#427, tv_unlock.py): {minutes?}.
    202 {request_id, expires_at, asked}: each adult at home gets a card to confirm on their phone."""
    import tv_unlock
    dev = _tv_device(_identity(request))
    if dev is None:
        return JSONResponse({"error": "only a household TV unlocks this way"}, status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    minutes = body.get("minutes") if isinstance(body, dict) else None
    people = await asyncio.to_thread(_store().people)
    code, out = await asyncio.to_thread(lambda: tv_unlock.request(dev, people, minutes=int(minutes or tv_unlock.DEFAULT_MINUTES)
                                                                  if isinstance(minutes, (int, float)) else tv_unlock.DEFAULT_MINUTES))
    return JSONResponse(out, status_code=code)


@app.get("/v1/tv-unlocks/self")
async def tv_unlock_self(request: Request) -> JSONResponse:
    """This TV's unlock: {state: none|pending|active, person?, expires_at?, ended?: {reason, at}}."""
    import tv_unlock
    dev = _tv_device(_identity(request))
    if dev is None:
        return JSONResponse({"error": "only a household TV unlocks this way"}, status_code=403)
    return JSONResponse(await asyncio.to_thread(tv_unlock.state, dev["id"]))


@app.get("/v1/tv-unlocks/self/layer")
async def tv_unlock_layer(request: Request) -> JSONResponse:
    """The unlocking adult's own calendar, cards and bills, read-only, while the unlock is live; 403 otherwise."""
    import tv_unlock
    dev = _tv_device(_identity(request))
    if dev is None:
        return JSONResponse({"error": "only a household TV unlocks this way"}, status_code=403)
    out = await asyncio.to_thread(tv_unlock.layer, dev["id"])
    if out is None:
        return JSONResponse({"error": "locked"}, status_code=403)
    return JSONResponse(out)


@app.delete("/v1/tv-unlocks/self")
async def tv_unlock_lock_tv(request: Request) -> JSONResponse:
    """The TV locks: {reason?: lock_tv | kid_profile | ambient | sleep}. Ends any unlock and withdraws a pending ask."""
    import tv_unlock
    dev = _tv_device(_identity(request))
    if dev is None:
        return JSONResponse({"error": "only a household TV unlocks this way"}, status_code=403)
    reason = request.query_params.get("reason") or "lock_tv"
    try:
        body = await request.json()
        if isinstance(body, dict) and body.get("reason"):
            reason = str(body["reason"])
    except Exception:  # noqa: BLE001
        pass
    n = await asyncio.to_thread(tv_unlock.end_on_device, dev["id"], reason)
    return JSONResponse({"ended": n})


@app.delete("/v1/tv-unlocks/{unlock_id}")
async def tv_unlock_lock_phone(unlock_id: str, request: Request) -> JSONResponse:
    """An adult's Lock now on their phone: only their own unlock."""
    import tv_unlock
    ident = _identity(request)
    ok = await asyncio.to_thread(tv_unlock.end_unlock, unlock_id, ident.person_id, "lock_phone")
    return JSONResponse({"ended": ok}, status_code=200 if ok else 404)


@app.get("/v1/tv-unlocks")
async def tv_unlock_audit(request: Request, days: float = 30) -> JSONResponse:
    """The audit (admin): every unlock, who, which TV, when it started and ended, and why."""
    import tv_unlock
    return JSONResponse({"unlocks": await asyncio.to_thread(tv_unlock.audit, max(1.0, min(365.0, days)))})


@app.get("/v1/handoff/rooms")
async def handoff_rooms(request: Request) -> JSONResponse:
    """The rooms with a household screen, for "Continue on…" (jellybean#433): {rooms: [{room, screens}]}."""
    import handoff
    return JSONResponse({"rooms": handoff.rooms(await asyncio.to_thread(_store().people))})


@app.post("/v1/handoff")
async def handoff_send(request: Request) -> JSONResponse:
    """Put one of your replies on the screens in a room you name (handoff.py): {to_room, kind: "reply", text, title?,
    thread_id?}. 201 {id, room, screens, expires_at}; 404 {rooms} for a room with no screen."""
    import handoff
    ident = _identity(request)
    body = await _json_body(request)
    people = await asyncio.to_thread(_store().people)
    import room_state
    code, out = await asyncio.to_thread(lambda: handoff.send(ident.person_id, ident.role, body, people,
                                                             hold=(lambda room: room_state.hold(room))
                                                             if room_state.hold_enabled() else None))
    return JSONResponse(out, status_code=code)


@app.get("/v1/rooms/state")
async def rooms_state(request: Request) -> JSONResponse:
    """Each room with a household screen: occupied, empty, asleep or unknown (room_state.py, jellybean#433 slice 2).
    A room's state, never a person's: from Home Assistant's motion and presence sensors by area (never a camera),
    else quiet hours. Read only; nothing kept."""
    import handoff
    import room_state
    import rooms as _rooms
    names = [r["room"] for r in handoff.rooms(await asyncio.to_thread(_store().people))]
    try:   # (#1511) a room renamed in Settings is read by its Home Assistant area
        area_of = await asyncio.to_thread(_rooms.area_of)
    except Exception:  # noqa: BLE001
        area_of = {}
    return JSONResponse({"rooms": await asyncio.to_thread(room_state.states, names, area_of=area_of)})


@app.get("/v1/handoff")
async def handoff_sent(request: Request) -> JSONResponse:
    """The caller's own hand-offs still on a screen (to take one back)."""
    import handoff
    return JSONResponse({"handoffs": await asyncio.to_thread(handoff.sent_by, _identity(request).person_id)})


@app.get("/v1/handoff/self")
async def handoff_self(request: Request) -> JSONResponse:
    """A household screen's own: what people put on it, newest first, while it lasts (10 minutes)."""
    import handoff
    ident = _identity(request)
    dev = (ident.device or {}).get("id")
    if ident.role != "household" or not dev:
        return JSONResponse({"error": "a household screen reads its own"}, status_code=403)
    return JSONResponse({"handoffs": await asyncio.to_thread(handoff.for_device, dev)})


@app.delete("/v1/handoff/{handoff_id}")
async def handoff_end(handoff_id: str, request: Request) -> JSONResponse:
    """Done on the screen it went to, or taken back by its sender; the text is gone from titan then."""
    import handoff
    ident = _identity(request)
    dev = (ident.device or {}).get("id") if ident.role == "household" else None
    ok = await asyncio.to_thread(lambda: handoff.end(handoff_id, sender=None if dev else ident.person_id, device_id=dev))
    return JSONResponse({"ended": ok}, status_code=200 if ok else 404)


def _sitter_tv(device_id: Any) -> dict[str, Any] | tuple[int, dict[str, Any]]:
    """The household TV an adult names (or the only one there is), as tv_unlock's device row; else (status, error)."""
    import tv_unlock
    tvs = [{"id": d.get("id"), "name": d.get("name"), "platform": d.get("platform"), "room": d.get("room")}
           for p in _store().people() if p.get("role") == "household" for d in (p.get("devices") or [])
           if str(d.get("platform") or "") in tv_unlock.TV_PLATFORMS and not d.get("revoked")]
    if device_id:
        tv = next((t for t in tvs if t["id"] == device_id), None)
        return tv if tv else (404, {"error": "There's no household TV with that id.", "code": "not_found"})
    if len(tvs) == 1:
        return tvs[0]
    if not tvs:
        return 404, {"error": "There's no household TV paired.", "code": "not_found"}
    return 409, {"error": "Which TV?", "code": "ambiguous", "candidates": [{"device_id": t["id"], "name": t["name"]} for t in tvs]}


@app.get("/v1/home/sitter")
async def sitter_view(request: Request, device_id: str | None = None) -> JSONResponse:
    """Sitter mode (jellybean#428, sitter.py). From the TV: {state: off|pending|on, kind?, ends_at?, and while on the
    sheet: basics, wifi?, contacts, kids, kids_today}. From an adult: each TV's state and the audit."""
    import sitter
    ident = _identity(request)
    if ident.role == "household":
        dev = _tv_device(ident)
        if dev is None:
            return JSONResponse({"error": "Only a household TV shows sitter mode.", "code": "not_tv"}, status_code=403)
        return JSONResponse(await asyncio.to_thread(sitter.state, dev["id"]))
    rows = await asyncio.to_thread(sitter.audit, 7)
    return JSONResponse({"sessions": [{k: r[k] for k in ("id", "device_id", "device_name", "kind", "state", "started_at",
                                                         "ends_at", "ended_at", "end_reason")} for r in rows]})


@app.post("/v1/home/sitter")
async def sitter_start(request: Request) -> JSONResponse:
    """{kind?: sitter|guest, until?, device_id?}. An adult: on now (200). A household TV: the adults at home confirm on
    their phones (202)."""
    import sitter
    ident = _identity(request)
    body = await _json_body(request)
    body = body if isinstance(body, dict) else {}
    kind, until = str(body.get("kind") or "sitter"), body.get("until")
    if ident.role == "household":
        dev = _tv_device(ident)
        if dev is None:
            return JSONResponse({"error": "Only a household TV asks for sitter mode.", "code": "not_tv"}, status_code=403)
        people = await asyncio.to_thread(_store().people)
        code, out = await asyncio.to_thread(lambda: sitter.request(dev, people, kind=kind, until=until))
        return JSONResponse(out, status_code=code)
    tv = await asyncio.to_thread(_sitter_tv, body.get("device_id"))
    if isinstance(tv, tuple):
        return JSONResponse(tv[1], status_code=tv[0])
    code, out = await asyncio.to_thread(lambda: sitter.start(tv, ident.person_id, ident.role, kind=kind, until=until))
    return JSONResponse(out, status_code=code)


@app.delete("/v1/home/sitter")
async def sitter_end(request: Request, device_id: str | None = None) -> JSONResponse:
    """An adult ends it: on one TV (device_id) or every one."""
    import sitter
    ident = _identity(request)
    n = await asyncio.to_thread(lambda: sitter.end_session(None, ident.person_id, ident.role, device_id=device_id))
    return JSONResponse({"ended": n})


@app.get("/v1/home/sitter/sheet")
async def sitter_sheet(request: Request) -> JSONResponse:
    """The sitter sheet, Wi-Fi password included: for the adults' own edit view only."""
    import sitter
    return JSONResponse(await asyncio.to_thread(sitter.sheet))


@app.put("/v1/home/sitter/sheet")
async def sitter_sheet_save(request: Request) -> JSONResponse:
    """Replace the sheet: {contacts, basics, kids, wifi?}."""
    import sitter
    ident = _identity(request)
    body = await _json_body(request)
    if not isinstance(body, dict):
        return JSONResponse({"error": "The sheet is a JSON object.", "code": "bad_request"}, status_code=400)
    return JSONResponse(await asyncio.to_thread(sitter.save_sheet, body, ident.person_id))


@app.post("/v1/speech")
async def speech(request: Request):
    """titan's voice for a reply (jellybean#709, the Mac's talk mode): {text} (≤ 2000 characters)
    → audio/wav, with X-Speech-Voice: local (Kokoro, nothing leaves the house) or gemini (the cloud fallback, #554;
    X-Egress: gemini too). The cloud is for an adult's or the admin's device only: a kid's or a household device gets
    Kokoro or nothing. 503 when no voice can speak (the app uses its own device's voice); 429 past 20 a minute per
    device. Nothing on titan stores the text or the audio, nor who asked: GET /health's `speech` counts calls by voice
    and kind of device, and the time to audio per voice (`ms`), in memory only; each call logs its voice and duration."""
    ident = _identity(request)
    body = await _json_body(request)
    text = " ".join(str((body or {}).get("text") or "").split())
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    if len(text) > _lib.SPEECH_MAX:
        return JSONResponse({"error": f"at most {_lib.SPEECH_MAX} characters at a time"}, status_code=400)
    key = str((ident.device or {}).get("id") or ident.person_id or "open")
    if not _lib.speech_allowed(key):
        _lib.count_speech("rate_limited")
        return JSONResponse({"error": "too many at once; try again in a minute"}, status_code=429)
    cloud = ident.role in ("admin", "adult")
    t0 = time.monotonic()
    out = await asyncio.to_thread(_lib.speech_wav, text, cloud)
    voice = out[1] if out else None
    if voice:   # #554: time to the audio, per voice (durations only; GET /health `speech.ms`)
        ms = (time.monotonic() - t0) * 1000
        _lib.time_speech(voice, ms)
        print(f"[speech] voice={voice} ms={int(ms)}", file=sys.stderr)
    _lib.count_speech("adult" if cloud else "kid_or_household", voice or "unavailable")
    if not out:
        return JSONResponse({"error": "titan's voice isn't available right now"}, status_code=503)
    from fastapi.responses import Response
    headers = {"X-Speech-Voice": voice, "Cache-Control": "no-store"}
    if voice == "gemini":
        headers["X-Egress"] = "gemini"
    return Response(content=out[0], media_type="audio/wav", headers=headers)


# Coding agents on the Mac, seen and stopped from the iPhone (jellybean#699; agent_runs.py). In memory only: a run is
# dropped 1 h after it ends (and on a restart); nothing about it is written to disk.

def _agent_run_push(event: str, run: dict) -> None:
    """The run's Live Activity on the owner's iPhones: started (push-to-start) or updated, or ended. Counts, times and
    the repo's name only (jellybean#699, decided 10-04)."""
    import agent_runs
    import push
    state = agent_runs.content_state(run)
    if event == "end":
        push.send_activity_async(run["person"], "agent", run["id"], state, "end")
    else:
        push.send_activity_async(run["person"], "agent", run["id"], state, "update",
                                 start=agent_runs.start_attributes(run), start_type=agent_runs.ATTRIBUTES_TYPE,
                                 start_alert=agent_runs.start_alert(run))


@app.post("/v1/agent-runs")
async def agent_run_report(request: Request) -> JSONResponse:
    """The Mac reports one of its coding agents (jellybean#699 §1): {id, repo, state, started_at, updated_at,
    files_changed, lines_added, lines_removed, ends_by?}, exactly those. Never a prompt, branch, path or file name
    (400 for any other field), never the ase repo. Kept in memory only, dropped 1 h after the run ends; nothing is
    written to disk. Its owner is the token's person; the Mac that sent it is where a stop goes."""
    import agent_runs
    ident = _identity(request)
    try:
        report = agent_runs.clean_report(await _json_body(request))
        key = _mac_key_from(request)
        told = agent_runs.RUNS.report(ident.person_id, str(key) if key else None, (ident.device or {}).get("name"),
                                      report)
    except agent_runs.ReportError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if told:
        _agent_run_push(*told)
    return JSONResponse({"id": report["id"], "kept": True})


@app.get("/v1/agent-runs")
async def agent_runs_list(request: Request) -> JSONResponse:
    """The asker's own coding agents, newest first (jellybean#699 §2): the Mac's report, the Mac's name, and state
    "unknown" for a running one not heard from in 10 minutes. In memory only (gone 1 h after a run ends)."""
    import agent_runs
    return JSONResponse({"runs": agent_runs.RUNS.for_person(_identity(request).person_id)})


@app.post("/v1/agent-runs/{run_id}/stop")
async def agent_run_stop(run_id: str, request: Request) -> JSONResponse:
    """Stop one of the asker's coding agents from the phone (jellybean#699 §4; the phone confirmed first). It's routed
    to the Mac that reported the run as the device tool agent_stop, which is never one of the model's tools. The Mac
    stops it and keeps its worktree. 200 {state: stopped}; 404 unknown or someone else's (nobody learns another's
    runs exist); 409 {state} already over; 503 the Mac didn't wake; 504 it didn't answer in time; 502 an answer titan
    can't read."""
    import agent_runs
    ident = _identity(request)
    run = agent_runs.RUNS.get(run_id) if agent_runs.valid_id(run_id) else None
    if run is None or run["person"] != str(ident.person_id).lower():
        return JSONResponse({"error": "no such run"}, status_code=404)
    if run["state"] in agent_runs.ENDED:
        return JSONResponse({"state": run["state"]}, status_code=409)
    macs = await asyncio.to_thread(lambda: _store().macs_for(ident.person_id))
    mac = next((m for m in macs if m.get("device_key") == run.get("device_key")), None)
    if mac is None:
        return JSONResponse({"error": _xd.NO_WAKE}, status_code=503)
    deadline = time.monotonic() + agent_runs.STOP_TIMEOUT_S
    out = await asyncio.to_thread(
        _xd.route, {"tool": "agent_stop", "args": {"id": run["id"]}, "risk": "dangerous"},
        person_id=ident.person_id, mac=mac, turn_id="stop-" + uuid.uuid4().hex[:12],
        origin={"device": (ident.device or {}).get("name") or "iPhone", "platform": (ident.device or {}).get("platform") or ""},
        session="agent-stop:" + run["id"], emit=lambda event, data: None,
        ask_confirm=lambda payload: {"approved": False, "remember_session": False},
        cancelled=lambda: time.monotonic() > deadline, approved=True)
    if out.get("error") == _xd.NO_ANSWER:
        return JSONResponse({"error": _xd.NO_ANSWER}, status_code=504)
    if out.get("error"):
        return JSONResponse({"error": "It didn't stop on your Mac."}, status_code=502)
    if out.get("completed") is False and out.get("result") == _xd.NO_WAKE:
        return JSONResponse({"error": _xd.NO_WAKE}, status_code=503)
    code, body = agent_runs.stop_outcome(out.get("result") or "")
    if code == 200:
        stopped = agent_runs.RUNS.mark_stopped(run["id"])
        if stopped:
            _agent_run_push("end", stopped)
    return JSONResponse(body, status_code=code)


# A kid's Watch, paired directly (titan-node#755; the contract and the owner's rules are on that issue).

_watch_pair_limiter = _hh.WatchPairLimiter()   # per source: rate, open codes, failures (W6)
_watch_hello_limiter = _hh.ClaimLimiter()   # per-source rate only: hello never records failures (no global lockout)


@app.post("/v1/watch/pair/request")
async def watch_pair_request(request: Request) -> JSONResponse:
    """(no auth; rate-limited per source) The Watch asks to be paired: {device_name} → {request_id, code, secret,
    expires_at}. It shows the code (6 digits, 10 minutes, single use) for a parent to type on their iPhone, and keeps
    the secret to collect its token. titan keeps only the secret's sha256, and never logs it."""
    ip = request.client.host if request.client else "?"
    if not _watch_pair_limiter.admit(ip):
        return JSONResponse({"error": "too many pairing attempts; try again in a few minutes"}, status_code=429)
    if not _watch_pair_limiter.issue(ip):
        return JSONResponse({"error": "this device already has pairing codes waiting; use one, or wait a few minutes"},
                            status_code=429)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        out = await asyncio.to_thread(_store().watch_pair_request, (body or {}).get("device_name") if isinstance(body, dict) else None)
    except _hh.PairingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return JSONResponse(out)


@app.post("/v1/watch/pair/confirm")
async def watch_pair_confirm(request: Request) -> JSONResponse:
    """A parent's iPhone confirms the code a kid's Watch shows: {code, person_id} → {request_id, kid_first_name}.
    Only the admin or one of that kid's parents (403); only for a kid (400); unknown 404, used or expired 410. A code
    takes 3 refusals, then it's spent; failed codes count toward that device's own lockout (W6), never everyone's."""
    ident = _identity(request)
    who = "dev:" + str((ident.device or {}).get("id") or ident.person_id)
    if _watch_pair_limiter.blocked(who):
        return JSONResponse({"error": "too many wrong codes; try again in a few minutes"}, status_code=429)
    body = await _json_body(request)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)
    try:
        out = await asyncio.to_thread(_store().watch_pair_confirm, body.get("code"), body.get("person_id"), ident)
    except _hh.PairingError as exc:
        if exc.status in (400, 404, 410):
            _watch_pair_limiter.failed(who)
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return JSONResponse(out)


@app.get("/v1/watch/pair/request/{request_id}")
async def watch_pair_poll(request_id: str, request: Request) -> JSONResponse:
    """(no auth; the Watch's secret in X-Pair-Secret) 202 {} while it waits for a parent; 200 {token, device,
    kid_first_name, servers} once confirmed, once; 404 unknown or the wrong secret; 410 expired or collected."""
    ip = request.client.host if request.client else "?"
    if not _watch_pair_limiter.admit(ip):
        return JSONResponse({"error": "too many pairing attempts; try again in a few minutes"}, status_code=429)
    try:
        out = await asyncio.to_thread(_store().watch_pair_poll, request_id, request.headers.get("X-Pair-Secret"))
    except _hh.PairingError as exc:
        if exc.status == 404:
            _watch_pair_limiter.failed(ip)
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    if out is not None:          # 10-07 W1: the Watch pins titan's identity with its token (server_identity.py)
        import server_identity
        out = {**out, "server_identity": await asyncio.to_thread(server_identity.identity)}
    return JSONResponse(out if out is not None else {}, status_code=200 if out is not None else 202)


# A shared screen (an Apple TV) paired with no typing on the remote (#411, jellybean#434): the TV shows a code and a
# QR, the admin confirms on their iPhone or Mac, and the TV collects a household-role token. The contract is on #411.

_screen_pair_limiter = _hh.WatchPairLimiter()   # its own book: TVs and Watches never use up each other's codes


@app.post("/v1/screen/pair/request")
async def screen_pair_request(request: Request) -> JSONResponse:
    """(no auth; rate-limited per source and globally) A TV asks to be paired: {device_name?} → {request_id, code,
    secret, expires_at, qr}. It shows the code and the QR (the code only; 10 minutes, single use) and keeps the secret
    to collect its token. titan keeps only the secret's sha256, and never logs it."""
    ip = request.client.host if request.client else "?"
    if not _screen_pair_limiter.admit(ip):
        return JSONResponse({"error": "too many pairing attempts; try again in a few minutes"}, status_code=429)
    if not _screen_pair_limiter.issue(ip):
        return JSONResponse({"error": "this screen already has pairing codes waiting; use one, or wait a few minutes"},
                            status_code=429)
    body = await _json_body(request)
    try:
        out = await asyncio.to_thread(_store().screen_pair_request, body.get("device_name") if isinstance(body, dict) else None)
    except _hh.PairingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return JSONResponse(out)


@app.post("/v1/screen/pair/confirm")
async def screen_pair_confirm(request: Request) -> JSONResponse:
    """(admin) The code a TV shows: {code, name, room?} adds a new household screen; {code, person_id, room?} pairs an
    existing one again → {request_id, name}. 403 not the admin; 400 bad input; 404 unknown code (a Watch's too); 409 the
    name is taken; 410 used or expired. Failed codes count toward this device's own lockout."""
    ident = _require_admin(request)
    who = "dev:" + str((ident.device or {}).get("id") or ident.person_id)
    if _screen_pair_limiter.blocked(who):
        return JSONResponse({"error": "too many wrong codes; try again in a few minutes"}, status_code=429)
    body = await _json_body(request)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)
    try:
        out = await asyncio.to_thread(_store().screen_pair_confirm, body.get("code"), ident, body.get("name"),
                                      body.get("person_id"), body.get("room"), (ident.device or {}).get("name"))
    except _hh.PairingError as exc:
        if exc.status in (400, 404, 410):
            _screen_pair_limiter.failed(who)
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return JSONResponse(out)


@app.get("/v1/screen/pair/request/{request_id}")
async def screen_pair_poll(request_id: str, request: Request) -> JSONResponse:
    """(no auth; the TV's secret in X-Pair-Secret) 202 {} while it waits for the admin; 200 {token, device, person,
    confirmed_by, servers, server_identity} once confirmed, once; 404 unknown or the wrong secret; 410 expired or
    collected. The adults get a card: "<name> joined"."""
    ip = request.client.host if request.client else "?"
    if not _screen_pair_limiter.admit(ip):
        return JSONResponse({"error": "too many pairing attempts; try again in a few minutes"}, status_code=429)
    try:
        out = await asyncio.to_thread(_store().screen_pair_poll, request_id, request.headers.get("X-Pair-Secret"))
    except _hh.PairingError as exc:
        if exc.status == 404:
            _screen_pair_limiter.failed(ip)
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    if out is None:
        return JSONResponse({}, status_code=202)
    import proactive
    import server_identity
    out = {**out, "server_identity": await asyncio.to_thread(server_identity.identity)}
    by = out["confirmed_by"]
    detail = f"Added by {by['first_name']}" + (f" on {by['device_name']}" if by.get("device_name") else "") + "."
    try:   # the TV has its token either way; the card is the adults' notice
        await asyncio.to_thread(lambda: proactive.add_card(f"{out['device']['name']} joined", detail, kind="info",
                                                           source="devices", to="adults", key=f"tv-joined-{out['device']['id']}"))
    except Exception as exc:  # noqa: BLE001
        print(f"[screen-pair] the joined card failed: {type(exc).__name__}", file=sys.stderr)
    return JSONResponse(out)


@app.post("/v1/watch/hello")
async def watch_hello(request: Request) -> JSONResponse:
    """(no auth; rate-limited per source) {nonce: base64, 16–64 bytes} → {alg, key, fp, sig}: titan's Ed25519 signature
    over b"titan-watch-hello-v1\\0" + nonce. A kid's Watch verifies it against the key it pinned at pairing before it
    sends its token to this address (10-07 review W1; the contract is server_identity.py's docstring)."""
    ip = request.client.host if request.client else "?"
    if not _watch_hello_limiter.admit(ip):
        return JSONResponse({"error": "too many requests; try again in a minute"}, status_code=429)
    body = await _json_body(request)
    import server_identity
    try:
        return JSONResponse(await asyncio.to_thread(server_identity.hello, (body or {}).get("nonce") if isinstance(body, dict) else None))
    except ValueError as exc:   # not counted as a failure: a global lockout here would cut every Watch off titan
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/v1/watch/devices")
async def watch_devices(request: Request) -> JSONResponse:
    """The kids' Watches this caller may unpair (the admin: all; a parent: their kids'): {watches: [{id, name,
    kid_first_name, paired_at, last_seen}]}. Unpair one with DELETE /v1/devices/{id}."""
    return JSONResponse({"watches": await asyncio.to_thread(_store().watch_devices_for, _identity(request))})


@app.get("/v1/watch/today")
async def watch_today(request: Request) -> JSONResponse:
    """A kid's Watch's day (titan-node#755): {events: [{start, end, all_day, title}], chores: [{id, text, done}],
    next}. Built only from what the kid can already read: GET /v1/schedule's rules (fences included), narrowed to
    the kid's own row and the household calendars (never another person's, even one shared with them), and the
    kids' screen narrowed to their own chores. Title and time only: no place, notes, calendar or organizer."""
    import kids_screen
    import schedule_view
    from datetime import timedelta as _td
    ident = _identity(request)
    if ident.role != "kid":
        return JSONResponse({"error": "this is a kid's Watch's view"}, status_code=403)
    today = _lib._local_now().date()
    try:
        sched = await asyncio.to_thread(schedule_view.for_viewer, today, today + _td(days=1), ident.person_id, "kid", None)
    except Exception:  # noqa: BLE001
        sched = {"people": [], "household": {"events": []}}
    mine = [e for row in sched.get("people", []) if row.get("you") for e in row.get("events", [])]
    events = sorted(({"start": e["start"], "end": e.get("end"), "all_day": bool(e.get("all_day")), "title": e["title"]}
                     for e in mine + list((sched.get("household") or {}).get("events") or [])), key=lambda e: e["start"])
    try:
        kids = (await asyncio.to_thread(kids_screen.gather, "kid")).get("kids") or []
    except Exception:  # noqa: BLE001
        kids = []
    me = next((k for k in kids if str(k.get("id")) == ident.person_id), None) or {}
    chores = [{"id": c.get("id"), "text": c.get("text"), "done": bool(c.get("done"))} for c in me.get("chores") or []]
    return JSONResponse({"events": events, "chores": chores, "next": me.get("next")})


@app.post("/v1/voice-notes")
async def voice_note(request: Request) -> JSONResponse:
    """A voice note to the household (jellybean#313, voice_notes.py): {file_id (an .m4a or .aac the sender uploaded
    with POST /v1/files), to: "household" | "adults" | [person ids], seconds (≤ 60), transcript? (only when the sender
    chose to send one)}. 201 {card_id, to}: a card with Play for everyone it's for, pushed."""
    import voice_notes
    ident = _identity(request)
    body = await _json_body(request)
    people = await asyncio.to_thread(_store().people)
    code, out = await asyncio.to_thread(voice_notes.send, ident.person_id, ident.role, body, people)
    return JSONResponse(out, status_code=code)


@app.get("/v1/household/voice-notes")
async def voice_notes_setting(request: Request) -> JSONResponse:
    """Whether kids may send voice notes (default yes; jellybean#313), and who a note can go to: {kids_may_send,
    people: [{id, name, role}]}, everyone but the caller and the shared household devices (a phone's "choose people")."""
    import voice_notes
    ident = _identity(request)
    people = await asyncio.to_thread(_store().people)
    return JSONResponse({"kids_may_send": await asyncio.to_thread(voice_notes.kids_may_send),
                         "people": voice_notes.recipients(people, ident.person_id)})


@app.patch("/v1/household/voice-notes")
async def voice_notes_set(request: Request) -> JSONResponse:
    """An adult turns kids' voice notes on or off: {kids_may_send}."""
    import voice_notes
    ident = _identity(request)
    body = await _json_body(request)
    if not isinstance(body, dict) or not isinstance(body.get("kids_may_send"), bool):
        return JSONResponse({"error": "expected {kids_may_send: true|false}"}, status_code=400)
    try:
        on = await asyncio.to_thread(voice_notes.set_kids_may_send, body["kids_may_send"], ident.role)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    return JSONResponse({"kids_may_send": on})


@app.get("/v1/files/generated/{name}")
async def generated_file(name: str, request: Request):
    """A picture generate_image made, for the reply's images block (jellybean#201 slice 2): only to the person
    who asked for it; anyone else, and any other name, gets 404."""
    from fastapi.responses import FileResponse
    ident = _identity(request)
    owner = await asyncio.to_thread(_lib.generated_owner, name)
    path = Path(_lib.GENERATED_DIR) / name
    if not owner or owner != str(ident.person_id or "").lower() or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="image/png")


@app.get("/v1/tasks")
async def list_tasks(request: Request) -> JSONResponse:
    """The tasks this caller may see: their own, every one for an admin, none for a household device
    (jellybean#228)."""
    pid, role, _ = _world_viewer(request)
    if not pid:
        return JSONResponse({"tasks": []})
    # in-process (#1181), not a Python subprocess per request; tasks_for reports its own errors in the body
    return JSONResponse(await asyncio.to_thread(_lib.tasks_for, pid, role))


# ---------------------------------------------------------------------------
# Household connectors (docs/household-connectors.md): Jellybean's collectors send items here.
# ---------------------------------------------------------------------------

@app.post("/v1/connectors/{source}/items")
async def connectors_ingest(source: str, request: Request) -> JSONResponse:
    """Write a batch of items from one source: {"items": [{source_id, observed_at, kind, title,
    text?, fields?, links?, timeline?, account?, deleted?, deleted_reason?}, ...], "sync"?: {complete, window_start,
    window_end}} (at most 500 items). A calendar item's deleted needs proof (#1612): a deleted_reason the device saw
    happen, or a complete sync window covering it; otherwise it's kept and counted as kept_absent. Transition: a batch
    with no proof fields from a device that never sent "sync" deletes as before (legacy_calendar_deletes)."""
    import connectors_runner as cx
    body = await request.json()
    items = body.get("items")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items must be a list")
    sync = body.get("sync") if isinstance(body.get("sync"), dict) else None
    try:
        # The KB notes are private to the person whose device sent them (kb_privacy.py).
        ident = _identity(request)
        out = await asyncio.to_thread(cx.ingest, source, items, owner=ident.person_id,
                                      device_id=(ident.device or {}).get("id"), sync=sync)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if source == "calendar":   # after the batch is stored: a trip whose event moved or went (jellybean#753); never raises
        import trip_push
        # only removals titan applied: a kept absence ends no trip (#1612)
        kept = set(out.get("kept_absent_ids") or ())
        trip_push.after_calendar_batch(ident.person_id, [i for i in items if not (
            isinstance(i, dict) and i.get("deleted") and str(i.get("source_id") or "").strip() in kept)])
    return JSONResponse(out)


@app.get("/v1/connectors")
async def connectors_status() -> JSONResponse:
    """Each sync source (calendar, mail, scans, downloads, browsing…): on or off, last run, items."""
    import connectors_runner as cx
    return JSONResponse({"sources": await asyncio.to_thread(cx.status)})


@app.delete("/v1/connectors/{source}")
async def connectors_forget(source: str, request: Request, apply: bool = False, everyone: bool = False) -> JSONResponse:
    """Remove everything a source wrote for the caller (dry run unless ?apply=true); applied, the source is off for
    them from now on (Zach, 10-02: it stays forgotten; /v1/me's connectors_off tells their devices to stop).
    ?everyone=true: the admin's maintenance path, every person's items, nothing turned off."""
    import connectors_runner as cx
    ident = _identity(request)
    if ident.role == "household":
        return JSONResponse({"error": "a shared device has no synced sources of its own"}, status_code=403)
    owner = None if (everyone and ident.role == "admin") else ident.person_id
    return JSONResponse(await asyncio.to_thread(cx.forget, source, apply, None, None, owner))


@app.get("/v1/connectors/state")
async def connectors_state(request: Request, days: int = 7) -> JSONResponse:
    """{ledger_epoch, errors: [{day, source, class, count}]}: the ledger's epoch (a device that last saw another one
    sends everything again: titan's ledger was reset) and the caller's own per-item ingest errors by day, source and
    class (the text before the first ':'), the last `days` (≤ 90). Counts only; never a title or an id."""
    import connectors_runner as cx
    ident = _identity(request)
    out = await asyncio.to_thread(cx.state, ident.person_id or "", max(1, min(int(days), 90)))
    if ident.role == "household":
        out["errors"] = []
    return JSONResponse(out)


@app.get("/v1/connectors/tombstones")
async def connectors_tombstones(request: Request, since: float = 0.0) -> JSONResponse:
    """What the caller's devices should drop from their outbox and caches (jellybean#233): forgotten items since
    `since` ({source, source_id, at}) and the sources that are off for them. Pass the returned `now` next time.
    ledger_epoch: when it differs from the one the device last saw, titan's ledger was reset: send everything again
    (jellybean#1157; a titan without the field means no resend)."""
    import connectors_runner as cx
    ident = _identity(request)
    if ident.role == "household":
        return JSONResponse({"tombstones": [], "sources_off": [], "now": time.time(),
                             "ledger_epoch": await asyncio.to_thread(cx.ledger_epoch)})
    return JSONResponse(await asyncio.to_thread(cx.tombstones, ident.person_id, since))


# ---------------------------------------------------------------------------
# Proactive loop (scripts/proactive.py): events in, triaged locally, cards out to Jellybean.
# ---------------------------------------------------------------------------

@app.post("/v1/triggers")
async def trigger_submit(request: Request) -> JSONResponse:
    """{source, kind?, summary, detail?, dedupe_key?} → 202 {id, status}; processed in the background."""
    import proactive
    body = await request.json()
    try:
        out = await asyncio.to_thread(proactive.submit_trigger, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(out, status_code=202)


@app.get("/v1/triggers")
async def trigger_recent(last: int = 20) -> JSONResponse:
    """The last triggers the proactive loop received (?last=, up to 200)."""
    import proactive
    return JSONResponse({"triggers": await asyncio.to_thread(proactive.recent_triggers, min(last, 200))})


def _note_active(ident: "_hh.Identity") -> None:
    """The person just did something on this device: their other phones' card pushes land quietly for a
    while (push.ACTIVE_S, jellybean#204). Never raises."""
    if ident.role == "household" or not ident.device:
        return
    try:
        import push
        push.note_active(ident.person_id, ident.device.get("id"))
    except Exception:  # noqa: BLE001
        pass


def _cleared(request: Request, card_id: str) -> None:
    """A card dealt with here: the person's other phones drop its notification (jellybean#204)."""
    ident = _identity(request)
    _note_active(ident)
    if ident.role == "household":
        return
    try:
        import push
        push.clear_card_async(ident.person_id, card_id, (ident.device or {}).get("id"))
    except Exception:  # noqa: BLE001 - the card is dealt with either way
        pass


def _card_viewer(request: Request) -> tuple[str, str]:
    """(person id, role) whose cards a request sees; a household device sees the household's."""
    ident = _identity(request)
    return (ident.person_id if ident.role != "household" else "household"), ident.role


def _viewer_project_ok(ident: "_hh.Identity") -> Callable[[str], bool]:
    """May this viewer see a project (or a group they're in)? As GET /v1/items decides it (#1085); a shared screen
    sees none. The groups are read once, on first use."""
    import spaces
    pid = None if ident.role == "household" else ident.person_id
    held: dict[str, Any] = {}

    def ok(p: str) -> bool:
        if "groups" not in held:
            held["groups"] = spaces.all_groups()
        groups = held["groups"]
        pr = spaces.get_project(p)
        if pr:
            return spaces.project_visible(pr, pid, ident.role, groups)
        g = groups.get(p)
        return bool(g) and spaces.in_group(g, pid, ident.role)
    return ok


def _card_items(ident: "_hh.Identity", cards: list) -> list:
    """#1235: `item: {id, project?}` on each card about an item this viewer may see, from their own item readers, once
    per request (card_items.attach). Never on a safety card; a failure anywhere leaves it off every card."""
    import card_items
    try:
        return card_items.attach(cards, _item_readers(ident, _lib._local_now()), _viewer_project_ok(ident))
    except Exception as exc:  # noqa: BLE001 - cards without their item, never a failed list
        print(f"[cards] item links skipped: {type(exc).__name__}", file=sys.stderr)
        for c in cards:
            c.pop("item", None)
        return cards


@app.get("/v1/proactive")
async def proactive_cards(request: Request, since: float = 0.0, include_acked: bool = False,
                          as_: str | None = Query(None, alias="as")) -> JSONResponse:
    """The caller's cards since a time (?since=, ?include_acked=), plus what's running in the background. A card about
    an item the caller may see carries `item: {id, project?}` (#1235), decided for this caller now. With ?as=surface
    (#1060), the caller's deck as a ResolvedSurface (`attention`): needs a decision, cards, done today; old clients
    keep this shape."""
    import proactive
    person, role = _card_viewer(request)
    now = time.time()
    ident = _identity(request)
    dev = (ident.device or {}).get("id")   # `alert` is per device (#1039, 10-06)
    if as_ == "surface":
        import local_time
        import surfaces
        day_start = local_time.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        left_out: dict[str, int] = {}   # this viewer's own muted/snoozed counts, for the trace (§4)
        served = await asyncio.to_thread(lambda: _card_items(ident, proactive.list_cards(
            0.0, True, person=person, role=role, device_id=dev, ineligible=left_out)))
        return JSONResponse(surfaces.cards(served, person, role, now, day_start,
                                           safety_sources=frozenset(getattr(proactive, "SAFETY_SOURCES", ())),
                                           ineligible=left_out))
    cards = await asyncio.to_thread(lambda: _card_items(ident, proactive.list_cards(since, include_acked, person=person,
                                                                                    role=role, device_id=dev)))
    bg = await asyncio.to_thread(background_for, person, role)
    return JSONResponse({"cards": cards, "now": now, "background": bg})


@app.get("/v1/cards/wait")
async def cards_wait(request: Request, since: float = 0.0, timeout: float = 25.0) -> JSONResponse:
    """Long-poll: returns as soon as a card for the caller appears or changes after `since`
    (created, updated, or expired-and-gone isn't reported), or {"cards": []} after `timeout` s.
    Pass the returned `now` as the next `since`. Cards carry `item` as GET /v1/proactive's do (#1235)."""
    import proactive
    person, role = _card_viewer(request)
    ident = _identity(request)
    if ident.device:   # this phone's app is open: its cards show on screen, so no push for them
        import push
        await asyncio.to_thread(push.mark_live, ident.device["id"])
    deadline = time.monotonic() + max(1.0, min(float(timeout), 55.0))
    seen = -1.0
    while True:
        now = time.time()
        latest = await asyncio.to_thread(proactive.last_change)
        if latest > since and latest != seen:
            seen = latest
            cards = await asyncio.to_thread(lambda: _card_items(ident, proactive.list_cards(
                since, True, person=person, role=role, device_id=(ident.device or {}).get("id"))))
            if cards:
                return JSONResponse({"cards": cards, "now": now,
                                     "background": await asyncio.to_thread(background_for, person, role)})
        if _xd.SHUTTING_DOWN.is_set():
            return _stopping_response()
        if time.monotonic() >= deadline or await request.is_disconnected():
            return JSONResponse({"cards": [], "now": now,
                                 "background": await asyncio.to_thread(background_for, person, role)})
        await asyncio.sleep(1.5)


def _card_source_and_key(ident: "_hh.Identity", body: dict) -> tuple[str, Any]:
    """POST /v1/cards' source and key. A safety source (Lock Up, alarms) or a "leave-" key (time to leave) makes a push
    time-sensitive and unmutable, through Focus; only a device-less caller (the master token, Home Assistant; titan's
    own jobs call add_card directly) may set one (titan-node#1289 review: a kid's or a shared screen's card broke
    through Focus on every adult's phone). A paired device's is kept, renamed so it's an ordinary card: "device:alarm"
    (the apps show the last word, as for "trigger:…"), "device-leave-…"."""
    import proactive
    source = str(body.get("source") or _card_default_source(ident))[:40]
    key = body.get("key")
    if ident.device:
        # matched as the phone matches (jellybean#1056: trimmed, lowercased), so "Alarm" can't slip past here and be
        # loud there
        if proactive.is_safety_source(source):
            source = ("device:" + source.strip())[:40]
        if isinstance(key, str) and key.strip().lower().startswith("leave-"):
            key = "device-" + key.strip()
    return source, key


def _card_default_source(ident: "_hh.Identity") -> str:
    """POST /v1/cards with no source. From a paired device it's a person writing a note (#1286): "person" for a member,
    "note" for the admin (it was "titan", which the phone can't tell from titan's own cards; the phone shows a note's
    words on the Lock Screen, NoticePrivacy.noteSources). From the master token or any other device-less caller (Home
    Assistant automations, scripts) it stays "titan": an automation never reads as "From Zach:" on a Lock Screen."""
    if not ident.device:
        return "titan"
    return "note" if ident.role == "admin" else "person"


@app.post("/v1/cards")
async def cards_send(request: Request) -> JSONResponse:
    """Send a card to people or devices: {to, title, detail?, kind?, urgency?, actions?, key?,
    expires_in?, data?}. `data` is a JSON object (≤ 4 KB) the apps may use (e.g. event refs). `to` is "household", "adults", "admins", a person id or a list of them. The same
    `key` and `to` updates the earlier card in place."""
    import proactive
    ident = _identity(request)
    body = await request.json()
    title = str(body.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    aud, unknown = _hh.card_audience_ids(proactive.audience(body.get("to")), _hh.get_store())
    if unknown:
        return JSONResponse({"error": "unknown people", "detail": f"No one in the household called {', '.join(unknown)}."},
                            status_code=400)
    why = _hh.card_send_refusal(ident.role, ident.person_id, aud, _hh.get_store())
    if why:
        return JSONResponse({"error": "forbidden", "detail": why}, status_code=403)
    try:
        expires_in = float(body["expires_in"]) if body.get("expires_in") else None
        urgency = int(body.get("urgency", 1))
    except (TypeError, ValueError):
        return JSONResponse({"error": "urgency and expires_in must be numbers"}, status_code=400)
    source, key = _card_source_and_key(ident, body)
    card = await asyncio.to_thread(lambda: proactive.add_card(
        title, str(body.get("detail") or ""), kind=str(body.get("kind") or "info"), urgency=urgency,
        source=source, actions=body.get("actions"), to=aud, key=key, expires_in=expires_in,
        sender=ident.person["name"], data=body.get("data")))
    return JSONResponse({"card": card})


# ---------------------------------------------------------------------------
# Claude conversations (scripts/claude_chats.py): each person's own, sent by their own device and
# only ever searched for them. Kept out of the household KB.
# ---------------------------------------------------------------------------

def _claude_chats_owner(request: Request) -> str:
    ident = _identity(request)
    if ident.role == "household" or not ident.person_id:
        raise HTTPException(status_code=403, detail="Claude conversations belong to a person, not a shared device.")
    return ident.person_id


@app.post("/v1/claude-chats/sessions")
async def claude_chats_sessions(request: Request) -> JSONResponse:
    """Claude Code sessions from the caller's Mac: {"sessions": [{"session_id", "lines": [jsonl line,
    ...]}]} (at most 50). Lines of any type are accepted; only prompts and Claude's words are kept.
    A session with {"mode": "append", "from_line": N} adds lines N onward (#1140) when titan holds exactly N lines
    of it; otherwise it's left alone and the reply is 409 with {"conflicts": [{"session_id", "have": M}]}
    ("have" too when there's one), so the Mac re-sends from M. Without mode, a session is replaced as before."""
    import claude_chats as cc
    owner = _claude_chats_owner(request)
    body = await request.json()
    sessions = body.get("sessions") if isinstance(body, dict) else None
    if not isinstance(sessions, list) or len(sessions) > cc.MAX_SESSIONS:
        raise HTTPException(status_code=400, detail=f"sessions must be a list of at most {cc.MAX_SESSIONS}")
    whole = [x for x in sessions if isinstance(x, dict) and isinstance(x.get("lines"), list) and x.get("mode") != "append"]
    tails = [x for x in sessions if isinstance(x, dict) and isinstance(x.get("lines"), list) and x.get("mode") == "append"]
    for x in tails:
        if not str(x.get("session_id") or "").strip() or not isinstance(x.get("from_line"), int) \
                or isinstance(x.get("from_line"), bool) or x["from_line"] < 0:
            raise HTTPException(status_code=400, detail="an append needs session_id and from_line (an integer ≥ 0)")
    convs = [cc.condense_claude_code(str(x.get("session_id") or ""), x.get("lines") or []) for x in whole]
    out: dict = await asyncio.to_thread(cc.store, owner, "claude-code", convs)
    appended, conflicts = 0, []
    for x in tails:
        r = await asyncio.to_thread(cc.append_claude_code, owner, str(x["session_id"]), x["from_line"], x["lines"])
        if r.get("conflict"):
            conflicts.append({"session_id": str(x["session_id"]), "have": r["have"]})
        else:
            appended += 1
    if tails:
        out = {**out, "appended": appended}
    if conflicts:
        return JSONResponse({**out, "conflicts": conflicts, **({"have": conflicts[0]["have"]} if len(conflicts) == 1 else {})},
                            status_code=409)
    return JSONResponse(out)


@app.post("/v1/claude-chats/import")
async def claude_chats_import(request: Request) -> JSONResponse:
    """A claude.ai data export from the caller's own device: the zip, or its conversations.json."""
    import claude_chats as cc
    owner = _claude_chats_owner(request)
    data = await request.body()
    try:
        convs = await asyncio.to_thread(cc.parse_claude_ai_export, data)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc
    out = await asyncio.to_thread(cc.store, owner, "claude-ai", convs)
    return JSONResponse({**out, "conversations": len(convs)})


@app.get("/v1/claude-chats")
async def claude_chats_status(request: Request) -> JSONResponse:
    """How many of the caller's own conversations titan has, per source."""
    import claude_chats as cc
    return JSONResponse(await asyncio.to_thread(cc.status, _claude_chats_owner(request)))


@app.delete("/v1/claude-chats")
async def claude_chats_forget(request: Request, source: str | None = None, apply: bool = False) -> JSONResponse:
    """Remove the caller's own conversations (?source=claude-code|claude-ai; dry run unless ?apply=true)."""
    import claude_chats as cc
    try:
        return JSONResponse(await asyncio.to_thread(cc.forget, _claude_chats_owner(request), source, apply))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/todos")
async def todos_list(request: Request, include_done: bool = False) -> JSONResponse:
    """The todo lists the caller may see: {todos:[{id, title, detail, for, created_by, created_at,
    done_at, done_by}]}. ?for=claude (admin) is the list the Claude sessions read."""
    import todos
    ident = _identity(request)
    visible = todos.lists_visible(ident.person_id if ident.role != "household" else None, ident.role)
    want = request.query_params.get("for")
    lists = [w for w in visible if w == want or (want == "me" and w == ident.person_id)] if want else visible
    return JSONResponse({"todos": await asyncio.to_thread(lambda: todos.list_for(lists, include_done=include_done))})


@app.post("/v1/todos")
async def todos_add(request: Request) -> JSONResponse:
    """{title, detail?, for?: me | household | claude, project?} → the todo. Claude's list is the admin's. With a
    project (or group) the caller may file into, it's made there (#1173, a project page's Add) → {todo, project, undo}."""
    import todos
    ident = _identity(request)
    body = await request.json()
    proj = str(body.get("project") or "").strip() if isinstance(body, dict) else ""
    if proj:
        import projects
        try:
            await asyncio.to_thread(projects._target_ok, ident.person_id, ident.role, proj, None)
        except projects.Refused as exc:
            return JSONResponse({"error": exc.detail}, status_code=exc.status)
    visible = todos.lists_visible(ident.person_id if ident.role != "household" else None, ident.role)
    want = str(body.get("for") or "me").strip().lower()
    owner = ident.person_id if want == "me" and ident.role != "household" else want
    if owner not in visible:
        return JSONResponse({"error": "forbidden", "detail": "Not a list you can add to."}, status_code=403)
    try:
        t = await asyncio.to_thread(lambda: todos.add(body.get("title"), owner, detail=str(body.get("detail") or ""),
                                                      created_by=ident.person["name"], created_by_id=ident.person_id))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if proj and owner != todos.CLAUDE:
        import projects
        filed = await asyncio.to_thread(projects.file, ident.person_id, ident.role, f"todo:{t['id']}", proj)
        return JSONResponse({"todo": t, "project": proj, "undo": filed["undo"]})
    return JSONResponse({"todo": t})


@app.post("/v1/todos/{todo_id}/done")
async def todos_done(todo_id: str, request: Request) -> JSONResponse:
    """Mark a to-do done, only on a list the caller can see → {done}."""
    import todos
    ident = _identity(request)
    visible = todos.lists_visible(ident.person_id if ident.role != "household" else None, ident.role)
    if not any(t["id"] == todo_id for t in await asyncio.to_thread(lambda: todos.list_for(visible))):
        return JSONResponse({"done": False})
    return JSONResponse({"done": await asyncio.to_thread(lambda: todos.complete(todo_id, ident.person["name"]))})


@app.post("/v1/proactive/{card_id}/action")
async def proactive_action(card_id: str, request: Request) -> JSONResponse:
    """A card button was pressed in Jellybean: {"action_id": "a0"}, and for a slider card (approve_cap, jellybean#707)
    the app's own {"value": "cap:<id>:<number>"} (checked against the button's own cap). {"via": "watch"}
    (jellybean#900): the iPhone relayed a Watch tap; Lock Up won't lock from it and a limit needs its amount."""
    import proactive
    person, role = _card_viewer(request)
    body = await request.json()
    device = _identity(request).device   # Lock Up (#413) locks only from the person's own iPhone
    out = await asyncio.to_thread(lambda: proactive.card_action(card_id, str(body.get("action_id", "")), person=person,
                                                                role=role, device=device,
                                                                value=body.get("value") if isinstance(body, dict) else None,
                                                                via=body.get("via") if isinstance(body, dict) else None))
    if out.get("ok", True) is not False:
        await asyncio.to_thread(_cleared, request, card_id)
    return JSONResponse(out)


@app.get("/v1/host/summary")
async def host_summary_endpoint() -> JSONResponse:
    """Structured health for Jellybean's watchdog (ok, problems, disks, gpus, failed_units, canary, network, boot)."""
    return JSONResponse(await asyncio.to_thread(_lib.host_summary))


def _snooze_until(raw: Any) -> float:
    """`until` as epoch seconds or ISO 8601 (with a zone, or UTC)."""
    from datetime import datetime, timezone
    if isinstance(raw, bool):
        raise ValueError("until")
    if isinstance(raw, (int, float)):
        return float(raw)
    d = datetime.fromisoformat(str(raw or "").strip().replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


@app.post("/v1/proactive/{card_id}/snooze")
async def proactive_snooze(card_id: str, request: Request) -> JSONResponse:
    """Later (#1039): {"until": epoch seconds | ISO 8601}, at most 30 days out → {id, snoozed_until}. Hidden for the
    caller until then; it comes back once with snoozed_from and one push to their own devices."""
    import proactive
    person, role = _card_viewer(request)
    body = await request.json() if (await request.body()) else {}
    try:
        until = _snooze_until((body or {}).get("until"))
        out = await asyncio.to_thread(lambda: proactive.snooze(card_id, until, person=person, role=role))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad_until", "detail": "until must be a time in the next 30 days"}, status_code=400)
    if out is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    await asyncio.to_thread(_cleared, request, card_id)
    return JSONResponse(out)


@app.post("/v1/proactive/{card_id}/mute")
async def proactive_mute(card_id: str, request: Request) -> JSONResponse:
    """Stop these (#1039): the caller stops seeing cards in this card's scope (its key family, else source + kind);
    this one is dismissed for them. 403 cannot_mute for a safety card or a shared household screen."""
    import proactive
    person, role = _card_viewer(request)
    try:
        out = await asyncio.to_thread(lambda: proactive.mute(card_id, person=person, role=role))
    except PermissionError:
        return JSONResponse({"error": "cannot_mute"}, status_code=403)
    if out is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    await asyncio.to_thread(_cleared, request, card_id)
    return JSONResponse({"mute": out})


@app.get("/v1/proactive/sources")
async def proactive_sources(request: Request) -> JSONResponse:
    """The caller's card sources (#1039, 10-06): {sources: [{source, label, mode: notify|quiet|stop, can_stop,
    this_week: {shown, done, snoozed, expired, acted}}]}: every source they've had a visible card from in 30 days or
    set a mode for, with their own counts since Monday."""
    import proactive
    person, role = _card_viewer(request)
    return JSONResponse({"sources": await asyncio.to_thread(proactive.sources, person, role) if person else []})


@app.put("/v1/proactive/sources")
async def proactive_source_mode(request: Request) -> JSONResponse:
    """{source, mode: notify|quiet|stop} → {source, mode}. stop is a source-wide mute; quiet keeps the cards in the deck
    with no push, banner or peek. 403 cannot_stop for a safety source or a shared household screen."""
    import proactive
    person, role = _card_viewer(request)
    body = await request.json() if (await request.body()) else {}
    try:
        out = await asyncio.to_thread(lambda: proactive.set_source_mode(person, role, str((body or {}).get("source") or ""),
                                                                          str((body or {}).get("mode") or "")))
    except PermissionError:
        return JSONResponse({"error": "cannot_stop"}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    return JSONResponse(out)


def _own_devices(request: Request) -> tuple[list[dict[str, Any]], str, str]:
    """(the caller's own devices, person, role): a person's paired devices; a household screen only itself."""
    ident = _identity(request)
    person, role = _card_viewer(request)
    if ident.role == "household":
        dev = ident.device or {}
        return ([{"id": dev["id"], "name": dev.get("name"), "platform": dev.get("platform")}] if dev.get("id") else []), person, role
    me = next((p for p in _store().people() if p["id"] == ident.person_id), None) or {}
    return ([{"id": d["id"], "name": d.get("name"), "platform": d.get("platform")}
             for d in (me.get("devices") or []) if not d.get("revoked")]), person, role


def _delivery_body(devs: list[dict[str, Any]], person: str | None, role: str | None) -> dict[str, Any]:
    import proactive
    levels = list(proactive.HOUSEHOLD_LEVELS if role == "household" else proactive.PERSON_LEVELS)
    return {"quiet": proactive.person_quiet(person, role),
            "devices": [{**d, "level": proactive.device_level(d["id"], role), "levels": levels,
                         "quiet": proactive.device_quiet(d["id"])} for d in devs]}


@app.get("/v1/proactive/delivery")
async def proactive_delivery(request: Request) -> JSONResponse:
    """The caller's own devices and how cards may interrupt on each (#1039, 10-06): {quiet, devices: [{id, name,
    platform, level, levels, quiet}]}: all | important | off for a person's device; household | off for a household TV
    (itself only). quiet: the caller's own quiet hours ({weekdays, weekend: {start, end} | null, except_important}) or
    null; a device's quiet is null (follows its person), an override, or {"off": true}. A TV has only its own."""
    devs, person, role = _own_devices(request)
    return JSONResponse(await asyncio.to_thread(_delivery_body, devs, person, role))


@app.put("/v1/proactive/delivery")
async def proactive_set_delivery(request: Request) -> JSONResponse:
    """Any of {quiet} (the caller's own hours), {device_id, level}, {device_id, quiet} (an override; null = follow the
    person) → the full GET body, plus {id, level} when a device was named. Only the caller's own device (a TV: itself);
    404 otherwise, 400 for a level that device doesn't take or a bad quiet body. Nothing is saved unless all of it is
    valid."""
    import proactive
    devs, person, role = _own_devices(request)
    body = await request.json() if (await request.body()) else {}
    body = body if isinstance(body, dict) else {}
    did = str(body.get("device_id") or "")
    if did and did not in {d["id"] for d in devs}:
        return JSONResponse({"error": "not_found"}, status_code=404)
    want_level, want_dq, want_pq = "level" in body or (did and "quiet" not in body), did and "quiet" in body, \
        not did and "quiet" in body
    try:
        if not (want_level or want_dq or want_pq):
            raise ValueError("nothing to set")
        if want_level:
            levels = proactive.HOUSEHOLD_LEVELS if role == "household" else proactive.PERSON_LEVELS
            if str(body.get("level") or "") not in levels:
                raise ValueError(f"level must be one of {', '.join(levels)}")
        if want_dq:
            proactive.check_quiet(body["quiet"], device=True)
        if want_pq:
            if role == "household":
                raise ValueError("a household screen has no person quiet hours; set the device's")
            proactive.check_quiet(body["quiet"])
    except ValueError as exc:
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    if want_level:
        await asyncio.to_thread(proactive.set_device_level, did, person, role, str(body.get("level")))
    if want_dq:
        await asyncio.to_thread(proactive.set_device_quiet, did, person, body["quiet"])
    if want_pq:
        await asyncio.to_thread(proactive.set_person_quiet, person, role, body["quiet"])
    out = await asyncio.to_thread(_delivery_body, devs, person, role)
    if did:
        out = {**out, "id": did, "level": proactive.device_level(did, role)}
    return JSONResponse(out)


@app.get("/v1/proactive/mutes")
async def proactive_mutes(request: Request) -> JSONResponse:
    """The caller's own muted card kinds (#1039: Settings › Cards › Muted)."""
    import proactive
    person, _role = _card_viewer(request)
    return JSONResponse({"mutes": await asyncio.to_thread(proactive.mutes, person) if person else []})


@app.delete("/v1/proactive/mutes")
async def proactive_unmute(request: Request, id: str = "") -> JSONResponse:
    """Unmute one of the caller's own (?id=) → {removed}."""
    import proactive
    person, _role = _card_viewer(request)
    return JSONResponse({"removed": bool(person and id and await asyncio.to_thread(proactive.unmute, person, id))})


@app.post("/v1/proactive/{card_id}/ack")
async def proactive_ack(card_id: str, request: Request) -> JSONResponse:
    """Dismiss a card for the person dismissing it; the others it's for still see it."""
    import proactive
    person, role = _card_viewer(request)
    if not any(c["id"] == card_id for c in await asyncio.to_thread(
            lambda: proactive.list_cards(include_acked=True, person=person, role=role))):
        return JSONResponse({"acked": False})
    acked = await asyncio.to_thread(lambda: proactive.ack_card(card_id, person=person, signal="done"))
    if acked:
        await asyncio.to_thread(_cleared, request, card_id)
    return JSONResponse({"acked": acked})


# Named checks: Jellybean publishes check names, claims jobs, posts results (commands stay on the Mac).

@app.post("/v1/device-jobs/catalog")
async def device_jobs_catalog(request: Request) -> JSONResponse:
    """The Mac publishes its named checks {checks: [...]}; titan runs them by name only."""
    import proactive
    body = await request.json()
    checks = body.get("checks")
    if not isinstance(checks, list):
        raise HTTPException(status_code=400, detail="checks must be a list")
    return JSONResponse({"published": await asyncio.to_thread(proactive.set_catalog, checks)})


@app.get("/v1/device-jobs")
async def device_jobs(claim: bool = False, limit: int = 3) -> JSONResponse:
    """Recent device jobs and the catalog; ?claim=true hands up to ?limit= queued jobs to the Mac."""
    import proactive
    if claim:
        return JSONResponse({"jobs": await asyncio.to_thread(proactive.claim_jobs, max(1, min(limit, 10)))})
    return JSONResponse({"jobs": await asyncio.to_thread(proactive.recent_jobs, 20),
                         "catalog": await asyncio.to_thread(proactive.catalog)})


@app.post("/v1/device-jobs/{job_id}/result")
async def device_job_result(job_id: str, request: Request) -> JSONResponse:
    """The Mac reports a job: {ok, exit_code, output, duration_ms} → {recorded}."""
    import proactive
    b = await request.json()
    ok = await asyncio.to_thread(proactive.post_result, job_id, bool(b.get("ok")), b.get("exit_code"),
                                 str(b.get("output") or ""), b.get("duration_ms"))
    return JSONResponse({"recorded": ok})


# ---------------------------------------------------------------------------
# Nightly reflection (scripts/reflection.py): the Activity Letter and the overnight dream.
# Written on the LOCAL model only; timers write them nightly, the POST routes re-run a day.
# ---------------------------------------------------------------------------

_reflection_running = threading.Lock()


def _reflection_day(day: str | None) -> str | None:
    if not day:
        return None
    try:
        return date.fromisoformat(str(day)).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD") from exc


@app.get("/v1/letter")
async def reflection_letter(day: str | None = None) -> JSONResponse:
    """The Activity Letter for ?day= (default: the latest): {day, weekday, written_at, written_time,
    model, material_count, empty, paragraphs: [[{text, ref}]], log: [{id, time, kind, title}], ...}."""
    import reflection
    out = await asyncio.to_thread(reflection.get_letter, _reflection_day(day))
    if out is None:
        raise HTTPException(status_code=404, detail="no letter for that day")
    return JSONResponse(out)


@app.get("/v1/dream")
async def reflection_dream(day: str | None = None) -> JSONResponse:
    """The overnight dream (default: the latest): {day, written_at, written_time, empty,
    findings: [{icon, text, ref}], question, action_prompt, log, model}."""
    import reflection
    out = await asyncio.to_thread(reflection.get_dream, _reflection_day(day))
    if out is None:
        raise HTTPException(status_code=404, detail="no dream for that day")
    return JSONResponse(out)


async def _reflection_run(kind: str, request: Request) -> JSONResponse:
    import reflection
    body = await request.json() if (await request.body()) else {}
    day = _reflection_day((body or {}).get("day"))
    if not _reflection_running.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="a reflection is already being written")
    try:
        fn = reflection.write_letter if kind == "letter" else reflection.write_dream
        out = await asyncio.to_thread(fn, day)
    except Exception as exc:  # noqa: BLE001 - e.g. the local model is down
        raise HTTPException(status_code=502, detail=f"{kind} failed: {str(exc)[:200]}") from exc
    finally:
        _reflection_running.release()
    return JSONResponse(out)


@app.post("/v1/letter/run")
async def reflection_letter_run(request: Request) -> JSONResponse:
    """Write (or rewrite) a day's letter now: optional {"day": "YYYY-MM-DD"} (default today; yesterday before 05:00)."""
    return await _reflection_run("letter", request)


@app.post("/v1/dream/run")
async def reflection_dream_run(request: Request) -> JSONResponse:
    """Write (or rewrite) a dream now: optional {"day": "YYYY-MM-DD"} (default yesterday)."""
    return await _reflection_run("dream", request)


# ---------------------------------------------------------------------------
# Routines (scripts/routines.py): stored here, run as ordinary chat turns from Jellybean.
# ---------------------------------------------------------------------------

def _routine_viewer(request: Request) -> tuple[str, str]:
    """(person id, role) for routines.py: the caller (a shared device as its own person)."""
    ident = _identity(request)
    return ident.person_id, ident.role


@app.get("/v1/routines")
async def routines_list(request: Request) -> JSONResponse:
    """The routines the caller sees (routines.py: its audience)."""
    import routines
    person, role = _routine_viewer(request)
    rows = await asyncio.to_thread(lambda: routines.list_all(person=person, role=role))
    return JSONResponse({"routines": rows, "count": len(rows)})


@app.post("/v1/routines")
async def routines_save(request: Request) -> JSONResponse:
    """Create or update: {id?, name, icon?, steps: [str], schedule?: {time: "HH:MM", days?: [0-6]}, enabled?,
    audience?: "household" (default) | "private" | "adults" | [ids]}. Changing one: its owner, the
    admin, or a household device for a household routine (403 otherwise)."""
    import routines
    person, role = _routine_viewer(request)
    body = await request.json()
    try:
        r = await asyncio.to_thread(lambda: routines.save(body, person=person, role=role))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({**r, "run_prompt": routines.run_prompt(r)})


@app.delete("/v1/routines/{rid}")
async def routines_delete(rid: str, request: Request) -> JSONResponse:
    """Delete a routine (its owner or the admin; 404 if the caller can't see it)."""
    import routines
    person, role = _routine_viewer(request)
    try:
        gone = await asyncio.to_thread(lambda: routines.delete(rid, person=person, role=role))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not gone:
        raise HTTPException(status_code=404, detail="no such routine")
    return JSONResponse({"id": rid, "deleted": True})


@app.get("/v1/grants")
async def grants_list(request: Request) -> JSONResponse:
    """The caller's own active grants (grants.py, #395/#382): what a scheduled action or a task may do later
    without asking then. The admin sees everyone's with ?all=true."""
    import grants
    ident = _identity(request)
    everyone = request.query_params.get("all") == "true" and ident.role == "admin"
    rows = await asyncio.to_thread(grants.list_for, None if everyone else ident.person_id)
    return JSONResponse({"grants": rows, "count": len(rows)})


@app.post("/v1/grants")
async def grants_register(request: Request) -> JSONResponse:
    """A device registers a grant its person approved on it ("Approve for 4:30"): {subject: {kind, ref}, effect,
    tool, args, not_before, expires_at, uses}. Only a paired person's own device; never a shared one. The
    device keeps its own record and is the authority at run time; titan's copy is for listing, checking and
    revoking."""
    import grants
    ident = _identity(request)
    if not ident.device:
        raise HTTPException(status_code=403, detail="Only a person's own device registers a grant it approved.")
    body = await request.json()
    plat = str((ident.device or {}).get("platform") or "").lower()
    issued_by = f"{'iphone' if plat in ('ios', 'iphone', 'ipados') else 'mac'}:{ident.device.get('id')}"
    subj = body.get("subject") or {}
    tool = str(body.get("tool") or "")
    deadline = None
    if subj.get("kind") == "scheduled":
        # Only one of the caller's own pending actions, and its own tool; anything else is "not found", so a
        # grant can't probe for others' actions.
        import scheduled
        row = await asyncio.to_thread(scheduled.get, str(subj.get("ref") or ""))
        if not row or row.get("owner") != ident.person_id or row.get("state") != "scheduled" or row.get("tool") != tool:
            raise HTTPException(status_code=404, detail="no such pending action of yours")
        deadline = (float(row["run_at"]) + scheduled.LATE_S) if row.get("run_at") else row.get("expires_at")
    try:
        rec = await asyncio.to_thread(
            grants.issue, ident.person_id, issued_by, (str(subj.get("kind") or ""), str(subj.get("ref") or "")),
            str(body.get("effect") or ""), [tool],
            args=(tool, body.get("args") or {}) if body.get("args") is not None else None,
            not_before=body.get("not_before"), expires_at=float(body.get("expires_at") or 0),
            uses=int(body.get("uses") or 1), issuer_role=ident.role, issuer_person=ident.person_id,
            subject_deadline=deadline, classify=_lib.effect_class)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(rec)


@app.delete("/v1/grants/{grant_id}")
async def grants_revoke(grant_id: str, request: Request) -> JSONResponse:
    """Revoke a grant (its owner, or the admin) and cancel what holds it, so nothing runs later on a stale
    card instead."""
    import grants
    import scheduled
    ident = _identity(request)

    def holder(g: dict) -> None:
        if g["subject_kind"] == "scheduled":
            try:
                scheduled.cancel(g["subject_ref"], ident.person_id, ident.role)
            except (LookupError, PermissionError, ValueError):
                pass   # already ran, or gone
    ok = await asyncio.to_thread(grants.revoke, grant_id, None if ident.role == "admin" else ident.person_id,
                                 cancel_holder=holder)
    if not ok:
        raise HTTPException(status_code=404, detail="no such active grant of yours")
    return JSONResponse({"id": grant_id, "revoked": True})


@app.get("/v1/scheduled")
async def scheduled_list(request: Request, include_done: bool = False) -> JSONResponse:
    """The caller's own scheduled actions (scheduled.py), soonest first: {scheduled: [{id, what, tool,
    args, run_at (null for a place), trigger: {place, on} | null, when ("today at 11:00 PM", "when you get
    home"), expires_at, state, result, card_key, ...}], count}. ?include_done=true adds the ones that
    ran, failed or were cancelled in the last day."""
    import scheduled
    ident = _identity(request)
    rows = await asyncio.to_thread(lambda: scheduled.list_for(ident.person_id, ident.role, include_done=include_done))
    return JSONResponse({"scheduled": [{**r, "when": scheduled.row_when(r)} for r in rows],
                         "count": len(rows)})


@app.delete("/v1/scheduled/{sid}")
async def scheduled_cancel(sid: str, request: Request) -> JSONResponse:
    """Cancel a pending scheduled action: its owner or the admin. 404 none the caller sees, 403 not
    theirs, 409 it already ran or is running. Its card says it was cancelled."""
    import scheduled
    ident = _identity(request)
    try:
        r = await asyncio.to_thread(lambda: scheduled.cancel(sid, ident.person_id, ident.role))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"id": r["id"], "cancelled": True, "what": r["what"]})


@app.get("/v1/routines/{rid}")
async def routines_get(rid: str, request: Request) -> JSONResponse:
    """One routine with the prompt it runs (404 if the caller can't see it)."""
    import routines
    person, role = _routine_viewer(request)
    r = await asyncio.to_thread(lambda: routines.get(rid, person=person, role=role))
    if not r:
        raise HTTPException(status_code=404, detail="no such routine")
    return JSONResponse({**r, "run_prompt": routines.run_prompt(r)})


@app.post("/v1/tools/{name}")
async def run_tool(name: str, request: Request) -> JSONResponse:
    """Run one agent tool directly: {"args": {...}} or {"argv": ["key", "value", ...]} (the CLI's
    `titan agent tool <name> key value` form). Returns {"result": "<tool output>"}."""
    body = await request.json() if (await request.body()) else {}
    if isinstance(body.get("args"), dict):
        cli_args = [json.dumps(body["args"])]
    else:
        cli_args = [str(a) for a in (body.get("argv") or [])]
    try:
        out = await asyncio.to_thread(_lib_cli, "tool_cli", name, *cli_args, timeout=float(body.get("timeout", 200)))
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail=f"tool {name!r} timed out") from exc
    return JSONResponse({"tool": name, "result": out})


# ---------------------------------------------------------------------------
# Memory (cross-surface: shared with the ai-workspace-ide agent)
#
# Exposes the L0-L3 pyramid over HTTP so other surfaces can recall from and
# contribute to the same memory. `conv_id` omitted on recall = search across all
# conversations (the global, cross-surface view).
#
# Every route works on the asking person's own memory only (household_identity): the master
# token and the ASE are Zach; a paired device is its person. Shared household devices have no
# personal memory (the middleware refuses these routes for them).
# ---------------------------------------------------------------------------

@app.post("/v1/memory/recall")
async def memory_recall(request: Request) -> JSONResponse:
    """{query, conv_id?} → memory passages for it (all conversations without conv_id)."""
    if not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=503, detail="memory unavailable")
    body = await request.json()
    query = str(body.get("query") or "").strip()
    conv_id = body.get("conv_id")  # None → recall across all conversations
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    try:
        mem = _get_memory()
        result = await asyncio.to_thread(lambda: mem.recall(query, conv_id, person_id=_identity(request).person_id))
        return JSONResponse({
            "prependContext": result.prependContext,
            "appendSystemContext": result.appendSystemContext,
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/memory/facts")
async def memory_add_fact(request: Request) -> JSONResponse:
    """Save a fact {text, conv_id?, confidence?} in the caller's own memory. 403 when the caller has turned memory off
    (or the house has, #1690): nothing is stored."""
    if not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=503, detail="memory unavailable")
    body = await request.json()
    ident = _identity(request)
    ok = await asyncio.to_thread(_memory_writes, ident.person_id)
    if not ok.run:
        raise HTTPException(status_code=403, detail=ok.message)
    text = str(body.get("text") or "").strip()
    conv_id = str(body.get("conv_id") or ("ide-shared" if ident.role == "admin" else _hh.conv_id_for(ident.person_id))).strip()
    try:
        confidence = float(body.get("confidence") or 1.0)
    except (TypeError, ValueError):
        confidence = 1.0
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    try:
        mem = _get_memory()
        await asyncio.to_thread(lambda: mem.add_fact(conv_id, text, confidence, person_id=ident.person_id))
        return JSONResponse({"stored": True, "conv_id": conv_id}, status_code=201)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/memory/facts")
async def memory_list_facts(request: Request) -> JSONResponse:
    """The caller's remembered facts (?conv_id=, ?limit=, default 200; ?sort=pinned|use|new|old, ?unused_since=<epoch>,
    #1072; ?group=, ?source=said|chat, #1073)."""
    if not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=503, detail="memory unavailable")
    conv_id = request.query_params.get("conv_id")
    try:
        limit = int(request.query_params.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    pid = _identity(request).person_id
    try:
        mem = _get_memory()
        sort = request.query_params.get("sort") or "pinned"
        unused = request.query_params.get("unused_since")
        if sort not in ("pinned", "use", "new", "old"):
            return JSONResponse({"error": "sort is pinned, use, new or old"}, status_code=400)
        try:
            unused_since = float(unused) if unused not in (None, "") else None
        except ValueError:
            return JSONResponse({"error": "unused_since is an epoch time"}, status_code=400)
        if request.query_params.get("source") not in (None, "", "said", "chat"):
            return JSONResponse({"error": "source is said or chat"}, status_code=400)
        if (request.query_params.get("detail") in ("1", "true") or sort != "pinned" or unused_since is not None
                or request.query_params.get("group") or request.query_params.get("source")):
            # [{id, text, ts, confidence, pinned, last_used, use_count}] — Jellybean's mind view pins and forgets by id;
            # #1072: sorted by use, or the ones no real turn has used since `unused_since`. `fading_since`: when use
            # tracking began, so a fading list is only meaningful after it.
            grp, src = request.query_params.get("group") or None, request.query_params.get("source") or None
            rows = await asyncio.to_thread(lambda: mem.list_facts(limit, conv_id, person_id=pid, sort=sort,
                                                                  unused_since=unused_since, group=grp, source=src))
            return JSONResponse({"facts": rows, "count": len(rows), "fading_since": mem.usage_since()})
        facts = await asyncio.to_thread(lambda: mem.get_facts(conv_id, limit, person_id=pid))
        return JSONResponse({"facts": facts, "count": len(facts)})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/memory/groups")
async def memory_groups_list(request: Request) -> JSONResponse:
    """The caller's remembered facts by group (#1073): [{group, label, count, examples, last_used}], most recently
    used first. Only ever the caller's own."""
    if not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=503, detail="memory unavailable")
    import memory_groups
    pid = _identity(request).person_id
    people = await asyncio.to_thread(_store().people)
    rows = await asyncio.to_thread(lambda: _get_memory().groups(person_id=pid))
    return JSONResponse({"groups": [{**g, "label": memory_groups.label(g["group"], people)} for g in rows],
                         "count": sum(g["count"] for g in rows)})


@app.get("/v1/memory/review")
async def memory_review_list(request: Request) -> JSONResponse:
    """Settings › Assistant › What it remembers › Needs a look (#1074): the caller's own queue (at most 5): disagree
    {a, b}, duplicate {a, b}, fading {count, oldest}, each with its choices."""
    if not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=503, detail="memory unavailable")
    import memory_review
    ident = _identity(request)
    try:
        items_ = await asyncio.to_thread(memory_review.queue, _get_memory(), ident.person_id, ident.role)
    except memory_review.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    return JSONResponse({"items": items_, "count": len(items_)})


@app.post("/v1/memory/review/{rid}")
async def memory_review_resolve(rid: str, request: Request) -> JSONResponse:
    """{choice}: disagree → still_true | not_any_more | forget_both; duplicate → merge | keep_both; fading → review |
    forget_all. Forgetting is restorable: the reply's undo token goes to POST /v1/memory/restore. A choice that writes
    (a wish to dev notes, a rewrite) is 403 while the caller's memory is off (#1690); forgetting always works."""
    if not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=503, detail="memory unavailable")
    import memory_review
    ident = _identity(request)
    body = await _json_body(request)
    choice = str((body or {}).get("choice") or "") if isinstance(body, dict) else ""
    try:
        writes = (await asyncio.to_thread(_memory_writes, ident.person_id)).run
        got = await asyncio.to_thread(lambda: memory_review.resolve(_get_memory(), ident.person_id, ident.role, rid, choice,
                                                                    writes=writes))
    except memory_review.Refused as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    return JSONResponse(got)


@app.post("/v1/memory/forget")
async def memory_forget_batch(request: Request) -> JSONResponse:
    """{group} or {conv_id}: forget a whole group, or everything learned from one chat, as one batch (#1073) →
    {token, count}. POST /v1/memory/restore {token} brings the batch back exactly. Only the caller's own facts."""
    if not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=503, detail="memory unavailable")
    body = await _json_body(request)
    body = body if isinstance(body, dict) else {}
    group, conv = str(body.get("group") or "").strip() or None, str(body.get("conv_id") or "").strip() or None
    if bool(group) == bool(conv):
        return JSONResponse({"error": "send group or conv_id, one of them"}, status_code=400)
    pid = _identity(request).person_id
    return JSONResponse(await asyncio.to_thread(lambda: _get_memory().forget_batch(person_id=pid, group=group, conv_id=conv)))


@app.post("/v1/memory/restore")
async def memory_restore_batch(request: Request) -> JSONResponse:
    """{token}: undo a bulk forget (#1073) → {restored}. Only the caller's own batch (another's token restores 0)."""
    if not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=503, detail="memory unavailable")
    body = await _json_body(request)
    token = str((body or {}).get("token") or "").strip() if isinstance(body, dict) else ""
    if not token.startswith("fb-"):
        return JSONResponse({"error": "token is required"}, status_code=400)
    pid = _identity(request).person_id
    return JSONResponse({"restored": await asyncio.to_thread(lambda: _get_memory().restore_batch(token, person_id=pid))})


@app.patch("/v1/memory/facts/{fact_id}")
async def memory_pin_fact(fact_id: str, request: Request) -> JSONResponse:
    """{"pinned": true|false} pins (a pinned fact is in every turn's recall); {"text"} rewrites the caller's own fact in place.
    A rewrite keeps the fact's id, group and pin and says it in the person's words (#1605); the reply's `previous` is
    the old wording, so Undo is the same PATCH back. 404 for a fact that isn't the caller's; 403 for a rewrite while the
    caller's memory is off (#1690; a pin is still theirs to set)."""
    if not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=503, detail="memory unavailable")
    body = await _json_body(request)
    body = body if isinstance(body, dict) else {}
    has_pin, has_text = isinstance(body.get("pinned"), bool), "text" in body
    if not has_pin and not has_text:
        raise HTTPException(status_code=400, detail="pinned (true/false) or text is required")
    pid = _identity(request).person_id
    mem = _get_memory()
    out: dict[str, Any] = {"id": fact_id}
    if has_text:
        text = body.get("text")
        if not isinstance(text, str) or not (3 <= len(" ".join(text.split())) <= 500):
            raise HTTPException(status_code=400, detail="text is 3 to 500 characters")
        ok = await asyncio.to_thread(_memory_writes, pid)
        if not ok.run:
            raise HTTPException(status_code=403, detail=ok.message)
        got = await asyncio.to_thread(lambda: mem.edit_fact(fact_id, text, person_id=pid))
        if not got:
            raise HTTPException(status_code=404, detail="no such fact")
        out.update(text=got["text"], previous=got["previous"])
    if has_pin:
        if not await asyncio.to_thread(lambda: mem.set_pinned(fact_id, body["pinned"], person_id=pid)):
            raise HTTPException(status_code=404, detail="no such fact")
        out["pinned"] = body["pinned"]
    return JSONResponse(out)


@app.get("/v1/memory/dev_notes")
async def memory_dev_notes(request: Request) -> JSONResponse:
    """The caller's own dev notes (#1605): wishes about titan read out of their chats, never kept as facts about them
    and never recalled into a turn. [{id, text, ts}], newest first (?limit=, default 200). Only ever the caller's own."""
    if not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=503, detail="memory unavailable")
    try:
        limit = max(1, min(int(request.query_params.get("limit") or 200), 1000))
    except (TypeError, ValueError):
        limit = 200
    pid = _identity(request).person_id
    rows = await asyncio.to_thread(lambda: _get_memory().dev_notes(person_id=pid, limit=limit))
    return JSONResponse({"notes": rows, "count": len(rows)})


@app.delete("/v1/memory/facts/{fact_id}")
async def memory_forget_fact(fact_id: str, request: Request) -> JSONResponse:
    """Forget a fact: it leaves recall and moves to forgotten_facts (restorable)."""
    if not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=503, detail="memory unavailable")
    if not await asyncio.to_thread(lambda: _get_memory().forget_fact(fact_id, person_id=_identity(request).person_id)):
        raise HTTPException(status_code=404, detail="no such fact")
    return JSONResponse({"id": fact_id, "forgotten": True})


@app.post("/v1/memory/facts/{fact_id}/restore")
async def memory_restore_fact(fact_id: str, request: Request) -> JSONResponse:
    """Bring back a forgotten fact (the caller's own; 404 if none)."""
    if not _MEMORY_AVAILABLE:
        raise HTTPException(status_code=503, detail="memory unavailable")
    if not await asyncio.to_thread(lambda: _get_memory().restore_fact(fact_id, person_id=_identity(request).person_id)):
        raise HTTPException(status_code=404, detail="no forgotten fact with that id")
    return JSONResponse({"id": fact_id, "restored": True})


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

@app.post("/v1/workspaces")
async def create_workspace(request: Request) -> JSONResponse:
    body = await request.json()
    driver = str(body.get("driver") or "worktree").strip().lower()
    ws_id = _safe_id(body.get("id") or uuid.uuid4().hex[:12])
    if ws_id in _workspaces:
        return JSONResponse(_workspaces[ws_id], status_code=200)
    if driver != "worktree":
        return JSONResponse({"error": "the only workspace driver is worktree (e2b and cube were removed, #1624)"},
                            status_code=400)
    meta = await _create_worktree_workspace(ws_id)
    return JSONResponse(meta, status_code=201)


@app.get("/v1/workspaces/{ws_id}")
async def get_workspace(ws_id: str) -> JSONResponse:
    meta = _workspaces.get(ws_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"workspace {ws_id!r} not found")
    procs = [{k2: v2 for k2, v2 in v.items() if not k2.startswith("_")}
             for k, v in _processes.items() if k.startswith(f"{ws_id}:")]
    return JSONResponse({**meta, "processes": procs})


@app.delete("/v1/workspaces/{ws_id}")
async def discard_workspace(ws_id: str) -> JSONResponse:
    meta = _workspaces.pop(ws_id, None)
    if not meta:
        raise HTTPException(status_code=404, detail=f"workspace {ws_id!r} not found")
    _stop_all_processes(ws_id)
    path = Path(meta["path"])
    if path.exists():
        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
    return JSONResponse({"discarded": ws_id})


@app.post("/v1/workspaces/{ws_id}/apply")
async def apply_workspace(ws_id: str) -> JSONResponse:
    """Stop the workspace's processes and commit everything in it, in the workspace's own git repo. It merges and
    pushes nothing: the header used to say "squash-merge → main", which it never did (#1098)."""
    meta = _workspaces.get(ws_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"workspace {ws_id!r} not found")
    _stop_all_processes(ws_id)
    wp = Path(meta["path"])
    await asyncio.to_thread(_run_sync, ["git", "add", "-A"], wp)
    result = await asyncio.to_thread(_run_sync, ["git", "commit", "-m", f"AI workspace {ws_id}", "--allow-empty"], wp)
    return JSONResponse({"applied": ws_id, **result})


# ── Workspace file ops ───────────────────────────────────────────────────────

@app.get("/v1/workspaces/{ws_id}/files")
async def list_files(ws_id: str, path: str = ".") -> JSONResponse:
    meta = _workspaces.get(ws_id)
    if not meta:
        raise HTTPException(status_code=404, detail="workspace not found")
    target = _guard_path(ws_id, path)
    skip = {".git", "node_modules", ".next", "dist", "__pycache__"}
    entries = []
    for e in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name)):
        if e.name in skip:
            continue
        entries.append({"name": e.name, "is_dir": e.is_dir(), "size": e.stat().st_size if e.is_file() else None,
                         "rel_path": str(Path(path) / e.name)})
    return JSONResponse({"entries": entries})


@app.get("/v1/workspaces/{ws_id}/files/read")
async def read_file(ws_id: str, path: str) -> JSONResponse:
    meta = _workspaces.get(ws_id)
    if not meta:
        raise HTTPException(status_code=404, detail="workspace not found")
    target = _guard_path(ws_id, path)
    try:
        return JSONResponse({"content": target.read_text(encoding="utf-8", errors="replace")})
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"{path!r} not found in workspace")


@app.post("/v1/workspaces/{ws_id}/files")
async def write_file(ws_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    rel = str(body.get("path") or "").strip()
    content = str(body.get("content") or "")
    if not rel:
        raise HTTPException(status_code=400, detail="path is required")
    meta = _workspaces.get(ws_id)
    if not meta:
        raise HTTPException(status_code=404, detail="workspace not found")
    target = _guard_path(ws_id, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return JSONResponse({"written": rel, "bytes": len(content.encode())})


# ── Workspace command runner ─────────────────────────────────────────────────

_ALLOWED_COMMANDS = {
    "install":   (["pnpm", "install"], 120),
    "build":     (["pnpm", "run", "build"], 120),
    "test":      (["pnpm", "test"], 120),
    "lint":      (["pnpm", "run", "lint"], 60),
    "typecheck": (["pnpm", "exec", "tsc", "--noEmit"], 60),
    "gitStatus": (["git", "status", "--porcelain"], 10),
    "gitDiff":   (["git", "diff"], 10),
}


@app.post("/v1/workspaces/{ws_id}/run")
async def run_command(ws_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    kind = str(body.get("kind") or "").strip()
    if kind not in _ALLOWED_COMMANDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of: {', '.join(_ALLOWED_COMMANDS)}")
    meta = _workspaces.get(ws_id)
    if not meta:
        raise HTTPException(status_code=404, detail="workspace not found")
    cmd, timeout = _ALLOWED_COMMANDS[kind]
    result = await asyncio.to_thread(_run_sync, cmd, Path(meta["path"]), timeout)
    return JSONResponse(result)


# ── Workspace process management ─────────────────────────────────────────────

_ALLOWED_BINARIES = {"pnpm", "npm", "npx", "node", "python3", "python", "cargo", "go"}


PROCESS_KEEP_S = 3600   # an ended process's state (status, last logs) stays readable this long
PROCESS_CAP = 200


def _prune_processes(now: float | None = None) -> None:
    """Ended processes go after an hour, and past the cap the oldest ended ones first; a running one never."""
    now = now or time.time()
    ended = [(v.get("ended_at") or v.get("started_at") or 0, k) for k, v in list(_processes.items())
             if v.get("status") in ("exited", "failed", "killed")]
    for at, k in ended:
        if now - at > PROCESS_KEEP_S:
            _processes.pop(k, None)
    over = len(_processes) - PROCESS_CAP
    for _, k in sorted(ended)[:max(0, over)]:
        _processes.pop(k, None)


def _stop_all_processes(ws_id: str) -> None:
    for key in list(_processes.keys()):
        if key.startswith(f"{ws_id}:"):
            proc = _processes[key].get("_proc")
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
            _processes[key]["status"] = "killed"
            _processes[key]["ended_at"] = time.time()


@app.post("/v1/workspaces/{ws_id}/processes")
async def start_process(ws_id: str, request: Request) -> JSONResponse:
    """Start a process in a workspace. Off unless AGENT_WORKSPACE_PROCESSES=1 (10-04, Box's finding): it runs
    node/python/cargo… with any arguments, unsandboxed and with no time limit, as the agent's user, in the API
    server's own directory for non-worktree drivers. No caller uses it (ASE runs its own processes; zero calls in
    the 30-day logs), so it stays off until it's sandboxed."""
    if os.environ.get("AGENT_WORKSPACE_PROCESSES", "") != "1":
        raise HTTPException(status_code=403, detail="starting workspace processes is turned off on this titan "
                                                    "(unsandboxed; AGENT_WORKSPACE_PROCESSES)")
    body = await request.json()
    proc_id = str(body.get("process_id") or uuid.uuid4().hex[:8])
    command = str(body.get("command") or "").strip()
    args = [str(a) for a in (body.get("args") or [])]
    if command not in _ALLOWED_BINARIES:
        raise HTTPException(status_code=400, detail=f"command must be one of: {', '.join(sorted(_ALLOWED_BINARIES))}")
    meta = _workspaces.get(ws_id)
    if not meta:
        raise HTTPException(status_code=404, detail="workspace not found")
    key = f"{ws_id}:{proc_id}"
    _prune_processes()
    state: dict[str, Any] = {
        "process_id": proc_id, "ws_id": ws_id, "command": command, "args": args,
        "status": "running", "logs": [], "port": None, "started_at": time.time(),
    }
    _processes[key] = state

    def _run() -> None:
        import subprocess as sp
        cwd = meta["path"] if meta["driver"] == "worktree" else None
        proc = sp.Popen([command] + args, cwd=cwd, stdout=sp.PIPE, stderr=sp.STDOUT, text=True)
        state["_proc"] = proc
        with proc.stdout or contextlib.nullcontext([]) as out:   # the pipe closed when the process ends
            for line in out:
                line = line.rstrip()
                state["logs"].append(line)
                if len(state["logs"]) > 1000:
                    state["logs"].pop(0)
                m = re.search(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):([0-9]+)|port\s+([0-9]+)", line, re.I)
                if m:
                    state["port"] = int(m.group(1) or m.group(2))
        proc.wait()
        state["status"] = "exited" if proc.returncode == 0 else "failed"
        state["ended_at"] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"process_id": proc_id, "status": "running"}, status_code=201)


@app.delete("/v1/workspaces/{ws_id}/processes/{proc_id}")
async def stop_process(ws_id: str, proc_id: str) -> JSONResponse:
    """Stop a workspace process (SIGTERM); its status becomes killed."""
    key = f"{ws_id}:{proc_id}"
    state = _processes.get(key)
    if not state:
        raise HTTPException(status_code=404, detail="process not found")
    proc = state.get("_proc")
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    state["status"] = "killed"
    return JSONResponse({"stopped": proc_id})


# ---------------------------------------------------------------------------
# Device bridge reply
# ---------------------------------------------------------------------------

@app.post("/v1/device/result/{turn_id}")
@app.post("/v1/device/reply/{turn_id}")
async def device_result(turn_id: str, request: Request) -> JSONResponse:
    """The app's outcome for the pending device action: {request_id, result} or
    {request_id, error}. `approved: false` (the old reply shape) counts as declined."""
    body = await request.json()
    with _turns_lock:
        turn = _turns.get(turn_id)
        if not turn or not _owns_turn(request, turn):
            raise HTTPException(status_code=404, detail=f"turn {turn_id!r} not found")
        pending = turn.get("_device_req")
        if turn.get("state") != "device_pending" or not pending:
            raise HTTPException(status_code=409, detail=f"turn state is {turn.get('state')!r}, not device_pending")
        rid = body.get("request_id")
        if rid and rid != pending.get("request_id"):
            raise HTTPException(status_code=409, detail="stale request_id")
        if "result" in body or "error" in body:
            outcome = {k: body[k] for k in ("result", "error") if k in body}
        elif body.get("approved") is False:
            outcome = {"error": "Declined by the user."}
        else:
            outcome = {"result": str(body.get("approved", "Done."))}
        asked = body.get("asked_ms")   # how long the app's own question was up (#619); older apps omit it
        if isinstance(asked, (int, float)) and not isinstance(asked, bool) and asked >= 0:
            outcome["asked_ms"] = int(min(asked, DEVICE_TIMEOUT_SECONDS * 1000))
        turn["device_result"] = outcome
        evt = turn.get("device_event")
    if evt:
        evt.set()
    return JSONResponse({"turn_id": turn_id, "accepted": True})


def _owns_turn(request: Request, turn: dict) -> bool:
    """Admin sees every turn; anyone else only the turns their own token started."""
    ident = _identity(request)
    owner = turn.get("identity")
    return ident.role == "admin" or (owner is not None and owner.person_id == ident.person_id
                                     and owner.device == ident.device)


# ---------------------------------------------------------------------------
# Cross-device: the Mac's channel, its presence, and the phone's confirmations
# ---------------------------------------------------------------------------

_inbox_known: set[tuple[str, str]] = set()   # Macs whose presence row exists (skip a write per poll)
INBOX_KNOWN_MAX = 1024   # it only saves a write: past this many (devices come and go), start over


def _inbox_note(who: tuple[str, str]) -> None:
    if len(_inbox_known) >= INBOX_KNOWN_MAX:
        _inbox_known.clear()
    _inbox_known.add(who)


def _refuse_executor_key(ident, key: Any) -> None:
    """#1631: a person's Mac or phone can't name their executor's key (taking its inbox, or its presence row)."""
    if key and ident.via != "executor" and _store().presence_kind(ident.person_id, str(key)) == "executor":
        raise HTTPException(status_code=403, detail="that key belongs to a paired executor")


def _mac_key_from(request: Request, body: dict | None = None) -> str | None:
    ident = _identity(request)
    if ident.via == "executor":   # #1631: an executor's key is its own device id, never one it names (a Mac's)
        return (ident.device or {}).get("id")
    named = (body or {}).get("device_key") or request.query_params.get("device_key") or request.headers.get("X-Device-Key")
    _refuse_executor_key(ident, named)
    return ((body or {}).get("device_key") or request.query_params.get("device_key")
            or request.headers.get("X-Device-Key") or (ident.device or {}).get("id"))


@app.get("/v1/device/inbox")
async def device_inbox(request: Request, wait: float = 25.0) -> JSONResponse:
    """The Mac's long-poll: its queued requests at once, or {"requests": []} after `wait` s (≤ 30).
    The Mac is (person of its token, device_key); it only ever sees its own person's requests."""
    ident = _identity(request)
    try:
        key = _xd.clean_device_key(_mac_key_from(request))
    except _xd.PresenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    name = _xd.clean_name(request.query_params.get("name") or request.headers.get("X-Device-Name")
                          or (ident.device or {}).get("name"))
    phone = str((ident.device or {}).get("platform") or "") in _xd.PHONE_PLATFORMS
    if (ident.person_id, key) not in _inbox_known and not phone:   # an iPhone drains it (#776) but isn't a Mac
        await asyncio.to_thread(_store().record_presence, ident.person_id, (ident.device or {}).get("id"),
                                {"device_key": key, "name": name}, touch_only=True,
                                kind="executor" if ident.via == "executor" else "mac")
        _inbox_note((ident.person_id, key))
    items = await _xd.CHANNEL.long_poll((ident.person_id, key), wait)
    if not items and _xd.SHUTTING_DOWN.is_set():
        return _stopping_response()
    return JSONResponse({"requests": items})


@app.post("/v1/device/inbox/{request_id}/result")
async def device_inbox_result(request_id: str, request: Request) -> JSONResponse:
    """The Mac's outcome for one request: {result, status, confirm?}."""
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    status = str(body.get("status") or "").strip()
    if status not in _xd.STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(_xd.STATUSES)}")
    if body.get("confirm") is not None and not isinstance(body.get("confirm"), dict):
        raise HTTPException(status_code=400, detail="confirm must be an object")
    result = {"status": status, "result": str(body.get("result") or "")[:200_000], "confirm": body.get("confirm")}
    raw_key = body.get("device_key") or request.query_params.get("device_key") or request.headers.get("X-Device-Key")
    if _identity(request).via == "executor":
        raw_key = (_identity(request).device or {}).get("id")   # #1631: only its own requests' results
    else:
        _refuse_executor_key(_identity(request), raw_key)
    got = _xd.CHANNEL.post_result(_identity(request).person_id, str(raw_key) if raw_key else None, request_id, result)
    if got == "unknown":
        raise HTTPException(status_code=404, detail="no such request for this Mac")
    if got == "early":
        raise HTTPException(status_code=409, detail="that request hasn't been delivered")
    return JSONResponse({"id": request_id, "accepted": True})


@app.post("/v1/device/presence")
async def device_presence(request: Request) -> JSONResponse:
    """The Mac's heartbeat (every 60 s and on wake/network change): where it is and how to wake it."""
    ident = _identity(request)
    body = await _json_body(request)
    executor = ident.via == "executor"
    if executor and isinstance(body, dict):
        body = {**body, "device_key": (ident.device or {}).get("id")}   # #1631: its own key only
    elif isinstance(body, dict):
        _refuse_executor_key(ident, body.get("device_key"))
    try:
        p = _xd.clean_presence(body)
    except _xd.PresenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await asyncio.to_thread(_store().record_presence, ident.person_id, (ident.device or {}).get("id"), p,
                            kind="executor" if executor else "mac")
    _inbox_note((ident.person_id, p["device_key"]))
    if not p["awake"]:
        _xd.CHANNEL.mark_asleep((ident.person_id, p["device_key"]))   # its open long-poll is a dead connection
    return JSONResponse({"ok": True, "live": _xd.CHANNEL.live((ident.person_id, p["device_key"]))})


@app.get("/v1/device/presence")
async def device_presence_list(request: Request) -> JSONResponse:
    """The asking person's Macs: last presence, whether a long-poll is live, whether it can be woken."""
    pid = _identity(request).person_id
    macs = await asyncio.to_thread(_store().macs_for, pid)
    return JSONResponse({"macs": [{**m, "live": _xd.CHANNEL.live((pid, m["device_key"])), "wakeable": _xd.wakeable(m)}
                                  for m in macs]})


@app.post("/v1/device/confirm/{turn_id}")
async def device_confirm(turn_id: str, request: Request) -> JSONResponse:
    """The phone's answer to a `confirm` event: {confirm_id, approved, remember_session}."""
    body = await _json_body(request)
    if not isinstance(body, dict) or not body.get("confirm_id"):
        raise HTTPException(status_code=400, detail="confirm_id is required")
    ident = _identity(request)
    with _turns_lock:
        turn = _turns.get(turn_id)
        owner = (turn or {}).get("identity")
        # Only the person whose turn it is (their own device; the admin's master token is that same
        # person's) may answer: another household member can't approve an action on Zach's Mac.
        if not turn or not _owns_turn(request, turn) or owner is None or owner.person_id != ident.person_id:
            raise HTTPException(status_code=404, detail=f"turn {turn_id!r} not found")
        pending = turn.get("_confirm")
        if not pending:
            raise HTTPException(status_code=409, detail="no confirmation is waiting on this turn")
        if str(body["confirm_id"]) != pending["confirm_id"]:
            raise HTTPException(status_code=409, detail="stale confirm_id")
        pending["answer"] = {"approved": body.get("approved") is True,
                             "remember_session": body.get("remember_session") is True}
        evt = pending["event"]
    evt.set()
    return JSONResponse({"turn_id": turn_id, "accepted": True})


@app.get("/v1/turns/{turn_id}")
async def get_turn(turn_id: str, request: Request) -> JSONResponse:
    with _turns_lock:
        turn = _turns.get(turn_id)
    if not turn or not _owns_turn(request, turn):
        raise HTTPException(status_code=404, detail=f"turn {turn_id!r} not found")
    done = turn["state"] == "done"
    if _is_watch_turn(turn):   # a kid's Watch: the shaped answer only (titan-node#755)
        err = (turn.get("message") or turn.get("error")) and WATCH_ERROR
        return JSONResponse({"turn_id": turn_id, "state": turn["state"], "error": err,
                             **_watch_shaped({"text": turn.get("text", "") if done else "",
                                              "egress": list(turn.get("egress") or []) if done else []})})
    return JSONResponse({"turn_id": turn_id, "state": turn["state"],
                         "text": turn.get("text", ""), "error": turn.get("message") or turn.get("error"),
                         "brain": turn.get("brain"), "fallback": turn.get("fallback"),
                         "token_counts": turn.get("token_counts", {}),
                         # A phone following a dropped stream shows the answer as the stream would have (jellybean#369).
                         "truncated": bool(turn.get("truncated")),
                         "sources": list(turn.get("sources") or []) if done else [],
                         "blocks": list(turn.get("blocks") or []) if done else [],
                         **({"cites": list(turn["cites"])} if done and turn.get("cites") else {}),   # jellybean#636
                         "egress": list(turn.get("egress") or []) if done else []})


@app.post("/v1/turns/{turn_id}/cancel")
async def cancel_turn(turn_id: str, request: Request):
    """Stop (jellybean#369): the one real cancel. The turn's owner (or the admin) only; 404 for a turn that isn't
    theirs or is gone. A running turn becomes cancelled: no more tools run, a pending device request or
    confirmation is released, and nothing of it is remembered. A finished turn is left as it is (204 too)."""
    from fastapi.responses import Response
    with _turns_lock:
        turn = _turns.get(turn_id)
        if not turn or not _owns_turn(request, turn):
            raise HTTPException(status_code=404, detail=f"turn {turn_id!r} not found")
        if turn.get("state") in ("running", "device_pending"):
            turn.update({"state": "cancelled", "finished_at": time.time()})
            for evt in (turn.get("device_event"), (turn.get("_confirm") or {}).get("event")):
                if evt is not None:
                    evt.set()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Prompt (non-streaming)
# ---------------------------------------------------------------------------

def _eval_about(body: dict, source: str | None) -> "dict | None":
    """An eval turn's own Ask-about thing (`eval_about: {kind, title, text}`), so the privacy red team can put an attack
    inside one (#1328): honoured on eval turns only, never looked up, never stored. A real turn's `about` is always
    resolved for its asker (about_ref)."""
    raw = body.get("eval_about") if source == "eval" and isinstance(body, dict) else None
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind") if raw.get("kind") in ("document", "event") else "document"
    return {"kind": kind, "id": "eval-about", "title": str(raw.get("title") or "")[:200],
            "text": str(raw.get("text") or "")[:4000], "source": "calendar" if kind == "event" else "file"}


async def _about_for(body: dict, ident: "_hh.Identity", source: str | None = None) -> "dict | JSONResponse | None":
    """#1322: the prompt's `about: {kind, id}` (a surface's Ask), resolved for this asker through the same rule the
    surface used (about_ref.py), or the error to answer instead of starting a turn. None without a reference."""
    fixture = _eval_about(body, source)
    if fixture is not None:
        return fixture
    if not isinstance(body, dict) or body.get("about") is None:
        return None
    import about_ref
    try:
        return await asyncio.to_thread(about_ref.resolve, body.get("about"), ident.person_id, ident.role,
                                       _lib._local_now().date())
    except about_ref.Refused as exc:
        if exc.status in (400, 404):   # answered before any turn, so counted here (role and kind only)
            import feature_counts
            raw = body.get("about") if isinstance(body.get("about"), dict) else {}
            feature_counts.note(f"about_{exc.status}", ident.role, raw.get("kind") if isinstance(raw.get("kind"), str) else None)
        return JSONResponse({"error": exc.detail}, status_code=exc.status)


@app.post("/v1/prompt")
async def prompt(request: Request) -> JSONResponse:
    body = await request.json()
    question, history, tools, ws_id, device = _parse_body(body)
    detail = _parse_detail(body)
    turn_id = str(uuid.uuid4())
    _init_turn(turn_id)
    _turns[turn_id]["remember"] = body.get("remember", True) is not False
    _turns[turn_id]["effort"] = _parse_effort(body)
    _turns[turn_id]["persona"] = str(body.get("persona") or "").strip().lower() or None
    _turns[turn_id]["source"] = _parse_source(body, _identity(request))
    _turns[turn_id]["eval_brain"], _turns[turn_id]["eval_tools"] = _parse_eval_pins(body, _turns[turn_id]["source"])
    _turns[turn_id]["eval_fixtures"] = _parse_eval_fixtures(body, _turns[turn_id]["source"])
    _turns[turn_id]["eval_world"] = _parse_eval_world(body, _turns[turn_id]["source"])
    _turns[turn_id]["eval_brief"] = (_turns[turn_id]["source"] == "eval" and body.get("eval_brief") is True) or None
    _turns[turn_id]["eval_local_where"] = ((_turns[turn_id]["source"] == "eval" and body.get("eval_local_where") == "short")
                                           and "short") or None   # #627's arm, eval turns only
    # #676's measurement: an eval turn may stream its reply while AGENT_STREAM_TOKENS is off for everyone else.
    _turns[turn_id]["eval_stream"] = (_turns[turn_id]["source"] == "eval" and body.get("stream_tokens") is True) or None
    _turns[turn_id]["eval_cites"] = (_turns[turn_id]["source"] == "eval" and body.get("eval_cites") is True) or None
    _turns[turn_id]["client"] = _parse_client(body)
    _turns[turn_id]["device_tools"] = _parse_device_tools(body)
    _turns[turn_id]["session"] = str(body.get("session_id") or "").strip()[:80] or None
    _turns[turn_id]["thread"] = str(body.get("thread_id") or "").strip()[:80] or None   # the app's conversation (#117)
    _turns[turn_id]["file_under"] = {k: body.get(k) for k in ("project", "group") if body.get(k)} or None   # #240
    _turns[turn_id]["branched_from"] = body.get("branched_from")   # #895: a branch's first turn (checked when kept)
    _turns[turn_id]["client_msg_id"] = str(body.get("client_msg_id") or "").strip()[:80] or None   # its echo is skipped (#138)
    try:
        _turns[turn_id]["identity"] = ident = _eval_as(body, _turns[turn_id]["source"], _identity(request))
    except EvalAsRefused as exc:
        _turns.pop(turn_id, None)   # never left behind as a turn in flight
        raise HTTPException(status_code=403, detail=f"eval_as refused: {exc}") from None
    refused = _temporary_refused(_turns[turn_id]["source"], ident)
    if refused is not None:
        _turns.pop(turn_id, None)
        return refused
    _turns[turn_id]["skill_overrides"] = _parse_skill_overrides(body, _turns[turn_id]["source"], ident)
    if _turns[turn_id]["source"] != "eval":
        await asyncio.to_thread(_note_active, ident)   # busy here: other phones' card pushes land quietly (#204)
    _note_more_asked(body, _turns[turn_id], ident)
    _turns[turn_id]["eval_mac"] = _parse_eval_mac(body, _turns[turn_id]["source"])
    _turns[turn_id]["eval_device"] = _parse_eval_device(body, _turns[turn_id]["source"])
    _turns[turn_id]["eval_prior"], _turns[turn_id]["eval_continue"], _turns[turn_id]["eval_signal"] = \
        _parse_eval_continue(body, _turns[turn_id]["source"])
    _turns[turn_id]["eval_item"] = (body.get("eval_item") if _turns[turn_id]["source"] == "eval"
                                    and isinstance(body.get("eval_item"), dict) else None)   # #446: the fixture item
    about = await _about_for(body, ident, _turns[turn_id]["source"])   # #1322: resolved for this asker, before any turn
    if isinstance(about, JSONResponse):
        _turns.pop(turn_id, None)
        return about
    _turns[turn_id]["about"] = about
    if ident.role != "admin":
        ws_id = None   # workspaces are the admin's (the route itself is admin-only)
    try:
        text = await asyncio.get_running_loop().run_in_executor(
            _TURN_POOL, _turn_call(turn_id, question, history, tools, ws_id, device, detail, _device_policy(body)))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_lib.plain_failure(exc)) from exc
    turn = _turns.get(turn_id, {})
    return JSONResponse({"turn_id": turn_id, "text": text, "truncated": bool(turn.get("truncated")),
                         "sources": turn.get("sources") or [], "egress": list(turn.get("egress") or []),
                         "blocks": turn.get("blocks") or [], "trace": turn.get("trace"),
                         **({"cites": turn["cites"]} if turn.get("cites") else {}),   # jellybean#636
                         "brain": turn.get("brain"), "fallback": turn.get("fallback"),
                         **({"peek": turn["peek"]} if turn.get("peek") else {})})   # jellybean#426 (older apps ignore it)


# ---------------------------------------------------------------------------
# Prompt streaming (SSE)
# ---------------------------------------------------------------------------

@app.post("/v1/prompt/stream")
async def prompt_stream(request: Request) -> StreamingResponse:
    body = await request.json()
    question, history, tools, ws_id, device = _parse_body(body)
    detail = _parse_detail(body)
    # The same message again within 10 minutes (Retry after a dropped stream): its turn, not a second one.
    try:
        as_ident = _eval_as(body, _parse_source(body, _identity(request)), _identity(request))
    except EvalAsRefused as exc:
        raise HTTPException(status_code=403, detail=f"eval_as refused: {exc}") from None
    again = _attached_turn(as_ident,
                           str(body.get("client_msg_id") or "").strip()[:80] or None)
    if again:
        return StreamingResponse(_sse_follow(again), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Turn-ID": again})
    turn_id = str(uuid.uuid4())
    _init_turn(turn_id)
    _turns[turn_id]["remember"] = body.get("remember", True) is not False
    _turns[turn_id]["effort"] = _parse_effort(body)
    _turns[turn_id]["persona"] = str(body.get("persona") or "").strip().lower() or None
    _turns[turn_id]["source"] = _parse_source(body, _identity(request))
    _turns[turn_id]["eval_brain"], _turns[turn_id]["eval_tools"] = _parse_eval_pins(body, _turns[turn_id]["source"])
    _turns[turn_id]["eval_fixtures"] = _parse_eval_fixtures(body, _turns[turn_id]["source"])
    _turns[turn_id]["eval_world"] = _parse_eval_world(body, _turns[turn_id]["source"])
    _turns[turn_id]["eval_brief"] = (_turns[turn_id]["source"] == "eval" and body.get("eval_brief") is True) or None
    _turns[turn_id]["eval_local_where"] = ((_turns[turn_id]["source"] == "eval" and body.get("eval_local_where") == "short")
                                           and "short") or None   # #627's arm, eval turns only
    # #676's measurement: an eval turn may stream its reply while AGENT_STREAM_TOKENS is off for everyone else.
    _turns[turn_id]["eval_stream"] = (_turns[turn_id]["source"] == "eval" and body.get("stream_tokens") is True) or None
    _turns[turn_id]["eval_cites"] = (_turns[turn_id]["source"] == "eval" and body.get("eval_cites") is True) or None
    _turns[turn_id]["client"] = _parse_client(body)
    _turns[turn_id]["device_tools"] = _parse_device_tools(body)
    _turns[turn_id]["session"] = str(body.get("session_id") or "").strip()[:80] or None
    _turns[turn_id]["thread"] = str(body.get("thread_id") or "").strip()[:80] or None   # the app's conversation (#117)
    _turns[turn_id]["file_under"] = {k: body.get(k) for k in ("project", "group") if body.get(k)} or None   # #240
    _turns[turn_id]["branched_from"] = body.get("branched_from")   # #895: a branch's first turn (checked when kept)
    _turns[turn_id]["client_msg_id"] = str(body.get("client_msg_id") or "").strip()[:80] or None   # its echo is skipped (#138)
    try:
        _turns[turn_id]["identity"] = ident = _eval_as(body, _turns[turn_id]["source"], _identity(request))
    except EvalAsRefused as exc:
        _turns.pop(turn_id, None)   # never left behind as a turn in flight
        raise HTTPException(status_code=403, detail=f"eval_as refused: {exc}") from None
    refused = _temporary_refused(_turns[turn_id]["source"], ident)
    if refused is not None:
        _turns.pop(turn_id, None)
        return refused
    _turns[turn_id]["skill_overrides"] = _parse_skill_overrides(body, _turns[turn_id]["source"], ident)
    if _turns[turn_id]["source"] != "eval":
        await asyncio.to_thread(_note_active, ident)   # busy here: other phones' card pushes land quietly (#204)
    _note_more_asked(body, _turns[turn_id], ident)
    _turns[turn_id]["eval_mac"] = _parse_eval_mac(body, _turns[turn_id]["source"])
    _turns[turn_id]["eval_device"] = _parse_eval_device(body, _turns[turn_id]["source"])
    _turns[turn_id]["eval_prior"], _turns[turn_id]["eval_continue"], _turns[turn_id]["eval_signal"] = \
        _parse_eval_continue(body, _turns[turn_id]["source"])
    _turns[turn_id]["eval_item"] = (body.get("eval_item") if _turns[turn_id]["source"] == "eval"
                                    and isinstance(body.get("eval_item"), dict) else None)   # #446: the fixture item
    about = await _about_for(body, ident, _turns[turn_id]["source"])   # #1322: resolved for this asker, before any turn
    if isinstance(about, JSONResponse):
        _turns.pop(turn_id, None)
        return about
    _turns[turn_id]["about"] = about
    if ident.role != "admin":
        ws_id = None   # workspaces are the admin's (the route itself is admin-only)
    return StreamingResponse(
        _sse_gen(turn_id, question, history, tools, ws_id, device, detail, _device_policy(body)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Turn-ID": turn_id},
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_body(body: dict) -> tuple[str, list, bool, str | None, bool]:
    question = str(body.get("prompt") or body.get("message") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="prompt is required")
    history: list = body.get("history") or []
    tools: bool = bool(body.get("tools", True))
    ws_id: str | None = body.get("workspace_id") or None
    # device=true: the caller (the Jellybean app) runs device tools for this turn; each one
    # arrives as an SSE `device` event and its outcome comes back on /v1/device/result.
    device: bool = bool(body.get("device", False)) and tools
    return question, history, tools, ws_id, device


# How much to say. The Jellybean panel sends "brief" (a small window: answer first, offer more);
# its conversation window sends "full". Absent (other clients) leaves the prompt unchanged.
_REPLY_STYLES = {
    "brief": ("[REPLY STYLE: brief] The user is reading in a small chat panel. Answer in at most "
              "3 short sentences or 5 short bullets (about 80 words). Lead with the answer; skip "
              "preamble, caveats and generic advice. If more detail would genuinely help, end with "
              "one short offer on its own line (for example \"Want the full list?\"). Never pad."),
    "full": ("[REPLY STYLE: full] The user asked for the complete answer. Be thorough but "
             "organized: short headings or bullets, no filler."),
}


# Jellybean's Settings › Assistant › Personality (the design's Concise / Balanced / Warm).
_PERSONAS = {
    "concise": "[VOICE: concise] Plain and direct. No greetings, no sign-offs, no exclamation marks.",
    "balanced": "[VOICE: balanced] Friendly and to the point.",
    "warm": "[VOICE: warm] Warm and personable, like a thoughtful housemate — a little encouragement is "
            "welcome, but never at the cost of clarity or length.",
}


_TRUST_GROUPS = {
    "screen": "screen reading, clicking and typing, messaging the Claude session",
    "changes": "shell commands, AppleScript, writing files and saving new skills",
    "automation": "running Shortcuts and saved skills",
    "calendar": "creating calendar events, reminders and alarms",
    "contacts": "creating contacts",
    "messaging": "sending messages and drafting email",
}


_TIER_TEXT = {"always": "Runs without asking", "askSession": "Asks the user once per session, then runs",
              "askAlways": "Asks the user every time", "never": "Never allowed (declined on the Mac)"}
_DOMAIN_TEXT = {
    "calendar": "calendar events", "reminders": "reminders and alarms", "contacts": "contacts",
    "filesystem": "writing files", "shell": "shell commands and AppleScript", "screenAwareness": "reading the screen",
    "uiControl": "clicking and typing in other apps", "mailDraft": "drafting email", "mailSend": "sending email",
    "messagesSend": "sending messages", "permanentDelete": "permanent deletes", "keychain": "the keychain",
    "knowledgeBase": "the knowledge base", "automation": "Shortcuts and saved skills", "skills": "saving new skills",
    "notes": "searching Apple Notes", "messagesRead": "reading messages", "location": "Find My locations",
    "context": "seeing which apps are open, the browser's address and the Finder folder", "media": "controlling Music",
    "clipboard": "copying to the clipboard", "mailScans": "reading one email for an answer", "mailArchive": "archiving mail",
    "payments": "payments", "health": "health data", "credentials": "account credentials",
}
# The Mac's trust domains (jellybean TrustModel.swift's TrustDomain, 23 since jellybean#1274): every one has words above
# (test_tool_descriptors.py checks).
MAC_TRUST_DOMAINS = ("calendar", "reminders", "contacts", "notes", "mailDraft", "messagesRead", "messagesSend",
                     "filesystem", "shell", "skills", "automation", "screenAwareness", "context", "uiControl", "location",
                     "media", "clipboard", "mailScans", "mailArchive", "keychain", "payments", "health", "credentials")


def _tier_policy(tiers: dict) -> str | None:
    """[DEVICE POLICY] from Jellybean's per-domain tiers (device_trust_tiers, 0.11+)."""
    by: dict[str, list[str]] = {}
    for dom, tier in tiers.items():
        if tier in _TIER_TEXT and isinstance(dom, str):
            by.setdefault(tier, []).append(_DOMAIN_TEXT.get(dom, dom))
    if not by:
        return None
    lines = ["[DEVICE POLICY — from Jellybean, for this turn]"]
    for tier in ("always", "askSession", "askAlways", "never"):
        if by.get(tier):
            lines.append(f"{_TIER_TEXT[tier]}: " + "; ".join(sorted(by[tier])) + ".")
    lines.append("Reads (screen, apps, files, calendar) never need approval. Don't ask the user to change "
                 "these settings unless they ask how; for something set to Never, say it's off in Trust.")
    return "\n".join(lines)


def _device_policy(body: dict) -> str | None:
    """Jellybean sends its trust settings ({"device_trust": {"automation": true, ...}}) so the model
    knows which Mac actions run without asking, instead of telling the user to fix approvals
    they already granted. Jellybean still enforces them itself; this is information only."""
    tiers = body.get("device_trust_tiers")
    if isinstance(tiers, dict) and tiers:
        pol = _tier_policy(tiers)
        if pol:
            return pol
    trust = body.get("device_trust")
    if not isinstance(trust, dict):
        return None
    if "changes" not in trust:
        # Jellybean before 0.9.1 had no separate "changes" group: shell and AppleScript followed
        # automation, file writes followed files.
        trust = {**trust, "changes": trust.get("automation") is True and trust.get("files") is True}
    free = [desc for k, desc in _TRUST_GROUPS.items() if trust.get(k) is True]
    asks = [desc for k, desc in _TRUST_GROUPS.items() if trust.get(k) is not True]
    lines = ["[DEVICE POLICY — from Jellybean, for this turn]"]
    lines.append("Runs on the Mac without asking the user: " + ("; ".join(free) if free else "nothing") + ".")
    lines.append("Asks the user first: " + ("; ".join(asks) if asks else "nothing (all groups trusted)") + ".")
    lines.append("Reads (screen, apps, files, calendar) never need approval. Don't ask the user to change "
                 "these settings unless they ask how.")
    return "\n".join(lines)


def _parse_effort(body: dict) -> str | None:
    """#876: "high" (the composer's Think harder) or "normal"; anything else (or nothing) lets titan decide."""
    e = str(body.get("effort") or "").strip().lower()
    return e if e in ("high", "normal") else None


_APP_CLIENTS = frozenset({"iphone", "mac", "tv", "watch"})
# Asking for the long version in so many words (jellybean#699, Zach 10-04: "longer answers only when asked").
_ASKED_LONG_RX = re.compile(
    r"\b(?:tell me more|more detail(?:s|ed)?|in (?:more |full |great |greater )?detail|elaborate|in depth|go deeper|"
    r"explain (?:it |that |this )?(?:fully|thoroughly|properly|in full|step by step)|walk me through|"
    r"the (?:full|whole|complete) (?:answer|story|version|list|picture|details|breakdown)|(?:a )?longer (?:answer|version)|"
    r"everything (?:you know|about))\b", re.I)


def _more_asked_reason(body: dict) -> str | None:
    """Why this turn asked for the long answer (#1076): Think harder, or the words; None when it didn't."""
    if _parse_effort(body) == "high":
        return "think_harder"
    q = body.get("prompt") or body.get("message") or ""
    return "phrase" if isinstance(q, str) and _ASKED_LONG_RX.search(q) else None


def _note_more_asked(body: dict, turn: dict, ident: "_hh.Identity") -> None:
    """A real turn of a person's notes its reason (Settings › Assistant's "you asked for more"), and its persona.
    Never an eval, temporary or brain-pinned turn, a canary or test process, or a shared screen."""
    if (turn.get("source") is not None or turn.get("eval_brain") or ident.role == "household"
            or os.environ.get("AGENT_TURN_SOURCE", "user") != "user"):
        return
    try:
        import about_info
        about_info.note_turn(ident.person_id, _more_asked_reason(body), turn.get("persona"))
    except Exception as exc:  # noqa: BLE001 - the turn goes on
        _lib.quiet_failure("more-asked note", exc)


def _parse_detail(body: dict) -> str | None:
    """How much to say (jellybean#699, decided 10-04): short by default, long only when asked. Long when the client
    says "full", the composer's Think harder is on, or the person's own message asks for more ("tell me more", "in
    detail"…); otherwise brief for the apps, also one that sends nothing (the iPhone, the TV, the Watch); a caller that isn't an
    app keeps the prompt as it was."""
    detail = str(body.get("detail") or "").strip().lower()
    if detail == "full" or _parse_effort(body) == "high":
        return "full"
    q = body.get("prompt") or body.get("message") or ""
    if isinstance(q, str) and _ASKED_LONG_RX.search(q):
        return "full"
    if detail == "brief" or str(body.get("client") or "").strip().lower() in _APP_CLIENTS:
        return "brief"
    return None   # a caller that isn't one of the apps (scripts, the harnesses): the prompt as it was


# A finished turn stays readable this long (GET /v1/turns/{id}, a re-attach by client_msg_id): a phone whose
# stream dropped follows its turn to the answer instead of sending it again (jellybean#369). Bounded: past
# the TTL it's gone, and past the cap the oldest finished turns go first. A running turn is never dropped
# (a turn stuck over an hour is).
TURN_KEEP_S = 600
TURN_CAP = 500
_FINISHED = ("done", "error", "cancelled")
# Clients whose dropped stream keeps the turn running (they follow it, and Stop is POST …/cancel). Every other
# client's disconnect is still its Stop (an older Mac that sends no client). A resumable turn that nobody stops
# still ends on its own: the turn budget (TURN_BUDGET_S), a device wait (120 s) and a confirmation (110 s) bound it.
RESUMABLE_CLIENTS = frozenset({"iphone", "mac"})   # the Mac since jellybean#392 (follows GET /v1/turns, has cancelTurn)
_turn_futs: set = set()   # the executor futures of turns running with no stream attached (kept referenced)
# Turns run on their own threads. They used to share asyncio's default executor with every asyncio.to_thread handler
# (~260 of them: stores, items readers, long reads), and that pool is min(32, CPUs + 4) = 16 threads on titan (12
# CPUs). A turn holds its thread for its whole run (a device wait is up to 120 s, a confirmation 110 s), so a few slow
# turns could leave the plain handlers queueing behind them, and a burst of handlers could hold a turn back. Now each
# has its own 16: turns keep the ceiling they effectively had, the handlers keep the whole default pool (left at its
# size: nothing else changes for them). Threads are made as turns need them, named "turn_N" in a stack dump.
TURN_WORKERS = 16
_TURN_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=TURN_WORKERS, thread_name_prefix="turn")


def _turn_call(*args: Any):
    """_run_turn(*args) as asyncio.to_thread would call it (in a copy of this context), for _TURN_POOL."""
    import contextvars
    import functools
    return functools.partial(contextvars.copy_context().run, _run_turn, *args)


def _prune_turns(now: float | None = None) -> None:
    """Under _turns_lock."""
    now = now or time.time()
    for tid in [t for t, v in _turns.items() if (v.get("state") in _FINISHED and now - v.get("finished_at", now) > TURN_KEEP_S)
                or now - v.get("created_at", now) > 3600]:
        _turns.pop(tid, None)
    if len(_turns) > TURN_CAP:
        done = sorted((v.get("finished_at", 0), t) for t, v in _turns.items() if v.get("state") in _FINISHED)
        for _, tid in done[:len(_turns) - TURN_CAP]:
            _turns.pop(tid, None)


def _finish(turn_id: str, state: str, **fields: Any) -> bool:
    """Move a turn to done/error/cancelled, stamping when. A cancelled turn stays cancelled (its late result is
    dropped): False then."""
    with _turns_lock:
        t = _turns.get(turn_id)
        if t is None or (t.get("state") == "cancelled" and state != "cancelled"):
            return False
        t.update({**fields, "state": state, "finished_at": time.time()})
        return True


def _turn_cancelled(turn_id: str) -> bool:
    return (_turns.get(turn_id) or {}).get("state") == "cancelled"


def _attached_turn(ident: "_hh.Identity", cmid: str | None) -> str | None:
    """The same person's turn with this client_msg_id from the last 10 minutes, running or done: a resend after a
    dropped stream re-attaches to it instead of starting another (jellybean#369). Cancelled or failed: None."""
    if not cmid:
        return None
    now = time.time()
    with _turns_lock:
        for tid, t in _turns.items():
            who = t.get("identity")
            if (t.get("client_msg_id") == cmid and who is not None and who.person_id == ident.person_id
                    and now - t.get("created_at", 0) <= TURN_KEEP_S and t.get("state") in ("running", "device_pending", "done")):
                return tid
    return None


# A turn still running this long after it started is stuck: its own bounds (the turn budget, a device wait of 120 s, a
# confirmation of 110 s) are long past. Its stream ends with an error then, so no client waits on keep-alives forever
# (10-15: privacy_eval's kid-files turn kept its stream open for 25 minutes and the nightly eval timed out), and where its
# thread is is written to the log once, as code locations only, for the fix.
TURN_STUCK_S = float(os.environ.get("AGENT_TURN_STUCK_S", "900"))


def _turn_stack(ident: int | None) -> str:
    """Where a turn's thread is: file:line function, innermost last; never a value. "" when it isn't running."""
    import traceback
    frame = sys._current_frames().get(ident) if ident else None
    if frame is None:
        return ""
    return " < ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(traceback.extract_stack(frame)[-30:]))


def _stuck(turn_id: str, now: float | None = None) -> bool:
    """Whether a running turn is past TURN_STUCK_S; the first time, it's marked an error and its stack is logged."""
    now = time.time() if now is None else now
    with _turns_lock:
        t = _turns.get(turn_id)
        if not t or t.get("state") not in ("running", "device_pending") or now - t.get("created_at", now) <= TURN_STUCK_S:
            return False
        t.update({"state": "error", "error": "stuck", "message": "This took too long, so it was stopped. Try again.",
                  "finished_at": now})
        if t.get("device_event") is not None:
            t["device_event"].set()
        ident, source = t.get("thread_ident"), t.get("source")
    print(f"[turn-stuck] {turn_id} ({source}) still running after {int(now - t.get('created_at', now))} s: "
          f"{_turn_stack(ident) or 'no thread'}", file=sys.stderr, flush=True)
    return True


def _init_turn(turn_id: str) -> None:
    with _turns_lock:
        _prune_turns()
        _turns[turn_id] = {
            "state": "running", "text": "",
            "device_event": None, "device_approved": None, "error": None, "created_at": time.time(),
        }


def _workspace_system_addendum(ws_id: str | None) -> str:
    """Extra system instructions that sandbox the agent to the active workspace."""
    if not ws_id:
        return ""
    meta = _workspaces.get(ws_id)
    if not meta:
        return ""
    ws_path = meta["path"]
    return (
        f"\n\n[WORKSPACE]\n"
        f"You are working inside an isolated git worktree at: {ws_path}\n"
        f"ALL file writes MUST use absolute paths under {ws_path}/  — never write outside this directory.\n"
        f"Run shell commands with cwd={ws_path} unless otherwise instructed.\n"
        f"Read design.md and .agent-memory/lessons.md in the workspace root if they exist.\n"
        f"The user will apply or discard changes when done."
    )


def _history_when(history: list) -> str | None:
    """When the earlier messages were written, from the apps' optional per-message `at` (epoch
    seconds): "Earlier messages in this chat: the first Tue Sep 29 21:04, the last 13 h ago." so a
    long chat can tell "an hour ago" from "yesterday". Nothing when the client sends no times."""
    try:
        return _history_when_inner(history)
    except Exception:  # noqa: BLE001 - a hint; it never fails a turn
        return None


def _history_when_inner(history: list) -> str | None:
    from datetime import datetime
    import local_time
    ats: list[float] = []
    for h in history or []:
        try:
            at = float(h.get("at")) if isinstance(h, dict) and h.get("at") is not None else None
        except (TypeError, ValueError):
            at = None
        if at and at > 1e12:
            at /= 1000                                   # milliseconds too
        if at and 1e9 < at < 4e9:                        # 2001–2096; anything else isn't a time
            ats.append(at)
    if not ats:
        return None
    first, last = min(ats), max(ats)
    line = f"Earlier messages in this chat: the last {local_time.ago(last)}"
    if last - first > 3600:
        line = f"Earlier messages in this chat: the first {local_time.ago(first)}, the last {local_time.ago(last)}"
    if datetime.fromtimestamp(first, local_time.zone()).date() != local_time.today():
        line += ". Words like \"today\" or \"tomorrow\" in them meant that day, not this one"
    return line + "."


# How much of the conversation each turn re-sends verbatim (chars, ~4 per token). Gemini reads it all;
# the local model trims to its 16k context (_fit_local_context); past 400k, _compact_messages summarises.
HISTORY_KEEP_CHARS = int(os.environ.get("AGENT_HISTORY_KEEP_CHARS", "120000"))


def _pre_timing(key: str, t0: float) -> None:
    """One stage of building a turn's prompt, timed for the turn log (#683): the turn takes these over when it
    starts (_TURN_IO.pre_timing, like pre_egress)."""
    pt = getattr(_lib._TURN_IO, "pre_timing", None)
    if isinstance(pt, dict):
        pt[key] = pt.get(key, 0) + int((time.monotonic() - t0) * 1000)


def _build_messages(
    question: str,
    history: list,
    tools: bool,
    ws_id: str | None,
    turn_id: str | None = None,
    device: bool = False,
    detail: str | None = None,
    policy: str | None = None,
) -> tuple[list[dict], int]:
    msgs: list[dict] = []
    ident: _hh.Identity = (_turns.get(turn_id) or {}).get("identity") if turn_id else None
    ident = ident or _hh.owner_identity()

    # Memory recall — per-turn context injection: the asking person's own memory only, and none
    # at all on a shared household device.
    recall_prepend = ""
    recall_sys = ""
    # A shared screen (the household identity) gets no recall at all, so none of anyone's health or other facts.
    # A temporary chat (jellybean#708) starts clean: no memory read in, as none is written out.
    if turn_id and _MEMORY_AVAILABLE and ident.role != "household" and (_turns.get(turn_id) or {}).get("source") != "temporary":
        t_recall = time.monotonic()
        try:
            mem = _get_memory()
            # #1073: health facts only in the person's own chats, never on the TV (a shared screen has no recall at all)
            # #1073: health facts only in the person's own chat, on their own device (a paired device or the master token).
            # A shared screen never reaches here (household identity: no recall at all); a service token (engineering,
            # ASE, open auth) gets no health.
            own_channel = ident.role in ("admin", "adult", "kid") and ident.via in ("master", "device")
            recall = (mem.recall(question, conv_id=turn_id, person_id=ident.person_id) if own_channel
                      else mem.recall(question, conv_id=turn_id, person_id=ident.person_id, include_health=False))
            recall_prepend = recall.prependContext   # L1 facts → prepend to user msg
            recall_sys = recall.appendSystemContext  # L3 persona + L2 scenes → append to system
        except Exception as exc:  # noqa: BLE001 - the turn goes on without memory, but not silently
            _lib.quiet_failure("memory recall", exc)
        # The world model's page for an entity wins over memory sentences about it (#325, decision 4).
        recall_prepend = _lib.world_drop_recalled(recall_prepend, question)
        _pre_timing("recall_ms", t_recall)
        # #1072: the facts this real turn actually put in front of the model (after the world model's drop) get
        # last_used and use_count. Never an eval, temporary, brain-pinned or canary/test-process turn, or nightly
        # evals would keep everything "fresh".
        tt = _turns.get(turn_id) or {}
        if (tt.get("source") is None and not tt.get("eval_brain")
                and os.environ.get("AGENT_TURN_SOURCE", "user") == "user"):
            try:
                used = [f for f in getattr(recall, "used", []) if f"• {f}" in recall_prepend]
                if used:
                    _get_memory().stamp_used(used, person_id=ident.person_id)
            except Exception as exc:  # noqa: BLE001 - the turn goes on; a missed stamp is only a stamp
                _lib.quiet_failure("memory use stamp", exc)

    # Two system messages. The first is the prompt prefix every turn of the same shape shares
    # (identity, tools, workspace, the skill index), so llama-server's prompt cache and Gemini's
    # implicit cache reuse it instead of re-reading it; everything that changes turn to turn (the
    # picked skill's guide, memory, reply style, voice, device policy, who is asking) goes in the
    # second, after it. Before 10-01 all of it was one message with the picked skill in the middle,
    # so a turn re-read the skill list and whatever followed it.
    # The memoized prompt has no device tools; a device turn (its hook is already set on this
    # thread) builds the prompt fresh so they are advertised.
    # Someone who can't use every tool (not the admin) gets the prompt built for their role.
    fresh = device or ident.role != "admin"
    t_tools = time.monotonic()
    tool_prompt = (_lib.build_tool_system_prompt() if fresh else _TOOL_SYSTEM_PROMPT) if tools else ""
    _pre_timing("toolprompt_ms", t_tools)
    system = _lib.AGENT_IDENTITY + "\n\n" + tool_prompt + _workspace_system_addendum(ws_id)
    per_turn: list[str] = []
    # Skills describe the admin's and adults' tools (shell, files, media on the Mac); kids and
    # shared devices don't get them. Every skill is listed (description only); the guide of the one
    # picked for this turn follows the prefix, and any other is a skill_guide call away, so a host
    # without Gemini or Jev (the local brain alone) still has them.
    loaded = {"picked": None, "body": False}
    if _SKILLS_AVAILABLE and _SKILLS and ident.role in ("admin", "adult"):
        skills = _turn_skills(turn_id)
        system += "\n\n" + _skills_index(skills, guide_tool=bool(tools))
        t_skill = time.monotonic()
        needs_body, active = _skill_match(question, skills)
        _pre_timing("skill_ms", t_skill)
        if needs_body and active:
            per_turn += [_skill_guide_prompt(s) for s in skills if s.name in active]
        loaded = {"picked": sorted(active) if active else None, "body": bool(needs_body and active)}
    if turn_id and turn_id in _turns:
        _turns[turn_id]["skills"] = loaded   # which skill's full text this turn saw (eval turns report it)
    if recall_sys:
        per_turn.append(recall_sys)
    if detail:
        per_turn.append(_REPLY_STYLES[detail])
    persona = (_turns.get(turn_id) or {}).get("persona") if turn_id else None
    if persona in _PERSONAS:
        per_turn.append(_PERSONAS[persona])
    if policy and device:
        per_turn.append(policy)
    per_turn.append(_who_is_asking(ident))
    when = _history_when(history)
    if when:
        per_turn.append(when)
    msgs.append({"role": "system", "content": system})
    msgs.append({"role": "system", "content": "\n\n".join(per_turn)})

    # The conversation so far, verbatim, newest first up to HISTORY_KEEP_CHARS (always from the last
    # user message on). Until 10-01, any recall hit cut the history to the last exchange: a long chat
    # lost what was said three messages earlier ("Ava's recital moved to 6:30" → "no mention of a
    # recital"), and a correction lost to that cut let the old fact come back (F-06). Older messages
    # past the cap are replaced by the recalled facts; the local model trims further to fit (16k).
    history_avoided = 0
    turns = [(str(h.get("role") or "user").strip(), str(h.get("content") or h.get("text") or "").strip())
             for h in (history or [])]
    turns = [(r, c) for r, c in turns if c]
    last_user_idx = next((i for i in range(len(turns) - 1, -1, -1) if turns[i][0] == "user"), 0)
    start, used = len(turns), 0
    while start > 0 and (start > last_user_idx or used + len(turns[start - 1][1]) <= HISTORY_KEEP_CHARS):
        start -= 1
        used += len(turns[start][1])
    dropped, kept = turns[:start], turns[start:]
    if dropped:
        history_avoided = sum(len(c) for _, c in dropped)
        note = f"[OLDER MESSAGES OMITTED: {len(dropped)}]"
        msgs.append({"role": "system", "content": note + (f"\n[PRIOR CONTEXT]\n{recall_prepend}" if recall_prepend else "")})
    for role, content in kept:
        msgs.append({"role": role, "content": content})
    where_note = _lib.location_history_note([c for r, c in kept if r == "assistant"])   # #1611: never copied as an answer
    if where_note:
        msgs.append({"role": "system", "content": where_note})
    # Recalled facts go in their own system message, not glued onto the user's words: the model and
    # the turn router should see exactly what the user asked.
    if recall_prepend and not dropped:
        msgs.append({"role": "system", "content": recall_prepend})
    carried = _continue_block(turn_id, ident, history, question)
    if carried:
        msgs.append({"role": "system", "content": carried})
    msgs.append({"role": "user", "content": question})

    # Accumulate per-turn token accounting.
    if turn_id:
        input_chars = sum(len(m.get("content") or "") for m in msgs)
        recall_chars = len(recall_prepend)
        tc = _TokenCounts(
            estimated_input_chars=input_chars,
            recall_injected_chars=recall_chars,
            history_avoided_chars=history_avoided,
        )
        prev = _session_token_stats.get(turn_id, _TokenCounts())
        _session_token_stats[turn_id] = _TokenCounts(
            estimated_input_chars=prev.estimated_input_chars + tc.estimated_input_chars,
            recall_injected_chars=prev.recall_injected_chars + tc.recall_injected_chars,
            history_avoided_chars=prev.history_avoided_chars + tc.history_avoided_chars,
        )

    return msgs, history_avoided


def _continue_block(turn_id: str | None, ident: "_hh.Identity", history: list, question: str = "") -> str | None:
    """#1016 step 5: a new chat (no history yet) that continues the asker's own recent one gets the earlier chat's
    name, last exchange and what its tools did, as untrusted "chats" text. Off unless CONV_CONTINUE=1; an eval turn
    chooses its arm (eval_continue) and brings its own previous chat (eval_prior), never a real stream. Never on a
    kid's turn, a shared screen or a temporary chat, and never anyone else's chat. Never breaks a turn."""
    t = (_turns.get(turn_id) or {}) if turn_id else {}
    _lib._TURN_IO.conv_continue = None
    # #1016: why a real turn did or didn't carry an earlier chat in, content-free, so the counts can tell "not eligible"
    # from "eligible, nothing recent" (eval turns pick their own arm and aren't counted)
    real = bool(turn_id) and t.get("source") != "eval"

    def why(reason: str) -> None:
        _lib._TURN_IO.conv_continue_why = reason if real else None
    why("")
    if not turn_id:
        return None
    if ident.role in ("kid", "household"):
        why("kid_or_screen")
        return None
    if t.get("source") == "temporary":
        why("temporary")
        return None
    if history:
        why("not_first_turn")
        return None
    try:
        import conv_continue as cc
        now = time.time()
        standin = True
        if t.get("source") == "eval":
            if not t.get("eval_continue"):
                return None
            chats = cc.eval_chats(t.get("eval_prior"), ident.person_id, now)
            # the edge arm: KB's live check over the eval's own chats only, no stand-in behind it
            edge = cc.eval_edge(chats, ident.person_id, question, now) if t.get("eval_signal") == "edge" else None
            standin = t.get("eval_signal") != "edge"
        else:
            if not cc.on() or not _MEMORY_AVAILABLE:
                why("off")
                return None
            chats = cc.live_chats(_get_memory()._db, _hh.conv_id_for(ident.person_id), ident.person_id)
            edge = cc._edge(ident.person_id, t.get("thread"), question)
        prev = cc.previous(chats, ident.person_id, now, current=t.get("thread"), edge=edge, standin=standin)
        if not prev:
            why("no_recent")
            return None
        why("carried")
        # which signal, no content; the turn's record is started after the prompt is built, so it's handed over
        _lib._TURN_IO.conv_continue = "edge" if edge and prev.get("id") == edge else "recent"
        return cc.HEAD + "\n" + _lib._untrusted(cc.block_text(prev, now, trace=_lib.turn_trace), "chats", "earlier chat")
    except Exception as exc:  # noqa: BLE001
        _lib.quiet_failure("conversation carry-over", exc)
        why("error")
        return None


def _who_is_asking(ident: "_hh.Identity") -> str:
    """[WHO IS ASKING]: the person's first name and role, so replies address the right person,
    and what a kid or a shared device can't have (titan refuses those tools anyway)."""
    dev = f" on {ident.device['name']}" if ident.device and ident.device.get("name") else ""
    name = ident.first_name
    lines = [f"[WHO IS ASKING] {name} ({ident.role}){dev}."]
    call = ident.call_me
    if call != name:   # #1080: their own choice, cleaned to one plain line; the roster name stays who they are
        lines.append(f'Call them "{call}" when you address them.')
    if ident.role != "household":
        try:
            prefs = _hh.get_store().prefs(ident.person_id)
            on = [f"{k}: {_hh.PREFERENCES[k][1]}" for k, v in prefs.items()
                  if v == "on" and k not in _hh.DISPLAY_PREFERENCES]
            lines.append("[PREFERENCES] " + ("; ".join(on) if on else
                         "defaults: do clear requests without asking first, no offers at the end") + ".")
        except Exception:  # noqa: BLE001 - never block a turn on preferences
            pass
    if ident.role == "admin":
        lines.append(f"{name} runs this household and titan.")
    elif ident.role == "adult":
        lines.append(f"{name} is an adult in the household. \"The user\" above means {name}. Their memory is "
                     "their own; don't bring up other people's personal things.")
    elif ident.role == "kid" and ident.via == "watch":
        lines.append(f"{name} is a child in the household, asking from their Watch. \"The user\" above means {name}. "
                     "Answer in one or two short, friendly sentences of plain facts. Never quote or read out a "
                     "message, note, mail, file, document or anyone's words; say what it means instead. They can't "
                     "use messages, notes, mail, files, the shell, other people's memory, or locks, cameras and "
                     f"alarms through you; if they ask, say plainly: \"{_hh.REFUSAL}\"")
    elif ident.role == "kid":
        lines.append(f"{name} is a child in the household. \"The user\" above means {name}. Keep it friendly "
                     "and age-appropriate; for homework, help them understand and work it out rather than "
                     "handing over answers. They can't use messages, notes, mail, files, the shell, other "
                     "people's memory, or locks, cameras and alarms through you; if they ask, say plainly: "
                     f"\"{_hh.REFUSAL}\"")
    else:
        lines.append("This is a shared household device, not one person: you don't know who is speaking. "
                     "Nothing personal: no one's memory, messages, notes, mail, files or contacts. House "
                     "control, the family calendar, weather, routines and the family knowledge base are fine. "
                     f"If asked for anything else, say plainly: \"{_hh.REFUSAL}\"")
    return "\n".join(lines)


def _parse_eval_pins(body: dict, source: str | None) -> tuple[str | None, frozenset[str] | None]:
    """An eval turn's own settings (ignored on any other turn): `eval_brain` "local" | "gemini" pins
    the brain, so a benchmark scores each one; `eval_tools` [names] is a hard allowlist, so the
    turn can't run anything else (titan refuses the call, whatever the model asks for)."""
    if source != "eval":
        return None, None
    brain = str(body.get("eval_brain") or "").strip().lower()
    brain = brain if brain in ("local", "gemini") else None
    tools = body.get("eval_tools")
    only = frozenset(str(t) for t in tools if isinstance(t, str)) if isinstance(tools, list) else None
    return brain, only


def _parse_eval_fixtures(body: dict, source: str | None) -> dict[str, str] | None:
    """An eval turn's `eval_fixtures` {tool: canned text}: those titan tools answer with the fixture instead of the
    house's real data (turn_eval's fictional threads, bills and web pages). Ignored on any other turn."""
    fx = body.get("eval_fixtures")
    if source != "eval" or not isinstance(fx, dict):
        return None
    return {str(k): str(v)[:4000] for k, v in list(fx.items())[:10] if isinstance(v, str)}


def _parse_eval_world(body: dict, source: str | None) -> bool | None:
    """An eval turn's `eval_world` true | false: the world model on or off for that turn only (world_eval
    scores each question both ways). None (the server's AGENT_WORLD_MODEL) on any other turn."""
    if source != "eval" or not isinstance(body.get("eval_world"), bool):
        return None
    return body["eval_world"]


# A kid's Watch (titan-node#755): derived facts only. Its turns stream nothing while they run; `done` carries a short
# text and nothing else that could quote an item (no blocks, sources, cites or peek).
WATCH_REPLY_CHARS = 300
# All a Watch's stream says besides `done` (10-07 review W4): one generic status, and a fixed error (an error's own
# words can name a tool, a file or an item). No "from your iPhone": a Family Setup kid's Watch has none (#764).
WATCH_STATUS = "Working on it…"
WATCH_ERROR = "Something went wrong. Try again in a minute."


def _is_watch_turn(turn: dict) -> bool:
    ident = turn.get("identity")
    return getattr(ident, "via", None) == "watch"


def _watch_text(text: str) -> str:
    t = " ".join(str(text or "").split())
    if len(t) <= WATCH_REPLY_CHARS:
        return t
    cut = t[:WATCH_REPLY_CHARS]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > 80 else cut.rstrip() + "…")


def _watch_shaped(done: dict) -> dict:
    return {"text": _watch_text(done.get("text") or ""), "truncated": False, "sources": [], "blocks": [],
            "egress": list(done.get("egress") or [])}


def _parse_source(body: dict, ident: "_hh.Identity | None" = None) -> str | None:
    """A turn's source from the request: "eval" (scripts/turn_eval.py) or "temporary" (a temporary chat, jellybean#708);
    anything else is an ordinary user turn. "eval" only from the admin (10-07 review W5): an eval turn remembers
    nothing, takes fixtures, pins and skill overrides, and is left out of the turn reports, so anyone else's "eval"
    is an ordinary turn of theirs."""
    s = str(body.get("source") or "").strip().lower()
    if s == "eval" and getattr(ident, "role", None) != "admin":
        return None
    return s if s in ("eval", "temporary") else None


TEMPORARY_ROLES = ("admin", "adult")


def _temporary_refused(source: str | None, ident: "_hh.Identity") -> JSONResponse | None:
    """A temporary chat (jellybean#708): an adult's own device only (Zach, 10-04). A kid's or a shared device's turns
    keep their usual record."""
    if source == "temporary" and getattr(ident, "role", None) not in TEMPORARY_ROLES:
        return JSONResponse({"error": "temporary chats are for adults on their own devices"}, status_code=403)
    return None


def _leaves_no_memory(turn: dict | None) -> bool:
    """An eval or a temporary chat: nothing remembered, no facts saved, not in Past conversations, no search trace."""
    return (turn or {}).get("source") in ("eval", "temporary")


_SKILL_OVERRIDE_MAX_CHARS = 8000


def _parse_skill_overrides(body: dict, source: str | None, ident) -> dict | None:
    """Candidate skill texts for one eval turn (scripts/skillopt: SkillOpt tries edits to a skill
    through the real turn path). `skill_overrides` is {skill name: whole SKILL.md}; it's honoured
    only for an admin's eval turn, replaces a skill that exists, and is ignored otherwise, so it
    can never change what a person's turn sees."""
    raw = body.get("skill_overrides")
    if not raw or source != "eval" or getattr(ident, "role", None) != "admin" or not isinstance(raw, dict):
        return None
    known = {s.name for s in _SKILLS}
    out = {}
    for name, text in raw.items():
        if name in known and isinstance(text, str) and len(text) <= _SKILL_OVERRIDE_MAX_CHARS:
            skill = _parse_skill_text(text)
            if skill and skill.name == name:
                out[name] = skill
    return out or None


def _turn_skills(turn_id: str | None) -> list:
    """The skills this turn sees: the loaded ones, with an eval turn's overrides swapped in."""
    over = (_turns.get(turn_id) or {}).get("skill_overrides") if turn_id else None
    return [over.get(s.name, s) for s in _SKILLS] if over else _SKILLS


EVAL_MAC_STATES = ("live", "wakeable", "unreachable", "never")


def _parse_eval_mac(body: dict, source: str | None) -> str | None:
    """An eval turn never uses a real Mac: it names a simulated one ("eval_mac": live | wakeable |
    unreachable | never; default never). Its Mac tools are answered by the eval's own fake device,
    like any device request. None on every other turn."""
    if source != "eval":
        return None
    v = str(body.get("eval_mac") or "").strip().lower()
    return v if v in EVAL_MAC_STATES else "never"


def _parse_eval_continue(body: dict, source: str | None) -> tuple[Any, bool | None, str | None]:
    """An eval turn's carry-over arm (#1016 step 5): `eval_prior`, the previous chat(s) as the carry-over bank
    gives them (never the person's real stream), `eval_continue` true | false, the arm, and `eval_signal`
    "recent" (the 30-min stand-in, the default) | "edge" (KB's live check over eval_prior). (None, None, None) on
    any other turn: a real turn only ever reads its asker's own stream, behind CONV_CONTINUE."""
    if source != "eval":
        return None, None, None
    prior = body.get("eval_prior")
    prior = prior if isinstance(prior, (list, dict)) else None
    arm = body.get("eval_continue") if isinstance(body.get("eval_continue"), bool) else None
    signal = "edge" if body.get("eval_signal") == "edge" else None
    return prior, arm, signal


def _parse_eval_device(body: dict, source: str | None) -> str | None:
    """An eval client that answers device requests itself (turn_eval, the SkillOpt harness) says so:
    "eval_device": "fake". Only then do an eval turn's device writes reach the device hook; without it
    (jb ask drives the real Mac) titan simulates them. Honoured on eval turns only."""
    if source != "eval":
        return None
    return "fake" if str(body.get("eval_device") or "").strip().lower() == "fake" else None


def _eval_remote(ident: "_hh.Identity", client: str | None, state: str,
                 phone_tools: frozenset[str] | None = None) -> dict | None:
    """An eval phone turn's simulated Mac: the tools a real one would offer, answered by the eval."""
    if client != "iphone" or state not in ("live", "wakeable"):
        return None
    tools = frozenset(n for n in _lib.mac_tools_for_phone(phone_tools) if _hh.role_allows_tool(ident.role, n))
    mac = {"device_key": "eval-mac", "name": "Eval Mac", "mac_address": "02:00:00:00:00:01", "awake": True,
           "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))}
    return {"mac": mac, "tools": tools, "from": "your iPhone", "platform": "ios", "eval": True} if tools else None


def _eval_macs(state: str) -> tuple[list[dict], bool]:
    """The presence rows and liveness _mac_status_line sees for a simulated Mac."""
    import capabilities
    return capabilities.eval_macs(state)


def _parse_device_tools(body: dict) -> list[str] | None:
    """The device tools the iPhone app says it runs ("device_tools"); None when it doesn't say
    (an older app: device_tools_for_client falls back to its five)."""
    tools = body.get("device_tools")
    if not isinstance(tools, list):
        return None
    return [str(t).strip() for t in tools if isinstance(t, str) and t.strip()][:64]


# Cross-person evals (plan item 9, scripts/privacy_eval.py): an eval turn sent with the admin token
# may run AS a synthetic adult or kid, to check that one person's data never reaches another. The
# person is built here and never stored or paired, so it's in no roster.
EVAL_AS = {"adult": ("eval-adult", "Eval Adult"), "kid": ("eval-kid", "Eval Kid")}


class EvalAsRefused(Exception):
    """An eval turn named a fixture person it may not run as (box_fixture.person): the turn is refused (403), never run
    as the asker."""


def _eval_as(body: dict, source: str | None, ident: "_hh.Identity") -> "_hh.Identity":
    """The synthetic person an eval turn names ("eval_as": {"role": "adult" | "kid"}), or `ident`
    unchanged: only an eval turn with the admin token may use it. {"person": "<a golden household id>"}: only in a
    fixture box (box_fixture.py, #1399 P4); any other use raises EvalAsRefused."""
    want = body.get("eval_as")
    if isinstance(want, dict) and want.get("person"):
        import box_fixture
        p, why = box_fixture.person(want, source, ident.role, _hh.get_store())
        if p is None:
            raise EvalAsRefused(why)
        return _hh.Identity(p, None, p["role"], "eval")
    if not want or source != "eval" or ident.role != "admin":
        return ident
    role = str(want.get("role") if isinstance(want, dict) else want).strip().lower()
    if role not in EVAL_AS:
        return ident
    pid, name = EVAL_AS[role]
    # {"role": "kid", "via": "watch"}: the turn as a kid's Watch asks it (titan-node#755's privacy_eval cases).
    watch = role == "kid" and isinstance(want, dict) and str(want.get("via") or "").lower() == "watch"
    return _hh.Identity({"id": pid, "name": name, "role": role}, None, role, "watch" if watch else "eval")


def _parse_client(body: dict) -> str | None:
    """Which Jellybean runs this turn's device tools: "iphone" (the phone app: calendar, reminders
    and contacts only, no Mac field state), "mac" (a Mac that says so, jellybean#392: it follows a
    dropped turn and stops by name) or None (an older Mac, and every other client). Every reader
    treats "mac" as None: only "iphone" is a phone (_remote_for_turn, _eval_remote, _mac_status_line,
    device_tools_for_client, _turn_client); "mac" differs only in RESUMABLE_CLIENTS."""
    c = str(body.get("client") or "").strip().lower()
    return c if c in ("iphone", "mac") else None


def _remote_for_turn(ident: "_hh.Identity", client: str | None,
                     phone_tools: frozenset[str] | None = None) -> dict | None:
    """Cross-device: for a phone turn, the person's own Mac (live or wakeable) and the Mac tools
    their role allows. None: not a phone turn, no Mac, or nothing their role may run there."""
    if client != "iphone":
        return None
    mac = _xd.mac_for(ident.person_id)
    if not mac:
        return None
    tools = frozenset(n for n in _lib.mac_tools_for_phone(phone_tools) if _hh.role_allows_tool(ident.role, n))
    runs = _lib.mac_runs(mac.get("device_tools"))
    if runs is not None:   # what this Mac's Jellybean actually runs (an older one lacks newer tools)
        tools &= runs
    if not tools:
        return None
    dev = ident.device or {}
    return {"mac": mac, "tools": tools, "from": dev.get("name") or "your iPhone",
            "platform": {"ipados": "ipados"}.get(dev.get("platform"), "ios")}


# What the Mac does that the iPhone can't, named when the Mac is out of reach so a phone turn says
# which thing is unavailable (live 09-30: "where's Ava's watch?" with no Mac got a vague "I don't
# have a way" and an offer, never that Find My lives on the Mac).
MAC_ONLY_NAMED = ("Find My (where people and devices are)", "the Mac's files and screen",
                  "searching Notes and Messages", "shell commands and checks")
_UNREACHABLE_TAIL = ("Unavailable from the iPhone this turn: " + ", ".join(MAC_ONLY_NAMED) + ". When the user asks "
                     "for one of these, say it needs their Mac, which isn't reachable now; don't offer a workaround.")


def _mac_status_line(ident: "_hh.Identity", client: str | None, remote: dict | None,
                     eval_mac: str | None = None) -> str | None:
    """For an iPhone turn, one context line saying what titan knows about the person's Mac: connected,
    wakeable, or out of reach, and when it last checked in. Facts from the presence store and the
    channel, so the model never infers the Mac's state from the timeline. None: not a phone turn.
    An eval turn describes its simulated Mac (eval_mac) instead."""
    if client != "iphone":
        return None
    live_override = None
    if eval_mac:
        macs, live_override = _eval_macs(eval_mac)
    else:
        try:
            macs = _store().macs_for(ident.person_id)
        except Exception:  # noqa: BLE001 - no store: say nothing rather than guess
            return None
    if not macs:
        return ("Your Mac: titan has never heard from it on the phone-to-Mac channel, so from the iPhone you "
                "can't reach, wake or check it, and its tools aren't available this turn. Whether Jellybean is "
                "running on it is unknown. " + _UNREACHABLE_TAIL)
    m = (remote or {}).get("mac") or macs[0]
    name = m.get("name") or "Mac"
    try:
        from datetime import datetime
        age = time.time() - datetime.fromisoformat(str(m["updated_at"]).replace("Z", "+00:00")).timestamp()
        seen = f"last checked in {_lib._ago(age)} ago" + (", going to sleep" if not m.get("awake", True) else "")
    except Exception:  # noqa: BLE001
        seen = "last check-in time unknown"
    if live_override if live_override is not None else _xd.CHANNEL.live((ident.person_id, m["device_key"])):
        return (f"Your Mac ({name}): connected to titan now; tools marked (on your Mac) run there." if remote else
                f"Your Mac ({name}): connected to titan, but none of its tools are open to you from the iPhone.")
    if remote and _xd.wakeable(m):
        return (f"Your Mac ({name}): not connected right now ({seen}). titan will try to wake it when you use a "
                "tool marked (on your Mac); the result says whether it woke.")
    return (f"Your Mac ({name}): not connected ({seen}) and titan can't wake it (it hasn't shared how). Its tools "
            "aren't available from the iPhone this turn, and whether Jellybean is running on it is unknown. "
            + _UNREACHABLE_TAIL)


def _item_route(turn_id: str, ident: "_hh.Identity", req: dict, owner: str, mac: dict, approve) -> dict:
    """One item_body call to the device holding an item (#446 step 3): the owner's Mac, through cross_device. The
    owner reading: their own confirmation (the phone's sheet, as for any Mac tool). Anyone else: approve() is the
    owner's answer (a card on their devices), never the reader's. An eval turn's Mac is its fixture."""
    turn = _turns.get(turn_id) or {}
    if turn.get("source") == "eval":
        import item_open
        fx = turn.get("eval_mac")
        return item_open.fixture_route({"asleep": True} if fx in ("unreachable", "never", None) else turn.get("eval_item"))(
            req, owner, mac, approve)

    def emit(event: str, data: dict) -> None:
        with _turns_lock:
            _turns[turn_id].setdefault("events", []).append((event, data))

    def cancelled() -> bool:
        return (_turns.get(turn_id) or {}).get("state") == "cancelled"

    if approve is not None:
        # Someone else's read (#776): the owner's yes first, then the request is queued already approved, so the
        # holding device (a Mac or an iPhone) never holds an unapproved one for another asker.
        if not approve():
            return {"result": _xd.DECLINED, "completed": False}

        def ask_confirm(payload: dict) -> dict:
            return {"approved": False, "remember_session": False}   # approved already; a second ask is a refusal
    else:
        def ask_confirm(payload: dict) -> dict:
            evt = threading.Event()
            with _turns_lock:
                _turns[turn_id]["_confirm"] = {"confirm_id": payload["confirm_id"], "event": evt, "answer": None}
                _turns[turn_id].setdefault("events", []).append(("confirm", payload))
            end = time.monotonic() + _xd.CONFIRM_TIMEOUT_S
            while not evt.wait(0.25):
                if cancelled() or time.monotonic() >= end:
                    break
            with _turns_lock:
                pending = _turns[turn_id].pop("_confirm", None) or {}
            return pending.get("answer") or {"approved": False, "remember_session": False}

    dev = ident.device or {}
    hook = getattr(_lib._TURN_IO, "device", None)
    if (approve is None and dev.get("id") and dev.get("id") == mac.get("device_id") and hook is not None
            and "item_body" in (getattr(_lib._TURN_IO, "device_tools", None) or ())):
        # #776: the owner asking on the very iPhone that holds it: a device call in this turn, no inbox or push
        return hook(req) or {"error": _xd.NO_ANSWER}
    session = turn.get("session") or ("device:" + dev["id"] if dev.get("id") else "person:" + ident.person_id)
    return _xd.route(req, person_id=owner, mac=mac, turn_id=turn_id,
                     origin={"device": dev.get("name") or "titan", "platform": dev.get("platform") or "ios"},
                     session=f"{session}:items:{owner}", emit=emit, ask_confirm=ask_confirm, cancelled=cancelled,
                     approved=approve is not None)


def _route_to_mac(turn_id: str, req: dict, ident: "_hh.Identity", remote: dict) -> dict:
    """Run one Mac tool for a phone turn (cross_device.route), with this turn's SSE events and the
    phone's confirmation sheet."""
    def emit(event: str, data: dict) -> None:
        with _turns_lock:
            _turns[turn_id].setdefault("events", []).append((event, data))

    def cancelled() -> bool:
        return (_turns.get(turn_id) or {}).get("state") == "cancelled"

    def ask_confirm(payload: dict) -> dict:
        evt = threading.Event()
        with _turns_lock:
            _turns[turn_id]["_confirm"] = {"confirm_id": payload["confirm_id"], "event": evt, "answer": None}
            _turns[turn_id].setdefault("events", []).append(("confirm", payload))
        end = time.monotonic() + _xd.CONFIRM_TIMEOUT_S
        while not evt.wait(0.25):
            if cancelled() or time.monotonic() >= end:
                break
        with _turns_lock:
            pending = _turns[turn_id].pop("_confirm", None) or {}
        return pending.get("answer") or {"approved": False, "remember_session": False}

    turn = _turns.get(turn_id) or {}
    session = turn.get("session") or ("device:" + ident.device["id"] if ident.device else "person:" + ident.person_id)
    return _xd.route(req, person_id=ident.person_id, mac=remote["mac"], turn_id=turn_id,
                     origin={"device": remote["from"], "platform": remote["platform"]}, session=session,
                     emit=emit, ask_confirm=ask_confirm, cancelled=cancelled)


def turn_prefix_messages(ident: "_hh.Identity", client: str | None = None, device: bool = False) -> list[dict]:
    """The messages a turn of this shape would start with, built with the same turn state _run_turn
    sets for the prompt (role, client, the client's device tools, the person's Mac from a phone).
    scripts/warm_local.py sends their first system message to put the shape's prefix in the local
    model's cache; test_prompt_prefix checks it equals a real turn's. Reads only: it never contacts a
    device or runs a tool."""
    io = _lib._TURN_IO
    tid = f"prefix-{uuid.uuid4().hex[:8]}"
    device_tools = _lib.device_tools_for_client(client, None)
    remote = _remote_for_turn(ident, client, device_tools) if device else None
    _turns[tid] = {"identity": ident, "client": client, "source": "eval"}
    io.device = (lambda req: {"error": "warm-up: no device"}) if device else None
    io.person = {"id": ident.person_id, "name": ident.person.get("name")}
    io.role = ident.role
    io.client = client
    io.device_tools = device_tools
    io.remote = ({"tools": remote["tools"], "from": remote["from"], "device": remote["mac"].get("name"),
                  "device_id": remote["mac"].get("device_id")} if remote else None)
    try:
        msgs, _ = _build_messages("hi", [], True, None, turn_id=tid, device=device, detail="brief")
        return msgs
    finally:
        _turns.pop(tid, None)
        io.device = io.person = io.role = io.client = io.device_tools = io.remote = None
        io.item_route = io.eval_item = io.about = None


CHAT_CONV_ID = "chat"   # Zach's chat conversation in the memory store (others: household_identity.conv_id_for)


def _run_turn(turn_id: str, question: str, history: list, tools: bool, ws_id: str | None,
              device: bool = False, detail: str | None = None, policy: str | None = None) -> str:
    io = _lib._TURN_IO
    # Identifiers never reach a model (09-30: an attached bill's text carried its account number to
    # Gemini). The last four digits, amounts and dates stay, so a bill is still identifiable.
    import redact
    question = redact.redact_personal(question)
    # The apps resend the last 12 messages with every request: a bill pasted two messages ago would
    # otherwise reach the model on every later turn (the owner's review).
    history = [{**m, "content": redact.redact_personal(m["content"])} if isinstance(m, dict) and isinstance(m.get("content"), str)
               else m for m in (history or [])]

    with _turns_lock:
        if turn_id in _turns:
            _turns[turn_id]["thread_ident"] = threading.get_ident()   # for _turn_stack when it's stuck
    ident: _hh.Identity = (_turns.get(turn_id) or {}).get("identity") or _hh.owner_identity()
    client = (_turns.get(turn_id) or {}).get("client")
    eval_mac = (_turns.get(turn_id) or {}).get("eval_mac")
    # The iPhone runs only its own device tools: the prompt, Gemini's declarations and the dispatch
    # all follow this per-turn set (None: the Mac, every tool); the rest can go to the person's Mac.
    device_tools = _lib.device_tools_for_client(client, (_turns.get(turn_id) or {}).get("device_tools"))
    if eval_mac:   # an eval turn: a simulated Mac, never the person's real one
        remote = _eval_remote(ident, client, eval_mac, device_tools) if device else None
    else:
        remote = _remote_for_turn(ident, client, device_tools) if device else None

    def device_hook(req: dict) -> dict:
        """One device action: publish it on the turn (the SSE stream sends it to the app) and
        wait for the app's outcome on /v1/device/result. Bounded, so a closed app declines.
        A phone turn's Mac tool goes to the person's Mac instead (cross-device)."""
        if (remote and not remote.get("eval") and req.get("tool") in remote["tools"]
                and not _lib.phone_runs(req.get("tool"), req.get("args"), io.device_tools)):
            return {**(_route_to_mac(turn_id, req, ident, remote) or {}), "ran_on": "mac"}
        evt = threading.Event()
        rid = uuid.uuid4().hex[:12]
        with _turns_lock:
            turn = _turns[turn_id]
            if turn.get("state") == "cancelled":
                return {"error": "Stopped by the user.", "completed": False}
            turn.update({"state": "device_pending", "device_event": evt, "device_result": None,
                         "_device_req": {"request_id": rid, **req}})
        # A bounded read (the local schedule prefetch, #347) waits its own few seconds; a late answer is
        # refused as a stale request_id, never taken for the next request.
        got = evt.wait(min(DEVICE_TIMEOUT_SECONDS, float(req.get("wait_s") or DEVICE_TIMEOUT_SECONDS)))
        with _turns_lock:
            turn = _turns[turn_id]
            if turn.get("state") == "cancelled":   # Stop while the device had it: released, nothing more
                turn.update({"_device_req": None, "device_event": None})
                return {"error": "Stopped by the user.", "completed": False}
            result = turn.get("device_result") if got else None
            turn.update({"state": "running", "_device_req": None, "device_event": None})
        return {**(result or {"error": "Confirmation timed out." if not got else "No result from the device."}),
                "ran_on": asker_platform}

    # #1366: the asking device's platform (mac, iphone, ipad, watch, tv) for the turn log's tool_devices; its paired
    # platform, else the client the request named.
    asker_platform = _lib.device_platform((ident.device or {}).get("platform"), client)

    def status_hook(label: str) -> None:
        with _turns_lock:
            _turns[turn_id].setdefault("status", []).append(label)

    io.device = device_hook if device else None
    io.cancelled = lambda: _turn_cancelled(turn_id)   # Stop: execute_tool runs nothing more
    io.status = status_hook
    io.detail = detail
    io.effort = (_turns.get(turn_id) or {}).get("effort")   # #876: "high" (Think harder), "normal", or auto
    io.effort_level = None
    io.source = (_turns.get(turn_id) or {}).get("source")
    io.turn_id = turn_id      # tools that learn from a turn (#359) name it
    io.thread = (_turns.get(turn_id) or {}).get("thread")   # the app's conversation id, for the turn log (#1267)
    # Who is asking: the prompt, the tool list, every tool call and memory follow the person's role.
    io.person = {"id": ident.person_id, "name": ident.person.get("name")}
    io.role = ident.role
    io.via = ident.via   # a Watch token runs household_identity.WATCH_TOOLS only
    io.device_id = (ident.device or {}).get("id")
    io.client = client
    io.device_tools = device_tools
    # Cross-device: a phone turn whose person has a Mac also gets the Mac's tools ("(on your Mac)").
    io.remote = ({"tools": remote["tools"], "from": remote["from"], "device": remote["mac"].get("name"),
                  "device_id": remote["mac"].get("device_id")} if remote else None)
    io.eval_item = (_turns.get(turn_id) or {}).get("eval_item")
    io.about = (_turns.get(turn_id) or {}).get("about")   # #1322: added to the turn as data, read-only
    io.item_route = lambda req, owner, mac, approve, _t=turn_id, _i=ident: _item_route(_t, _i, req, owner, mac, approve)
    io.mac_status = _mac_status_line(ident, client, remote, eval_mac) if device else None
    io.eval_mac = eval_mac
    io.eval_device = (_turns.get(turn_id) or {}).get("eval_device")
    # The skills skill_guide reads from: the loaded ones, with an eval turn's candidate swapped in.
    io.skills = {s.name: s for s in _turn_skills(turn_id)}
    try:
        io.brain = (_turns.get(turn_id) or {}).get("eval_brain")       # None: the usual routing
        io.eval_tools = (_turns.get(turn_id) or {}).get("eval_tools")
        io.eval_fixtures = (_turns.get(turn_id) or {}).get("eval_fixtures")
        io.eval_world = (_turns.get(turn_id) or {}).get("eval_world")
        io.eval_brief = (_turns.get(turn_id) or {}).get("eval_brief")   # #375's experiment arm, eval turns only
        io.eval_local_where = (_turns.get(turn_id) or {}).get("eval_local_where")   # #627's arm, eval turns only
        import retrieval_trace
        retrieval_trace.begin()          # #368: this turn's searches, content-free
        _lib.begin_turn_info()           # the context search's hits are this turn's, and citable
        # The skill pick (Jev) runs while the prompt is built, before the turn opens its egress sink:
        # collect it here and hand it to the turn (#409; live 10-02: a calendar turn asked Jev and
        # reported egress []).
        import egress as _egress
        pre_egress: list[str] = []
        prev_sink = _egress.begin(pre_egress)
        io.pre_timing = {}   # the prompt build's stages (#683); _run_agent_turn takes them over
        t_build = time.monotonic()
        try:
            messages, _history_avoided = _build_messages(question, history, tools, ws_id,
                                                         turn_id=turn_id, device=device, detail=detail,
                                                         policy=policy)
        finally:
            _egress.begin(prev_sink)
            _pre_timing("build_ms", t_build)
        io.pre_egress = pre_egress
        try:
            import proactive as _pa
            _pa.track_cards()   # the cards this turn makes, for its reply's action blocks (jellybean#201 slice 3)
            io.reply_sink = (_reply_sink(turn_id) if reply_stream.enabled() or (_turns.get(turn_id) or {}).get("eval_stream")
                             else None)   # #676: off by default; an eval turn can ask
            try:
                text = _lib._run_agent_turn(messages)
            finally:
                made = _pa.tracked_cards()
            info = dict(_lib.last_turn_info())
            if io.reply_sink is not None:   # how much was streamed and why it stopped, if it did (the eval reads it)
                info["streamed"] = {"chars": len(io.reply_sink.shown), "stopped": io.reply_sink.why or None}
            kb_hits = info.pop("_kb_hits", None) or []
            info["sources"] = _lib.cited_sources(text, kb_hits)
            # Inline cites (jellybean#636): sentence-aligned in code, behind CITE_INLINE; an eval turn may ask for them
            # (and the evidence behind each, for scripts/cite_precision.py) with the switch off.
            want_evidence = bool((_turns.get(turn_id) or {}).get("eval_cites"))
            if _lib.cite_inline_enabled() or want_evidence:
                try:
                    spans = _lib.cite_spans(text, info["sources"], kb_hits, evidence=want_evidence)
                    info["cites"] = [{"n": c["n"], "after": c["after"]} for c in spans]
                    if want_evidence:
                        info["cite_evidence"] = spans
                except Exception as exc:  # noqa: BLE001 - cites never break a turn
                    print(f"[cites] skipped: {exc}", file=sys.stderr)
            try:
                if _leaves_no_memory(_turns.get(turn_id)):
                    retrieval_trace.begin()      # an eval's or a temporary chat's searches aren't the household's usage (#370, #708)
                retrieval_trace.end(turn_id, ident.person_id, [s.get("source_id") for s in info["sources"]],
                                    corrected=1 if info.get("answer_corrections") else 0)
            except Exception as exc:  # noqa: BLE001 - the trace never breaks a turn
                print(f"[trace] write failed: {exc}", file=sys.stderr)
            info["trace"] = _lib.turn_trace(info.get("tool_names"))   # the steps line, in code (jellybean#329)
            try:
                mine = _pa.cards_for_reply(made, ident.person_id, ident.role)
            except Exception as exc:  # noqa: BLE001 - the blocks never break a turn
                mine = []
                print(f"[blocks] cards skipped: {exc}", file=sys.stderr)
            info["blocks"] = _lib.reply_blocks(text, info.pop("_links", None) or [], info["sources"],   # #201
                                               info.pop("_images", None) or [], cards=mine,
                                               results=info.pop("_results", None) or [],
                                               choices=info.pop("_choices", None),   # its untrusted mark is set when offered
                                               saved=info.pop("_saved", None) or [],   # jellybean#699
                                               files=info.pop("_files", None) or [])
            with _turns_lock:
                _turns[turn_id].update(info)   # which brain answered, any fallback, KB sources used
        except Exception as exc:
            # "error" stays raw (the turn log, GET /v1/turns); "message" is what a person sees.
            if _finish(turn_id, "error", error=str(exc), message=_lib.plain_failure(exc),
                       actions=_lib.failure_actions(exc)):   # buttons, jellybean#162
                # 09-29/30: nine turns that failed (a BrokenPipe, a timeout) never reached the chat
                # history, so the next turn didn't know they'd happened. Record them, with what broke.
                _remember_turn(turn_id, ident, question,
                               f"(This reply didn't go through: {_lib.plain_failure(exc)})")
            raise
    finally:
        # read once, then gone, whether the turn finished or failed (it grew by one per turn, forever; a failed turn's
        # stayed until the review of 10-10 moved this into the finally)
        tc = _session_token_stats.pop(turn_id, None)
        if tc is not None:   # /health's session_token_savings counted a failed turn's while it leaked: still counted
            global _token_savings_done
            _token_savings_done += getattr(tc, "estimated_savings_tokens", 0) or 0
        io.reply_sink = None
        io.pre_timing = None   # never handed to a later turn on this thread (#683)
        io.device = None
        io.cancelled = None
        io.status = None
        io.detail = None
        io.effort = io.effort_level = None
        io.source = None
        io.brain = None
        io.eval_tools = None
        io.eval_fixtures = None
        io.eval_world = None
        io.eval_brief = None
        io.eval_local_where = None
        io.world_newer = None
        io.client = None
        io.device_tools = None
        io.remote = None
        io.item_route = io.eval_item = io.about = None
        io.mac_status = None
        io.eval_mac = None
        io.eval_device = None
        io.device_completed = None
        io.person = None
        io.role = None
        io.via = None
        io.device_id = None
        io.skills = None
        io.pre_egress = None
        io.thread = None

    token_counts = (
        {
            "estimated_input_chars": tc.estimated_input_chars,
            "recall_injected_chars": tc.recall_injected_chars,
            "history_avoided_chars": tc.history_avoided_chars,
            "estimated_savings_tokens": getattr(tc, "estimated_savings_tokens", 0),
        }
        if tc else {}
    )
    # Where the person is (where_am_i, jellybean#387) is never kept: taken off the turn record now, whatever happens next.
    where = (_turns.get(turn_id) or {}).pop("_where_said", None)
    if not _finish(turn_id, "done", text=text, token_counts=token_counts):
        return text   # stopped (POST …/cancel, or a Mac's disconnect): its answer isn't shown or remembered

    # Memory: record the turn in the person's chat conversation and extract facts every few turns.
    # (Each turn used to be its own conversation, so "every 5th turn of a conversation" never
    # came and titan learned nothing from chat: 218 messages, 0 facts.) Zach keeps "chat"; each
    # other person has "chat:<id>" and facts of their own; a shared household device remembers
    # nothing. Test traffic sends remember: false.
    if _leaves_no_memory(_turns.get(turn_id)):
        return text   # an eval turn or a temporary chat (jellybean#708): nothing remembered, no household facts saved
    if ident.role == "household":
        return text
    # A turn that read health data (health_summary): the conversation keeps it (the person's own),
    # but no facts are extracted and nothing goes to the knowledge base.
    sensitive = bool(set((_turns.get(turn_id) or {}).get("tool_names") or []) & _lib.SENSITIVE_TOOLS)
    _remember_turn(turn_id, ident, question, _lib.without_location(text, where) if where else text, extract=not sensitive)
    # Same household-fact saving the CLI turn does (Jev-gated; explicit "remember ..." without Jev).
    # It writes the family knowledge base, which only adults (and the admin) edit.
    if ident.role in ("admin", "adult") and not sensitive:
        try:
            _lib._maybe_save_fact(question, text,
                                  noted="ingest_text" in ((_turns.get(turn_id) or {}).get("tool_names") or []))
        except Exception as exc:  # noqa: BLE001
            _lib.quiet_failure("fact save", exc)

    return text


def _memory_writes(person_id: str | None) -> Any:
    """Whether titan may store something new in this person's memory (#1690): governance's "memory" capability, on
    until they turn it off, and not killed for the house. Reading, forgetting, exporting and deleting never ask."""
    import governance
    return governance.allowed(person_id, "memory")


def _remember_turn(turn_id: str, ident: "_hh.Identity", question: str, reply: str, *, extract: bool = False) -> None:
    """Record a turn in the person's chat conversation (never an eval turn, a household device, or
    remember: false). A failed turn is recorded too, with what broke, so the next turn knows."""
    t = _turns.get(turn_id) or {}
    if _leaves_no_memory(t) or ident.role == "household" or not t.get("remember", True):
        return
    if not _MEMORY_AVAILABLE:
        return
    try:
        mem = _get_memory()
        conv = _hh.conv_id_for(ident.person_id)
        import redact
        # The history titan keeps holds no account numbers or codes (09-30: an attached bill's account
        # number sat in it, and every later turn that read that history sent it to Gemini).
        import conversations
        # platform (#1287): the paired device's own (a TV named "Living room" is a TV); null for the master token
        where = {"thread": t.get("thread"), "device": (ident.device or {}).get("name") or t.get("client"),
                 "platform": conversations.platform_of(ident.device)}
        said, answered = redact.redact_personal(question), redact.redact_personal(reply)
        u = mem.add_message(conv, "user", said, client_msg_id=t.get("client_msg_id"), **where)
        a = mem.add_message(conv, "assistant", answered, tools=list(t.get("tool_names") or []) or None, **where)
        if isinstance(u, dict):   # which things the turn's tools touched, beside its question (#1016; keyed, no text)
            try:
                import chat_links
                chat_links.note_turn(ident.person_id, u.get("id"), where.get("thread"), float(u.get("ts") or time.time()),
                                     t.get("tool_names") or [], t.get("tool_targets") or [])
            except Exception as exc:  # noqa: BLE001 - the chat is kept without them
                print(f"[chat_links] turn not noted: {type(exc).__name__}", file=sys.stderr)
        if t.get("file_under") and where.get("thread"):   # "New chat here" (jellybean#240): its first turn files it
            try:
                import spaces
                project, group = spaces.check_target(ident.person_id, ident.role, **t["file_under"])
                spaces.tag(ident.person_id, where["thread"], project, group, only_new=True)
            except Exception as exc:  # noqa: BLE001 - a project they can't see files nothing; the chat is kept
                print(f"[spaces] not filed: {type(exc).__name__}", file=sys.stderr)
        _file_turn_made(t, ident)
        if t.get("branched_from") and where.get("thread"):   # a branch's first turn: where it came from (#895)
            try:
                import spaces
                spaces.note_branch(ident.person_id, where["thread"], spaces.clean_branch(t["branched_from"]))
            except Exception as exc:  # noqa: BLE001 - the lineage is a nicety; the chat is kept
                print(f"[spaces] lineage not kept: {type(exc).__name__}", file=sys.stderr)
        try:
            # Their other devices see the turn (jellybean#138), before the slow fact extraction; the
            # asking device skips its own echo.
            import continuity
            # The reply names its question's client_msg_id (`reply_to`), so a device without its own id
            # (the master token) can still skip both halves of its turn.
            cmid = t.get("client_msg_id")
            for m, role, text, extra in ((u, "user", said, {"client_msg_id": cmid}),
                                         (a, "assistant", answered, {"client_msg_id": None, "reply_to": cmid})):
                if isinstance(m, dict):
                    continuity.emit(ident.person_id, "message", {"thread_id": where["thread"], "msg_id": m["id"], "role": role,
                                                                 "text": text, "at": m["ts"], "device": where["device"], **extra},
                                    device=(ident.device or {}).get("id"))
        except Exception:  # noqa: BLE001 - the feed never stops memory
            pass
        if extract and _memory_writes(ident.person_id).run:   # memory off for them (#1690): no facts, no dev notes
            mem.maybe_extract_facts(conv, person_id=ident.person_id,
                                    on_facts=_world_from_memory(ident) if _lib.world_on() else None)
    except Exception:  # noqa: BLE001 - memory must never break a turn
        pass


def _world_from_memory(ident):
    """The memory extractor's facts → world claims for this person (#358): corrections of facts that back a
    claim become their set_fact, new facts with a value become proposals. Decided in code (world.from_memory)."""
    def hook(added, corrected, user_text, evaluator):
        import world
        try:
            folks = _store().people()
        except Exception:  # noqa: BLE001
            folks = []
        if corrected:
            try:   # #368: what the corrected answer drew on, marked for tuning (content-free)
                import retrieval_trace
                retrieval_trace.mark_corrected(ident.person_id)
            except Exception:  # noqa: BLE001
                pass
        got = world.from_memory(world.get(), ident.person_id, ident.role, folks, added, corrected, user_text, evaluator)
        for cid, attr, _, code in got:
            print(f"[world] memory → {cid} {attr}: {code}", file=sys.stderr)   # never the value: it's personal
    return hook


async def _sse_gen(turn_id: str, question: str, history: list, tools: bool, ws_id: str | None,
                   device: bool = False, detail: str | None = None,
                   policy: str | None = None) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(_TURN_POOL, _run_turn, turn_id, question, history, tools, ws_id, device, detail, policy)
    _turn_futs.add(fut)
    fut.add_done_callback(lambda f: (_turn_futs.discard(f), f.cancelled() or f.exception()))
    finished = False
    try:
        async for chunk in _sse_follow(turn_id):
            yield chunk
        finished = True
    except (GeneratorExit, asyncio.CancelledError):
        # The client went away (closed at a yield, or cancelled mid-wait). A phone (RESUMABLE_CLIENTS) follows its turn (GET /v1/turns/{id}, or the same
        # client_msg_id again) and stops it by name, so the turn runs on. Any other client's disconnect is its
        # Stop: the turn is cancelled, and nothing more of it runs or is remembered.
        if (_turns.get(turn_id) or {}).get("client") not in RESUMABLE_CLIENTS:
            with _turns_lock:
                t = _turns.get(turn_id)
                if t and t.get("state") in ("running", "device_pending"):
                    t.update({"state": "cancelled", "finished_at": time.time()})
                    if t.get("device_event") is not None:
                        t["device_event"].set()
        raise
    finally:
        if finished:   # the reply was sent: let memory finish before the response closes, as before
            try:
                await asyncio.shield(fut)
            except Exception:
                pass


def _reply_sink(turn_id: str) -> "reply_stream.ReplyStream":
    """The turn's streamed reply (#676): each time the safe text grows it's kept on the turn, and _sse_follow sends
    it (a re-attach gets it from the start)."""
    def publish(shown: str) -> None:
        with _turns_lock:
            if turn_id in _turns:
                _turns[turn_id]["streaming"] = shown
    return reply_stream.ReplyStream(publish, filter=_lib.stream_filter, stops=_lib.stream_stops)


async def _sse_follow(turn_id: str) -> AsyncIterator[str]:
    """Stream one turn from wherever it is: the first stream, or a re-attach by client_msg_id (status and events
    from the start, the device request still pending, the answer, or its done again). Starts nothing."""
    sent = ""   # the reply text sent as deltas so far
    sent_status = 0
    sent_device: set[str] = set()
    sent_events = 0
    while True:
        await asyncio.sleep(0.05)
        _stuck(turn_id)   # past TURN_STUCK_S: an error now, below
        with _turns_lock:
            turn = _turns.get(turn_id, {})
            state = turn.get("state", "running")
            text  = turn.get("text", "")
            streaming = turn.get("streaming", "")   # the safe streamed prefix while it runs (#676)
            error = turn.get("message") or turn.get("error")
            error_actions = list(turn.get("actions") or [])
            req   = turn.get("_device_req")
            status = list(turn.get("status", []))
            events = list(turn.get("events", []))
        watch = _is_watch_turn(turn)

        if watch:   # a kid's Watch (10-07 review W4): one generic status, never a label, event or device request
            if status and not sent_status:
                yield _sse("status", {"text": WATCH_STATUS})
        else:
            for label in status[sent_status:]:
                yield _sse("status", {"text": label})
            for event, data in events[sent_events:]:   # cross-device: remote / confirm
                yield _sse(event, data)
        sent_status, sent_events = len(status), len(events)

        # Deltas come only from the turn's finished text, which has already been through the reply
        # filters (_with_credentials_removed, _with_honest_outcome, _with_plain_tool_names). If model
        # tokens are ever streamed while the turn runs, those filters must run on the stream too.
        # Streamed (#676), only the prefix reply_stream judged safe goes out while the turn runs; a later text
        # that doesn't extend what was sent goes only in `done`, which the apps show in its place.
        piece, sent = reply_stream.next_delta(sent, text or streaming)
        if piece and not watch:   # a kid's Watch gets only the shaped `done` (titan-node#755)
            yield _sse("delta", {"text": piece})

        if state == "device_pending" and req and req.get("request_id") not in sent_device and not watch:
            sent_device.add(req["request_id"])
            yield _sse("device", {"turn_id": turn_id, **req})

        if state == "cancelled" or turn_id not in _turns:
            yield _sse("error", {"message": "This turn was stopped.", "actions": []})
            break

        if state == "error":
            yield _sse("error", {"message": WATCH_ERROR, "actions": []} if watch else
                       {"message": error or "unknown error", "actions": error_actions})
            break

        if state == "done":
            with _turns_lock:
                final = _turns[turn_id].get("text", "")
                truncated = bool(_turns[turn_id].get("truncated"))
                sources = list(_turns[turn_id].get("sources") or [])
                egress_kinds = list(_turns[turn_id].get("egress") or [])
                is_eval = _turns[turn_id].get("source") == "eval"
                skills_seen = _turns[turn_id].get("skills") if is_eval else None
                answered = {"brain": _turns[turn_id].get("brain"), "fallback": _turns[turn_id].get("fallback")} if is_eval else None
                # Eval turns: which tools ran, titan's own included (turn_eval checks a promise was
                # backed by schedule_action; device tools alone show on the stream).
                tools_seen = list(_turns[turn_id].get("tool_names") or []) if is_eval else None
                ran_seen = list(_turns[turn_id].get("ran") or []) if is_eval else None   # what ran, not refused (#1203)
                if skills_seen is not None:
                    skills_seen = {**skills_seen, "read": list(_turns[turn_id].get("skills_read") or [])}
            piece, sent = reply_stream.next_delta(sent, final)
            if piece and not _is_watch_turn(_turns.get(turn_id, {})):
                yield _sse("delta", {"text": piece})
            # egress: what left the house this turn (#409), kinds only; [] = fully local. The apps'
            # "left the house" badge reads it (contract on titan-node#409).
            done = {"text": final, "truncated": truncated, "sources": sources, "egress": egress_kinds,
                    "blocks": list(_turns[turn_id].get("blocks") or [])}   # #201 (older apps ignore it)
            if _turns[turn_id].get("trace"):
                done["trace"] = _turns[turn_id]["trace"]   # what the turn did, one line (jellybean#329)
            if _turns[turn_id].get("peek"):
                done["peek"] = _turns[turn_id]["peek"]     # a private peek as data (jellybean#426); the text is unchanged
            if _turns[turn_id].get("cites"):
                done["cites"] = _turns[turn_id]["cites"]   # inline cites beside sources (jellybean#636); older apps ignore them
            if _turns[turn_id].get("cite_evidence") and is_eval:
                done["cite_evidence"] = _turns[turn_id]["cite_evidence"]   # eval turns that asked only (cite_precision.py)
            if tools_seen is not None:
                done["tools"] = tools_seen
                done["ran"] = ran_seen
            if skills_seen is not None:
                done["skills"] = skills_seen   # eval turns only (scripts/skillopt): picked skill, body shown, guides read
            if answered is not None:
                done["answered"] = answered    # eval turns only: which brain really answered (truth_eval)
            if _is_watch_turn(_turns.get(turn_id, {})):
                done = _watch_shaped(done)   # derived facts only: short text, nothing quoted (titan-node#755)
            yield _sse("done", done)
            # A long answer's headline (jellybean#584), after `done` so the answer is never held for it: its own event,
            # `tldr` {turn_id, tldr}, within TLDR_WAIT seconds; nothing on a short or local-only answer or any failure.
            if not truncated and not watch:   # never on a Watch: it gets the shaped `done` only
                tldr = await _reply_tldr_soon(final, egress_kinds)
                if tldr:
                    yield _sse("tldr", {"turn_id": turn_id, "tldr": tldr})
            break


TLDR_WAIT = 3.0


async def _reply_tldr_soon(final: str, egress: list[str], make: Any = None) -> str | None:
    """reply_tldr in a thread, at most TLDR_WAIT seconds; None when it isn't due, slow or fails (`make` for tests)."""
    if len(final or "") < _lib.TLDR_MIN_CHARS or "gemini" not in (egress or []):
        return None
    try:
        return await asyncio.wait_for(asyncio.to_thread(make or _lib.reply_tldr, final, egress=egress), TLDR_WAIT)
    except Exception:  # noqa: BLE001
        return None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser(description="Titan Agent HTTP API")
    parser.add_argument("--host",   default="127.0.0.1")
    parser.add_argument("--port",   type=int, default=AGENT_API_PORT)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--no-discovery", action="store_true", help="don't advertise _titan._tcp over mDNS")
    args = parser.parse_args()
    # Read by the app's startup hook (uvicorn imports this module afresh as agent_api_server).
    os.environ["AGENT_API_PORT"] = str(args.port)
    if not args.no_discovery:
        os.environ.setdefault("TITAN_DISCOVERY", "1")
    print(f"[agent-api] http://{args.host}:{args.port}", flush=True)
    # A restart shouldn't wait out every open stream: the Mac's 25 s long-poll and live SSE turns held
    # a deploy in "deactivating" for 20-30 s (09-30). After 5 s they're closed; clients reconnect.
    uvicorn.run("agent_api_server:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info",
                timeout_graceful_shutdown=float(os.environ.get("AGENT_API_STOP_GRACE_S", "5")))
