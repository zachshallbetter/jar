#!/usr/bin/env python3
"""Claude's todos ⇄ GitHub issues (todo_issues.py), against a fake GitHub. Hermetic.
Run: python3 scripts/test_todo_issues.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-todo-issues-"))
os.environ["TODOS_DB"] = str(_TMP / "todos.db")
sys.path.insert(0, str(Path(__file__).parent))
import todo_issues as ti  # noqa: E402
import todos  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

_DAY = [datetime(2026, 10, 1, 18, 0)]


def nxt() -> datetime:
    """Each sync a day later at 6 PM, so each one sends that day's card (#650: one a day)."""
    _DAY[0] += timedelta(days=1)
    return _DAY[0]

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


class FakeGitHub:
    def __init__(self):
        self.issues, self.labels, self.calls = {}, {"titan-node": set(), "jellybean": set()}, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        parts = path.strip("/").split("/")   # repos/owner/repo/...
        repo = parts[2]
        if parts[3] == "labels" and method == "GET":
            return (200, {}) if parts[4] in self.labels[repo] else (404, {})
        if parts[3] == "labels" and method == "POST":
            self.labels[repo].add(body["name"]); return 201, {}
        if parts[3] == "issues" and method == "POST":
            n = len(self.issues) + 1
            self.issues[(repo, n)] = {"number": n, "state": "open", "labels": body["labels"], "title": body["title"],
                                      "body": body["body"], "html_url": f"https://github.com/x/{repo}/issues/{n}"}
            return 201, self.issues[(repo, n)]
        if parts[3].startswith("pulls") and method == "GET":
            return 200, getattr(self, "prs", {}).get(repo, [])
        if parts[3] == "issues" and method == "GET" and len(parts) > 5 and parts[5] == "comments":
            return 200, self.issues[(repo, int(parts[4]))].get("comments", [])
        if parts[3] == "issues" and method == "GET":
            return 200, self.issues[(repo, int(parts[4]))]
        return 404, {}


cards = []
gh = FakeGitHub()
print("── which repo ──")
check("an app request → jellybean", ti.repo_for({"title": "Find My in the iPhone app", "detail": ""}), "jellybean")
check("a titan request → titan-node", ti.repo_for({"title": "Weekly digest of bills", "detail": "on titan"}), "titan-node")
check("an explicit repo wins", ti.repo_for({"title": "iPhone thing", "detail": "", "repo": "titan-node"}), "titan-node")

print("\n── open, then ship ──")
a = todos.add("Tailscale as part of phone setup", todos.CLAUDE, detail="offer the VPN during pairing", created_by="Zach")
b = todos.add("Weekly bill digest card", todos.CLAUDE, detail="titan reads the records", created_by="Zach")
todos.add("Not Claude's", "zach")
out = ti.sync(gh, card=lambda *a, **k: cards.append((a, k)), now=nxt())
check("one issue per Claude todo, none for others", sorted(out["opened"]),
      ["jellybean#1: Tailscale as part of phone setup", "titan-node#2: Weekly bill digest card"])
check("labels: a feature-sized titan-node request isn't autocoder's (#384), jellybean never is",
      (gh.issues[("jellybean", 1)]["labels"], gh.issues[("titan-node", 2)]["labels"]), (["claude-todo"], ["claude-todo"]))
check("labels are created once", (gh.labels["titan-node"], gh.labels["jellybean"]), ({"claude-todo", "autocoder"}, {"claude-todo", "autocoder"}))
print("\n── autocoder only for small requests (#384) ──")
for title, detail, want in [
        ("Fix the bill card showing the due date in UTC", "", True),
        ("The weather card says 'tomorrow' for today: wrong day", "", True),
        ("Rename 'Labs' to 'Experiments' in titan's replies", "", True),
        ("Spatial presence and room-level routing architecture", "Device sensor fusion", False),
        ("Integrate Kokoro-82M voice model", "", False),
        ("Fix sync across devices", "", False),
        ("Weekly bill digest card", "titan reads the records", False),
        ("Fix the typo", "x" * 400, False)]:
    check(f"small_request: {title[:40]}", ti.small_request({"title": title, "detail": detail}), want)
check("the body carries the detail and who asked", ("offer the VPN during pairing" in gh.issues[("jellybean", 1)]["body"],
                                                    "Requested by Zach through titan" in gh.issues[("jellybean", 1)]["body"]), (True, True))
check("the todo remembers its issue", [t["issue_url"] for t in todos.list_for([todos.CLAUDE]) if t["id"] == a["id"]],
      ["https://github.com/x/jellybean/issues/1"])
check("a second sync opens nothing new", ti.sync(gh, card=lambda *a, **k: None, now=nxt())["opened"], [])
gh.issues[("jellybean", 1)].update(state="closed", state_reason="completed")
gh.issues[("titan-node", 2)].update(state="closed", state_reason="not_planned")
out = ti.sync(gh, card=lambda *a, **k: cards.append((a, k)), now=nxt())
check("closed as completed → shipped", out["shipped"], ["jellybean#1: Tailscale as part of phone setup"])
check("closed as not planned → dropped", out["dropped"], ["titan-node#2: Weekly bill digest card"])
check("both todos are done", todos.list_for([todos.CLAUDE]), [])
check("one card for the day to the admins, not one per todo (#650)", [(c[0][0], c[1]["to"], c[1]["key"][:12]) for c in cards],
      [("Claude's list today: 1 shipped, 1 not doing", "admins", "todo-digest-")])
