#!/usr/bin/env python3
"""Memory's "Needs a look" queue (#1074): what extraction can't catch at insert time, found nightly, per person.

  disagree  {a, b}       two facts from different chats that can't both be true now ("Robin is vegetarian" from Sep 20
                         and a later "Robin ordered the steak"): Still true (a stands, b goes) / Not any more (a goes) /
                         Forget both
  duplicate {a, b}       two wordings of one fact stored before the near-duplicate check: Merge (keep the newer
                         wording, forget the other)
  fading    {count, oldest}  facts no real turn has used in FADING_DAYS (#1072): Review (nothing changes) / Forget all

The nightly pass (titan_memory_review.timer, 04:40Z: outside the nightly 00:25–01:10 PT and the model eval 01:35–03:00 PT)
compares facts **within each person's groups** (#1073): the model sees at most PAIRS_PER_GROUP pairs a group, never
N² of everything. Duplicates are code
(word overlap, the same names, numbers and days); a disagreement is the local model's call (titan's own :8010 via
typed_local, never a hosted model) on pairs that share a name or two content words. Each person's queue is theirs:
built only from their own facts, read only by them. At most CAP open items per person; one ignored for EXPIRE_DAYS
drops off with nothing changed. Resolving uses memory's own forget paths (restorable).

  python3 scripts/memory_review.py nightly [--dry-run]     counts only; a dry run never calls the model
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

CAP = 5
EXPIRE_DAYS = 30
FADING_DAYS = 60
PAIRS_PER_GROUP = 30
DUP_OVERLAP = 0.5
CHOICES = {"disagree": ("still_true", "not_any_more", "forget_both"), "duplicate": ("merge", "keep_both"),
           "fading": ("review", "forget_all"),
           # the one-time cleanup of facts stored before 10-10's extraction rules (Zach: "these are not really useful")
           "wish": ("to_dev_note", "keep"), "other_adult": ("forget", "keep"), "one_off": ("forget", "keep"),
           "voice": ("rewrite_all", "keep")}
CLEANUP_CAP = 40            # the cleanup is one pass, offered once: its own cap, not the nightly CAP


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _ensure(db: Any) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS memory_review (id TEXT PRIMARY KEY, person TEXT NOT NULL, kind TEXT NOT NULL,
        a TEXT, b TEXT, data TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'open', choice TEXT,
        created_at REAL NOT NULL, resolved_at REAL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_review_person ON memory_review(person, state)")


# ── finding them ─────────────────────────────────────────────────────────────────────────────────────────────────────

def is_duplicate(a: str, b: str) -> bool:
    import agent_memory as am
    return am._near_duplicate(a, [b], threshold=DUP_OVERLAP)


def worth_asking(a: str, b: str) -> bool:
    """A pair the model is asked about: the same name, or two content words in common."""
    import agent_memory as am
    na, nb = am._specifics(a)[0], am._specifics(b)[0]
    return bool(na & nb) or len(am._content_words(a) & am._content_words(b)) >= 2


def ask_local(pairs: list[tuple[str, str]], post: Any = None) -> list[bool]:
    """Whether each pair disagrees (can't both be true now), from titan's local model; [] when it can't answer."""
    import typed_local
    import memory_groups
    url = memory_groups.LOCAL_URL
    import urllib.parse
    if (urllib.parse.urlparse(url).hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("memory is only ever reviewed by the local model")
    lines = "\n".join(f"{i + 1}. A: {a[:200]}\n   B: {b[:200]}" for i, (a, b) in enumerate(pairs))
    prompt = ("Each numbered item is two remembered facts about the same household. Say whether they disagree: they "
              "can't both be true now (one corrects or contradicts the other). Different details that can both be true "
              "do not disagree.\n\n" + lines + f'\n\nAnswer {{"disagree": [...]}} with exactly {len(pairs)} true/false '
              "values, in order.")
    schema = typed_local.obj({"disagree": {"type": "array", "items": {"type": "boolean"}, "minItems": len(pairs),
                                           "maxItems": len(pairs)}}, ["disagree"])
    try:
        got, _ = typed_local.ask(prompt, schema, url, max_tokens=20 + 6 * len(pairs), timeout=180,
                                 ensure=lambda o: None if len(o.get("disagree") or []) == len(pairs) else "one per pair",
                                 post=post)
    except Exception:  # noqa: BLE001
        return []
    return [bool(x) for x in (got or {}).get("disagree", [])] if got else []


def _key(kind: str, a: str | None, b: str | None) -> tuple[str, str, str]:
    x, y = sorted([a or "", b or ""])
    return kind, x, y


def nightly(mem: Any, people: list[dict[str, Any]], *, ask: Callable[[list[tuple[str, str]]], list[bool]] | None = None,
            now: float | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Expire, then fill each person's queue up to CAP. → counts per kind (no fact text, no names)."""
    ask = ask or ask_local
    now = time.time() if now is None else now
    db = mem._db
    _ensure(db)
    out = {"people": 0, "expired": 0, "disagree": 0, "duplicate": 0, "fading": 0, "asked": 0, "would_ask": 0}
    if not dry_run:
        out["expired"] = db.execute("UPDATE memory_review SET state='expired', resolved_at=? WHERE state='open' AND "
                                    "created_at < ?", (now, now - EXPIRE_DAYS * 86400)).rowcount
    for p in people:
        pid = str(p["id"])
        if p.get("role") == "household":
            continue
        out["people"] += 1
        seen = {_key(r["kind"], r["a"], r["b"]) for r in db.execute("SELECT kind, a, b FROM memory_review WHERE person=?",
                                                                    (pid,))}
        room = CAP - db.execute("SELECT COUNT(*) FROM memory_review WHERE person=? AND state='open'", (pid,)).fetchone()[0]
        found: list[tuple[str, str | None, str | None, dict]] = []
        facts = mem.list_facts(limit=10**6, person_id=pid, sort="old")
        by_group: dict[str, list[dict]] = {}
        for f in facts:
            by_group.setdefault(f["group"], []).append(f)
        for g, fs in sorted(by_group.items()):
            to_ask = []
            for a, b in itertools.combinations(fs, 2):    # code checks are cheap: every pair in the group
                if is_duplicate(a["text"], b["text"]):
                    found.append(("duplicate", a["id"], b["id"], {"group": g}))
                elif worth_asking(a["text"], b["text"]) and len(to_ask) < PAIRS_PER_GROUP:
                    to_ask.append((a, b))               # the model is asked at most PAIRS_PER_GROUP per group
            if to_ask and dry_run:
                out["would_ask"] += len(to_ask)   # a dry run never wakes the model (it may be busy with an eval)
            elif to_ask:
                out["asked"] += len(to_ask)
                verdict = ask([(a["text"], b["text"]) for a, b in to_ask])
                found += [("disagree", a["id"], b["id"], {"group": g}) for (a, b), v in zip(to_ask, verdict) if v]
        try:
            since = float(mem.usage_since() or now)
        except Exception:  # noqa: BLE001
            since = now
        if now - since >= FADING_DAYS * 86400:     # only once usage has been tracked that long
            unused = mem.list_facts(limit=10**6, person_id=pid, unused_since=now - FADING_DAYS * 86400, sort="old")
            if unused:
                found.append(("fading", None, None, {"count": len(unused), "oldest": unused[0]["ts"],
                                                     "ids": [f["id"] for f in unused]}))
        for kind, a, b, data in found:
            if room <= 0:
                break
            if _key(kind, a, b) in seen:
                continue                       # asked before (open, resolved or expired): never asked again
            seen.add(_key(kind, a, b))
            out[kind] += 1
            room -= 1
            if not dry_run:
                db.execute("INSERT INTO memory_review (id, person, kind, a, b, data, created_at) VALUES (?,?,?,?,?,?,?)",
                           ("mr-" + uuid.uuid4().hex[:12], pid, kind, a, b, json.dumps(data), now))
    if not dry_run:
        db.commit()
    return out


def cleanup(mem: Any, people: list[dict[str, Any]], person: str | None = None, *, now: float | None = None,
            dry_run: bool = False) -> dict[str, Any]:
    """Offer each person a one-time cleanup of facts the 10-10 rules would now refuse, as proposals in their own queue,
    never a change: a wish about titan (→ their dev notes), another adult's health or private matter, a one-off
    event (→ forget), and one item rewriting the rest in the second person. Only their own facts; asked once (a
    proposal resolved or expired is never offered again). → counts per kind (never a fact's text)."""
    import agent_memory as am
    now = time.time() if now is None else now
    db = mem._db
    _ensure(db)
    out = {"people": 0, "wish": 0, "other_adult": 0, "one_off": 0, "voice": 0}
    for p in people:
        pid = str(p["id"])
        if p.get("role") == "household" or (person and pid != person):
            continue
        out["people"] += 1
        seen = {_key(r["kind"], r["a"], r["b"]) for r in db.execute("SELECT kind, a, b FROM memory_review WHERE person=?",
                                                                    (pid,))}
        found: list[tuple[str, str | None, dict]] = []
        rewrite: list[str] = []
        for f in mem.list_facts(limit=10**6, person_id=pid, sort="old"):
            t = f["text"]
            kind = ("wish" if am.product_wish(t) else "other_adult" if am.other_adults_private(t, people, pid)
                    else "one_off" if am.one_off(t) else None)
            if kind:
                found.append((kind, f["id"], {}))
            elif am.second_person(t) != " ".join(t.split()):
                rewrite.append(f["id"])
        if rewrite:
            found.append(("voice", None, {"count": len(rewrite), "ids": rewrite}))
        room = CLEANUP_CAP
        for kind, a, data in found:
            if room <= 0 or _key(kind, a, None) in seen or (kind == "voice" and any(k == "voice" for k, _x, _y in seen)):
                continue
            seen.add(_key(kind, a, None))
            out[kind] += 1
            room -= 1
            if not dry_run:
                db.execute("INSERT INTO memory_review (id, person, kind, a, b, data, created_at) VALUES (?,?,?,?,?,?,?)",
                           ("mr-" + uuid.uuid4().hex[:12], pid, kind, a, None, json.dumps(data), now))
    if not dry_run:
        db.commit()
    return out


# ── the person's queue ───────────────────────────────────────────────────────────────────────────────────────────────

def queue(mem: Any, person: str | None, role: str | None, now: float | None = None) -> list[dict[str, Any]]:
    if not person or role == "household":
        raise Refused(403, "a shared screen has no memory of its own")
    now = time.time() if now is None else now
    _ensure(mem._db)
    facts = {f["id"]: f for f in mem.list_facts(limit=10**6, person_id=person)}
    out = []
    for r in mem._db.execute("SELECT * FROM memory_review WHERE person=? AND state='open' AND created_at >= ? "
                             "ORDER BY created_at", (person, now - EXPIRE_DAYS * 86400)):
        data = json.loads(r["data"] or "{}")
        item: dict[str, Any] = {"id": r["id"], "kind": r["kind"], "choices": list(CHOICES[r["kind"]]),
                                "expires_at": r["created_at"] + EXPIRE_DAYS * 86400}
        if r["kind"] == "fading":
            item.update({"count": data.get("count", 0), "oldest": data.get("oldest")})
        elif r["kind"] == "voice":
            ids = [i for i in data.get("ids") or [] if i in facts]
            if not ids:
                continue
            import agent_memory as am
            item.update({"count": len(ids), "examples": [{"id": i, "text": facts[i]["text"],
                                                          "rewritten": am.second_person(facts[i]["text"])}
                                                         for i in ids[:3]]})
        elif r["kind"] in ("wish", "other_adult", "one_off"):
            a = facts.get(r["a"])
            if not a:
                continue
            item["a"] = {k: a[k] for k in ("id", "text", "ts")}
        else:
            a, b = facts.get(r["a"]), facts.get(r["b"])
            if not a or not b:
                continue                       # one was forgotten or changed meanwhile: nothing left to look at
            item.update({"a": {k: a[k] for k in ("id", "text", "ts")}, "b": {k: b[k] for k in ("id", "text", "ts")}})
        out.append(item)
    return out


def _forget(mem: Any, person: str, ids: list[str]) -> str | None:
    """Forget these facts of theirs as one batch (restore_batch undoes it)."""
    token = "fb-" + uuid.uuid4().hex[:16]
    n = 0
    with mem._lock:
        for i in ids:
            row = mem._db.execute(f"SELECT {mem._KEEP} FROM facts WHERE id=? AND person_id=?", (i, person)).fetchone()
            if row:
                mem._move_to_forgotten(row, person, token)
                n += 1
        mem._db.commit()
        mem._bm25 = None
    return token if n else None


def resolve(mem: Any, person: str | None, role: str | None, rid: str, choice: str,
            now: float | None = None, *, writes: bool = True) -> dict[str, Any]:
    """`writes`: whether the person's memory may take something new (governance's "memory", #1690). False refuses the
    choices that write (a wish moved to dev notes, a rewrite); forgetting and keeping still resolve."""
    if not person or role == "household":
        raise Refused(403, "a shared screen has no memory of its own")
    now = time.time() if now is None else now
    _ensure(mem._db)
    r = mem._db.execute("SELECT * FROM memory_review WHERE id=? AND person=? AND state='open'", (rid, person)).fetchone()
    if not r:
        raise Refused(404, "no such item")
    if choice not in CHOICES[r["kind"]]:
        raise Refused(400, f"choice is one of: {', '.join(CHOICES[r['kind']])}")
    if not writes and (r["kind"], choice) in (("wish", "to_dev_note"), ("voice", "rewrite_all")):
        raise Refused(403, "memory is turned off for you: nothing new is stored (turn it back on in Settings)")
    forget: list[str] = []
    if r["kind"] == "disagree":
        forget = {"still_true": [r["b"]], "not_any_more": [r["a"]], "forget_both": [r["a"], r["b"]]}[choice]
    elif r["kind"] == "duplicate" and choice == "merge":
        facts = {f["id"]: f for f in mem.list_facts(limit=10**6, person_id=person)}
        a, b = facts.get(r["a"]), facts.get(r["b"])
        if a and b:
            forget = [a["id"] if (a["ts"] or 0) <= (b["ts"] or 0) else b["id"]]   # keep the newer wording
    elif r["kind"] == "fading" and choice == "forget_all":
        forget = list(json.loads(r["data"] or "{}").get("ids") or [])
    elif r["kind"] in ("other_adult", "one_off") and choice == "forget":
        forget = [r["a"]]
    elif r["kind"] == "wish" and choice == "to_dev_note":
        row = mem._db.execute("SELECT conv_id, text FROM facts WHERE id=? AND person_id=?", (r["a"], person)).fetchone()
        if row:
            mem.add_dev_note(row["conv_id"], row["text"], person_id=person)
            forget = [r["a"]]
    rewritten = 0
    if r["kind"] == "voice" and choice == "rewrite_all":
        import agent_memory as am
        for i in json.loads(r["data"] or "{}").get("ids") or []:
            row = mem._db.execute("SELECT text FROM facts WHERE id=? AND person_id=?", (i, person)).fetchone()
            if row and am.second_person(row["text"]) != row["text"]:
                rewritten += bool(mem.edit_fact(i, am.second_person(row["text"]), person_id=person))
    token = _forget(mem, person, forget) if forget else None
    mem._db.execute("UPDATE memory_review SET state='resolved', choice=?, resolved_at=? WHERE id=?", (choice, now, rid))
    mem._db.commit()
    return {"id": rid, "choice": choice, "forgotten": len(forget), "undo": token,
            **({"rewritten": rewritten} if r["kind"] == "voice" else {})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Memory's needs-a-look queue (#1074); counts only")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("nightly")
    n.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("cleanup", help="offer the one-time 10-10 cleanup (proposals only)")
    c.add_argument("--person")
    c.add_argument("--apply", action="store_true", help="put the proposals in the queue (default: count only)")
    a = ap.parse_args(argv)
    import agent_memory
    import household_identity as hh
    if a.cmd == "cleanup":
        print(json.dumps(cleanup(agent_memory.AgentMemory(), hh.get_store().people(), a.person, dry_run=not a.apply)))
        return 0
    print(json.dumps(nightly(agent_memory.AgentMemory(), hh.get_store().people(), dry_run=a.dry_run)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
