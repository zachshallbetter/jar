#!/usr/bin/env python3
"""The chat links cycle (#1125): conversations' code-scored edges written to links.db, then titan files what's verified.

  1. chat_links.run(--apply): edges the code scores from hooks (branches, continues, records, entities, and the
     person's own "related"). Never the local judge's labels: chat_judge keeps those apart, as candidates and
     spot-check items (75% agreement, not trusted, #1016); this cycle doesn't ask any model (no --ask-local).
  2. then (inside chat_links.run when applied) file_suggest.run_all: for each adult, verified links only, one undo each, never kids or screens, a pair
     once undone never re-filed (#1087).
Skipped, with the reason, inside an eval window (scripts/eval_windows.conf), or while someone holds
deploys (/ai/tmp/deploy.hold holds a future epoch). Every run appends counts to CHAT_LINKS_CYCLE_LOG (no titles, no
ids). titan-chat-links.timer: every 2 h through the household's day, and once after the nightly.

  python3 scripts/chat_links_cycle.py            dry run: counts only, nothing written
  python3 scripts/chat_links_cycle.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

def windows_at(now: float) -> tuple[tuple[str, str], ...]:
    """The eval windows of now's UTC day as ("HH:MM", "HH:MM") UTC, from eval_windows.conf (#1518: the one list); a
    line with a zone is converted for that day (#1566). Empty when the file can't be read."""
    import eval_windows
    return tuple((f"{a[:2]}:{a[2:]}", f"{b[:2]}:{b[2:]}") for a, b, _ in
                 eval_windows.windows_at(now, Path(__file__).resolve().parent / "eval_windows.conf"))


LOG = Path(os.environ.get("CHAT_LINKS_CYCLE_LOG", "/ai/logs/chat-links-cycle.jsonl"))
HOLD = Path(os.environ.get("TITAN_DEPLOY_HOLD", "/ai/tmp/deploy.hold"))


def blocked(now: float, hold: Path | None = None) -> str | None:
    """Why this run must not write now, or None."""
    hm = datetime.fromtimestamp(now, timezone.utc).strftime("%H:%M")
    for a, b in windows_at(now) or (("00:00", "24:00"),):   # no list readable: refuse rather than write into an eval
        if a <= hm < b:
            return f"inside {a}–{b}Z"
    h = hold or Path(os.environ.get("TITAN_DEPLOY_HOLD") or HOLD)
    if h.is_file():
        try:
            if float(h.read_text().split()[0]) > now:
                return "a deploy hold is set"
        except (ValueError, IndexError):
            pass
    return None


def cycle(apply: bool = False, *, now: float | None = None, links: Callable[..., dict] | None = None,
          filing: Callable[..., dict] | None = None, log: Path | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    out: dict[str, Any] = {"at": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "applied": apply}
    why = blocked(now)
    if why:
        out["skipped"] = why
    else:
        if links is None:
            import chat_links
            links = lambda apply: chat_links.run(14, apply, False)   # noqa: E731 - never asks a model
        if filing is None:
            import file_suggest
            filing = lambda apply: file_suggest.run_all(apply=apply)   # noqa: E731
        got = dict(links(apply=apply) or {})
        filed = got.pop("auto_filed", None)       # an applied run files inside chat_links.run (#1087)
        out["links"] = {k: v for k, v in got.items() if isinstance(v, (int, float, dict)) and k != "hook_kinds"}
        out["filing"] = filed if apply else filing(apply=False)   # a dry run: what it would file, nothing written
    p = Path(log or os.environ.get("CHAT_LINKS_CYCLE_LOG") or LOG)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(out) + "\n")
    except OSError as exc:
        print(f"[chat_links_cycle] log not written: {type(exc).__name__}", file=sys.stderr)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Chat links, then verified auto-filing (#1125); counts only")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    print(json.dumps(cycle(a.apply)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
