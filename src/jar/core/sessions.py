#!/usr/bin/env python3
"""Coding sessions in one place (titan-node#300, docs/session-tracking.md): Claude Code, Codex, Warp and
Antigravity on this Mac, one row each, with the branch and pull request each one's work landed in.

Metadata only, and nothing leaves the Mac (Zach's choice, 10-02): the tools' own stores are read for ids,
working folders, times and the titles the tools wrote themselves; no message text is printed or sent.
GitHub is asked only for the pull request of a branch (gh, the user's own login).

  python3 scripts/sessions.py [--since 24h] [--tool claude,codex,warp,antigravity] [--no-prs] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

HOME = Path.home()
CLAUDE = HOME / ".claude" / "projects"
CODEX = HOME / ".codex" / "sessions"
WARP = HOME / "Library/Group Containers/2BBY89MBSN.dev.warp/Library/Application Support/dev.warp.Warp-Stable/warp.sqlite"
AGY = HOME / ".gemini" / "antigravity" / "conversation_summaries.db"
ACTIVE_S, IDLE_S = 5 * 60, 6 * 3600


def _state(last: float, now: float) -> str:
    age = now - last
    return "active" if age < ACTIVE_S else "idle" if age < IDLE_S else "quiet"


def _tail_records(path: Path, nbytes: int = 262144) -> Iterator[dict[str, Any]]:
    """JSON records from the end of a transcript, newest first (only their metadata fields are used)."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - nbytes))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            yield r


def claude(since: float) -> list[dict[str, Any]]:
    out = []
    for f in CLAUDE.glob("*/*.jsonl"):
        last = f.stat().st_mtime
        if last < since:
            continue
        cwd = branch = title = name = pr = None
        for r in _tail_records(f):   # metadata records only: never a message's or a prompt's text
            cwd = cwd or r.get("cwd")
            branch = branch or r.get("gitBranch")
            if r.get("type") == "custom-title":
                title = title or r.get("customTitle")
            elif r.get("type") == "agent-name":
                name = name or r.get("agentName")
            elif r.get("type") == "pr-link" and not pr and r.get("prNumber"):
                pr = {"number": r["prNumber"], "url": r.get("prUrl"), "repo": r.get("prRepository")}
            if cwd and branch and (title or name) and pr:
                break
        out.append({"tool": "claude", "id": f.stem, "cwd": cwd, "branch": branch, "last": last,
                    "title": _title(name or title), "linked_pr": pr})
    return out


def codex(since: float) -> list[dict[str, Any]]:
    out = []
    for f in CODEX.rglob("*.jsonl"):
        last = f.stat().st_mtime
        if last < since:
            continue
        meta: dict[str, Any] = {}
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                first = json.loads(fh.readline() or "{}")
            if first.get("type") == "session_meta":
                meta = first.get("payload") or {}
        except (OSError, ValueError):
            pass
        out.append({"tool": "codex", "id": str(meta.get("id") or meta.get("session_id") or f.stem),
                    "cwd": meta.get("cwd"), "branch": None, "last": last, "title": None})
    return out


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None


def _ts(text: Any) -> float:
    s = str(text or "").strip().replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d*", r"\1", s)
    s = re.sub(r"\+00$", "+00:00", s)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def warp(since: float) -> list[dict[str, Any]]:
    con = _ro(WARP)
    if con is None:
        return []
    out = []
    try:
        for cid, mod, summary in con.execute("SELECT conversation_id, last_modified_at, summary FROM agent_conversations"):
            last = _ts(mod)
            if last < since:
                continue
            try:   # its title and folder only: the summary also holds the first message (initial_query)
                meta = json.loads(summary or "{}")
            except ValueError:
                meta = {}
            meta = meta if isinstance(meta, dict) else {}
            out.append({"tool": "warp", "id": str(cid), "cwd": meta.get("initial_working_directory"), "branch": None,
                        "last": last, "title": _title(meta.get("title"))})
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def antigravity(since: float) -> list[dict[str, Any]]:
    con = _ro(AGY)
    if con is None:
        return []
    out = []
    try:
        for cid, title, mod, uris, status, killed in con.execute(
                "SELECT conversation_id, title, last_modified_time, workspace_uris, status, killed FROM conversation_summaries"):
            last = _ts(mod)
            if last < since:
                continue
            cwd = None
            m = re.search(r"file://(/[^\s\"',\]]+)", str(uris or ""))
            if m:
                cwd = m.group(1)
            st = "killed" if killed else ("idle" if "IDLE" in str(status or "") else None)
            out.append({"tool": "antigravity", "id": str(cid), "cwd": cwd, "branch": None, "last": last,
                        "title": _title(title), "tool_state": st})
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def _title(t: Any, limit: int = 60) -> str | None:
    t = " ".join(str(t or "").split())
    return (t[:limit - 1] + "…" if len(t) > limit else t) or None


