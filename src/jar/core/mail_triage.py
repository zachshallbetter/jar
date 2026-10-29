#!/usr/bin/env python3
"""Mail triage at ingest (10-03 KB cleanup, Zach: "we've pulled in a ton of garbage": 354 mail items, mostly marketing,
newsletters, notifications, codes and duplicates). Only mail a person wrote, or mail that needs the reader to act,
reaches the knowledge base.

  at ingest   (connectors_runner, source mail, with MAIL_TRIAGE=1) a code-like mail (a verification code, a
              magic sign-in link, "confirm your email") is caught by rule and kept nowhere, its title masked too;
              every other new mail waits here (redacted text, mode 0600), not in the KB
  the pass    (mail_bills' timer, before it reads mail) titan's local model puts each waiting mail in one category:
                personal, action   indexed into the KB, as mail was before
                record             kept here for mail_bills and mail_suggestions (receipts, orders, statements), then
                                   deleted once both have read it; a meeting invite, reschedule or cancel
                                   (mail_invites, by rule) the model put in a dropped category comes here too
                newsletter, marketing, notification   held DIGEST_HOLD_DAYS (title, sender, text; 0600) for the
                                   owner's weekly "what got filtered" card (kb_digest.py, #861), then deleted
                code               deleted at once
              unreachable model: it waits; untriaged after UNTRIAGED_TTL_H it's deleted, never indexed
  never       a tombstoned item (forget_source / deletions) comes back: connectors_runner skips those before this

  python3 scripts/mail_triage.py [--apply] [--limit N]    the pass (titan-mail-bills.service runs it with --apply)
  python3 scripts/mail_triage.py --status                 counts only
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable
from ops_ports import CODE_MODEL_URL  # noqa: E402

DB = Path(os.environ.get("MAIL_TRIAGE_DB", "/ai/datasets/connectors/mail_triage.db"))
CATS = ("personal", "action", "record", "newsletter", "marketing", "notification", "code")
KEEP = frozenset({"personal", "action"})
UNTRIAGED_TTL_H = float(os.environ.get("MAIL_TRIAGE_TTL_H", "72"))
RECORD_TTL_H = 48.0
MASKED_TITLE = "(a sign-in or verification code)"
DROPPED = frozenset({"newsletter", "marketing", "notification"})
DIGEST_HOLD_DAYS = 8.0
# What makes a dropped mail borderline (#861): words a mail worth keeping tends to carry, and a person as the sender.
_REPLY_RX = re.compile(r"(?i)^\s*(?:re|fwd?|fw)\s*:")   # a reply or forward: a person's thread
_KEEP_CUES = re.compile(r"(?i)\b(security|password|account|payment|paid|declined|failed|invoice|bill|due|overdue|"
                        r"appointment|reservation|booking|confirm\w*|renew\w*|expir\w*|deadline|action|urgent|"
                        r"school|doctor|dentist|insurance|tax|refund|delivery|tracking)\b")
_AUTOMATED = re.compile(r"(?i)no-?reply|notifications?@|news(letter)?@|marketing@|info@|hello@|team@|updates?@")
_lock = threading.Lock()

_CODE_RX = re.compile(
    r"(?i)\b(?:verification|security|login|log-in|sign[- ]?in|one[- ]time|confirmation|access|auth(?:entication)?|2fa)"
    r"\s+(?:code|pin|link)\b|\bone[- ]time\s+pass(?:word|code)\b|\botp\b|\bmagic\s+(?:sign[- ]?in\s+)?link\b|"
    r"\bsecure\s+(?:sign[- ]?in\s+)?link\s+to\b|\b(?:confirm|verify)\s+your\s+(?:email|e-mail|account)\b|"
    r"\bactivate\s+your\s+account\b|\baccount\s+activation\b|\bid\s+login\b|\byour\s+code\s+is\b|"
    r"\b\d{3}[- ]?\d{3}\s+is\s+your\b|\b\d{4,8}\s+is\s+your\b|\blogin\s*\[\s*\d")


def is_invite(title: str, text: str) -> bool:
    """A meeting invite, reschedule or cancel (mail_invites.detect): kept for mail_suggestions even when the model
    calls it a notification, as "an event was added or changed" mail used to be dropped."""
    try:
        import mail_invites
        return mail_invites.detect(title, text)
    except Exception:  # noqa: BLE001 - the rule never stops triage
        return False


def enabled() -> bool:
    return os.environ.get("MAIL_TRIAGE", "") == "1"


def is_code(title: str, text: str) -> bool:
    """A sign-in or verification code mail, by rule (never stored, not even its title)."""
    return bool(_CODE_RX.search(f"{title}\n{str(text or '')[:400]}"))


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("MAIL_TRIAGE_DB") or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS queue (item_id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, "
                "text TEXT, metadata TEXT NOT NULL, category TEXT, done TEXT NOT NULL DEFAULT '[]', "
                "created_at REAL NOT NULL, decided_at REAL)")
    # #1644: what each person taught triage about a sender ("Keep this kind" on the digest): their own rows only
    con.execute("CREATE TABLE IF NOT EXISTS sender_rules (owner TEXT NOT NULL, sender TEXT NOT NULL, rule TEXT NOT NULL, "
                "keeps INTEGER NOT NULL DEFAULT 0, drops INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL, "
                "PRIMARY KEY (owner, sender))")
    have = {r[1] for r in con.execute("PRAGMA table_info(queue)")}
    for col, typ in (("sender", "TEXT"), ("borderline", "REAL"), ("answer", "TEXT")):   # #861's digest
        if col not in have:
            try:
                con.execute(f"ALTER TABLE queue ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass   # another process added it first
    return con


def sender_of(text: str) -> str:
    """The "From:" line's name or address, if the mail's text starts with headers."""
    m = re.search(r"(?im)^from:\s*(.+)$", str(text or "")[:600])
    return " ".join(m.group(1).split())[:120] if m else ""


_ADDR_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def sender_key(sender: str) -> str:
    """The sender a rule is about: their address, lower-case; a sender with no address gets no rule ("")."""
    m = _ADDR_RX.search(str(sender or ""))
    return m.group(0).lower() if m else ""


def learn(owner: str, sender: str, verdict: str, path: Path | None = None, now: float | None = None) -> str | None:
    """#1644: the person's own answer about one sender's mail (kb_digest: "keep" or "drop"). "keep" makes a rule that
    keeps that sender's mail for this person from now on; "drop" counts that dropping it was right (and ends a keep
    rule only if they keep saying so: more drops than keeps). → the rule now, or None when the sender has no address."""
    key = sender_key(sender)
    if not key or not owner or verdict not in ("keep", "drop"):
        return None
    now = now or time.time()
    with _lock:
        con = _db(path)
        try:
            r = con.execute("SELECT keeps, drops FROM sender_rules WHERE owner=? AND sender=?", (owner, key)).fetchone()
            keeps, drops = (r["keeps"], r["drops"]) if r else (0, 0)
            keeps, drops = keeps + (verdict == "keep"), drops + (verdict == "drop")
            rule = "keep" if keeps > drops else "drop"
            con.execute("INSERT INTO sender_rules (owner, sender, rule, keeps, drops, updated_at) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(owner, sender) DO UPDATE SET rule=excluded.rule, keeps=excluded.keeps, "
                        "drops=excluded.drops, updated_at=excluded.updated_at", (owner, key, rule, keeps, drops, now))
            con.commit()
        finally:
            con.close()
    return rule


def rule_for(owner: str, sender: str, path: Path | None = None) -> str | None:
    """This person's rule for this sender ("keep" or "drop"), or None."""
    key = sender_key(sender)
    if not key or not owner:
        return None
    con = _db(path)
    try:
        r = con.execute("SELECT rule FROM sender_rules WHERE owner=? AND sender=?", (owner, key)).fetchone()
    finally:
        con.close()
    return r["rule"] if r else None


