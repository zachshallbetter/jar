#!/usr/bin/env python3
"""How well triage matches what each person does with their mail (#1644 "measured"; feeds #1645's earned autonomy).

Read-only, counts only. It joins two things titan already keeps in the mail store:
  - triage's category for each server mail (mail_server's messages.category, from the server passes), and
  - what the person confirmed with their own tap (mail_moves' plans): mail they cleared (archive or trash), mail they
    filed into a folder, and clears they took back with Undo.

  agreed          they cleared it and triage had dropped it (newsletter, marketing, notification, code)
  triage_kept     they cleared it but triage would have kept it (personal, action, record): triage too generous for them
  missed          triage dropped it but they kept it: filed it into a folder, or undid its clear
  untriaged       no category on record (triage didn't run on it, or it's older than the passes' window)

agreement = agreed / (agreed + triage_kept): of the mail a person throws out that triage judged, how much triage would
have thrown out too. missed counts the opposite mistake, the costly one (#1645: acting alone needs it near zero).

Nothing here names a sender or a subject, and nothing is written. A person's figures are their own (an owner key).

  python3 scripts/triage_scorecard.py [--days 30]        every owner's figures, as JSON
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

DROPPED = frozenset({"newsletter", "marketing", "notification", "code"})
KEPT = frozenset({"personal", "action", "record"})
CLEAR_VERBS = ("archive", "trash")


def _category(con, account: str, mid: str) -> str | None:
    r = con.execute("SELECT category FROM messages WHERE account=? AND message_id=? AND category IS NOT NULL LIMIT 1",
                    (account, mid)).fetchone()
    return r[0] if r else None


def score(owner: str, *, since: float, db: Path | None = None) -> dict[str, Any]:
    """One person's figures from their own confirmed plans done since `since`."""
    import mail_moves
    con = mail_moves._db(db)
    try:
        plans = con.execute("SELECT id, account, mids, verb, undo_of FROM mail_plans WHERE owner=? AND state='done' "
                            "AND done_at >= ?", (owner, float(since))).fetchall()
        undone = {p["undo_of"] for p in plans if p["undo_of"]}
        out = {"cleared": 0, "agreed": 0, "triage_kept": 0, "untriaged": 0, "filed": 0, "undone": 0, "missed": 0}
        for p in plans:
            verb = (p["verb"] or "archive")
            if p["undo_of"] or verb not in (*CLEAR_VERBS, "move"):
                continue   # an undo is counted on the plan it undid; marks (#1642) aren't a keep-or-clear choice
            mids = [m for m, _s, _d in json.loads(p["mids"] or "[]")]
            for mid in mids:
                cat = _category(con, p["account"], mid)
                if verb == "move":
                    out["filed"] += 1
                    out["missed"] += cat in DROPPED
                    continue
                if p["id"] in undone:          # cleared, then taken back: they wanted it kept
                    out["undone"] += 1
                    out["missed"] += cat in DROPPED
                    continue
                out["cleared"] += 1
                if cat is None:
                    out["untriaged"] += 1
                elif cat in DROPPED:
                    out["agreed"] += 1
                elif cat in KEPT:
                    out["triage_kept"] += 1
    finally:
        con.close()
    judged = out["agreed"] + out["triage_kept"]
    out["agreement"] = round(out["agreed"] / judged, 3) if judged else None
    return out


def owners(*, since: float, db: Path | None = None) -> list[str]:
    import mail_moves
    con = mail_moves._db(db)
    try:
        return [r[0] for r in con.execute("SELECT DISTINCT owner FROM mail_plans WHERE state='done' AND done_at >= ? "
                                          "ORDER BY owner", (float(since),))]
    finally:
        con.close()


def main(argv: list[str]) -> int:
    days = 30
    if "--days" in argv:
        try:
            days = max(1, min(int(argv[argv.index("--days") + 1]), 90))
        except (IndexError, ValueError):
            print("usage: triage_scorecard.py [--days N]", file=sys.stderr)
            return 2
    since = time.time() - days * 86400
    print(json.dumps({"days": days, "owners": {o: score(o, since=since) for o in owners(since=since)}}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