def _git(cwd: str, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


_PR_CACHE: dict[tuple[str, str], dict[str, Any] | None] = {}


def _pr_row(r: dict[str, Any]) -> dict[str, Any]:
    checks = [c.get("conclusion") or c.get("state") for c in r.get("statusCheckRollup") or []]
    failing = sum(1 for c in checks if str(c).upper() in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED"))
    return {"number": r["number"], "state": r["state"], "url": r["url"],
            "repo": "/".join(str(r["url"]).split("/")[3:5]) if r.get("url") else None,
            "checks": "failing" if failing else ("ok" if checks else None)}


def linked_pr(pr: dict[str, Any]) -> dict[str, Any] | None:
    """The state of a pull request a Claude session linked (its pr-link record), from gh."""
    key = (str(pr.get("repo")), str(pr.get("number")))
    if key not in _PR_CACHE:
        try:
            p = subprocess.run(["gh", "pr", "view", str(pr["number"]), "--repo", str(pr["repo"]), "--json",
                                "number,state,url,statusCheckRollup"], capture_output=True, text=True, timeout=20)
            _PR_CACHE[key] = _pr_row(json.loads(p.stdout)) if p.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
            _PR_CACHE[key] = None
    return _PR_CACHE[key] or {"number": pr["number"], "state": "?", "url": pr.get("url"), "repo": pr.get("repo"),
                              "checks": None}


def pull_request(cwd: str, branch: str) -> dict[str, Any] | None:
    """The newest pull request whose head is this branch in this folder's repository (gh, read-only)."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not top or branch in ("main", "master", "HEAD"):
        return None
    key = (top, branch)
    if key not in _PR_CACHE:
        try:
            p = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "1", "--json",
                                "number,state,url,statusCheckRollup"], cwd=top, capture_output=True, text=True, timeout=20)
            rows = json.loads(p.stdout or "[]") if p.returncode == 0 else []
        except (OSError, subprocess.TimeoutExpired, ValueError):
            rows = []
        _PR_CACHE[key] = _pr_row(rows[0]) if rows else None
    return _PR_CACHE[key]


READERS = {"claude": claude, "codex": codex, "warp": warp, "antigravity": antigravity}


def collect(since: float, tools: list[str], prs: bool = True, now: float | None = None) -> list[dict[str, Any]]:
    now = now or time.time()
    rows: list[dict[str, Any]] = []
    for t in tools:
        try:
            rows += READERS[t](since)
        except Exception as exc:  # noqa: BLE001 - one unreadable store never hides the others
            print(f"[sessions] {t}: {type(exc).__name__}", file=sys.stderr)
    for r in rows:
        cwd = r.get("cwd")
        if cwd and os.path.isdir(cwd) and not r.get("branch"):
            r["branch"] = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        r["project"] = os.path.basename(str(cwd).rstrip("/")) if cwd else None
        r["state"] = r.pop("tool_state", None) or _state(r["last"], now)
        link = r.pop("linked_pr", None)
        if not prs:
            r["pr"] = ({"number": link["number"], "state": "?", "url": link.get("url"), "repo": link.get("repo"),
                        "checks": None} if link else None)
        elif link:
            r["pr"] = linked_pr(link)   # the last pull request the session itself linked
        else:
            r["pr"] = pull_request(cwd, r["branch"]) if cwd and r.get("branch") and os.path.isdir(cwd) else None
    return sorted(rows, key=lambda r: -r["last"])


def _ago(s: float) -> str:
    return f"{int(s // 60)} min" if s < 3600 else f"{int(s // 3600)} h" if s < 86400 else f"{int(s // 86400)} d"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", default="24h", help="how far back: 30m, 6h, 2d (default 24h)")
    ap.add_argument("--tool", default=",".join(READERS))
    ap.add_argument("--no-prs", action="store_true", help="skip GitHub")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    m = re.fullmatch(r"(\d+)([mhd])", a.since)
    if not m:
        ap.error("--since like 30m, 6h or 2d")
    now = time.time()
    since = now - int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
    rows = collect(since, [t for t in a.tool.split(",") if t in READERS], not a.no_prs, now)
    if a.json:
        print(json.dumps(rows, indent=1, default=str))
        return 0
    if not rows:
        print("No sessions in that window.")
        return 0
    for r in rows:
        pr = r.get("pr")
        repo = str((pr or {}).get("repo") or "").split("/")[-1]
        prs = (f"{repo}#{pr['number']} {pr['state'].lower()}" + (f", checks {pr['checks']}" if pr.get("checks") else "")
               if pr else "")
        print(f"{r['tool']:<11} {r['state']:<7} {_ago(now - r['last']):>6} ago  {(r.get('project') or '—')[:24]:<24} "
              f"{(r.get('branch') or '')[:28]:<28} {prs:<34} {r.get('title') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
