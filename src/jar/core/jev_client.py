#!/usr/bin/env python3
"""jev — stdlib-only client, CLI and Claude Code hooks for Jev (TypeSafe System One).

COPY of ~/Projects/claude-jev/skills/jev/scripts/jev.py (the claude-jev plugin is canonical);
re-copy after editing either. On titan the key comes from agent.env (OPENROUTER_API_KEY).

Jev is a hosted decision model: send a `state` plus typed questions (choice / score /
noul), get typed answers with probabilities back. It never writes text. The contract is
identical on TypeSafe direct and on OpenRouter (`POST {base}/v1/systemone`); only the
base URL and key differ. OpenRouter additionally returns `usage.cost`.

Credentials (first hit wins; the key is never printed or logged):
  1. env JEV_API_KEY        (+ JEV_BASE_URL, default OpenRouter)
  2. env TYPESAFE_API_KEY   (+ TYPESAFE_BASE_URL, default https://api.typesafe.ai)
  3. env OPENROUTER_API_KEY (OpenRouter)
  4. macOS Keychain item `jev-openrouter`, then `jev-typesafe`
     (store with: security add-generic-password -U -a "$USER" -s jev-openrouter -w)

State lives in $JEV_HOME (default ~/.claude/jev): state.json (toggles) and usage.jsonl
(one line per call: latency, tokens, cost — never the content sent).

This file is also the library the titan household agent imports (scripts/jev_client.py
in titan-node is a copy — keep them in sync).
"""
from __future__ import annotations

import argparse
import concurrent.futures as _cf
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

VERSION = "1.0.0"
OPENROUTER_BASE = "https://openrouter.ai/api"
TYPESAFE_BASE = "https://api.typesafe.ai"
# Pinned per provider so thresholds stay calibrated to one model version.
DEFAULT_MODEL = {"openrouter": "typesafe/jev-1.13", "typesafe": "jev-1.13.0", "broker": "typesafe/jev-1.13",
                 "local": "drex-v1.5"}   # a local server (#1434): JEV_MODEL names its model when it isn't Drex
PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000   # output is free
# Below this confidence an answer goes to review (Claude or a person decides). 0.75 came from the
# 2026-09-25 household calibration (0.6 let half the mistakes through). The saved setting in
# state.json wins; this is the fallback everywhere, so a missing setting never lowers the bar.
REVIEW_THRESHOLD = 0.75
KEYCHAIN_SERVICES = (("jev-openrouter", "openrouter"), ("jev-typesafe", "typesafe"))
RETRY_STATUSES = {429, 500, 502, 503, 524, 529}

# Claude Code's config dir (CLAUDE_CONFIG_DIR when set, else ~/.claude); state lives beside it.
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
JEV_HOME = Path(os.environ.get("JEV_HOME") or CLAUDE_HOME / "jev")
SKILL_DIR = Path(__file__).resolve().parent.parent
# Installed as a Claude Code plugin (skills/jev inside a plugin root): the plugin ships the
# hooks (hooks/hooks.json) and the helper agents (agents/) itself, so install/uninstall must
# not write them into ~/.claude, and doctor checks the plugin's copies instead.
PLUGIN_ROOT = SKILL_DIR.parent.parent if (SKILL_DIR.parent.parent / ".claude-plugin" / "plugin.json").exists() else None

DEFAULT_STATE: dict[str, Any] = {
    "router": False,        # size every prompt and suggest a pinned helper agent
    "skills": True,         # (only while router is on) also suggest the best-matching skill
    "perms": False,         # auto-approve routine Bash permission prompts
    "threshold": REVIEW_THRESHOLD,   # below this confidence, Claude decides instead of Jev
    "perms_threshold": 0.9,
    "tiers": {"tiny": "haiku", "everyday": "sonnet", "large": "opus", "hardest": "fable"},
}


class JevError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Credentials + transport
# ---------------------------------------------------------------------------

def _is_local(base: str) -> bool:
    """A System One server on this machine or the LAN (a local Drex, #1434): loopback or a private address, never a
    hosted provider. Such a base needs no key and is never charged Jev's per-token price."""
    import ipaddress
    import urllib.parse
    host = (urllib.parse.urlparse(base).hostname or "").strip("[]").lower()
    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _provider_for(base: str) -> str:
    if _is_local(base):
        return "local"
    return "openrouter" if "openrouter.ai" in base else "typesafe"


def _bearer(creds: dict[str, str]) -> str:
    """The Authorization token: the Jev key, or a fresh coordinator JWT for a decision-broker."""
    if creds.get("provider") != "broker":
        return creds["key"]
    try:
        return mint_coordinator_jwt(creds["key_file"], creds.get("sub", "jev-client"))
    except Exception as exc:  # noqa: BLE001 — missing file, wrong key type, no `cryptography`
        raise JevError(f"coordinator key unusable ({creds['key_file']}): {exc}") from None


