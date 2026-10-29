#!/usr/bin/env python3
"""What got filtered, weekly, private to its owner (#861; Zach's KB-improvement list, 10-04). Mail triage (#823) and
the KB lifecycle (#851) drop things invisibly; once a week each person gets:

  a summary card   their mail by what triage decided (kept, records for the bills, newsletters, marketing,
                   notifications, codes never stored), and, for the admin, what the lifecycle expired
  up to 5 cards    the most borderline mail it dropped (subject, sender), each with "Keep this kind" / "Right call"

A tap is decided in code (proactive.card_action → answer): "Keep this kind" puts that mail into the person's memory
(it was held for the digest, mail_triage.DIGEST_HOLD_DAYS) and both answers become a triage test case
($MAIL_TRIAGE_CASES, 0600: subject, sender, what triage said, what the person said), which `mail_triage.py --eval`
scores the model on. Nothing comes back without the person's tap; only the owner can answer.

  python3 scripts/kb_digest.py --send          every owner with triaged mail this week (the weekly timer)
  python3 scripts/kb_digest.py --show <owner>  what the cards would say, nothing sent
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mail_triage as mt  # noqa: E402

WEEK = 7 * 86400
ITEMS = 5
CASES = Path(os.environ.get("MAIL_TRIAGE_CASES", "/ai/datasets/evals/mail-triage-cases.jsonl"))
_KEPT = ("personal", "action")
_LABEL = {"personal": "personal", "action": "needing action", "record": "records (for the bills)",
          "newsletter": "newsletters", "marketing": "marketing", "notification": "notifications",
          "code": "sign-in codes (never stored)", "expired": "not sorted in time (dropped)"}


def _lifecycle_expired(since: float) -> int:
    import kb_lifecycle
    try:
        rows = [json.loads(x) for x in kb_lifecycle.LOG.read_text().splitlines() if x.strip()]
    except OSError:
        return 0
    return sum(int(r.get("expired") or 0) for r in rows if r.get("mode") == "apply" and float(r.get("ts") or 0) >= since)


def build(owner: str, now: float | None = None, *, admin: bool = False, path: Path | None = None) -> dict[str, Any] | None:
    """The week's summary and its borderline items for one owner, or None when triage decided nothing for them."""
    now = time.time() if now is None else now
    since = now - WEEK
    c = mt.counts(owner, since, path)
    if not c:
        return None
    kept = sum(c.get(k, 0) for k in _KEPT)
    lines = [f"Kept in your memory: {kept} ({', '.join(f'{c[k]} {_LABEL[k]}' for k in _KEPT if c.get(k))})" if kept
             else "Kept in your memory: none"]
    if c.get("record"):
        lines.append(f"Read for bills and receipts, then let go: {c['record']}")
    dropped = [f"{c[k]} {_LABEL[k]}" for k in ("newsletter", "marketing", "notification", "code", "expired") if c.get(k)]
    if dropped:
        lines.append("Filtered out: " + ", ".join(dropped))
    if admin:
        n = _lifecycle_expired(since)
        if n:
            lines.append(f"Expired from memory (old mail, past events, saved pages): {n}")
    items = sorted((d for d in mt.dropped(owner, since, path) if not d.get("answer") and (d.get("borderline") or 0) > 0),
                   key=lambda d: (-(d.get("borderline") or 0), -(d.get("decided_at") or 0)))[:ITEMS]
    if items:
        lines.append(f"{len(items)} closest calls below: tap Keep this kind if I got one wrong.")
    return {"owner": owner, "summary": "\n".join(lines), "items": items, "counts": c}


def send(owner: str, now: float | None = None, *, admin: bool = False, add_card: Callable[..., Any] | None = None,
         path: Path | None = None) -> int:
    """Post the week's cards to the owner only. → how many cards."""
    d = build(owner, now, admin=admin, path=path)
    if d is None:
        return 0
    if add_card is None:
        import proactive
        add_card = proactive.add_card
    week = time.strftime("%Y-%W", time.gmtime(time.time() if now is None else now))
    add_card("What got filtered this week", d["summary"], kind="info", urgency=0, source="kb-digest", to=[owner],
             key=f"kb-digest:{owner}:{week}", expires_in=WEEK)
    for it in d["items"]:
        frm = f"From {it['sender']} · " if it.get("sender") else ""
        add_card(f"Filtered: {it['title'][:100]}", f"{frm}sorted as {it['category']}", kind="info", urgency=0,
                 source="kb-digest", to=[owner], key=f"kb-digest-item:{it['item_id']}"[:80], expires_in=WEEK,
                 actions=[{"id": "keep", "label": "Keep this kind", "kind": "reply", "value": f"keep:{it['item_id']}"},
                          {"id": "right", "label": "Right call", "kind": "reply", "value": f"right:{it['item_id']}"}])
    return 1 + len(d["items"])


def _case(row: dict[str, Any], label: str, now: float) -> None:
    CASES.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(CASES), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps({"title": row.get("title"), "sender": row.get("sender") or "", "triaged": row.get("category"),
                            "label": label, "owner": row.get("owner"), "at": now}) + "\n")


def answer(value: str, person: str | None, *, index: Callable[..., Any] | None = None,
           path: Path | None = None) -> tuple[bool, str]:
    """A tap on a digest card: keep:<item> / right:<item>. Only the mail's owner; once. → (done, what to say)."""
    verb, _, iid = str(value or "").partition(":")
    if verb not in ("keep", "right") or not iid:
        return False, "That answer isn't one I know."
    row = mt.held(iid, path)
    if row is None:
        return False, "That mail isn't held any more: the week's hold ran out."
    if not person or row.get("owner") != person:
        return False, "Only the person whose mail it is can answer that."
    if row.get("answer"):
        return True, "Already answered."
    now = time.time()
    _case(row, "keep" if verb == "keep" else "drop", now)
    mt.set_answer(iid, verb, path)
    learned = mt.learn(person, row.get("sender") or mt.sender_of(row.get("text") or ""), "keep" if verb == "keep" else "drop",
                       path, now)   # #1644: triage learns it, for this person only
    if verb == "right":
        return True, "Noted: dropping that kind was right."
    if index is None:
        import connectors_runner
        index = connectors_runner.index_triaged
    index(iid, row["title"], row["text"] or "", json.loads(row.get("metadata") or "{}"))
    if learned == "keep":
        return True, "It's back in your memory, and mail from that sender will be kept for you from now on."
    return True, "It's back in your memory. (That sender has no address I can learn from, so the next one is decided afresh.)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The weekly 'what got filtered' cards (#861)")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--show", metavar="OWNER")
    a = ap.parse_args(argv)
    import household_identity as hh
    if a.show:
        d = build(a.show, admin=a.show == hh.ADMIN_PERSON_ID)
        print(json.dumps({"summary": d["summary"], "items": len(d["items"]), "counts": d["counts"]} if d else None, indent=1))
        return 0
    if a.send:
        con = mt._db()
        try:
            owners = [r[0] for r in con.execute("SELECT DISTINCT owner FROM queue WHERE decided_at >= ?", (time.time() - WEEK,))]
        finally:
            con.close()
        n = sum(send(o, admin=o == hh.ADMIN_PERSON_ID) for o in owners)
        print(f"kb digest: {n} card(s) to {len(owners)} owner(s)")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