def rules(owner: str, path: Path | None = None) -> list[dict[str, Any]]:
    """What this person has taught triage, newest first."""
    con = _db(path)
    try:
        return [dict(r) for r in con.execute("SELECT sender, rule, keeps, drops, updated_at FROM sender_rules WHERE owner=? "
                                             "ORDER BY updated_at DESC", (owner,))]
    finally:
        con.close()


def forget_rule(owner: str, sender: str, path: Path | None = None) -> bool:
    """The person takes back what they taught about one sender (Settings). → whether there was one."""
    key = sender_key(sender)
    if not key or not owner:
        return False
    with _lock:
        con = _db(path)
        try:
            cur = con.execute("DELETE FROM sender_rules WHERE owner=? AND sender=?", (owner, key))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def borderline(title: str, sender: str, text: str) -> float:
    """How close a dropped mail came to being worth keeping: keep-ish words in its subject (and, less, its first
    lines), and a person rather than a mailer as the sender. Higher is more borderline."""
    score = 2.0 * len(set(m.lower() for m in _KEEP_CUES.findall(title or "")))
    score += 2.0 if _REPLY_RX.match(title or "") else 0.0
    score += 0.5 * min(4, len(set(m.lower() for m in _KEEP_CUES.findall(str(text or "")[:600]))))
    if sender and not _AUTOMATED.search(sender):
        score += 1.0
    return score