check("…naming both, under their headings", ("Shipped:\n• Tailscale as part of phone setup (jellybean#1)" in cards[0][0][1],
      "Not doing:\n• Weekly bill digest card (titan-node#2)" in cards[0][0][1], cards[0][1]["urgency"]), (True, True, 1))
check("dry run touches nothing", ti.sync(gh, dry_run=True, card=lambda *a, **k: None), {"opened": [], "shipped": [], "dropped": []})

print("\n── closed as a duplicate (10-01: the same request twice in one night) ──")
c = todos.add("Location-aware routines", todos.CLAUDE, detail="on titan", created_by="Zach", repo="titan-node")
ti.sync(gh, card=lambda *a, **k: None, now=nxt())
n = max(k[1] for k in gh.issues)
gh.issues[("titan-node", n)].update(state="closed", state_reason="duplicate",
                                    comments=[{"body": "Same request as #284 (location-aware routines); tracking it there."}])
cards.clear()
out = ti.sync(gh, card=lambda *a, **k: cards.append((a, k)), now=nxt())
check("a duplicate isn't 'shipped' or 'dropped'", (out["shipped"], out["dropped"], len(out.get("merged", []))), ([], [], 1))
check("its line names the issue it joined, quietly", [(x[0][0], x[1]["urgency"], "titan-node#284" in x[0][1]) for x in cards],
      [("Claude's list today: 1 joined another", 0, True)])
check("and the todo is done", [t["id"] for t in todos.list_for([todos.CLAUDE])], [])

print("\n── being worked on (10-01, #285: nothing between 'added' and 'Shipped') ──")
p = todos.add("Weekly digest of chores", todos.CLAUDE, detail="on titan", created_by="Zach", repo="titan-node")
ti.sync(gh, card=lambda *a, **k: None, now=nxt())
num = max(k[1] for k in gh.issues if k[0] == "titan-node")
gh.prs = {"titan-node": [{"number": 400, "title": "Chores digest", "body": f"Refs #{num}0 (a different issue)"},
                         {"number": 401, "title": "Chores digest card", "body": f"Fixes #{num}.\n\nDetails"}]}
cards.clear()
out = ti.sync(gh, card=lambda *a, **k: cards.append((a, k)), now=nxt())
check("a PR naming the issue: a 'being worked on' line, naming the PR",
      [(c[0][0], "#401" in c[0][1]) for c in cards], [("Claude's list today: 1 being worked on", True)])
check("…and #4010-style numbers don't count", out.get("in_progress"), [f"titan-node#{num}: PR #401"])
cards.clear()
ti.sync(gh, card=lambda *a, **k: cards.append((a, k)), now=nxt())
check("the next run doesn't repeat it", cards, [])
gh.issues[("titan-node", num)].update(state="closed", state_reason="completed")
cards.clear()
ti.sync(gh, card=lambda *a, **k: cards.append((a, k)), now=nxt())
check("shipping comes in the next day's card", [c[0][0] for c in cards], ["Claude's list today: 1 shipped"])

print("\n── news goes to the person who asked (10-01, #383) ──")
m = todos.add("Robin's garden reminder card", todos.CLAUDE, detail="on titan", created_by="Robin",
              created_by_id="p_mir", repo="titan-node")
ti.sync(gh, card=lambda *a, **k: None, now=nxt())
mn = max(k[1] for k in gh.issues if k[0] == "titan-node")
gh.issues[("titan-node", mn)].update(state="closed", state_reason="completed")
cards.clear()
ti.sync(gh, card=lambda *a, **k: cards.append((a, k)), now=nxt())
check("her request's line goes in her own card, not the admins'", [(c[0][0], c[1]["to"]) for c in cards],
      [("Claude's list today: 1 shipped", ["p_mir"])])
check("a todo from before requesters were recorded still goes to the admins", ti._to({"created_by": "Zach"}), "admins")

print("\n── one card a day (#650: 74 cards in three days, 21 in one hour) ──")
st = {"pending": {}, "sent": {}}
got = []
for i in range(21):
    st["pending"].setdefault('"admins"', []).append(["shipped" if i % 3 else "same", f"Request {i} (titan-node#{i})"])
check("before 5 PM: nothing yet, all kept", (ti._send_digests(st, lambda *a, **k: got.append((a, k)),
      datetime(2026, 10, 9, 9, 0)), len(st["pending"]['"admins"'])), ([], 21))
ti._send_digests(st, lambda *a, **k: got.append((a, k)), datetime(2026, 10, 9, 17, 5))
check("from 5 PM: 21 updates are one card", (len(got), got[0][0][0]), (1, "Claude's list today: 14 shipped, 7 joined another"))
check("…long lists are cut with a count", "…and 2 more" in got[0][0][1], True)
st["pending"]['"admins"'].append(["shipped", "Late one (titan-node#99)"])
ti._send_digests(st, lambda *a, **k: got.append((a, k)), datetime(2026, 10, 9, 21, 0))
check("a later update the same day waits for tomorrow's", (len(got), len(st["pending"]['"admins"'])), (1, 1))
ti._send_digests(st, lambda *a, **k: got.append((a, k)), datetime(2026, 10, 10, 17, 0))
check("…and comes the next day, its own key", (len(got), got[-1][1]["key"]), (2, "todo-digest-2026-10-10"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
