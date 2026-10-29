#!/usr/bin/env python3
"""The local model labels chat pairs and titles threads (titan-node#1016; Zach, 10-06: "switch to the local model
labelling", "title the thread as well"), in place of a 75-pair hand-labelling pass.

  labels    every adjacent pair of a person's conversations, and every pair the code linked, judged by titan's local
            model (Qwen3.6 on :8198; never a cloud model): same thread or not. Stored as statements with source "judge"
            (link_statements: ids and a yes/no, no text). The person's own labels (source "baseline") always win; the
            judge's count toward precision only once a spot-check agrees with it (AGREE_MIN of the person's labels).
  spotcheck the few pairs worth the person's look: where the judge and the code disagree, then any judged pair, at
            most SPOTCHECK; carried in GET /v1/conversations?view=threads for the Mac's labelling sheet.
  titles    a short title per thread with no earned name, from the person's own messages in it; kept in their own
            store (TITLES_DB, 0600, per person), never in links.db.

  python3 scripts/chat_judge.py [--person zach] [--days 30] [--apply]     counts only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable
from ops_ports import CODE_MODEL_URL  # noqa: E402

JUDGE_URL = os.environ.get("CHAT_JUDGE_URL", CODE_MODEL_URL)
TITLES_DB = Path(os.environ.get("CHAT_TITLES_DB", "/ai/datasets/connectors/chat_titles.db"))
BY = "judge:local"
SPOTCHECK = 10
AGREE_MIN = 0.9      # the judge's agreement with the person's own labels before its labels count
AGREE_N = 8          # …over at least this many of them
TITLE_WORDS = 6


def _local(url: str) -> None:
    if (urllib.parse.urlparse(url).hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("chat labels and titles are only ever asked of the local model")


def _excerpt(texts: list[str], n: int = 3, chars: int = 300) -> str:
    keep = texts[:n] + (texts[-1:] if len(texts) > n else [])
    return "\n".join(f"- {t[:chars]}" for t in keep)


def judge_pair(a: dict[str, Any], b: dict[str, Any], url: str = JUDGE_URL, post: Any = None) -> bool | None:
    """Same thread? a, b: {title, texts (the person's own messages), start, end}. Local only. → yes/no, or None."""
    _local(url)
    import typed_local
    gap = max(0, round((b["start"] - a["end"]) / 60))
    prompt = ("Two conversations a person had with their assistant. Are they the same thread: one ongoing matter, "
              "task or subject the person would want grouped together? Different subjects that happen to be close in "
              "time are not the same thread.\n\n"
              f"Conversation A:\n{_excerpt(a['texts'])}\n\nConversation B, {gap} min later:\n{_excerpt(b['texts'])}\n\n"
              'Answer {"same": "yes"} or {"same": "no"}.')
    try:
        got, _ = typed_local.ask(prompt, typed_local.obj({"same": typed_local.enum(("yes", "no"))}, ["same"]), url,
                                 max_tokens=20, timeout=60, retries=0, post=post)
    except Exception:  # noqa: BLE001 - no answer is no label
        return None
    return None if not got else got.get("same") == "yes"


def title_for(texts: list[str], url: str = JUDGE_URL, post: Any = None) -> str | None:
    """A short title (at most TITLE_WORDS words) for a thread, from the person's own messages in it. Local only."""
    _local(url)
    import typed_local
    prompt = ("These are a person's own messages from one thread of conversations with their assistant. Give the thread "
              f"a short title, at most {TITLE_WORDS} words, naming its subject the way the person would (\"Harbor Gas "
              "bill\", \"Ava's science project\"). No quotes, no trailing period.\n\n" + _excerpt(texts, 6, 200) +
              '\n\nAnswer {"title": "…"}.')

    def ok(o: dict[str, Any]) -> str | None:
        t = " ".join(str(o.get("title") or "").split())
        return None if t and len(t.split()) <= TITLE_WORDS and len(t) <= 60 else f"a title of 1 to {TITLE_WORDS} words"
    try:
        got, _ = typed_local.ask(prompt, typed_local.obj({"title": {"type": "string"}}, ["title"]), url, ensure=ok,
                                 max_tokens=40, timeout=60, retries=1, post=post)
    except Exception:  # noqa: BLE001
        return None
    return " ".join(str(got["title"]).split()).rstrip(".") if got else None


# ── titles store (per person; text, so never links.db) ───────────────────────────────────────────────────────────

def _titles_db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("CHAT_TITLES_DB") or TITLES_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE IF NOT EXISTS titles (person TEXT NOT NULL, thread TEXT NOT NULL, title TEXT NOT NULL, "
                "model TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (person, thread))")
    return con


def thread_key(item_ids: list[str]) -> str:
    """The same key chat_threads uses for a thread's id: its members, in any order."""
    return "ct-" + hashlib.sha1("\0".join(sorted(item_ids)).encode()).hexdigest()[:12]


def titles(person: str, path: Path | None = None) -> dict[str, str]:
    con = _titles_db(path)
    try:
        return {r[0]: r[1] for r in con.execute("SELECT thread, title FROM titles WHERE person=?", (str(person),))}
    finally:
        con.close()


def save_title(person: str, thread: str, title: str, path: Path | None = None) -> None:
    con = _titles_db(path)
    try:
        con.execute("INSERT OR REPLACE INTO titles VALUES (?,?,?,?,?)", (str(person), thread, title, BY, time.time()))
        con.commit()
    finally:
        con.close()


# ── the judge's labels, and how far to trust them ───────────────────────────────────────────────────────────────

def judged(con: sqlite3.Connection, ids: set[str]) -> dict[tuple[str, str], bool]:
    import linker
    con.executescript(linker.SCHEMA)
    if "source" not in {r[1] for r in con.execute("PRAGMA table_info(link_statements)")}:
        return {}
    out: dict[tuple[str, str], bool] = {}
    for a, b, rel in con.execute("SELECT a, b, related FROM link_statements WHERE source='judge' ORDER BY at"):
        if a in ids and b in ids:
            out[linker._pair(a, b)] = bool(rel)
    return out


def agreement(person_labels: dict[tuple[str, str], bool], judge_labels: dict[tuple[str, str], bool]) -> dict[str, Any]:
    both = [p for p in person_labels if p in judge_labels]
    agree = sum(person_labels[p] == judge_labels[p] for p in both)
    rate = round(agree / len(both), 3) if both else None
    return {"compared": len(both), "agree": agree, "rate": rate,
            "trusted": bool(len(both) >= AGREE_N and rate is not None and rate >= AGREE_MIN)}


def spotcheck(code_pairs: dict[tuple[str, str], str], judge_labels: dict[tuple[str, str], bool],
              person_labels: dict[tuple[str, str], bool], n: int = SPOTCHECK) -> list[dict[str, Any]]:
    """Pairs for the person's look: the judge and the code disagree first (the code linked it, the judge says no; or the
    judge says yes where the code didn't), then the rest of the judged pairs. Never one the person already labelled."""
    rows = []
    for p, j in judge_labels.items():
        if p in person_labels:
            continue
        linked = p in code_pairs
        rows.append((0 if linked != j else 1, p, j, code_pairs.get(p)))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [{"a": p[0], "b": p[1], "judge": "same" if j else "different", "code": t or "none"} for _, p, j, t in rows[:n]]


# ── the run ────────────────────────────────────────────────────────────────────────────────────────────────────

def run(person: str, days: float = 30, apply: bool = False, judge: Callable | None = None,
        titler: Callable | None = None, con: sqlite3.Connection | None = None, mem: sqlite3.Connection | None = None,
        now: float | None = None) -> dict[str, Any]:
    import chat_links
    import chat_threads
    import conversations as C
    import hooks
    import household_identity as hh
    import linker
    judge = judge or judge_pair
    titler = titler or title_for
    now = time.time() if now is None else now
    con = con or hooks.connect()
    mem = mem or sqlite3.connect(f"file:{chat_links.MEMORY_DB}?mode=ro", uri=True)
    conv_id = hh.conv_id_for(person)
    texts: dict[str, list[str]] = {}
    spans: dict[str, tuple[float, float]] = {}
    for gid, rs in C._groups(C._rows(mem, conv_id)):
        if rs[-1]["ts"] < now - days * 86400:
            continue
        iid = chat_links.item_id(gid)
        texts[iid] = [C._clean(r["content"]) for r in rs if r["role"] == "user" and C._clean(r["content"])]
        spans[iid] = (float(rs[0]["ts"]), float(rs[-1]["ts"]))
    order = sorted(spans, key=lambda i: spans[i][0])
    pairs = {linker._pair(a, b) for a, b in zip(order, order[1:])}
    view = chat_threads.build(person, "admin" if person == hh.ADMIN_PERSON_ID else "adult", mem, con=con, now=now,
                              limit=200, since="")
    code_pairs = {linker._pair(e["a"], e["b"]): e["type"] for t in view["threads"] for e in t["edges"]}
    code_pairs |= {linker._pair(f["edge"]["a"], f["edge"]["b"]): f["edge"]["type"] for t in view["threads"] for f in t["fringe"]}
    pairs |= {p for p in code_pairs if p[0] in spans and p[1] in spans}
    have = judged(con, set(spans))
    asked = {"yes": 0, "no": 0, "none": 0, "skipped (already judged)": 0}
    for a, b in sorted(pairs):
        if (a, b) in have:
            asked["skipped (already judged)"] += 1
            continue
        x, y = sorted((a, b), key=lambda i: spans[i][0])
        same = judge({"texts": texts.get(x, []), "start": spans[x][0], "end": spans[x][1]},
                     {"texts": texts.get(y, []), "start": spans[y][0], "end": spans[y][1]})
        asked["none" if same is None else ("yes" if same else "no")] += 1
        if apply and same is not None:
            linker.say(con, a, b, same, BY, "judge")
    mine = chat_threads.baseline(con, set(spans))
    judge_labels = judged(con, set(spans)) if apply else have
    agree = agreement(mine, judge_labels)
    titled = 0
    if apply:
        known = titles(person)
        for t in chat_threads.build(person, "admin" if person == hh.ADMIN_PERSON_ID else "adult", mem, con=con, now=now,
                                    limit=200, since="")["threads"]:
            if t.get("name") or t["id"] in known:
                continue
            got = titler([x for r in t["conversations"] for x in texts.get(r["item_id"], [])])
            if got:
                save_title(person, t["id"], got)
                titled += 1
    return {"person_conversations": len(spans), "pairs": len(pairs), "judged": asked, "agreement_with_person": agree,
            "spotcheck": len(spotcheck(code_pairs, judge_labels, mine)), "titled": titled, "applied": apply}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The local model labels chat pairs and titles threads (#1016); counts only")
    ap.add_argument("--person", default="zach")
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    print(json.dumps(run(a.person, a.days, a.apply), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