def enqueue(item_id: str, owner: str, title: str, text: str, metadata: dict[str, Any], path: Path | None = None) -> None:
    """A new (or changed) mail waits for triage; a changed one is triaged again."""
    with _lock:
        con = _db(path)
        try:
            con.execute("INSERT OR REPLACE INTO queue (item_id, owner, title, text, metadata, category, done, created_at, "
                        "decided_at) VALUES (?,?,?,?,?,NULL,'[]',?,NULL)",
                        (item_id, owner, title, text, json.dumps(metadata, default=str), time.time()))
            con.commit()
        finally:
            con.close()


def forget(item_id: str, path: Path | None = None) -> None:
    """The mail was deleted on the device, or forgotten: whatever waits here goes."""
    with _lock:
        con = _db(path)
        try:
            con.execute("DELETE FROM queue WHERE item_id=?", (item_id,))
            con.commit()
        finally:
            con.close()


# ── the model ──────────────────────────────────────────────────────────────────────────────────

PROMPT = """Sort one email for a household assistant. Reply with JSON only: {"category": "<one of: personal, action, record, newsletter, marketing, notification, code>"}

personal: a real person wrote it to the reader: a reply thread, a direct note, a forwarded conversation. NOT a site telling the reader that someone messaged, posted or went live; NOT a calendar or app telling the reader an event was added or changed; NOT genealogy or "hint" emails.
action: the reader must do something soon or it costs them: a payment that failed or was declined, a bill due, an account at risk or a breach of THEIR data, an expiring certificate or domain, a request to approve access, an invitation to answer. NOT welcome or onboarding emails, tips, "set up your page", store reminders to reorder, or confirmations that something already changed.
record: proof of something already done: a receipt, an invoice paid, an order, shipping or delivery update, a statement, a confirmation that settings or account details changed.
newsletter: editorial content, digests, articles, product news, meeting reports or summaries.
marketing: promotions, offers, sales, ads, job alerts, upsells, "we miss you", reorder reminders from a store.
notification: an automated status ping with nothing to do: someone messaged or posted on a site, an event was added to a calendar, a sign-in from a known device, rankings, build results, genealogy hints.
code: a one-time verification or login code, a magic sign-in link, or "confirm your email / activate your account".

Email:
Subject: %s
%s"""


def model_urls() -> list[str]:
    """Qwen3.6 (:8198) first: on the 354 mails of 10-03 it kept 39 (v2 prompt on it: 55), and the 8B (:8010) kept 51
    with failed deploys and "X messaged you" among them. The 8B when the code model is busy or down."""
    return [u.rstrip("/") for u in (os.environ.get("MAIL_TRIAGE_URL", CODE_MODEL_URL),
                                    os.environ.get("MAIL_TRIAGE_FALLBACK_URL", "http://127.0.0.1:8010")) if u]