def mint_coordinator_jwt(key_file: str, sub: str, ttl: int = 300) -> str:
    """A short-lived EdDSA coordinator token (the fleet's claim shape) signed with an Ed25519 key."""
    import base64
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    with open(os.path.expanduser(key_file), "rb") as fh:
        key = load_pem_private_key(fh.read(), None)
    now = int(time.time())
    head = b64(json.dumps({"alg": "EdDSA", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64(json.dumps({"sub": sub, "type": "coordinator", "iat": now, "exp": now + ttl},
                          separators=(",", ":")).encode())
    return f"{head}.{body}.{b64(key.sign(f'{head}.{body}'.encode()))}"


def _keychain(service: str) -> str:
    if sys.platform != "darwin" or not shutil.which("security"):
        return ""
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def resolve_credentials() -> dict[str, str]:
    """Return {key, base, provider, source}; raises JevError if no key is configured.

    With JEV_AUTH=coordinator the base URL is a decision-broker and there is no Jev key here:
    every call carries a short-lived EdDSA coordinator token signed with JEV_COORDINATOR_KEY_FILE
    (the caller's own Ed25519 private key, whose public half the broker allowlists)."""
    if os.environ.get("JEV_AUTH", "").strip().lower() == "coordinator":
        base = os.environ.get("JEV_BASE_URL", "").strip().rstrip("/")
        key_file = os.environ.get("JEV_COORDINATOR_KEY_FILE", "").strip()
        if not base or not key_file:
            raise JevError("JEV_AUTH=coordinator needs JEV_BASE_URL (the decision-broker) and JEV_COORDINATOR_KEY_FILE")
        return {"key": "", "base": base, "provider": "broker", "source": f"coordinator:{key_file}",
                "key_file": key_file, "sub": os.environ.get("JEV_COORDINATOR_ID", "jev-client")}
    local = os.environ.get("JEV_BASE_URL", "").strip().rstrip("/")
    if local and _is_local(local) and not os.environ.get("JEV_API_KEY", "").strip():
        # a local System One server (#1434: Drex on The Box): no key, and no cloud key is ever sent to it
        return {"key": "", "base": local, "provider": "local", "source": "local:JEV_BASE_URL"}
    if os.environ.get("JEV_API_KEY", "").strip():
        base = os.environ.get("JEV_BASE_URL", OPENROUTER_BASE).rstrip("/")
        return {"key": os.environ["JEV_API_KEY"].strip(), "base": base,
                "provider": _provider_for(base), "source": "env:JEV_API_KEY"}
    if os.environ.get("TYPESAFE_API_KEY", "").strip():
        base = os.environ.get("TYPESAFE_BASE_URL", TYPESAFE_BASE).rstrip("/")
        return {"key": os.environ["TYPESAFE_API_KEY"].strip(), "base": base,
                "provider": _provider_for(base), "source": "env:TYPESAFE_API_KEY"}
    if os.environ.get("OPENROUTER_API_KEY", "").strip():
        return {"key": os.environ["OPENROUTER_API_KEY"].strip(), "base": OPENROUTER_BASE,
                "provider": "openrouter", "source": "env:OPENROUTER_API_KEY"}
    for service, provider in KEYCHAIN_SERVICES:
        key = _keychain(service)
        if key:
            base = OPENROUTER_BASE if provider == "openrouter" else TYPESAFE_BASE
            return {"key": key, "base": base, "provider": provider, "source": f"keychain:{service}"}
    raise JevError("no Jev key configured (set OPENROUTER_API_KEY / TYPESAFE_API_KEY, "
                   "or: security add-generic-password -U -a \"$USER\" -s jev-openrouter -w)")


ORIGIN_HEADERS = {"source": "X-Decision-Source", "person": "X-Decision-Person", "role": "X-Decision-Role"}


def origin_headers(origin: dict[str, str] | None, creds: dict[str, str]) -> dict[str, str]:
    """X-Decision-* headers for a decision-broker; nothing at all for a model provider, so a
    household member's id never leaves for OpenRouter or TypeSafe."""
    if not origin or creds.get("provider") != "broker":
        return {}
    return {ORIGIN_HEADERS[k]: str(v) for k, v in origin.items() if k in ORIGIN_HEADERS and v}


def decide(state: Any, questions: dict[str, Any], *, model: str | None = None,
           timeout: float = 15.0, retries: int = 2, source: str = "api",
           creds: dict[str, str] | None = None,
           origin: dict[str, str] | None = None) -> dict[str, Any]:
    """One System One call. Returns the API response plus `_meta` {latency_ms, cost_usd, ...}.

    Retries 429/5xx/529 with exponential backoff. Raises JevError on anything else.
    `origin` ({"source", "person", "role"}: e.g. user / robin / adult, or canary) is sent to a
    decision-broker as X-Decision-* headers so its readouts can tell traffic apart; it is never
    sent to a model provider.
    """
    if not questions:
        raise JevError("questions must be a non-empty map")
    creds = creds or resolve_credentials()
    model = model or os.environ.get("JEV_MODEL") or DEFAULT_MODEL[creds["provider"]]
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    url = f"{creds['base']}/v1/systemone"
    delay = 0.25
    t0 = time.monotonic()
    last: JevError | None = None
    for attempt in range(retries + 1):
        bearer = _bearer(creds)   # per attempt: a coordinator JWT is fresh each time; "" for a keyless local server
        req = urllib.request.Request(url, data=body, method="POST", headers={
            **({"Authorization": f"Bearer {bearer}"} if bearer else {}),   # a local server may take none (#1434)
            "Content-Type": "application/json",
            "User-Agent": f"jev-skill/{VERSION}",
            **origin_headers(origin, creds),
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode() or "{}")
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:600]
            last = JevError(f"HTTP {exc.code}: {detail}", exc.code)
            if exc.code not in RETRY_STATUSES or attempt == retries:
                _log_usage(source, ok=False, latency_ms=_ms(t0), error=f"HTTP {exc.code}")
                raise last from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = JevError(f"network error: {exc}")
            if attempt == retries:
                _log_usage(source, ok=False, latency_ms=_ms(t0), error="network")
                raise last from None
        time.sleep(delay)
        delay *= 2
    if not isinstance(data.get("answers"), dict):
        raise JevError(f"malformed response: {json.dumps(data)[:300]}")
    usage = data.get("usage") or {}
    cost = usage.get("cost")
    if cost is None:   # a local server runs on our own hardware: $0, never Jev's hosted price (#1434)
        cost = 0.0 if creds["provider"] == "local" else (usage.get("input_tokens") or 0) * PRICE_PER_INPUT_TOKEN
    meta = {"latency_ms": _ms(t0), "cost_usd": float(cost), "provider": creds["provider"],
            "model": data.get("model", model), "input_tokens": usage.get("input_tokens")}
    data["_meta"] = meta
    _log_usage(source, ok=True, latency_ms=meta["latency_ms"], cost=meta["cost_usd"],
               tokens=meta["input_tokens"])
    return data


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


# ---------------------------------------------------------------------------
# Answer helpers
# ---------------------------------------------------------------------------

def sureness(answer: dict[str, Any]) -> float:
    """How sure Jev is, 0..1, for any answer type (noul: distance-from-coin-flip as max(p, 1-p))."""
    if answer.get("type") == "noul":
        p = float(answer.get("noul", 0.5))
        return max(p, 1 - p)
    return float(answer.get("confidence", 0.0))


def value(answer: dict[str, Any]) -> Any:
    t = answer.get("type")
    return answer.get("choice") if t == "choice" else answer.get("score") if t == "score" else answer.get("noul")


def summarize(answers: dict[str, Any]) -> str:
    parts = []
    for k, a in answers.items():
        v = value(a)
        v = f"{v:.2f}" if isinstance(v, float) else v
        if a.get("type") == "score":
            top = a.get("legend", {}).get(str(round(float(a.get("score", 0)))), "")
            v = f"{v} ({top})" if top else v
        parts.append(f"{k}={v} [sure {sureness(a):.2f}]")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# State + usage log
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    st = json.loads(json.dumps(DEFAULT_STATE))
    try:
        st.update(json.loads((JEV_HOME / "state.json").read_text()))
    except Exception:
        pass
    return st


def save_state(st: dict[str, Any]) -> None:
    JEV_HOME.mkdir(parents=True, exist_ok=True)
    (JEV_HOME / "state.json").write_text(json.dumps(st, indent=2) + "\n")


def _log_usage(source: str, **fields: Any) -> None:
    try:
        JEV_HOME.mkdir(parents=True, exist_ok=True)
        rec = {"ts": round(time.time(), 3), "source": source, **fields}
        with open(JEV_HOME / "usage.jsonl", "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _read_usage() -> list[dict[str, Any]]:
    try:
        return [json.loads(l) for l in (JEV_HOME / "usage.jsonl").read_text().splitlines() if l.strip()]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Level 1: prompt router (UserPromptSubmit hook)
# ---------------------------------------------------------------------------

TIER_CRITERIA = {
    "tiny": "A tiny job: a lookup, a rename, a yes/no, or a one-line answer",
    "everyday": "An everyday job: a normal email, post, short document, or a small focused code change",
    "large": "A large job: a multi-step build, a multi-file change, research, or a full report",
    "hardest": "The hardest jobs: strategy, architecture, security, or anything where a wrong call is expensive",
}


def _frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return {}
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    key = None
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if km:
            key, val = km.group(1), km.group(2).strip()
            out[key] = "" if val in (">", ">-", "|", "|-") else val.strip("'\"")
        elif key and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def skill_catalog(cwd: str | None = None) -> dict[str, str]:
    """name -> first sentence of description, for user + project skills not switched off."""
    try:
        off = {k for k, v in json.loads((CLAUDE_HOME / "settings.json").read_text())
               .get("skillOverrides", {}).items() if v == "off"}
    except Exception:
        off = set()
    roots = [CLAUDE_HOME / "skills"] + ([Path(cwd) / ".claude" / "skills"] if cwd else [])
    cat: dict[str, str] = {}
    for root in roots:
        for sk in sorted(root.glob("*/SKILL.md")):
            fm = _frontmatter(sk)
            name = fm.get("name") or sk.parent.name
            if name in off or name == "jev" or name.endswith("-workspace"):
                continue
            desc = re.split(r"(?<=[.!?])\s", fm.get("description", ""), maxsplit=1)[0]
            cat[name] = desc[:220] or None  # type: ignore[assignment]
    return dict(list(cat.items())[:254])


def route(message: str, *, cwd: str | None = None, with_skills: bool = True,
          timeout: float = 3.0, source: str = "router") -> dict[str, Any]:
    """Size a message into a tier (and optionally match a skill) in ONE fan-out call."""
    questions: dict[str, Any] = {
        "tier": {"type": "choice",
                 "instructions": "What is the smallest AI model size that can do the job in `message` well?",
                 "criteria": TIER_CRITERIA},
        "needs_context": {"type": "noul",
                          "instructions": "`message` is a short follow-up or reply (like 'yes do that' or "
                                          "'make it shorter') that only makes sense inside an ongoing conversation."},
    }
    cat = skill_catalog(cwd) if with_skills else {}
    if cat:
        questions["skill"] = {"type": "choice",
                              "instructions": "Which skill in the catalog best fits the job in `message`? "
                                              "Pick `none` unless one clearly applies.",
                              "criteria": {**cat, "none": "No skill clearly applies"}}
    resp = decide({"message": message[:6000]}, questions, timeout=timeout, retries=0, source=source)
    a = resp["answers"]
    out = {"tier": a["tier"]["choice"], "tier_conf": float(a["tier"].get("confidence", 0)),
           "needs_context": float(a["needs_context"]["noul"]), "_meta": resp["_meta"]}
    if "skill" in a:
        out["skill"] = a["skill"]["choice"]
        out["skill_conf"] = float(a["skill"].get("confidence", 0))
    return out


def _is_trivial_prompt(prompt: str) -> bool:
    p = prompt.strip()
    return (not p or p.startswith("/") or p.startswith("<") or len(p) < 20 or len(p.split()) <= 3)


def hook_prompt() -> int:
    """UserPromptSubmit hook. Never blocks, never errors: any failure → silent exit 0."""
    try:
        st = load_state()
        if not st.get("router"):
            return 0
        inp = json.load(sys.stdin)
        prompt = str(inp.get("prompt", ""))
        if _is_trivial_prompt(prompt):
            return 0
        timeout = float(os.environ.get("JEV_ROUTER_TIMEOUT", "3"))
        r = route(prompt, cwd=inp.get("cwd"), with_skills=bool(st.get("skills")), timeout=timeout)
    except Exception:
        return 0
    thr = float(st.get("threshold", REVIEW_THRESHOLD))
    notes = []
    tier, conf = r["tier"], r["tier_conf"]
    model = st.get("tiers", {}).get(tier)
    decided = conf >= thr and r["needs_context"] < 0.5 and bool(model)
    _log_usage("router-decision", tier=tier if decided else "self", conf=round(conf, 3))
    if decided:
        notes.append(
            f"[jev-router] Jev sized this as {tier.upper()} → {model} (confidence {conf:.2f}). "
            f"If the work is self-contained and you are larger than {model}, delegate it to the "
            f"{_agent_ref(model)} agent and relay its result; if it depends on this conversation, or you "
            f"already are {model}, handle it yourself. (Advisory: a hook cannot switch models.)")
    skill, sconf = r.get("skill"), r.get("skill_conf", 0.0)
    if skill and skill != "none" and sconf >= thr:
        notes.append(f"[jev-router] Skill match: `{skill}` (confidence {sconf:.2f}) — load it if it fits.")
    if notes:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": "\n".join(notes)}}))
    return 0


# ---------------------------------------------------------------------------
# Permission gate (PermissionRequest hook) — static risk list first, then Jev
# ---------------------------------------------------------------------------

_RISKY = [re.compile(p) for p in (
    r"^(\S*['\"\\=$]|[-0-9])",
    r"^(sudo|doas|su)\b",
    r"^(bash|sh|zsh|fish|eval|xargs|source)\b|^\.(\s|$)",
    r"^(node|bun|deno|python3?|ruby|perl)\s+(-\S+\s+)*(-c|-e|-p|--eval)\b",
    r"^rm\b",
    # (checkout -- has its own branch: "--" is not a word, so a trailing \b could never match it)
    r"^git\b.*\b(?:(?:push|reset\s+--hard|clean\s+-\S*[fd]|branch\s+-D|restore|filter-branch|rebase)\b|checkout\s+--(?:\s|$)|checkout\s+\.(?:\s|$))",
    r"^(npm|pnpm|yarn|bun)\s+(publish|unpublish)\b",
    # installs change the environment and pull code from the network
    r"^(npm|pnpm|yarn|bun)\s+(i|install|add|ci|update|upgrade|remove|uninstall)\b|^(pip3?|pipx|uv\s+pip|brew|cargo|gem|go)\s+(install|uninstall|add|upgrade)\b|^uv\s+(add|sync|remove)\b",
    r"^(wrangler|vercel|fly|flyctl|netlify)\s+(deploy|--prod)\b|^vercel\b.*--prod\b",
    r"^terraform\s+(apply|destroy)\b|^kubectl\s+(delete|apply|exec)\b",
    r"^railway\s+(up|down|delete|redeploy|variables?\s+(set|delete))\b",
    r"^gh\s+(pr\s+merge|release|repo\s+(delete|edit)|secret|api\b.*-X\s*(POST|PATCH|PUT|DELETE))",
    r"^(ssh|scp|rsync|sftp|mosh)\b",
    r"^(curl|wget|http|xh)\b.*(\s-X\s*(POST|PUT|PATCH|DELETE)|\s--data|\s-d\s|\s-F\s|--upload|\s-T\s)",
    r"^(psql|dropdb|createdb|mysql|mongo|redis-cli)\b",
    r"^(launchctl|systemctl|defaults\s+write|security|csrutil|spctl|tccutil|networksetup|scutil)\b",
    r"^(docker|podman)\s+(rm|rmi|push|system\s+prune|volume\s+rm)\b",
    r"^(kill|killall|pkill|shutdown|reboot|halt)\b",
    r"^(dd|mkfs|diskutil|fdisk|mount|umount)\b",
    r"^(chmod|chown)\b.*\s-R\b",
    r"^(crontab|at)\b",
    r"(?i)\.env\b|\.ssh\b|\.aws\b|\.npmrc\b|\.netrc\b|\.pgpass\b|credentials|secret|token|keychain|agent\.env|service\.env",
)]

PERM_QUESTIONS = {
    "reversible": {"type": "noul", "instructions":
        "Every command in `commands` only reads or changes files inside `project` and can be undone with "
        "git or by rerunning it. It does not push, publish, deploy, delete files outside the project, change "
        "system settings, or send data to a network service."},
    "serves_task": {"type": "noul", "instructions": "Running `commands` is a reasonable next step toward `task`."},
}


def _normalize_command(part: str) -> str:
    cur = part
    while True:
        nxt = cur.strip()
        nxt = re.sub(r"^(if|then|elif|else|fi|while|until|do|done|for|in|case|esac|!)\s+", "", nxt)
        nxt = re.sub(r"^(command|builtin|exec|env|nohup|time|timeout|nice|watch|npx|bunx|pnpx|caffeinate)\s+", "", nxt)
        nxt = re.sub(r"^[A-Za-z_][A-Za-z0-9_]*=[^\s'\"\\$]*\s+", "", nxt)
        nxt = re.sub(r"^\\", "", nxt)
        nxt = re.sub(r"^[^\s'\"\\$]*/", "", nxt)
        if nxt == cur:
            return cur
        cur = nxt


# Provably harmless shapes that used to block most real commands (2026-09-26 history review):
# stderr/stdout thrown away, a bare `cd <dir>`, and assigning a plain value to a variable. None can
# change a file or reach the network; `$(...)` stays blocked everywhere, and a later `rm "$X"` is
# still caught by the rm rule.
_HARMLESS_REDIRECT = re.compile(r"\s*(?:\d?>\s*/dev/null|2>&1|>&2|&>\s*/dev/null)(?=\s|$|[;&|)])")
_CD_SEG = re.compile(r"""^cd(?:\s+(?:"[^"`$]*(?:\$HOME[^"`$]*)?"|'[^']*'|[^\s"'`$;&|]+|\$HOME[^\s"'`;&|]*))?$""")
_ASSIGN_SEG = re.compile(r"""^[A-Za-z_][A-Za-z0-9_]*=(?:"[^"`]*"|'[^']*'|[^\s"'`;&|]*)$""")


# Values that look like credentials. A command carrying one never reaches Jev (it would leave the
# machine) — it gets the normal prompt. Words like "token"/"secret" are caught by _RISKY; this
# catches the values themselves (a pasted key, a bearer header, a JWT, a private-key block).
SECRET_VALUE = re.compile(
    r"sk-(?:or-|ant-|proj-)?[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|"
    r"xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"(?i:authorization|x-api-key|api[_-]?key)\s*[:=]\s*\S{12,}|\b[A-Fa-f0-9]{32,}\b")


def never_auto_approve(command: str) -> bool:
    if SECRET_VALUE.search(command):
        return True
    c = _HARMLESS_REDIRECT.sub(" ", command)
    if re.search(r"\$\(|[<>]\(|`|\\\r?\n|>", c):   # substitution, process subst, real redirects
        return True
    for part in re.split(r"\s*(?:&&|\|\||;|\||&|\n|[(){}])\s*", c):
        seg = part.strip()
        if not seg or _CD_SEG.match(seg) or (_ASSIGN_SEG.match(seg) and "$(" not in seg):
            continue
        if any(p.search(_normalize_command(seg)) for p in _RISKY):
            return True
    return False


def perm_decision(command: str, project: str, task: str | None) -> tuple[str, dict[str, Any]]:
    """Return ('allow'|'ask', detail). 'ask' = leave Claude Code's normal prompt alone."""
    if never_auto_approve(command):
        return "ask", {"why": "static risk list"}
    st = load_state()
    thr = float(st.get("perms_threshold", 0.9))
    asked = ["reversible"] + (["serves_task"] if task else [])
    state = {"commands": [command], "project": project, **({"task": task[:2000]} if task else {})}
    resp = decide(state, {k: PERM_QUESTIONS[k] for k in asked}, timeout=8, retries=0, source="perms")
    scores = {k: resp["answers"].get(k, {}).get("noul") for k in asked}
    ok = all(isinstance(s, (int, float)) and thr <= s <= 1 for s in scores.values())
    return ("allow" if ok else "ask"), {"scores": scores}


def hook_permission() -> int:
    """PermissionRequest hook for Bash. Prints an allow decision or nothing. Never denies."""
    try:
        if not load_state().get("perms"):
            return 0
        inp = json.load(sys.stdin)
        if inp.get("tool_name") != "Bash":
            return 0
        ti = inp.get("tool_input") or {}
        cmd, desc = ti.get("command"), ti.get("description")
        if not isinstance(cmd, str):
            return 0
        verdict, detail = perm_decision(cmd, str(inp.get("cwd", "")), desc if isinstance(desc, str) else None)
        _log_usage("perms-decision", verdict=verdict, **({"scores": detail["scores"]} if "scores" in detail else {}))
        if verdict == "allow":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                     "decision": {"behavior": "allow"}}}))
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# Level 2: sort a pile
# ---------------------------------------------------------------------------

def load_items(src: str, text_field: str | None = None) -> list[tuple[str, Any]]:
    """Return [(id, state)] from .jsonl / .json / .csv / directory / '-' (stdin jsonl)."""
    items: list[tuple[str, Any]] = []
    if src != "-" and Path(src).is_dir():
        for i, f in enumerate(sorted(p for p in Path(src).iterdir() if p.is_file() and not p.name.startswith("."))):
            items.append((f.name, f.read_text(errors="replace")))
        return items
    raw = sys.stdin.read() if src == "-" else Path(src).read_text(errors="replace")
    if src.endswith(".csv"):
        rows: list[Any] = list(csv.DictReader(raw.splitlines()))
    elif src.endswith(".json"):
        rows = json.loads(raw)
    else:
        rows = [json.loads(l) for l in raw.splitlines() if l.strip()]
    for i, row in enumerate(rows):
        rid = str(row.get("id", i + 1)) if isinstance(row, dict) else str(i + 1)
        st = row.get(text_field) if (text_field and isinstance(row, dict)) else row
        items.append((rid, st))
    return items


_REDACTIONS = [
    (re.compile(r"https?://\S+|www\.\S+"), "[url]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"(?:~|/Users/[^/\s]+|/home/[^/\s]+|[A-Za-z]:\\\\Users\\\\[^\\\\\s]+)(?:[/\\\\][^\s)`'\"]*)*"), "[path]"),
    (re.compile(r"(?<![\w.])\.{1,2}/[^\s)`'\"]+"), "[path]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[ip]"),
    # serial numbers: 10+ letters/digits with at least 2 digits and no separators (SN4829XK33ZQ91);
    # keys/tokens: 24+ chars that may contain - or _ (so names like 3d-floating-head survive)
    (re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{24,}\b"), "[id]"),
    (re.compile(r"\b(?=[A-Za-z0-9]*\d[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{10,}\b"), "[id]"),
]


def redact(value: Any, max_chars: int | None = None) -> Any:
    """Strip URLs, emails, local paths, IPs and serial/key-like tokens from every string in
    `value` (recursing into dicts/lists), then cut each string to max_chars."""
    if isinstance(value, str):
        for rx, repl in _REDACTIONS:
            value = rx.sub(repl, value)
        value = re.sub(r"[ \t]+", " ", value).strip()
        return value[:max_chars] if max_chars else value
    if isinstance(value, dict):
        return {k: redact(v, max_chars) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, max_chars) for v in value]
    return value


def truncate(value: Any, max_chars: int | None) -> Any:
    if not max_chars:
        return value
    if isinstance(value, str):
        return value[:max_chars]
    if isinstance(value, dict):
        return {k: truncate(v, max_chars) for k, v in value.items()}
    if isinstance(value, list):
        return [truncate(v, max_chars) for v in value]
    return value


def text_size(value: Any) -> int:
    """How much real text an item carries (string content only, not JSON punctuation)."""
    if isinstance(value, str):
        return len(value.strip())
    if isinstance(value, dict):
        return sum(text_size(v) for v in value.values())
    if isinstance(value, list):
        return sum(text_size(v) for v in value)
    return len(str(value)) if value is not None else 0


def content_size(value: Any, ignore: list[str] | None = None) -> int:
    """text_size, but first drop whole lines matching `ignore` (a preset's boilerplate labels),
    so a bar of N means N characters of real description, not of scaffolding."""
    if not ignore:
        return text_size(value)
    rxs = [re.compile(p) for p in ignore]
    strip = lambda t: "\n".join(l for l in t.splitlines() if not any(r.match(l.strip()) for r in rxs))  # noqa: E731
    if isinstance(value, str):
        return len(strip(value).strip())
    if isinstance(value, dict):
        return sum(content_size(v, ignore) for v in value.values())
    if isinstance(value, list):
        return sum(content_size(v, ignore) for v in value)
    return text_size(value)


def prepare_item(st: Any, *, do_redact: bool, max_chars: int | None) -> Any:
    return redact(st, max_chars) if do_redact else truncate(st, max_chars)


def sort_pile(items: list[tuple[str, Any]], questions: dict[str, Any], *, context: str | None,
              threshold: float, workers: int, do_redact: bool = False, max_chars: int | None = None,
              min_chars: int = 0, keep_fields: bool = False,
              thin_ignore: list[str] | None = None) -> list[dict[str, Any]]:
    """Sort every item. Thin items (under min_chars of real text) skip Jev and go straight to
    review — Jev will otherwise judge an empty folder by its name, confidently."""
    creds = resolve_credentials() if any(content_size(prepare_item(st, do_redact=do_redact, max_chars=max_chars),
                                                      thin_ignore) >= min_chars for _, st in items) else None

    def one(item: tuple[str, Any]) -> dict[str, Any]:
        rid, st = item
        sent = prepare_item(st, do_redact=do_redact, max_chars=max_chars)
        row: dict[str, Any] = {"id": rid}
        if keep_fields:
            row["input"] = sent
        if content_size(sent, thin_ignore) < min_chars:
            return {**row, "review": True, "unsure": ["thin input"], "decided_by": "review", "thin": True}
        state = {"about_us": context, "item": sent} if context else sent
        try:
            r = decide(state, questions, retries=3, source="sort", creds=creds)
        except JevError as exc:
            return {**row, "error": str(exc), "review": True, "unsure": ["error"], "decided_by": "review"}
        row["cost_usd"] = r["_meta"]["cost_usd"]
        unsure = []
        for k, a in r["answers"].items():
            row[k] = value(a)
            row[f"{k}_sure"] = round(sureness(a), 3)
            if sureness(a) < threshold:
                unsure.append(k)
        row["review"] = bool(unsure)
        row["unsure"] = unsure
        row["decided_by"] = "review" if unsure else "jev"
        return row

    with _cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, items))


_UNSURE_OPTION = re.compile(r"insufficient|unknown|unclear|not[_ -]?stated|none|other|can.?t tell", re.I)


def lint_questions(questions: dict[str, Any]) -> list[str]:
    """Design warnings worth fixing before paying for a pile."""
    notes = []
    for k, q in questions.items():
        if q.get("type") == "choice" and not any(_UNSURE_OPTION.search(o) for o in q.get("criteria", {})):
            notes.append(f"`{k}` has no 'insufficient information' option — with thin input Jev must still pick "
                         f"one of {list(q.get('criteria', {}))[:4]}…, often confidently")
    return notes


# ---------------------------------------------------------------------------
# Learning from use: turn a reviewed pile into a calibration set (†/‡ convention)
# ---------------------------------------------------------------------------

def build_truth(sorted_rows: list[dict[str, Any]], final: dict[str, dict[str, Any]],
                questions: dict[str, Any], threshold: float) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """sorted_rows: `sort --keep-fields` output; final: {id: {question: reviewed answer}}.
    Tags follow the report convention: agreed (Jev sure, kept) · reviewer-call († Jev unsure,
    Claude/you decided) · corrected (‡ Jev sure and overridden)."""
    rows, counts = [], {"agreed": 0, "reviewer-call": 0, "corrected": 0, "skipped": 0}
    for r in sorted_rows:
        if "input" not in r:
            raise JevError("sorted rows have no `input` — re-run sort with --keep-fields")
        truth, tags = {}, set()
        for k in questions:
            jev_ans, sure = r.get(k), r.get(f"{k}_sure")
            fin = final.get(r["id"], {}).get(k, jev_ans)
            if fin is None:
                continue
            truth[k] = fin
            if sure is None or sure < threshold:
                tags.add("reviewer-call")
            elif fin != jev_ans:
                tags.add("corrected")
            else:
                tags.add("agreed")
        if not truth:
            counts["skipped"] += 1
            continue
        tag = "corrected" if "corrected" in tags else "reviewer-call" if "reviewer-call" in tags else "agreed"
        counts[tag] += 1
        text = r["input"] if isinstance(r["input"], str) else json.dumps(r["input"], ensure_ascii=False)
        rows.append({"id": r["id"], "text": text, "truth": truth,
                     "tags": [tag] + (["tricky"] if tag == "corrected" else [])})
    return rows, counts


def decision_line(counts: dict[str, int]) -> str:
    return (f"Jev decided {counts['agreed']}, Claude decided {counts['reviewer-call']} where Jev was unsure (†), "
            f"Claude overrode {counts['corrected']} confident answers (‡).")


# ---------------------------------------------------------------------------
# embedded copy drift (a program may keep a copy of this file as its client library)
# ---------------------------------------------------------------------------

def _without_docstring(text: str) -> str:
    return re.sub(r'\A#![^\n]*\n"""[\s\S]*?"""\n', "", text, count=1)


def embedded_copy_status() -> tuple[str, str]:
    """('in sync'|'DRIFT'|'missing'|'unset', path) comparing everything after the module docstring.

    The copy is named by JEV_EMBEDDED_COPY, or by `embedded_copy` in the state file."""
    configured = os.environ.get("JEV_EMBEDDED_COPY") or load_state().get("embedded_copy") or ""
    if not configured:
        return "unset", ""
    path = Path(configured).expanduser()
    if not path.exists():
        return "missing", str(path)
    same = _without_docstring(path.read_text()) == _without_docstring(Path(__file__).read_text())
    return ("in sync" if same else "DRIFT"), str(path)


# ---------------------------------------------------------------------------
# Calibration: does Jev's confidence track how often it's right?
# ---------------------------------------------------------------------------

CAL_BINS = [0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0001]


def _grade(q: dict[str, Any], a: dict[str, Any], truth: Any) -> dict[str, Any]:
    """Score one answer against ground truth. `sure` is Jev's confidence in its own pick."""
    t = q["type"]
    if t == "noul":
        p = float(a.get("noul", 0.5))
        pred = p >= 0.5
        return {"p": round(p, 4), "pred": pred, "sure": round(max(p, 1 - p), 4),
                "truth": bool(truth), "correct": pred == bool(truth)}
    if t == "score":
        s = float(a.get("score", 0))
        pred = int(round(s))
        return {"score": round(s, 3), "pred": pred, "sure": round(float(a.get("confidence", 0)), 4),
                "truth": int(truth), "correct": pred == int(truth), "probs": a.get("probabilities", {})}
    pred = a.get("choice")
    return {"pred": pred, "sure": round(float(a.get("confidence", 0)), 4), "truth": truth,
            "correct": pred == truth, "probs": a.get("probabilities", {})}


def calibration_stats(items: list[dict[str, Any]], questions: dict[str, Any],
                      thr: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accuracy, reliability bins and ECE per question, plus accuracy per tag."""
    stats: dict[str, Any] = {}
    for k in questions:
        graded = [it["answers"][k] for it in items if k in it["answers"]]
        bins = []
        for lo, hi in zip(CAL_BINS, CAL_BINS[1:]):
            b = [g for g in graded if lo <= g["sure"] < hi]
            if b:
                bins.append({"lo": lo, "hi": min(hi, 1.0), "n": len(b),
                             "conf": round(sum(g["sure"] for g in b) / len(b), 4),
                             "acc": round(sum(g["correct"] for g in b) / len(b), 4)})
        n = len(graded)
        stats[k] = {"n": n, "accuracy": round(sum(g["correct"] for g in graded) / n, 4) if n else None,
                    "ece": round(sum(b["n"] / n * abs(b["acc"] - b["conf"]) for b in bins), 4) if n else None,
                    "bins": bins}
    by_tag: dict[str, dict[str, Any]] = {}
    for tag in sorted({t for it in items for t in it["tags"]}):
        tagged = [it for it in items if tag in it["tags"]]
        by_tag[tag] = {}
        for k in questions:
            g = [it["answers"][k] for it in tagged if k in it["answers"]]
            if g:
                by_tag[tag][k] = {"n": len(g), "accuracy": round(sum(x["correct"] for x in g) / len(g), 4),
                                  "confident_wrong": sum(1 for x in g if not x["correct"] and x["sure"] >= thr)}
    return stats, by_tag


def reslice(result: dict[str, Any], tags: list[str]) -> dict[str, Any]:
    """A saved calibration result restricted to items carrying any of `tags` — no new Jev calls.
    Use it to score only independently labelled rows (reviewer-call, corrected) of a set whose
    'agreed' rows are Jev's own earlier answers."""
    items = [it for it in result["items"] if set(it.get("tags", [])) & set(tags)]
    thr = float(result.get("threshold", load_state().get("threshold", REVIEW_THRESHOLD)))
    qs = {k: {"type": v["type"]} for k, v in result["questions"].items()}
    stats, by_tag = calibration_stats(items, qs, thr)
    ok = [it for it in items if "error" not in it]
    meta = {**result["meta"], "name": f"{result['meta']['name']} ({', '.join(tags)})", "items": len(items),
            "errors": len(items) - len(ok), "cost_usd": round(sum(it.get("cost_usd", 0) for it in ok), 6),
            "description": (f"Re-sliced from the {result['meta'].get('run_at', '')} run: only items tagged "
                            f"{' or '.join(tags)}. No new Jev calls. " + result["meta"].get("description", ""))}
    return {**result, "meta": meta, "stats": stats, "by_tag": by_tag, "items": items}


def calibrate(rows: list[dict[str, Any]], spec: dict[str, Any], *, workers: int = 8) -> dict[str, Any]:
    """Run every labelled row through Jev and measure accuracy against confidence.

    rows: [{"id", "text", "truth": {question_key: value}, "tags": [...]}]
    spec: {"name", "description", "context", "questions": {...}}
    """
    creds = resolve_credentials()
    questions, ctx = spec["questions"], spec.get("context")

    def one(row: dict[str, Any]) -> dict[str, Any]:
        state = {"about_us": ctx, "item": row["text"]} if ctx else row["text"]
        item = {"id": row["id"], "text": row["text"], "tags": row.get("tags", []), "answers": {}}
        try:
            r = decide(state, questions, retries=3, source="calibrate", creds=creds)
        except JevError as exc:
            item["error"] = str(exc)
            return item
        item["latency_ms"], item["cost_usd"] = r["_meta"]["latency_ms"], r["_meta"]["cost_usd"]
        for k, q in questions.items():
            if k in r["answers"] and k in row.get("truth", {}):
                item["answers"][k] = _grade(q, r["answers"][k], row["truth"][k])
        return item

    t0 = time.time()
    with _cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        items = list(pool.map(one, rows))
    thr = float(load_state().get("threshold", REVIEW_THRESHOLD))
    stats, by_tag = calibration_stats(items, questions, thr)
    ok = [it for it in items if "error" not in it]
    lat = sorted(it["latency_ms"] for it in ok)
    return {
        "meta": {"name": spec.get("name", "calibration"), "description": spec.get("description", ""),
                 "unit": spec.get("unit", "items"),
                 "model": (DEFAULT_MODEL[creds["provider"]] if not os.environ.get("JEV_MODEL") else os.environ["JEV_MODEL"]),
                 "provider": creds["provider"], "run_at": time.strftime("%Y-%m-%d %H:%MZ", time.gmtime()),
                 "wall_s": round(time.time() - t0, 1), "items": len(items), "errors": len(items) - len(ok),
                 "cost_usd": round(sum(it["cost_usd"] for it in ok), 6),
                 "p50_ms": lat[len(lat) // 2] if lat else None},
        "questions": {k: {"type": q["type"], "instructions": q["instructions"],
                          "options": list(q["criteria"]) if q["type"] == "choice" else None}
                      for k, q in questions.items()},
        "stats": stats,
        "by_tag": by_tag,
        "threshold": thr,
        "items": items,
    }


def render_calibration(result: dict[str, Any], out: Path) -> None:
    tpl = (SKILL_DIR / "templates" / "calibration.html").read_text()
    blob = json.dumps(result).replace("</", "<\\/")
    name = re.sub(r"[<>&\"]", "", result.get("meta", {}).get("name", "")).strip()
    tpl = tpl.replace("<title>Is Jev Calibrated?</title>", f"<title>Jev on {name}</title>" if name else
                      "<title>Is Jev Calibrated?</title>", 1)
    out.write_text(tpl.replace("/*__CAL_DATA__*/null", blob, 1))


# ---------------------------------------------------------------------------
# Install / uninstall (hooks + helper agents)
# ---------------------------------------------------------------------------

AGENT_DESC = {
    "haiku": "tiny jobs — lookups, renames, one-line answers",
    "sonnet": "everyday jobs — normal emails, posts, short docs, small focused code changes",
    "opus": "large jobs — multi-step builds, research, full reports",
    "fable": "the hardest jobs — strategy, architecture, anything where a wrong call is expensive",
}


def _agent_ref(model: str) -> str:
    """How the router names a tier's helper agent: plugin agents are namespaced by the plugin."""
    return f"`jev:jev-{model}` (or `jev-{model}`)" if PLUGIN_ROOT else f"`jev-{model}`"


def _hook_cmd(mode: str) -> str:
    # hook.py checks the on/off switch before importing this module; -S skips site-packages
    return f"python3 -S {Path(__file__).resolve().parent / 'hook.py'} {mode}"


def _is_jev_hook(cmd: str) -> bool:
    return "jev.py hook" in cmd or "jev/scripts/hook.py" in cmd


def agent_markdown(model: str) -> str:
    desc = AGENT_DESC[model]
    return (
        f"---\nname: jev-{model}\ndescription: Jev-router helper pinned to {model}, for {desc}. "
        f"Use when a [jev-router] note sizes a self-contained task as this tier.\nmodel: {model}\n---\n\n"
        f"You are the {model} helper in a Jev routing setup. Jev (a fast decision model) judged the task "
        f"you were given to be the right size for you: {desc}.\n\n"
        "Do the task completely and concisely with the context you were handed. If it turns out to need "
        "much more than your tier (or context you were not given), say so plainly instead of guessing.\n\n"
        f"End your reply with exactly one line: `— done by {model} (jev-{model})`\n")


def _settings_jev_hooks() -> list[str]:
    """Events that have a jev hook registered in ~/.claude/settings.json (the pre-plugin install)."""
    try:
        hooks = json.loads((CLAUDE_HOME / "settings.json").read_text()).get("hooks", {})
    except Exception:
        return []
    return [ev for ev, groups in hooks.items()
            if any(_is_jev_hook(h.get("command", "")) for g in groups for h in g.get("hooks", []))]


def install() -> list[str]:
    done = []
    if PLUGIN_ROOT:  # the plugin provides hooks and agents; only the state file is ours to create
        if not (JEV_HOME / "state.json").exists():
            save_state(load_state())
            done.append("state.json (all switches OFF)")
        return done
    settings_p = CLAUDE_HOME / "settings.json"
    settings = json.loads(settings_p.read_text()) if settings_p.exists() else {}
    hooks = settings.setdefault("hooks", {})
    for event, mode, matcher, timeout in (("UserPromptSubmit", "prompt", None, 6),
                                          ("PermissionRequest", "permission", "Bash", 12)):
        groups = hooks.setdefault(event, [])
        mine = [h for g in groups for h in g.get("hooks", []) if _is_jev_hook(h.get("command", ""))]
        if mine:
            for h in mine:                       # upgrade older `jev.py hook …` entries in place
                if h.get("command") != _hook_cmd(mode):
                    h["command"] = _hook_cmd(mode)
                    done.append(f"hook {event} (upgraded)")
            continue
        group: dict[str, Any] = {"hooks": [{"type": "command", "command": _hook_cmd(mode), "timeout": timeout}]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
        done.append(f"hook {event}")
    if done:
        backup = settings_p.with_suffix(".json.jev-bak")
        if settings_p.exists():
            shutil.copy2(settings_p, backup)
        settings_p.write_text(json.dumps(settings, indent=2) + "\n")
    agents = CLAUDE_HOME / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    for model, desc in AGENT_DESC.items():
        p = agents / f"jev-{model}.md"
        if p.exists():
            continue
        p.write_text(agent_markdown(model))
        done.append(f"agent jev-{model}")
    if not (JEV_HOME / "state.json").exists():
        save_state(load_state())
        done.append("state.json (all switches OFF)")
    return done


def uninstall() -> list[str]:
    done = []
    settings_p = CLAUDE_HOME / "settings.json"
    if settings_p.exists():
        settings = json.loads(settings_p.read_text())
        hooks = settings.get("hooks", {})
        for event in list(hooks):
            kept = [g for g in hooks[event]
                    if not any(_is_jev_hook(h.get("command", "")) for h in g.get("hooks", []))]
            if len(kept) != len(hooks[event]):
                done.append(f"hook {event}")
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
        if not hooks:
            settings.pop("hooks", None)
        settings_p.write_text(json.dumps(settings, indent=2) + "\n")
    for model in AGENT_DESC:
        p = CLAUDE_HOME / "agents" / f"jev-{model}.md"
        if p.exists():
            p.unlink()
            done.append(f"agent jev-{model}")
    return done


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SAMPLE_EMAIL = ("Hi, we run an online skincare store and want an AI agent to answer our order emails. "
                "Budget is signed off and we want to start next month. Can we book a call this week? — Dana, Founder")
SAMPLE_QUESTIONS = {
    "lead_strength": {"type": "score", "instructions": "How strong a sales lead is this email?",
                      "criteria": ["Not a lead at all", "Cold: vague interest, tiny budget or poor fit",
                                   "Warm: a real need that fits, but no budget or timeline yet",
                                   "Hot: a clear need that fits, plus at least two of a stated budget, "
                                   "a stated timeline, or a decision maker writing"]},
    "kind": {"type": "choice", "instructions": "What kind of email is this?",
             "criteria": {"new_lead": None, "existing_client": None, "vendor_pitch": None, "spam": None,
                          "job_seeker": None, "newsletter": None, "support": None}},
    "needs_personal_reply": {"type": "noul", "instructions": "This email needs a personal reply from the team today."},
}

ROUTE_SAMPLES = [
    "what's the capital of australia",
    "rename the variable usr to user in utils.py",
    "draft a friendly two-paragraph email declining the vendor's renewal offer",
    "write a short linkedin post announcing our new pricing page",
    "build a CLI that ingests a folder of PDFs, extracts invoice totals, and writes a CSV report with tests",
    "research the top five vector databases and write a comparison report with a recommendation",
    "should we migrate our auth from HS256 shared secrets to Ed25519 across 40 services? plan the rollout and the risks",
    "design the pricing strategy for our enterprise tier given churn in mid-market",
    "yes do that but make it shorter",
    "ok go ahead",
]


def _arg_json(v: str) -> Any:
    if v == "-":
        return json.loads(sys.stdin.read())
    if v.startswith("@"):
        return json.loads(Path(v[1:]).read_text())
    return json.loads(v)


def _arg_state(v: str) -> Any:
    if v == "-":
        v = sys.stdin.read()
    elif v.startswith("@"):
        v = Path(v[1:]).read_text(errors="replace")
    try:
        parsed = json.loads(v)
        return parsed if isinstance(parsed, (dict, list)) else v
    except Exception:
        return v


def _presets() -> dict[str, Path]:
    return {p.stem: p for p in sorted((SKILL_DIR / "presets").glob("*.json"))}


def _fmt_usage(recs: list[dict[str, Any]]) -> str:
    calls = [r for r in recs if r.get("source") in ("router", "sort", "perms", "api", "cli", "test", "embedded", "calibrate")]
    ok = [r for r in calls if r.get("ok")]
    cost = sum(float(r.get("cost") or 0) for r in ok)
    lat = sorted(r["latency_ms"] for r in ok if "latency_ms" in r)
    tiers: dict[str, int] = {}
    for r in recs:
        if r.get("source") == "router-decision":
            tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    perms: dict[str, int] = {}
    for r in recs:
        if r.get("source") == "perms-decision":
            perms[r["verdict"]] = perms.get(r["verdict"], 0) + 1
    by_src: dict[str, int] = {}
    for r in ok:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    lines = [f"calls: {len(ok)} ok, {len(calls) - len(ok)} failed · cost ${cost:.6f}"
             + (f" · p50 {lat[len(lat) // 2]} ms" if lat else "")]
    if by_src:
        lines.append("by source: " + ", ".join(f"{k} {v}" for k, v in sorted(by_src.items())))
    if tiers:
        lines.append("router tiers: " + ", ".join(f"{k} {v}" for k, v in sorted(tiers.items())))
    if perms:
        lines.append("permission gate: " + ", ".join(f"{k} {v}" for k, v in sorted(perms.items())))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jev", description="Jev (TypeSafe System One) client + Claude Code integration")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ask", help="one decision call; prints answers JSON")
    p.add_argument("--state", required=True, help="text, JSON, @file or - (stdin)")
    p.add_argument("--questions", required=True, help="questions JSON, @file, - or preset:<name>")
    p.add_argument("--model")
    p.add_argument("--raw", action="store_true", help="print the full response")

    sub.add_parser("test", help="the guide's sales-email smoke test (3 questions)")

    p = sub.add_parser("route", help="dry-run the router on messages (default: 10 built-in samples)")
    p.add_argument("messages", nargs="*")
    p.add_argument("--no-skills", action="store_true")

    p = sub.add_parser("sort", help="sort a pile (jsonl/json/csv/dir) with typed questions")
    p.add_argument("input")
    p.add_argument("--questions", required=True, help="questions JSON, @file or preset:<name>")
    p.add_argument("--context", help="one line about the business/situation, sent with every item")
    p.add_argument("--text-field", help="for jsonl/csv/json rows: the field to use as the item")
    p.add_argument("--threshold", type=float, default=None)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--sort-by", help="answer key to sort descending (e.g. lead_strength)")
    p.add_argument("--out", help="write .csv or .jsonl")
    p.add_argument("--redact", action="store_true", help="strip URLs, emails, paths, IPs and serial/key-like tokens")
    p.add_argument("--max-chars", type=int, help="cut every text field to this many characters")
    p.add_argument("--min-chars", type=int, default=None,
                   help="items with less real text skip Jev and go to review (default: the preset's "
                        "min_chars, else 40; 0 disables)")
    p.add_argument("--keep-fields", action="store_true", help="include each item as sent (`input`) in the output")
    p.add_argument("--trial", type=int, metavar="N", help="send only a random sample of N items and report "
                   "which questions come back mostly unsure")
    p.add_argument("--yes", action="store_true", help="actually send (without it: dry-run estimate only)")

    p = sub.add_parser("truth", help="turn a reviewed sort into a calibration set (agreed / † / ‡)")
    p.add_argument("sorted", help="jsonl written by sort --keep-fields --out x.jsonl")
    p.add_argument("--final", required=True, help="JSON {id: {question: reviewed answer}} (only the ids you changed "
                   "or decided; everything else keeps Jev's answer)")
    p.add_argument("--questions", required=True, help="the questions used for the sort (@file or preset:<name>)")
    p.add_argument("--name", required=True, help="calibration set name, e.g. projects-2026-09")
    p.add_argument("--context", help="context line to store in the spec")
    p.add_argument("--unit", default="items")
    p.add_argument("--append", action="store_true", help="add to an existing set with this name")

    p = sub.add_parser("calibrate", help="measure accuracy vs confidence on a labelled set; renders an HTML report")
    p.add_argument("dataset", help="bundled set name (e.g. household) or a labelled .jsonl")
    p.add_argument("--spec", help="spec JSON (@file) with questions/context; default: <dataset>.spec.json")
    p.add_argument("--html", help="write the report page here")
    p.add_argument("--json", dest="json_out", help="write raw results JSON here")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--tags", help="only items with any of these tags (comma-separated), e.g. reviewer-call,corrected")
    p.add_argument("--results", help="re-slice a saved --json result instead of calling Jev (sends nothing)")
    p.add_argument("--yes", action="store_true", help="actually send (without it: dry-run estimate only)")

    p = sub.add_parser("perm-check", help="dry-run the permission gate on a command")
    p.add_argument("command")
    p.add_argument("--task")
    p.add_argument("--project", default=os.getcwd())

    for name in ("on", "off"):
        p = sub.add_parser(name, help=f"turn a switch {name} (default: router)")
        p.add_argument("what", nargs="?", default="router", choices=["router", "skills", "perms"])
    p = sub.add_parser("set", help="set threshold / perms_threshold / tiers.<tier> / embedded_copy")
    p.add_argument("key")
    p.add_argument("value")

    sub.add_parser("status", help="switches, provider and usage totals")
    sub.add_parser("doctor", help="check key, hooks and agents (no network)")
    sub.add_parser("presets", help="list bundled question presets")
    sub.add_parser("install", help="register hooks + helper agents (switches stay OFF)")
    sub.add_parser("uninstall", help="remove hooks + helper agents")
    p = sub.add_parser("hook", help="(internal) hook entrypoints")
    p.add_argument("mode", choices=["prompt", "permission"])

    a = ap.parse_args(argv)

    if a.cmd == "hook":
        return hook_prompt() if a.mode == "prompt" else hook_permission()

    try:
        if a.cmd in ("ask", "sort", "truth") and a.questions.startswith("preset:"):
            name = a.questions.split(":", 1)[1]
            pre = _presets().get(name)
            if not pre:
                print(f"unknown preset {name!r}; have: {', '.join(_presets())}", file=sys.stderr)
                return 2
            spec = json.loads(pre.read_text())
            a.questions_obj = spec["questions"]
            if a.cmd == "sort" and a.min_chars is None and spec.get("min_chars") is not None:
                a.min_chars = int(spec["min_chars"])
            a.thin_ignore = spec.get("thin_ignore") if a.cmd == "sort" else None
            if a.cmd == "sort" and not a.context and spec.get("context_hint"):
                print(f"hint: {spec['context_hint']}", file=sys.stderr)
        elif a.cmd in ("ask", "sort", "truth"):
            a.questions_obj = _arg_json(a.questions)
            if isinstance(a.questions_obj, dict) and "questions" in a.questions_obj:
                a.questions_obj = a.questions_obj["questions"]

        if a.cmd == "ask":
            r = decide(_arg_state(a.state), a.questions_obj, model=a.model, source="cli")
            if a.raw:
                print(json.dumps(r, indent=2))
            else:
                print(json.dumps({"answers": r["answers"], "meta": r["_meta"]}, indent=2))
            return 0

        if a.cmd == "test":
            r = decide(SAMPLE_EMAIL, SAMPLE_QUESTIONS, source="test")
            m = r["_meta"]
            print(f"state: {SAMPLE_EMAIL}\n")
            for k, ans in r["answers"].items():
                print(f"  {k:22} {summarize({k: ans}).split('=', 1)[1]}")
            print(f"\nmodel {m['model']} via {m['provider']} · {m['latency_ms']} ms · "
                  f"{m['input_tokens']} input tokens · ${m['cost_usd']:.6f}")
            return 0

        if a.cmd == "route":
            msgs = a.messages or ROUTE_SAMPLES
            st = load_state()
            thr = float(st.get("threshold", REVIEW_THRESHOLD))
            print(f"{'tier':9} {'conf':>5} {'ctx':>5} {'→':2} {'model':7} {'skill':24} {'ms':>5}  message")
            for msg in msgs:
                try:
                    r = route(msg, cwd=os.getcwd(), with_skills=not a.no_skills, source="router")
                except JevError as exc:
                    print(f"ERROR {exc}  {msg[:60]}")
                    continue
                decided = r["tier_conf"] >= thr and r["needs_context"] < 0.5
                model = st["tiers"].get(r["tier"], "?") if decided else "self"
                sk = f"{r.get('skill', '-')} {r.get('skill_conf', 0):.2f}" if "skill" in r else "-"
                print(f"{r['tier']:9} {r['tier_conf']:5.2f} {r['needs_context']:5.2f} →  {model:7} "
                      f"{sk:24} {r['_meta']['latency_ms']:5}  {msg[:70]}")
            return 0

        if a.cmd == "sort":
            items = load_items(a.input, a.text_field)
            if a.min_chars is None:
                a.min_chars = 40
            thin_ignore = getattr(a, "thin_ignore", None)
            thr = a.threshold if a.threshold is not None else float(load_state().get("threshold", REVIEW_THRESHOLD))
            prep = lambda st: prepare_item(st, do_redact=a.redact, max_chars=a.max_chars)  # noqa: E731
            thin = [rid for rid, st in items if content_size(prep(st), thin_ignore) < a.min_chars]
            if a.trial:
                import random
                items = random.Random(7).sample(items, min(a.trial, len(items)))
            n_send = len(items) - (0 if a.trial else len(thin))
            est_chars = sum(len(json.dumps(prep(s_))) for _, s_ in items) + len(json.dumps(a.questions_obj)) * n_send
            est_cost = est_chars / 4 * PRICE_PER_INPUT_TOKEN
            if not a.yes:
                label = f"trial of {len(items)}" if a.trial else f"{len(items)} items"
                print(f"dry run: {label} × {len(a.questions_obj)} questions · ~{est_chars // 4:,} input tokens · "
                      f"~${est_cost:.5f}. Everything sent leaves this machine.")
                if thin and not a.trial:
                    print(f"  {len(thin)} thin item(s) (< {a.min_chars} chars of {'real description' if thin_ignore else 'text'}) "
                          f"go straight to review, not Jev: "
                          + ", ".join(thin[:8]) + ("…" if len(thin) > 8 else ""))
                for note in lint_questions(a.questions_obj):
                    print("  question design: " + note)
                sample = next((prep(st) for rid, st in items if rid not in thin), None)
                if sample is not None:
                    shown = sample if isinstance(sample, str) else json.dumps(sample, ensure_ascii=False, indent=1)
                    print("  sample exactly as it will be sent" + (" (redacted)" if a.redact else " (NOT redacted — add --redact)")
                          + ":\n    " + shown[:700].replace("\n", "\n    "))
                print("  Re-run with --yes to send." + ("" if a.trial else " Tip: --trial 10 first for new questions."))
                return 2
            rows = sort_pile(items, a.questions_obj, context=a.context, threshold=thr, workers=a.workers,
                             do_redact=a.redact, max_chars=a.max_chars, min_chars=0 if a.trial else a.min_chars,
                             keep_fields=a.keep_fields, thin_ignore=thin_ignore)
            if a.trial:
                sent = [r for r in rows if "error" not in r]
                print(f"trial: {len(sent)} items · threshold {thr}")
                bad = False
                for k in a.questions_obj:
                    vals = [r.get(f"{k}_sure") for r in sent if r.get(f"{k}_sure") is not None]
                    share = sum(v < thr for v in vals) / len(vals) if vals else 1.0
                    flag = share > 0.5
                    bad |= flag
                    print(f"  {k:20} {share:.0%} unsure" + ("  ← most answers below the threshold: the evidence is "
                          "probably not in the input. Get this fact in code (git, files) or add the field that holds it."
                          if flag else ""))
                full = sum(len(json.dumps(prep(s_))) for _, s_ in load_items(a.input, a.text_field)) / 4
                print(f"full run would be ~${full * PRICE_PER_INPUT_TOKEN + len(load_items(a.input, a.text_field)) * len(json.dumps(a.questions_obj)) / 4 * PRICE_PER_INPUT_TOKEN:.5f}"
                      + (" — fix the flagged questions first." if bad else "."))
                return 0
            if a.sort_by:
                rows.sort(key=lambda r: (r.get("review", True), -(r.get(a.sort_by) or 0)
                                         if isinstance(r.get(a.sort_by), (int, float)) else 0))
            if a.out:
                out = Path(a.out)
                if out.suffix == ".csv":
                    flat_rows = []
                    for r in rows:
                        fr = {k: v for k, v in r.items() if k != "input"}
                        inp = r.get("input")
                        if isinstance(inp, dict):
                            fr.update({f"in_{k}": (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in inp.items()})
                        elif inp is not None:
                            fr["in_text"] = inp if isinstance(inp, str) else json.dumps(inp)
                        flat_rows.append(fr)
                    keys: list[str] = []
                    for r in flat_rows:
                        keys += [k for k in r if k not in keys]
                    with open(out, "w", newline="") as fh:
                        w = csv.DictWriter(fh, fieldnames=keys)
                        w.writeheader()
                        for r in flat_rows:
                            w.writerow({k: (";".join(v) if isinstance(v, list) else v) for k, v in r.items()})
                else:
                    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            n_rev = sum(1 for r in rows if r.get("review"))
            n_thin = sum(1 for r in rows if r.get("thin"))
            cost = sum(r.get("cost_usd", 0) for r in rows)
            print(f"sorted {len(rows)} items · Jev decided {len(rows) - n_rev} · {n_rev} in the check-these pile "
                  f"(sure < {thr}{f'; {n_thin} thin, never sent' if n_thin else ''}) · ${cost:.6f}"
                  + (f" · wrote {a.out}" if a.out else ""))
            if not a.out:
                for r in rows:
                    print(json.dumps(r, ensure_ascii=False))
            return 0

        if a.cmd == "truth":
            thr = float(load_state().get("threshold", REVIEW_THRESHOLD))
            srows = [json.loads(l) for l in Path(a.sorted).read_text().splitlines() if l.strip()]
            final = _arg_json(a.final)
            new_rows, counts = build_truth(srows, final, a.questions_obj, thr)
            cal = SKILL_DIR / "calibration"
            data_p, spec_p = cal / f"{a.name}.jsonl", cal / f"{a.name}.spec.json"
            if a.append and data_p.exists():
                old = {json.loads(l)["id"]: l for l in data_p.read_text().splitlines() if l.strip()}
                for r in new_rows:
                    old[r["id"]] = json.dumps(r, ensure_ascii=False)
                data_p.write_text("\n".join(old.values()) + "\n")
            else:
                data_p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in new_rows))
            if not spec_p.exists() or not a.append:
                spec_p.write_text(json.dumps({"name": a.name, "unit": a.unit, "context": a.context or "",
                                              "description": f"Built from a reviewed sort ({time.strftime('%Y-%m-%d')}).",
                                              "questions": a.questions_obj}, indent=2) + "\n")
            print(f"wrote {len(new_rows)} labelled items to {data_p.name}"
                  + (f" ({counts['skipped']} skipped: no answer)" if counts["skipped"] else "") + "\n" + decision_line(counts))
            print(f"measure it: python3 {Path(__file__)} calibrate {a.name}")
            return 0

        if a.cmd == "calibrate":
            bundled = SKILL_DIR / "calibration" / f"{a.dataset}.jsonl"
            data_p = bundled if bundled.exists() else Path(a.dataset)
            spec_p = Path(a.spec[1:] if a.spec and a.spec.startswith("@") else a.spec) if a.spec else \
                data_p.with_name(data_p.name.replace(".jsonl", ".spec.json"))
            rows = [json.loads(l) for l in data_p.read_text().splitlines() if l.strip()]
            spec = json.loads(spec_p.read_text())
            tags = [t.strip() for t in (a.tags or "").split(",") if t.strip()]
            if tags:
                rows = [r for r in rows if set(r.get("tags", [])) & set(tags)]
            est = sum(len(r["text"]) for r in rows) // 4 + len(rows) * (len(json.dumps(spec["questions"])) // 4 + 150)
            if a.results:
                res = json.loads(Path(a.results).read_text())
                res = reslice(res, tags) if tags else res
                a.yes = True
            elif not a.yes:
                print(f"dry run: {len(rows)} labelled items × {len(spec['questions'])} questions · ~{est:,} input "
                      f"tokens · ~${est * PRICE_PER_INPUT_TOKEN:.4f}. Items leave this machine. Re-run with --yes.")
                return 2
            if not a.results:
                res = calibrate(rows, spec, workers=a.workers)
                if tags:
                    res["meta"]["name"] = f"{res['meta']['name']} ({', '.join(tags)})"
            m = res["meta"]
            print(f"{m['name']}: {m['items']} items, {m['errors']} errors · {m['wall_s']} s · p50 {m['p50_ms']} ms · ${m['cost_usd']:.5f}")
            for k, s in res["stats"].items():
                if s.get("accuracy") is None:   # no labelled items for this question in the set
                    print(f"  {k:16} no labelled items")
                    continue
                print(f"  {k:16} accuracy {s['accuracy']:.0%}  ECE {s['ece']:.3f}  "
                      + "  ".join(f"[{b['lo']:.2f}-{b['hi']:.2f}) n={b['n']} conf {b['conf']:.2f} acc {b['acc']:.2f}"
                                  for b in s["bins"]))
            for tag, qs in res["by_tag"].items():
                print(f"  tag {tag:14} " + "  ".join(f"{k} {v['accuracy']:.0%} of {v['n']}"
                      + (f" ({v['confident_wrong']} confidently wrong)" if v["confident_wrong"] else "")
                      for k, v in qs.items()))
            if a.json_out:
                Path(a.json_out).write_text(json.dumps(res, indent=1))
            if a.html:
                render_calibration(res, Path(a.html))
                print(f"report: {a.html}")
            return 0

        if a.cmd == "perm-check":
            verdict, detail = perm_decision(a.command, a.project, a.task)
            print(json.dumps({"verdict": verdict, **detail}))
            return 0

        if a.cmd in ("on", "off"):
            st = load_state()
            st[a.what] = a.cmd == "on"
            save_state(st)
            print(f"jev {a.what}: {'ON' if st[a.what] else 'OFF'}")
            if a.cmd == "on" and a.what in ("router", "perms"):
                what = "every prompt you type" if a.what == "router" else "each Bash command awaiting approval"
                print(f"note: while ON, {what} is sent to TypeSafe (via your provider). "
                      f"Turn it off for private work: /jev off{'' if a.what == 'router' else ' perms'}")
            return 0

        if a.cmd == "set":
            st = load_state()
            if a.key.startswith("tiers."):
                st["tiers"][a.key.split(".", 1)[1]] = a.value
            elif a.key in ("threshold", "perms_threshold"):
                st[a.key] = float(a.value)
            elif a.key == "embedded_copy":
                st[a.key] = str(Path(a.value).expanduser())
            else:
                print(f"unknown key {a.key}", file=sys.stderr)
                return 2
            save_state(st)
            print(f"{a.key} = {a.value}")
            return 0

        if a.cmd == "status":
            st = load_state()
            try:
                c = resolve_credentials()
                prov = f"{c['provider']} ({c['source']})"
            except JevError:
                prov = "NO KEY"
            print(f"router {'ON' if st['router'] else 'OFF'} · skills {'ON' if st['skills'] else 'OFF'} · "
                  f"perms {'ON' if st['perms'] else 'OFF'} · threshold {st['threshold']} · "
                  f"perms_threshold {st['perms_threshold']}")
            print(f"provider: {prov} · tiers: " + ", ".join(f"{k}→{v}" for k, v in st["tiers"].items()))
            print(_fmt_usage(_read_usage()))
            return 0

        if a.cmd == "doctor":
            problems = 0
            try:
                c = resolve_credentials()
                print(f"✓ key: {c['source']} → {c['provider']} {c['base']}")
            except JevError as exc:
                problems += 1
                print(f"✗ {exc}")
            if PLUGIN_ROOT:
                print(f"· installed as a plugin: {PLUGIN_ROOT}")
                try:
                    plugin_hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text()).get("hooks", {})
                except Exception:
                    plugin_hooks = {}
                for ev in ("UserPromptSubmit", "PermissionRequest"):
                    hit = ev in plugin_hooks
                    problems += not hit
                    print(f"{'✓' if hit else '✗'} hook {ev} (plugin hooks.json)")
                dup = _settings_jev_hooks()
                if dup:
                    problems += 1
                    print(f"✗ settings.json still has the pre-plugin jev hooks ({', '.join(dup)}): each "
                          "would run twice. Remove them with the old skill's `jev.py uninstall`.")
                agent_dir = PLUGIN_ROOT / "agents"
            else:
                registered = _settings_jev_hooks()
                for ev in ("UserPromptSubmit", "PermissionRequest"):
                    hit = ev in registered
                    problems += not hit
                    print(f"{'✓' if hit else '✗'} hook {ev}{'' if hit else ' (run: jev install)'}")
                agent_dir = CLAUDE_HOME / "agents"
            for m in AGENT_DESC:
                ok = (agent_dir / f"jev-{m}.md").exists()
                problems += not ok
                print(f"{'✓' if ok else '✗'} agent jev-{m}")
            status, path = embedded_copy_status()
            if status == "DRIFT":
                problems += 1
            if status != "unset":
                print(f"{'✗' if status == 'DRIFT' else '✓' if status == 'in sync' else '·'} embedded copy {status}: {path}"
                      + (" (re-copy this file over it, keeping its header docstring)" if status == "DRIFT" else ""))
            print(f"  review threshold: {load_state().get('threshold', REVIEW_THRESHOLD)}")
            print(f"  skills in router catalog: {len(skill_catalog(os.getcwd()))}")
            return 1 if problems else 0

        if a.cmd == "presets":
            for name, p in _presets().items():
                spec = json.loads(p.read_text())
                print(f"{name:10} {spec.get('description', '')}  [{', '.join(spec['questions'])}]")
            return 0

        if a.cmd == "install":
            done = install()
            print("installed: " + (", ".join(done) if done else "nothing to do (already installed)"))
            return 0

        if a.cmd == "uninstall":
            if PLUGIN_ROOT:
                print("installed as a plugin: remove it with /plugin uninstall "
                      "(your state and usage log in $JEV_HOME stay)")
                return 0
            done = uninstall()
            print("removed: " + (", ".join(done) if done else "nothing"))
            return 0
    except JevError as exc:
        print(f"jev error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
