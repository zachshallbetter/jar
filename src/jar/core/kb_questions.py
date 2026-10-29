#!/usr/bin/env python3
"""Likely questions for the system zone's notes, indexed as an extra chunk of each note (#371, adopted 10-06).

The A/B on the 95-case per-zone bank (kb_index_ab, V0Qsys): one chunk per system note holding its title and three
questions it answers takes hit@3 83→86, recall@5 86→88 and MRR 0.846→0.884, no zone or origin dropping. Appended to
the note's own text it measured flat (the runtime merges paragraphs), so it goes in as the retrieval runtime's
`extra_chunks` (AcmeProductCo/state#19): a hit on it stands in for the note's own first chunk and cites the note.

  refresh   every system-zone note (zones.zone_of → kb_bank.kind_of == "system"): its body text from the runtime
            (GET …/chunks, which never includes extras); its questions from titan's local model (:8010, never a
            hosted one), made only when the text's hash changed; then the same text re-sent with extra_chunks, so the
            runtime replaces only the extras (same checksum: no re-chunking, no new stamps for readers of the KB)
  cache     KB_QUESTIONS_CACHE (0600): {source_id: {hash, questions, sent_at}}

  python3 scripts/kb_questions.py refresh            dry run: counts only, no model calls
  python3 scripts/kb_questions.py refresh --apply    (titan-kb-questions.timer, 06:10Z, only with KB_QUESTIONS=apply)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

CACHE = Path(os.environ.get("KB_QUESTIONS_CACHE", "/ai/datasets/connectors/kb_questions.json"))
LOCAL_URL = os.environ.get("KB_DOC2QUERY_URL") or os.environ.get("AGENT_ENDPOINT") or "http://127.0.0.1:8010"


def _cache_path() -> Path:
    return Path(os.environ.get("KB_QUESTIONS_CACHE") or CACHE)


def _load() -> dict[str, Any]:
    p = _cache_path()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def _save(cache: dict[str, Any]) -> None:
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cache, f)
    tmp.replace(p)


def question_chunk(title: str, questions: list[str]) -> str:
    """The extra chunk, exactly as the A/B measured it: the note's title over its questions."""
    return (f"{title}\n" if title else "") + "\n".join(questions)


def local_questions(text: str, url: str = LOCAL_URL) -> list[str]:
    if (urllib.parse.urlparse(url).hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("questions are only ever written by titan's local model")
    import kb_index_ab
    return kb_index_ab.likely_questions(text, url)


def _body(call: Callable[..., Any], sid: str) -> tuple[dict[str, Any], str] | None:
    r = call("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}/chunks", "GET")
    chunks = sorted(r.get("chunks") or [], key=lambda c: c.get("chunk_index", 0))
    if not chunks:
        return None
    return dict(chunks[0].get("metadata") or {}), "\n\n".join(str(c.get("chunk_text") or "") for c in chunks)


def refresh(apply: bool = False, *, sources: list[dict[str, Any]] | None = None, call: Callable[..., Any] | None = None,
            ask: Callable[[str], list[str]] | None = None, now: float | None = None) -> dict[str, int]:
    """→ counts: system notes, unchanged, made, sent, failed (no text, no titles)."""
    import kb_bank
    import zones
    if call is None or sources is None:
        import agent_runtime_lib as lib
        call = call or lib._runtime_request
        sources = sources if sources is not None else lib.kb_sources()
    ask = ask or local_questions
    now = time.time() if now is None else now
    cache = _load()
    out = {"system": 0, "unchanged": 0, "made": 0, "sent": 0, "failed": 0, "would_make": 0}
    for s in sources:
        sid = str(s.get("source_id") or "")
        got = _body(call, sid) if sid else None
        if not got:
            continue
        md, text = got
        if kb_bank.kind_of(zones.zone_of(md, sid, s.get("source_type")), md, sid) != "system" or not text.strip():
            continue
        out["system"] += 1
        h = hashlib.sha256(text.encode()).hexdigest()
        c = cache.get(sid) or {}
        if c.get("hash") == h and c.get("sent_at"):
            out["unchanged"] += 1
            continue
        if not apply:
            out["would_make"] += 1
            continue
        try:
            qs = c.get("questions") if c.get("hash") == h and c.get("questions") else ask(text)
            if not qs:
                out["failed"] += 1                 # no questions this time: tried again on the next run
                continue
            if c.get("hash") != h:
                out["made"] += 1
            call("retrieval", "/v1/retrieval/index", "POST", {
                "source_id": sid, "parent_record_id": sid, "source_type": s.get("source_type") or "note",
                "title": s.get("title") or "", "text": text, "metadata": md,
                "source_updated_at": s.get("updated_at") or None,
                "extra_chunks": [question_chunk(str(s.get("title") or ""), qs)]})
            cache[sid] = {"hash": h, "questions": qs, "sent_at": now}
            out["sent"] += 1
        except Exception as exc:  # noqa: BLE001 - one note never stops the rest
            print(f"[kb_questions] one note skipped: {type(exc).__name__}", file=sys.stderr)
            out["failed"] += 1
    if apply:
        for sid in [k for k in cache if k not in {str(s.get("source_id")) for s in sources}]:
            cache.pop(sid)                         # a note that's gone takes its questions with it
        _save(cache)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Likely questions for system notes, as extra chunks (#371); counts only")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh")
    r.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if a.apply and os.environ.get("KB_QUESTIONS", "").strip().lower() != "apply":
        print(json.dumps({"skipped": "KB_QUESTIONS is not 'apply'"}))
        return 0
    print(json.dumps(refresh(a.apply)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