def classify(title: str, text: str, llm: Callable[[str], str] | None = None) -> str | None:
    """One category, or None when the model can't be reached or answers something else (it waits)."""
    prompt = PROMPT % (" ".join(str(title or "").split())[:200], str(text or "")[:1200])
    out = None
    for url in ([None] if llm is not None else model_urls()):
        try:
            if llm is not None:
                out = llm(prompt)
            else:
                import typed_local as tl   # #941: the category is an enum the grammar enforces
                got, _model = tl.ask(prompt, tl.obj({"category": tl.enum(CATS)}, required=("category",)), url,
                                     name="mail_category", max_tokens=40, timeout=120)
                out = json.dumps(got) if got else ""
            break
        except Exception:  # noqa: BLE001 - this model is down: the next, else try again next pass
            out = None
    if out is None:
        return None
    m = re.search(r'"category"\s*:\s*"(\w+)"', str(out or ""))
    return m.group(1) if m and m.group(1) in CATS else None


# ── the pass ───────────────────────────────────────────────────────────────────────────────────

def run(apply: bool, *, index: Callable[[str, str, str, dict[str, Any]], Any] | None = None,
        llm: Callable[[str], str] | None = None, limit: int = 200, now: float | None = None,
        path: Path | None = None) -> dict[str, Any]:
    """Triage what waits: index personal and action mail into the KB, keep records for the bills pass, drop the rest.
    `index(item_id, title, text, metadata)` writes the KB item (default: connectors_runner.index_triaged, which also
    marks the ledger)."""
    now = now or time.time()
    if index is None:
        import connectors_runner
        index = connectors_runner.index_triaged
    con = _db(path)
    try:
        todo = con.execute("SELECT * FROM queue WHERE category IS NULL ORDER BY created_at LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    stats = {"apply": apply, "waiting": len(todo), **{c: 0 for c in CATS}, "unreached": 0, "expired": 0, "indexed": 0,
             "invites": 0}
    stats["learned"] = 0
    for r in todo:
        if rule_for(r["owner"], sender_of(r["text"] or ""), path) == "keep":
            cat = "personal"            # #1644: this person said this sender's mail is worth keeping; no model call
            stats["learned"] += 1
        else:
            cat = classify(r["title"], r["text"] or "", llm)
        if cat is None:
            if now - r["created_at"] > UNTRIAGED_TTL_H * 3600:
                stats["expired"] += 1
                if apply:
                    _decide(r["item_id"], "expired", keep_text=False, path=path)
            else:
                stats["unreached"] += 1
            continue
        if cat in DROPPED and is_invite(r["title"], r["text"] or ""):
            # an invite, a reschedule or a cancel (mail_invites, by rule) is never dropped as a notification: it's
            # kept as a record for the suggestions pass, whatever the model said
            stats["invites"] += 1
            cat = "record"
        stats[cat] += 1
        if not apply:
            continue
        if cat in KEEP:
            if index(r["item_id"], r["title"], r["text"] or "", json.loads(r["metadata"] or "{}")) is not False:
                stats["indexed"] += 1
        _decide(r["item_id"], cat, keep_text=(cat == "record"), path=path)
    if apply:
        stats["swept"] = sweep(now=now, path=path)
    return stats


def _decide(item_id: str, category: str, keep_text: bool, path: Path | None = None) -> None:
    with _lock:
        con = _db(path)
        try:
            if category in DROPPED:
                # held for the owner's weekly digest (#861): a "keep this kind" tap can still bring it back
                r = con.execute("SELECT title, text FROM queue WHERE item_id=?", (item_id,)).fetchone()
                sender = sender_of(r["text"] if r else "")
                con.execute("UPDATE queue SET category=?, decided_at=?, sender=?, borderline=? WHERE item_id=?",
                            (category, time.time(), sender, borderline(r["title"] if r else "", sender,
                                                                       r["text"] if r else ""), item_id))
            elif keep_text:
                con.execute("UPDATE queue SET category=?, decided_at=? WHERE item_id=?", (category, time.time(), item_id))
            else:
                con.execute("UPDATE queue SET category=?, decided_at=?, text=NULL, title=? WHERE item_id=?",
                            (category, time.time(), "" if category == "code" else "(triaged)", item_id))
            con.commit()
        finally:
            con.close()


