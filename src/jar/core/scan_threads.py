#!/usr/bin/env python3
"""Threads for scans (titan-node#1504, epic #1498 slice 5): letters, claims and cases that share a reference form one row.

Built on the envelope (scan_envelope.py, #1500) and dedupe (scan_dedupe.py, #1501), and on the threading machinery mail
already uses (#951): hooks.extract reads the labelled codes (claim, case, reference, dispute ...), linker.score decides which
documents are related, and linker.threads_for makes the components. There is no store of its own: a thread is derived
from the documents each time, and a person's correction is a linker statement (`said`), the same as for mail.

What joins: two documents of ONE OWNER, both a letter or a claim letter, that share an identifier hook (a claim, case,
reference, request ... number; never an account, member or policy number, which name an account and not a matter) and that the
linker accepts. A dedupe cluster counts once (copies of one letter are one document); a thread needs two documents.

Privacy, absolute: a scan is its scanner's. Linking is done over the house's whole batch (the linker measures rarity against
it), but every edge across two owners is discarded before anything is read from it, and threads are then built one owner at a
time with that owner as the only viewer. So two people's claims against the same insurer, even under the same printed claim
number, are two threads. A thread's audience is its owner alone; it never widens one. Only the keyed hash of the shared code
is kept (the thread key), never the code, and nothing a scan says is acted on.

Per thread: id (stable for the same members), owner, audience, the scans (oldest first), count, first and latest date, the
sender, the thread key, and `waiting`: the thread has asked something of its owner (a request for papers, an appeal window)
and no letter in it closes the matter. An open case stays in "Waiting on you" until its owner marks it done (Done or Later on
the card), the same as any open card. The row is one row with its count and latest date, and it opens to its letters.

  python3 scripts/scans_score.py --pipeline scan_threads:pipeline
  python3 scripts/scans_score.py --pipeline scan_threads:pipeline --write scripts/evalsets/scans/threads.json
"""
from __future__ import annotations

import hashlib
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_dedupe as D  # noqa: E402
import scan_envelope as E  # noqa: E402

VERSION = 1
THREAD_TYPES = ("claim_letter", "letter")
_ASKS = re.compile(r"(?i)\b(please (?:send|provide|submit|return|reply|respond|call|sign)|you may appeal|you must (?:respond|reply|send)|"
                   r"respond (?:by|within)|(?:send|return) (?:us )?the)\b")
_CLOSES = re.compile(r"(?i)(claim (?:has been|is) (?:paid|closed|resolved|settled|approved)|case (?:is |has been )?closed|"
                     r"dispute (?:has been )?resolved|payment (?:has been )?(?:issued|sent)|no further action is (?:needed|required))")


def _letter_date(text: str, fallback: str) -> date:
    iso = E._date_after(text, r"\bdate\s*:") or str(fallback)[:10]
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return date.min


def _matter_keys(item: Any) -> set[str]:
    """The identifier keys that name a matter: every id: hook except those that name an account."""
    import linker
    return {k for kind, ks in item.keys.items() if kind.startswith("id:") and kind not in linker.ACCOUNT_IDS
            and kind not in ("id:message", "id:source") for k in ks}


def _is_waiting(texts: list[str]) -> bool:
    """texts oldest first. The thread asked its owner for something, and its latest letter does not close the matter."""
    return any(_ASKS.search(t) for t in texts) and not _CLOSES.search(texts[-1])


