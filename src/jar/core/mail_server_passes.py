#!/usr/bin/env python3
"""The mail derivation passes on server mail (titan-node#955 step 6, gaps 1 and 2; the checklist is on #955).

The Mac's mail goes through mail_triage, then mail_bills and mail_suggestions read it. Server mail goes through the same
passes inside mail_server.derive(), while its body is in memory, and nothing waits on disk:
  triage        mail_triage.classify on the (masked) original: personal / action → its envelope may reach the KB
                (MAIL_SERVER_KB); record → the bills and suggestions below, envelope kept locally; newsletter /
                marketing / notification → held for the weekly digest exactly as the Mac's are (mail_triage's queue,
                DIGEST_HOLD_DAYS, 0600); code → nothing. A model that can't be reached: no category; the envelope
                stays local and the mail is triaged on a later run.
  bills         every mail that looks like money (mail_bills.is_candidate), whatever triage said: a payment
                confirmation always reaches mail_bills (#955). Read from the original (#954), never a summary.
  suggestions   personal, action and record mail that names a date (mail_suggestions.is_candidate).
The batch's texts live only for the batch. MAIL_SERVER_PASSES=1, off otherwise (then derive() is as before).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))


def enabled() -> bool:
    return os.environ.get("MAIL_SERVER_PASSES", "0") == "1"


def classify(title: str, text: str, llm: Callable[[str], str] | None = None) -> str | None:
    import mail_triage
    return mail_triage.classify(title, text, llm)


def hold_for_digest(item: dict[str, Any], category: str, path: Path | None = None) -> None:
    """A newsletter, marketing or notification mail: held for the owner's weekly digest, as the Mac's are."""
    import mail_triage
    mail_triage.enqueue(item["item_id"], item["owner"], item["title"], item["text"], item["md"], path=path)
    mail_triage._decide(item["item_id"], category, keep_text=False, path=path)


def _source(item: dict[str, Any]) -> dict[str, Any]:
    """The shape mail_bills and mail_suggestions read (a KB source with its metadata)."""
    return {"source_id": item["item_id"], "source_type": "connector:mail", "title": item["title"],
            "updated_at": item["stamp"], "metadata": item["md"]}


def run_bills(items: list[dict[str, Any]], *, llm: Callable[[str], str] | None = None, request: Callable | None = None,
              db: Path | None = None) -> dict[str, Any]:
    import mail_bills
    picked = [i for i in items if mail_bills.is_candidate(i["title"], i["text"])]
    if not picked:
        return {"candidates": 0, "filed": []}
    texts = {i["item_id"]: i["text"] for i in picked}
    if request is None:
        import agent_runtime_lib as lib
        request = lambda rt, path, method="GET", body=None: lib._runtime_request(rt, path, method, body)  # noqa: E731
    con = sqlite3.connect(str(db or mail_bills.SEEN_DB))
    try:
        st = mail_bills.run(True, [_source(i) for i in picked], texts.get, llm or mail_bills.local_llm, request, con)
    finally:
        con.close()
        texts.clear()
    return {"candidates": st["candidates"], "filed": [f["node_id"] for f in st["filed"]]}


def run_suggestions(items: list[dict[str, Any]], *, llm: Callable[[str], str] | None = None,
                    events: list | None = None, send: Callable | None = None, roles: dict | None = None,
                    now: datetime | None = None, db: Path | None = None) -> dict[str, Any]:
    import mail_bills
    import mail_suggestions
    # notifications always; newsletters and marketing only with an invite, a known sender or a calendar title
    # (mail_suggestions.worth_suggesting); the owner's calendar titles are read only when one needs them
    if events is None and any(i.get("category") in mail_suggestions.SUGGEST_IF_KNOWN and not (i.get("ics") or
                                                                                          i.get("sender_known")) for i in items):
        import agent_runtime_lib as lib
        events = lib.record_nodes("appointment")[0]
    titles = {i.get("owner"): [str((n.get("data") or {}).get("title") or "") for n in (events or [])
                               if mail_suggestions._event_owner(n.get("data") or {}) == i.get("owner")] for i in items}
    picked = [i for i in items if mail_suggestions.worth_suggesting(
        i.get("category"), i["title"], has_ics=bool(i.get("ics")), sender_known=bool(i.get("sender_known")),
        cal_titles=titles.get(i.get("owner"), ())) and mail_suggestions.is_candidate(i["title"], i["text"])]
    if not picked:
        return {"candidates": 0, "suggested": 0}
    texts = {i["item_id"]: i["text"] for i in picked}
    if events is None or roles is None or now is None:
        import agent_runtime_lib as lib
        import household_identity as hh
        events = lib.record_nodes("appointment")[0] if events is None else events
        roles = {str(p["id"]): str(p.get("role") or "") for p in hh.get_store().people()} if roles is None else roles
        now = lib._local_now().replace(tzinfo=None) if now is None else now
    if send is None:
        import proactive
        send = proactive.add_card
    con = mail_suggestions._db(db)
    try:
        st = mail_suggestions.run(True, [_source(i) for i in picked], texts.get, llm or mail_bills.local_llm, events,
                                  con, now, roles, send=send)
    finally:
        con.close()
        texts.clear()
    return {"candidates": st["candidates"], "suggested": len(st["suggested"])}