def category_of(item_id: str, path: Path | None = None) -> str | None:
    con = _db(path)
    try:
        r = con.execute("SELECT category FROM queue WHERE item_id=?", (item_id,)).fetchone()
        return r["category"] if r else None
    finally:
        con.close()


# ── records for mail_bills and mail_suggestions ────────────────────────────────────────────────

def record_sources(path: Path | None = None) -> list[dict[str, Any]]:
    """Record mail still holding its text, shaped like the KB's source list (mail_bills.with_meta keeps its owner)."""
    con = _db(path)
    try:
        rows = con.execute("SELECT * FROM queue WHERE category='record' AND text IS NOT NULL").fetchall()
    finally:
        con.close()
    return [{"source_id": r["item_id"], "source_type": "connector:mail", "title": r["title"],
             "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["created_at"])),
             "metadata": {**json.loads(r["metadata"] or "{}"), "owner": r["owner"]}, "triaged": "record"} for r in rows]


def dropped_sources(path: Path | None = None) -> list[dict[str, Any]]:
    """Notifications, newsletters and marketing still holding their text (the digest's hold), shaped like the KB's
    source list, for the suggestions pass to judge by mail_suggestions.worth_suggesting (10-09)."""
    con = _db(path)
    try:
        q = ",".join("?" * len(DROPPED))
        rows = con.execute(f"SELECT * FROM queue WHERE category IN ({q}) AND text IS NOT NULL", sorted(DROPPED)).fetchall()
    finally:
        con.close()
    return [{"source_id": r["item_id"], "source_type": "connector:mail", "title": r["title"],
             "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["created_at"])),
             "metadata": {**json.loads(r["metadata"] or "{}"), "owner": r["owner"]}, "triaged": r["category"],
             "sender": r["sender"] or ""} for r in rows]


def dropped_text(item_id: str, path: Path | None = None) -> str | None:
    """A dropped mail's text during the digest's hold, for the suggestions pass only (never served as a record)."""
    con = _db(path)
    try:
        q = ",".join("?" * len(DROPPED))
        r = con.execute(f"SELECT text FROM queue WHERE item_id=? AND category IN ({q})", (item_id, *sorted(DROPPED))).fetchone()
        return r["text"] if r else None
    finally:
        con.close()


def record_text(item_id: str, path: Path | None = None) -> str | None:
    con = _db(path)
    try:
        r = con.execute("SELECT text FROM queue WHERE item_id=? AND category='record'", (item_id,)).fetchone()
        return r["text"] if r else None
    finally:
        con.close()


def text_of(fallback: Callable[[str], str], path: Path | None = None) -> Callable[[str], str]:
    def read(sid: str) -> str:
        t = record_text(sid, path)
        return t if t is not None else fallback(sid)
    return read


def required() -> set[str]:
    return {"bills"} | ({"suggestions"} if os.environ.get("MAIL_SUGGESTIONS", "1") == "1" else set())


def mark(pass_name: str, item_ids: list[str], path: Path | None = None) -> int:
    """mail_bills / mail_suggestions read these records; one every pass has read loses its text."""
    with _lock:
        con = _db(path)
        try:
            for iid in item_ids:
                r = con.execute("SELECT done FROM queue WHERE item_id=?", (iid,)).fetchone()
                if r is not None:
                    con.execute("UPDATE queue SET done=? WHERE item_id=?",
                                (json.dumps(sorted(set(json.loads(r["done"] or "[]")) | {pass_name})), iid))
            con.commit()
        finally:
            con.close()
    return sweep(path=path)


