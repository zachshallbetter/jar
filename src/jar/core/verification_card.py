#!/usr/bin/env python3
"""The weekly verification pass (Zach, 10-04): one card listing what's merged but not yet checked on a real device.

"Done" (merged) isn't "Verified" (seen working where it matters). Issues closed with the `needs-verification` label
in titan-node and jellybean are the list; the device steps live in jellybean's docs/device-checks-0.13.x.md. Once a
week the admin gets one card (it replaces last week's) with up to 8 of them and a "Walk me through it" prompt.
When a check passes, the label comes off the issue and it drops from next week's card.

  python3 scripts/verification_card.py          dry run: prints what the card would say
  python3 scripts/verification_card.py --apply  posts it (titan-verification.timer)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any, Callable

TOKEN_FILE = os.environ.get("GITHUB_TOKEN_FILE", "/ai/secrets/github_token")
REPOS = ("zachshallbetter/jellybean", "zachshallbetter/titan-node")
LABEL = "needs-verification"
SHOW = 8


def _github(path: str) -> Any:
    try:
        tok = open(TOKEN_FILE).read().strip()
    except OSError:
        return None
    req = urllib.request.Request("https://api.github.com" + path, headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json", "User-Agent": "titan"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def pending(api: Callable[[str], Any] = _github) -> list[dict[str, Any]] | None:
    """Issues awaiting a device check, oldest first; None when GitHub couldn't be read."""
    out = []
    for repo in REPOS:
        q = urllib.parse.quote(f"repo:{repo} label:{LABEL} is:issue")
        r = api(f"/search/issues?q={q}&per_page=50&sort=updated&order=asc")
        if r is None:
            return None
        for it in r.get("items") or []:
            out.append({"ref": f"{repo.split('/')[1]}#{it['number']}", "title": str(it.get("title") or "")[:90],
                        "url": it.get("html_url")})
    return out


def card_text(items: list[dict[str, Any]]) -> tuple[str, str]:
    n = len(items)
    title = f"{n} thing{'' if n == 1 else 's'} to check on your devices"
    lines = [f"• {i['title']} ({i['ref']})" for i in items[:SHOW]]
    if n > SHOW:
        lines.append(f"…and {n - SHOW} more.")
    lines.append("Steps are in Jellybean's device-checks docs; say which passed and I'll mark them verified.")
    return title, "\n".join(lines)


def run(apply: bool, api: Callable[[str], Any] = _github, add_card: Callable[..., Any] | None = None) -> dict[str, Any]:
    items = pending(api)
    if items is None:
        return {"posted": False, "why": "GitHub unreadable"}
    if not items:
        return {"posted": False, "why": "nothing awaiting a check"}
    title, detail = card_text(items)
    if apply:
        if add_card is None:
            import household_identity as hh
            import proactive
            add_card = proactive.add_card
            to = hh.ADMIN_PERSON_ID
        else:
            to = "admin"
        add_card(title, detail, kind="info", urgency=1, source="verification", key="verify-weekly", to=to,
                 expires_in=7 * 86400,
                 actions=[{"label": "Walk me through it", "kind": "prompt",
                           "value": "Walk me through this week's device checks one at a time."}])
    return {"posted": apply, "count": len(items), "title": title, "detail": detail}


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    print(json.dumps(run("--apply" in sys.argv), indent=1))