def threads(scans: list[dict], envs: dict[str, dict], dd: dict[str, Any],
            said: dict[tuple[str, str], bool] | None = None) -> dict[str, Any]:
    """The threads over the batch. Call inside E.bench_salt() (or with the house salt). `dd` is scan_dedupe.dedupe's result;
    `said` is {(scan a, scan b): related?}, a person's correction in the linker's own form (a kept scan's id is the
    document's). Returns {"threads": [...], "of": {scan id: thread id}}."""
    import hooks
    import linker
    by_id = {s["id"]: s for s in scans}
    owner = {sid: s["scanner"] for sid, s in by_id.items()}
    docs: dict[str, list[str]] = defaultdict(list)           # a document = a dedupe cluster, named by its kept copy
    for sid in sorted(by_id):
        docs[dd["cluster"][sid]].append(sid)
    items: dict[str, Any] = {}
    for kept, members in docs.items():
        if len({owner[m] for m in members}) != 1:
            raise AssertionError("a dedupe cluster holds two owners")
        it = linker.Item(kept, "scans", _letter_date(by_id[kept]["text"], by_id[kept]["scanned_at"]), owner[kept], owner[kept])
        for m in members:
            for h in hooks.extract({"text": by_id[m]["text"], "title": by_id[m].get("title") or "", "at": it.at.isoformat()}):
                it.keys[h.kind].add(h.key)
        items[kept] = it
    threadable = {k for k in items if envs[k]["fields"]["type"] in THREAD_TYPES}
    edges = []
    for e in linker.score(items, said):
        if e.a not in threadable or e.b not in threadable:
            continue
        if items[e.a].owner != items[e.b].owner:             # never across two people, whatever the linker thinks
            continue
        explicit = (said or {}).get(linker._pair(e.a, e.b)) is True
        if not explicit and not (_matter_keys(items[e.a]) & _matter_keys(items[e.b])):
            continue                                           # joined by a party or an account alone: not a matter
        edges.append(e)
    out: list[dict[str, Any]] = []
    of: dict[str, str] = {}
    for who in sorted({owner[k] for k in threadable}):
        mine = {k: items[k] for k in threadable if items[k].owner == who}
        for th in linker.threads_for(mine, edges, lambda it, who=who: it.owner == who and it.audience == who):
            keyed_docs = th.items                               # oldest first
            scan_ids = sorted((m for k in keyed_docs for m in docs[k]), key=lambda m: (_letter_date(by_id[m]["text"], by_id[m]["scanned_at"]), m))
            if len({owner[m] for m in scan_ids}) != 1 or owner[scan_ids[0]] != who:
                raise AssertionError("a thread holds two owners")
            shared = Counter(k for e in th.edges for k in _matter_keys(items[e.a]) & _matter_keys(items[e.b]))
            key = sorted(shared.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if shared else ""
            tid = "th-" + hashlib.sha1("\0".join(sorted(keyed_docs)).encode()).hexdigest()[:12]
            dates = [items[k].at for k in keyed_docs if items[k].at]
            texts = [by_id[k]["text"] for k in keyed_docs]
            senders = Counter(envs[k]["fields"]["sender"] for k in keyed_docs if envs[k]["fields"]["sender"])
            out.append({"id": tid, "owner": who, "audience": [who], "scans": scan_ids, "documents": list(keyed_docs),
                        "count": len(scan_ids), "first": dates[0].isoformat() if dates else None,
                        "latest": dates[-1].isoformat() if dates else None,
                        "sender": senders.most_common(1)[0][0] if senders else None, "key": f"thread:{key}",
                        "why": th.description, "waiting": _is_waiting(texts)})
            for m in scan_ids:
                of[m] = tid
    out.sort(key=lambda t: (t["owner"], t["latest"] or "", t["id"]))
    return {"threads": out, "of": of}


def pipeline(scans: list[dict]) -> dict:
    """The scorer's pipeline: dedupe's prediction, then threads. A scan in a thread takes the thread as its row, the thread's
    key as its association and the thread's waiting; a view row is the thread (one row per thread, with its count)."""
    return stage(scans, D.pipeline(scans))


def stage(scans: list[dict], pred: dict) -> dict:
    """Threads as a stage: takes a prediction that already has the dedupe (pred["dedupe"]); the thread rows replace the
    views, whatever earlier stages set on the documents that are not in a thread is kept."""
    envs = {sid: d["envelope"] for sid, d in pred["docs"].items()}
    with E.bench_salt():
        res = threads(scans, envs, pred["dedupe"])
    tb = {t["id"]: t for t in res["threads"]}
    for sid, tid in res["of"].items():
        t = tb[tid]
        d = pred["docs"][sid]
        d.update(row=tid, thread=tid, waiting=t["waiting"], assoc={"kind": "thread", "key": t["key"]})
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for sid, d in pred["docs"].items():
        groups[(d["owner"], d.get("row") or d["cluster"])].append(sid)
    pred["views"] = {}
    for (own, label), members in sorted(groups.items()):
        row: dict[str, Any] = {"members": sorted(members), "count": len(members)}
        if label in tb:
            row.update(thread=label, latest=tb[label]["latest"])
        pred["views"].setdefault(own, []).append(row)
    pred["threads"] = res
    prior = pred["canon"].get("assoc_key")
    mine = _canon_key()
    pred["canon"]["assoc_key"] = (lambda kind, key: mine(kind, key) if kind == "thread" else prior(kind, key)) if prior else mine
    return pred


def _canon_key() -> Callable[[str, str | None], str | None]:
    """Maps a true association key to the form this pipeline keeps it in: a thread is kept under the keyed hash of its
    claim number, never the number."""
    import hooks

    def canon(kind: str, key: str | None) -> str | None:
        if kind == "thread" and key:
            with E.bench_salt():
                return "thread:" + hooks.keyed("id", key.split(":", 1)[-1].upper().replace("-", ""))
        return key
    return canon