def sweep(now: float | None = None, path: Path | None = None) -> int:
    """Records every pass has read, or past RECORD_TTL_H, lose their text. → how many."""
    now = now or time.time()
    with _lock:
        con = _db(path)
        try:
            gone = [r["item_id"] for r in con.execute("SELECT item_id, done, decided_at FROM queue "
                                                       "WHERE category='record' AND text IS NOT NULL")
                    if required() <= set(json.loads(r["done"] or "[]"))
                    or now - (r["decided_at"] or now) > RECORD_TTL_H * 3600]
            for iid in gone:
                con.execute("UPDATE queue SET text=NULL WHERE item_id=?", (iid,))
            # dropped mail past the digest's hold: title, sender and text go (#861)
            q = ",".join("?" * len(DROPPED))
            con.execute(f"UPDATE queue SET text=NULL, title='(triaged)', sender=NULL WHERE category IN ({q}) AND "
                        f"text IS NOT NULL AND decided_at < ?", (*sorted(DROPPED), now - DIGEST_HOLD_DAYS * 86400))
            # a decided row is only an id and a category: kept 30 days, so the pass's counts can be checked
            con.execute("DELETE FROM queue WHERE text IS NULL AND decided_at < ?", (now - 30 * 86400,))
            con.commit()
            return len(gone)
        finally:
            con.close()


def dropped(owner: str, since: float, path: Path | None = None) -> list[dict[str, Any]]:
    """The owner's mail dropped since `since` and still held: {item_id, title, sender, category, borderline, answer}."""
    con = _db(path)
    try:
        q = ",".join("?" * len(DROPPED))
        rows = con.execute(f"SELECT item_id, title, sender, category, borderline, answer, decided_at FROM queue WHERE "
                           f"owner=? AND category IN ({q}) AND text IS NOT NULL AND decided_at >= ?",
                           (owner, *sorted(DROPPED), since)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def counts(owner: str, since: float, path: Path | None = None) -> dict[str, int]:
    """What triage decided for this owner since `since`, by category (code included: a count, nothing else)."""
    con = _db(path)
    try:
        return {r[0]: r[1] for r in con.execute("SELECT category, COUNT(*) FROM queue WHERE owner=? AND category IS NOT "
                                                "NULL AND decided_at >= ? GROUP BY category", (owner, since))}
    finally:
        con.close()


def held(item_id: str, path: Path | None = None) -> dict[str, Any] | None:
    """A held dropped mail, whole (for a "keep this kind" tap)."""
    con = _db(path)
    try:
        r = con.execute("SELECT * FROM queue WHERE item_id=? AND text IS NOT NULL", (item_id,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def set_answer(item_id: str, answer: str, path: Path | None = None) -> None:
    with _lock:
        con = _db(path)
        try:
            con.execute("UPDATE queue SET answer=? WHERE item_id=?", (answer, item_id))
            con.commit()
        finally:
            con.close()


def status(path: Path | None = None) -> dict[str, Any]:
    con = _db(path)
    try:
        c = {str(k): n for k, n in con.execute("SELECT COALESCE(category, 'waiting'), COUNT(*) FROM queue GROUP BY 1")}
        held = con.execute("SELECT COUNT(*) FROM queue WHERE text IS NOT NULL").fetchone()[0]
    finally:
        con.close()
    return {"by_category": c, "holding_text": held}


def eval_cases(cases: Path | None = None, llm: Callable[[str], str] | None = None) -> dict[str, Any]:
    """The model on the people's own answers from the weekly digest (#861): a case labelled keep should come out
    personal or action, one labelled drop anything else. Subject and sender only: the text is long gone."""
    import kb_digest
    p = Path(cases or kb_digest.CASES)
    try:
        rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    except OSError:
        rows = []
    ok, miss = 0, []
    for r in rows:
        cat = classify(r.get("title") or "", f"From: {r.get('sender') or ''}", llm)
        good = (cat in KEEP) == (r.get("label") == "keep")
        ok += good
        if not good:
            miss.append({"label": r.get("label"), "now": cat})
    return {"cases": len(rows), "right": ok, "misses": miss[:20]}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Mail triage: personal and action mail into the KB, records for the bills pass")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--eval", action="store_true", help="score the model on the digest's answered cases (#861)")
    a = ap.parse_args()
    if a.status:
        print(json.dumps(status()))
        return 0
    if a.eval:
        print(json.dumps(eval_cases()))
        return 0
    if not enabled():
        print("mail triage: off (MAIL_TRIAGE=1 turns it on)")
        return 0
    st = run(a.apply, limit=a.limit)
    print("mail triage: " + json.dumps(st))
    return 0


if __name__ == "__main__":
    sys.exit(main())
