#!/usr/bin/env python3
"""Titan alert delivery — the one place health checks send notifications through.

Every alert is appended to /ai/logs/titan-alerts.log. Optional channels come from the
environment (units load /ai/config/alerts.env):

  TITAN_ALERT_NTFY_URL   e.g. https://ntfy.sh/<unguessable-topic> — push to the ntfy phone
                         app (subscribe to the same topic). Plain-text body + Title header.
  TITAN_ALERT_WEBHOOK    generic JSON {"title", "text"} (Slack/Discord-compatible).

Alerts carry check names and short error text only — never secrets or household data.
Delivery failures are swallowed so a dead channel can't break the check that called it.

Edge-triggering: `EdgeAlert(name).update(problems)` fires once when a set of problems
appears or changes, once on recovery, and stays silent while nothing changes, so a
standing outage doesn't page every 10 minutes.

  python3 scripts/titan_alert.py test "hello from titan"   # send a test alert
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

ALERT_LOG = "/ai/logs/titan-alerts.log"
STATE_DIR = "/ai/logs"


def alert(title: str, msg: str, *, priority: str = "default", tags: str = "") -> list[str]:
    """Deliver one alert; returns the channels that accepted it (the log always counts)."""
    delivered = []
    line = "[%s] %s: %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), title, msg)
    try:
        # Group-writable: preflight runs as root, the agent checks as zach (both titanops). A
        # root-owned 0644 log once swallowed every non-root alert without a trace.
        fd = os.open(ALERT_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o664)
        with os.fdopen(fd, "a") as f:
            f.write(line + "\n")
        delivered.append("log")
    except Exception as exc:
        print("titan_alert: cannot write %s (%s): %s" % (ALERT_LOG, exc, line), file=sys.stderr)
    ntfy = os.environ.get("TITAN_ALERT_NTFY_URL", "").strip()
    if ntfy:
        try:
            headers = {"Title": title, "Priority": priority}
            if tags:
                headers["Tags"] = tags
            req = urllib.request.Request(ntfy, data=msg.encode(), headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=8)
            delivered.append("ntfy")
        except Exception:
            pass
    hook = os.environ.get("TITAN_ALERT_WEBHOOK", "").strip()
    if hook:
        try:
            req = urllib.request.Request(hook, data=json.dumps({"title": title, "text": msg}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8)
            delivered.append("webhook")
        except Exception:
            pass
    return delivered


class EdgeAlert:
    """Fire on change only: problems appear/change → alert; problems clear → recovered.

    `confirm` is how many consecutive failing updates it takes before the first alert. The
    canary runs every 10 min and a single Gemini hiccup used to page twice (DOWN, then
    RECOVERED 10 min later); with confirm=2 a one-run blip stays silent, and a recovery is
    only announced for an outage that was announced."""

    def __init__(self, name: str, title: str, confirm: int = 1):
        self.path = os.path.join(STATE_DIR, ".%s-state" % name)
        self.title = title
        self.confirm = max(1, confirm)

    def _prev(self) -> tuple[str, int]:
        """(alerted problem key, consecutive failing updates). The state file used to hold just
        the alerted key; read that as a streak that has already been announced."""
        try:
            raw = open(self.path).read().strip()
        except Exception:
            return "", 0
        if "|" in raw:
            key, _, streak = raw.partition("|")
            try:
                return key, int(streak)
            except ValueError:
                return key, 0
        return raw, (self.confirm if raw else 0)

    def update(self, problems: dict[str, str]) -> str | None:
        """problems: {check: short reason}. Returns "down" / "changed" / "recovered" / None."""
        key = ",".join(sorted(problems))
        alerted, streak = self._prev()
        streak = streak + 1 if key else 0
        event = None
        if key and streak >= self.confirm and key != alerted:
            event = "changed" if alerted else "down"
            detail = "; ".join("%s: %s" % (k, problems[k][:140]) for k in sorted(problems))
            alert("%s — %s" % (self.title, "DOWN" if event == "down" else "still failing, changed"),
                  detail, priority="high", tags="warning")
            alerted = key
        elif not key and alerted:
            event = "recovered"
            alert("%s — RECOVERED" % self.title, "all checks passing again (was: %s)" % alerted,
                  tags="white_check_mark")
            alerted = ""
        try:
            with open(self.path, "w") as f:
                f.write("%s|%d" % (alerted, streak))
        except Exception:
            pass
        return event


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "test":
        msg = " ".join(sys.argv[2:]) or "test alert from titan"
        print("delivered via:", ", ".join(alert("Titan test alert", msg)) or "nothing")
    else:
        print(__doc__)
