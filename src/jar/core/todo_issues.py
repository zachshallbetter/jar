#!/usr/bin/env python3
"""Claude's todos ⇄ GitHub issues: the "request" step of titan's gap protocol, closed end to end.

A todo on Claude's list (add_todo for=claude) becomes an issue in the repo it's about (private
repos): titan-node issues are labelled `claude-todo` and `autocoder`, so titan's nightly
autonomous coder drafts a PR (it never merges); jellybean issues get `claude-todo` for the
Claude Code sessions on the Mac (Swift can't be built on titan). When the issue is closed as
completed, the todo is marked done and the admin gets a "Shipped" card; closed as not planned,
the todo is done with a card that says so; closed as a duplicate, the todo is done quietly with a
card naming the issue it joined (10-01: Zach asked for the same thing twice in one night).

  python3 scripts/todo_issues.py [--dry-run]       (titan-todo-issues.timer, every 10 min)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import todos  # noqa: E402

OWNER = os.environ.get("TODO_ISSUES_OWNER", "zachshallbetter")
REPOS = ("titan-node", "jellybean")
TOKEN_FILE = os.environ.get("GITHUB_TOKEN_FILE", "/ai/secrets/github_token")
LABELS = {"claude-todo": ("5319e7", "Requested through titan's Claude todo list"),
          "autocoder": ("0e8a16", "titan's autonomous coder may draft a PR")}
_JELLYBEAN_RX = re.compile(r"\b(iphone|phone|ios|ipad|mac ?app|macos app|jellybean|swift|swiftui|widget|live activit|"
                           r"menu ?bar|find my|home screen|app icon|notification|the bean|deck|card view)\b", re.I)

Api = Callable[[str, str, Any], tuple[int, Any]]



def ref_of(t: dict) -> str:
    return f"{t['repo']}#{t['issue_number']}"


def _duplicate_of(t: dict, api) -> str | None:
    """The issue a duplicate points at: the last "#N" (or owner/repo#N) a comment names, if any."""
    try:
        st, comments = api("GET", f"/repos/{OWNER}/{t['repo']}/issues/{t['issue_number']}/comments", None)
    except Exception:  # noqa: BLE001
        return None
    if st != 200 or not isinstance(comments, list):
        return None
    for c in reversed(comments):
        m = re.findall(r"(?:[\w.-]+/)?([\w.-]+)?#(\d+)", str(c.get("body") or ""))
        if m:
            repo, num = m[0]
            return f"{repo or t['repo']}#{num}"
    return None

def repo_for(todo: dict[str, Any]) -> str:
    """The repo a request is about: its own `repo` if titan set one, else by what it mentions."""
    r = str(todo.get("repo") or "").strip().lower()
    if r in REPOS:
        return r
    return "jellybean" if _JELLYBEAN_RX.search(f"{todo['title']} {todo.get('detail', '')}") else "titan-node"


def _api(token: str) -> Api:
    def call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
        import egress
        egress.note("github")
        req = urllib.request.Request("https://api.github.com" + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                              "Content-Type": "application/json", "User-Agent": "titan-todo-issues"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {}
    return call


def _ensure_labels(api: Api, repo: str) -> None:
    for name, (color, desc) in LABELS.items():
        st, _ = api("GET", f"/repos/{OWNER}/{repo}/labels/{name}", None)
        if st == 404:
            api("POST", f"/repos/{OWNER}/{repo}/labels", {"name": name, "color": color, "description": desc})


# The nightly coder (local Qwen) finishes one small change in one place; a feature or an architecture
# request it abandons at its step cap (09-30), and 10-01 every titan-node todo got the label, including
# "spatial presence and room-level routing architecture" (#384). So only a short request that reads like a
# fix or a tweak, with no scope words, is labelled; everything else is the Claude sessions'.
_SMALL_RX = re.compile(r"\b(?:fix|bug|broken|typo|rename|wrong|incorrect|error|crash(?:es|ing)?|doesn'?t|isn'?t|"
                       r"won'?t|should(?:n'?t)?|tweak|adjust|reword|clarify|off by one|missing)\b", re.I)
_BIG_RX = re.compile(r"\b(?:architecture|architect|framework|platform|infrastructure|integrat\w*|implement\w*|"
                     r"comprehensive|research|evaluat\w*|design|redesign|migrat\w*|autonomous|multi[- ]?\w+|"
                     r"across (?:devices|services|turns)|sync\w*|pipeline|engine|model|routing|end[- ]to[- ]end|"
                     r"overhaul|rewrite|system|harness|orchestrat\w*)\b", re.I)


def small_request(todo: dict[str, Any]) -> bool:
    """Whether a titan-node request is small enough for the nightly coder (#384): short, reads like a fix or
    a tweak, and names no feature-sized scope."""
    title, detail = str(todo.get("title") or ""), str(todo.get("detail") or "")
    text = f"{title} {detail}"
    return (len(title) <= 80 and len(detail) <= 280 and bool(_SMALL_RX.search(text))
            and not _BIG_RX.search(text))


def issue_body(todo: dict[str, Any], repo: str) -> str:
    import local_time
    when = local_time.clock(todo["created_at"], "%Y-%m-%d %H:%M %Z")
    lines = [todo["detail"] or "(no detail given)", "",
             f"Requested by {todo.get('created_by') or 'the household'} through titan on {when} "
             f"(titan todo `{todo['id']}`).",
             "When this is closed as completed, titan marks the todo done and tells them it shipped."]
    if repo == "titan-node" and small_request(todo):
        lines.append("Labelled `autocoder`: titan's nightly coder may draft a PR. It never merges; a Claude "
                     "session reviews, merges, deploys and adds an eval case.")
    elif repo == "titan-node":
        lines.append("Larger than one small change, so it's for the Claude sessions, not the nightly coder (#384).")
    else:
        lines.append("For the Claude Code sessions on the Mac (Swift; not buildable on titan).")
    return "\n".join(lines)


def sync(api: Api, *, dry_run: bool = False, card: Callable[..., Any] | None = None,
         db: Path | None = None, now: datetime | None = None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"opened": [], "shipped": [], "dropped": []}
    if card is None:
        import proactive
        card = proactive.add_card
    digest = _digest_state(db)   # #650: updates gather into one card a day per person (74 cards in 3 days before)

    def note(t: dict, kind: str, line: str) -> None:
        digest["pending"].setdefault(json.dumps(_to(t)), []).append([kind, line])
    for t in todos.list_for([todos.CLAUDE], db=db):
        if t.get("issue_number"):
            continue
        repo = repo_for(t)
        if dry_run:
            out["opened"].append(f"(dry) {repo}: {t['title']}")
            continue
        _ensure_labels(api, repo)
        labels = ["claude-todo"] + (["autocoder"] if repo == "titan-node" and small_request(t) else [])
        st, issue = api("POST", f"/repos/{OWNER}/{repo}/issues",
                        {"title": t["title"], "body": issue_body(t, repo), "labels": labels})
        if st == 201 and isinstance(issue, dict) and issue.get("number"):
            todos.set_issue(t["id"], repo, int(issue["number"]), str(issue.get("html_url") or ""), db=db)
            out["opened"].append(f"{repo}#{issue['number']}: {t['title']}")
    open_prs: dict[str, list[dict]] = {}
    progress = _progress_state(db)
    for t in todos.list_for([todos.CLAUDE], db=db):
        if not t.get("issue_number"):
            continue
        st, issue = api("GET", f"/repos/{OWNER}/{t['repo']}/issues/{t['issue_number']}", None)
        if st != 200 or not isinstance(issue, dict):
            continue
        if issue.get("state") != "closed":
            # 10-01 (#285): between "added to Claude's list" and "Shipped" the requester heard nothing.
            # When a pull request names the issue, say so once per pull request.
            if t["repo"] not in open_prs:
                sp, prs = api("GET", f"/repos/{OWNER}/{t['repo']}/pulls?state=open&per_page=100", None)
                open_prs[t["repo"]] = prs if sp == 200 and isinstance(prs, list) else []
            pr = _pr_for(int(t["issue_number"]), open_prs[t["repo"]])
            if pr and progress.get(t["id"]) != pr["number"]:
                if not dry_run:
                    note(t, "working", f"{t['title'][:90]} ({ref_of(t)}, pull request #{pr['number']})")
                    progress[t["id"]] = pr["number"]
                out.setdefault("in_progress", []).append(f"{ref_of(t)}: PR #{pr['number']}")
            continue
        reason = issue.get("state_reason") or "completed"
        if reason == "duplicate":
            if dry_run:
                out.setdefault("merged", []).append(f"(dry) {ref_of(t)}: {t['title']}")
                continue
            todos.complete(t["id"], done_by="GitHub", db=db)
            into = _duplicate_of(t, api)
            note(t, "same", f"{t['title'][:90]} ({ref_of(t)} joined {into or 'another issue'})")
            out.setdefault("merged", []).append(f"{ref_of(t)}: {t['title']}")
            continue
        planned = reason != "not_planned"
        ref = f"{t['repo']}#{t['issue_number']}"
        if dry_run:
            out["shipped" if planned else "dropped"].append(f"(dry) {ref}: {t['title']}")
            continue
        todos.complete(t["id"], done_by="GitHub", db=db)
        if planned:
            note(t, "shipped", f"{t['title'][:90]} ({ref})")
            out["shipped"].append(f"{ref}: {t['title']}")
        else:
            note(t, "dropped", f"{t['title'][:90]} ({ref})")
            out["dropped"].append(f"{ref}: {t['title']}")
    if not dry_run:
        _save_progress(progress, db)
        out["digests"] = _send_digests(digest, card, now)
        _save_digest(digest, db)
    return out


# ── the daily card (#650) ───────────────────────────────────────────────────────────────────────
DIGEST_HOUR = int(os.environ.get("TODO_DIGEST_HOUR", "17"))   # household time: one card a day, from this hour
_KINDS = (("shipped", "Shipped", "shipped"), ("working", "Being worked on", "being worked on"),
          ("same", "Same request as another", "joined another"), ("dropped", "Not doing", "not doing"))


def _digest_path(db: Path | None) -> Path:
    return _progress_path(db).with_name("todo_digest.json")


def _digest_state(db: Path | None) -> dict[str, Any]:
    try:
        d = json.loads(_digest_path(db).read_text())
        if isinstance(d.get("pending"), dict) and isinstance(d.get("sent"), dict):
            return d
    except (OSError, ValueError, AttributeError):
        pass
    return {"pending": {}, "sent": {}}


def _save_digest(state: dict[str, Any], db: Path | None) -> None:
    try:
        _digest_path(db).write_text(json.dumps(state))
    except OSError:
        pass


def _send_digests(state: dict[str, Any], card: Callable[..., Any], now: datetime | None = None) -> list[str]:
    """Each person's gathered updates as one card, once a day from DIGEST_HOUR (household time); the rest wait for
    tomorrow's. "Shipped" ones make it a normal card; otherwise it's quiet. Returns who got one."""
    import local_time
    now = now or local_time.now()
    today = now.strftime("%Y-%m-%d")
    if now.hour < DIGEST_HOUR:
        return []
    sent = []
    for who, items in list(state["pending"].items()):
        if not items or state["sent"].get(who) == today:
            continue
        counts = {k: sum(1 for i in items if i[0] == k) for k, _, _ in _KINDS}
        title = "Claude's list today: " + ", ".join(f"{counts[k]} {w}" for k, _, w in _KINDS if counts[k])
        lines = []
        for k, head, _ in _KINDS:
            got = [i[1] for i in items if i[0] == k]
            if got:
                lines.append(f"{head}:\n" + "\n".join(f"• {x}" for x in got[:12])
                             + (f"\n…and {len(got) - 12} more" if len(got) > 12 else ""))
        card(title, "\n\n".join(lines) + ("\n\nAsk titan about a shipped one to try it." if counts["shipped"] else ""),
             kind="report", urgency=1 if counts["shipped"] else 0, source="todos", to=json.loads(who),
             key=f"todo-digest-{today}")
        state["sent"][who] = today
        state["pending"][who] = []
        sent.append(who)
    return sent


def _to(t: dict) -> Any:
    """Who hears about a Claude todo: the person who asked for it (10-01, #383: Robin's requests came back
    to the admins); the admins for todos from before requesters were recorded, or from titan itself."""
    pid = str(t.get("created_by_id") or "").strip()
    return [pid] if pid else "admins"


def _pr_for(number: int, prs: list[dict]) -> dict | None:
    """The newest open pull request whose title or body names issue #number (\"Fixes #263\", \"refs #284\",
    or its URL), never #2630 or another repo's #263."""
    rx = re.compile(rf"(?<![\w/-])#{number}\b|/issues/{number}\b")
    hits = [p for p in prs if isinstance(p, dict) and rx.search(f"{p.get('title') or ''}\n{p.get('body') or ''}")]
    return max(hits, key=lambda p: int(p.get("number") or 0)) if hits else None


def _progress_path(db: Path | None) -> Path:
    base = Path(db) if db else Path(os.environ.get("TODOS_DB", "/ai/datasets/connectors/todos.db"))
    return base.with_name("todo_progress.json")


def _progress_state(db: Path | None) -> dict[str, int]:
    try:
        return {str(k): int(v) for k, v in json.loads(_progress_path(db).read_text()).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _save_progress(state: dict[str, int], db: Path | None) -> None:
    try:
        _progress_path(db).write_text(json.dumps(state))
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        token = Path(TOKEN_FILE).read_text().strip()
    except OSError as exc:
        print(f"no GitHub token: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(sync(_api(token), dry_run=a.dry_run), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
