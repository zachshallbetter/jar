#!/usr/bin/env python3
"""The KB eval bank from real questions (#862; Zach's KB-improvement list, 10-04). retrieval_eval's fixed cases sit at
their ceiling, so this bank grows from the house's own KB and turns, per zone (zones.py), scored nightly:

  cited       a real question (the message store) joined to what titan cited for it (retrieval_trace's qhash):
              the cited items are its answer key
  generated   per zone, from the KB's own items: a question in the shapes real turns take ("When is …?", "Any mail
              about …?", "What was that article about …?"), its answer key the item itself, written in code
  miss        a turn-review or zone-replay miss, added by hand (`add`), kept until removed
  identifier  #1123: "Anything about <id>?" for an order or case number, a file name or an error code that appears in
              exactly one KB item, its answer key that item (exact matches: where hybrid's lexical part should help)

The bank holds the house's real titles, so it lives on titan only ($KB_BANK, 0600), never in git; the repo's tests use
made-up items. A case whose expected items all left the KB (expired, forgotten) is retired at the next harvest.

Scored as each case's owner sees it (their role, the live KB_ZONES): per zone recall@3 (an expected item in the top 3
of a search) and the wrong-zone rate (zones.plan doesn't include the zone the answer lives in). The first score is the
baseline ($KB_BANK.baseline.json); a zone below its baseline is a scorecard miss, and so is every new miss.

  python3 scripts/kb_bank.py harvest | score | status
  python3 scripts/kb_bank.py add "<question>" <source_id> [<source_id>…] [--note …]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zones  # noqa: E402

BANK = Path(os.environ.get("KB_BANK", "/ai/datasets/evals/kb-bank.jsonl"))
PER_ZONE = 15
K = 3
_STOP = frozenset("a an and the of for to in on at by with from your you my our is are was be it this that re fw fwd "
                  "titan runbook new update updated has have been".split())
KINDS = ("calendar", "mail", "facts", "web", "system")
_TEMPLATES = {
    "calendar": ("When is {k}?", "What time is {k}?", "Is {k} coming up?"),
    "mail": ("What did the email about {k} say?", "Any mail about {k}?", "Did I get anything about {k}?"),
    "facts": ("What do we have saved about {k}?", "What's the {k}?", "Remind me about {k}."),
    "web": ("What was that article about {k}?", "Find the page I saved about {k}."),
}
# No template for the system zone (#1225, 10-08): "How does titan handle {k}?" over a runbook title fragment made
# questions like "How does titan handle cards facts bill?" that no note answers (11 of 15 generated system cases). A
# system note's question is written from its own text by titan's local model instead (passage_question), or it gets
# no generated case.


# A real question that names nothing ("And the chat?") is a follow-up: its answer depends on the conversation, so a
# search for it alone finds its item by luck (#390, 10-05: one such case made batch 3 of the migration look like a drop).
_QSTOP = _STOP | frozenset("what when where who how why did does do any about and but the was were which there "
                           "can could would should tell me please also".split())


def meaningful(q: str) -> bool:
    """Whether a question names at least two things of its own (words beyond question and stop words)."""
    return len([w for w in re.findall(r"[a-z0-9][a-z0-9'&-]*", str(q or "").lower())
                if w not in _QSTOP and len(w) > 2]) >= 2


def _norm_title(t: Any) -> str:
    """A title's family: case and spacing folded, and a part number dropped ("… GPU layout (3)" = "… GPU layout")."""
    return re.sub(r"\s*\(\d+\)$", "", " ".join(str(t or "").split()).lower())


def _h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


def salient(title: str, n: int = 3) -> str:
    """A few of a title's own words: no dates, numbers, stop words or site suffixes ("… | Percepta"). A runbook part
    uses its deepest sub-heading ("Titan runbook — Reliability — GPU layout (3)" → "GPU layout"): the bare section
    name ("Reliability") is shared by a dozen parts since #914 split them (10-05: 6 of 9 system misses)."""
    t = re.split(r"\s+[|—–]\s+", re.sub(r"\s*\(\d+\)\s*$", "", str(title or "")))
    t = t[-1] if len(t) > 1 and t[0].lower().startswith("titan runbook") else t[0]
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'&-]+", t) if w.lower() not in _STOP and len(w) > 2]
    return " ".join(words[:n])


def kind_of(zone: str, md: dict[str, Any], sid: str) -> str:
    if zone != "sync":
        return zone
    src = str(md.get("source") or (sid.split("-")[1] if sid.startswith("cx-") else "")).lower()
    return "calendar" if src == "calendar" else "mail"


QUESTION_CHARS = 3000   # what the model reads of a system note to write its question


def _questions_path() -> Path:
    return Path(str(Path(os.environ.get("KB_BANK", str(BANK)))) + ".questions.json")


def passage_question(text: str, url: str | None = None) -> str | None:
    """One question the person running titan might ask that this text fully answers, by titan's local model only."""
    import urllib.parse
    import urllib.request
    url = url or os.environ.get("KB_DOC2QUERY_URL") or os.environ.get("AGENT_ENDPOINT") or "http://127.0.0.1:8010"
    if (urllib.parse.urlparse(url).hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("questions are only ever written by titan's local model")
    prompt = ("Write one short, specific question the person who runs this home server might ask that this text fully "
              "answers. Only the question, on one line.\n\n" + str(text or "")[:QUESTION_CHARS] + "\n/no_think")
    body = json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": 60, "temperature": 0}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=body,
                                                                headers={"Content-Type": "application/json"}), timeout=120))
    out = re.sub(r"(?s)<think>.*?</think>", "", r["choices"][0]["message"]["content"])
    return next((x.strip(" -•*\t\"") for x in out.splitlines() if x.strip().endswith("?")), None)


def _real_question(q: Any) -> bool:
    q = str(q or "").strip()
    return q.endswith("?") and 3 <= len(q.split()) <= 30 and "{" not in q


def generate(sources: list[dict[str, Any]], meta: Callable[[str], dict[str, Any]], *,
             text_of: Callable[[str], str | None] | None = None,
             ask_q: Callable[[str], str | None] | None = None) -> list[dict[str, Any]]:
    """Up to PER_ZONE cases per kind (calendar, mail, facts, web, system), each from one KB item. A title several items
    share ("Security alert": four mails, 10-05) expects any of them: the question can't tell them apart (#390); so do
    the parts of one runbook section ("GPU layout", "(2)", "(3)", "(4)"). A system note's question comes from its own
    text (ask_q over text_of, cached per note and text in $KB_BANK.questions.json), never from its title: without
    either, or no real question back, it gets no case."""
    qcache: dict[str, Any] | None = None
    by: dict[str, list[dict[str, Any]]] = {}
    same: dict[str, list[str]] = {}
    for s in sources:
        same.setdefault(_norm_title(s.get("title")), []).append(str(s["source_id"]))
    for s in sorted(sources, key=lambda s: _h(str(s["source_id"]))):
        sid = str(s["source_id"])
        md = meta(sid)
        z = zones.zone_of(md, sid, s.get("source_type"))
        kind = kind_of(z, md, sid)
        if len(by.get(kind, [])) >= PER_ZONE:
            continue
        if kind == "system":
            if text_of is None or ask_q is None:
                continue
            text = text_of(sid) or ""
            if not text.strip():
                continue
            if qcache is None:
                try:
                    qcache = json.loads(_questions_path().read_text())
                except (OSError, ValueError):
                    qcache = {}
            th = hashlib.sha256(text.encode()).hexdigest()
            hit = qcache.get(sid) or {}
            q = hit.get("q") if hit.get("hash") == th else None
            if q is None:
                try:
                    q = ask_q(text)
                except Exception:  # noqa: BLE001 - no model: no case this time, asked again next harvest
                    q = None
                if _real_question(q):
                    qcache[sid] = {"hash": th, "q": q}
            if not _real_question(q):
                continue
            case_id = f"gen-{_h(sid + chr(10) + str(q)):08x}"    # a new question is a new case
        else:
            k = salient(s.get("title") or "")
            if len(k.split()) < 1:
                continue
            q = _TEMPLATES[kind][_h(sid) % len(_TEMPLATES[kind])].format(k=k)
            case_id = f"gen-{_h(sid):08x}"
        by.setdefault(kind, []).append({"id": case_id, "q": q,
                                        "expect": sorted(same.get(_norm_title(s.get("title")), [sid])), "zone": z,
                                        "kind": kind, "origin": "generated", "owner": md.get("owner")})
    if qcache is not None:
        live = {str(s["source_id"]) for s in sources}
        qcache = {k: v for k, v in qcache.items() if k in live}
        p = _questions_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(qcache, f)
    return [c for v in by.values() for c in v]


def cited(trace_db: Path, memory_db: Path, meta: Callable[[str], dict[str, Any]], days: float = 30) -> list[dict[str, Any]]:
    """Real questions joined to what titan cited for them."""
    out: list[dict[str, Any]] = []
    try:
        t = sqlite3.connect(f"file:{trace_db}?mode=ro", uri=True)
        m = sqlite3.connect(f"file:{memory_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        qs = {hashlib.sha256(c.strip().lower().encode()).hexdigest()[:16]: c
              for (c,) in m.execute("SELECT content FROM messages WHERE role='user'")}
        rows = t.execute("SELECT id, qhash, person, path, turn_id FROM queries WHERE at > ?",
                         (time.time() - days * 86400,)).fetchall()
        # the model's own search (kb_retrieve) hashes its rewritten query, never the person's words: it takes the
        # question its turn's passive search hashed (10-05: 5 of 15 cited searches had been lost this way)
        asked = {turn: qh for _qid, qh, _p, path, turn in rows if path == "passive" and qh in qs and turn}
        for qid, qh, person, path, turn in rows:
            if qh not in qs:
                qh = asked.get(turn) if turn else None
                if qh is None:
                    continue
            ids = sorted({r[0] for r in t.execute("SELECT source_id FROM hits WHERE query=? AND cited=1", (qid,))})
            if not ids or not meaningful(qs[qh]):
                continue
            md = meta(ids[0])
            z = zones.zone_of(md, ids[0])
            prev = next((c for c in out if c["id"] == f"cit-{qh}"), None)
            if prev is not None:      # the passive search and the model's own in one turn: one case, both citations
                prev["expect"] = sorted(set(prev["expect"]) | set(ids))
                continue
            out.append({"id": f"cit-{qh}", "q": " ".join(qs[qh].split())[:300], "expect": ids, "zone": z,
                        "kind": kind_of(z, md, ids[0]), "origin": "cited", "owner": person})
    except sqlite3.Error:
        pass
    finally:
        t.close()
        m.close()
    return out


def load(path: Path | None = None) -> list[dict[str, Any]]:
    p = Path(path or BANK)
    try:
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    except OSError:
        return []


def save(cases: list[dict[str, Any]], path: Path | None = None) -> None:
    p = Path(path or BANK)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for c in cases:
            f.write(json.dumps(c) + "\n")
    os.chmod(p, 0o600)


# Real-shaped runbook questions with known answers (#371/#374, 10-05): the bank had 5 real cases, too few to judge an
# indexing or embeddings change. Each names the runbook section family that answers it (checked against the live
# runbook's text, 27 of 28 by key phrase). No household names. Resolved by title at each harvest.
CURATED = [("How do I deploy titan-node?", "Deploying titan-node itself"),
           ("What do I verify before touching titan?", "First thing, every time"),
           ("Which GPU does the local model use?", "GPU layout"),
           ("What runs on port 8198?", "GPU layout"),
           ("Why is the NVIDIA driver held back?", "NVIDIA driver is pinned"),
           ("How do I pair a new phone with titan?", "Household identity and pairing — Pairing"),
           ("How are household roles enforced?", "Roles are enforced on titan"),
           ("How does titan wake the Mac for a phone request?", "Cross-device (Mac requests from the phone) — Waking"),
           ("How do I check a Mac request works live?", "Cross-device (Mac requests from the phone) — Checking it live"),
           ("What happens in the nightly reflection?", "Nightly reflection"),
           ("How do scheduled actions work?", "Scheduled actions"),
           ("How do push notifications reach the iPhone?", "Push to the iPhone"),
           ("How do I run an A/B in a sandbox box?", "Sandbox boxes"),
           ("Where do titan's backups go?", "Backups"),
           ("What does titan's firewall allow?", "Reliability — Firewall"),
           ("How are titan's disks laid out?", "Reliability — Disks"),
           ("Where does a turn's time go?", "Latency: where a turn's time goes"),
           ("How do place routines trigger?", "Place routines"),
           ("Where are todos kept?", "Todos (todos.py)"),
           ("How do the runtimes check who is calling?", "Runtimes are coordinator-"),
           ("What datastores does titan use?", "Datastores / models"),
           ("How do I upgrade titan's OS safely?", "OS upgrade"),
           ("What wins when a person corrects a fact?", "Principles — Corrections win"),
           ("Where do titan's alerts go?", "Reliability — Alerts"),
           ("How is Home Assistant kept running on titan?", "Reliability — Home Assistant"),
           ("What does the autonomous coder work on?", "Reliability — Autonomous coder"),
           ("Which commands do I use to operate titan?", "Canonical commands"),
           ("How do cards reach people and devices?", "Cards to people and devices")]


# #1123: identifier-shaped questions, where exact matching (the hybrid's lexical part) should matter most
_IDENT = re.compile(r"(?<![\w.-])(?:"
                    r"[A-Z]{1,5}[-#]?\d{3,}(?:-\d+)*"                               # order, case, ticket: HM-2048-7731
                    r"|[\w-]+\.(?:pdf|docx?|xlsx?|csv|png|jpe?g|txt|md|zip|json|ya?ml|py|log)"   # an exact file name
                    r"|(?:E|ERR|0x)[0-9A-Fa-f]{3,}"                                  # an error code
                    r")(?![\w-])")
PER_IDENT = 20


def identifiers(sources: list[dict[str, Any]], meta: Callable[[str], dict[str, Any]],
                text_of: Callable[[str], str | None]) -> list[dict[str, Any]]:
    """Up to PER_IDENT cases asking about one identifier (an order or case number, a file name, an error code) that
    appears in exactly one KB item: "Anything about HM-2048-7731?". The answer key is that item."""
    where: dict[str, set[str]] = {}
    for s in sources:
        sid = str(s["source_id"])
        for m in set(_IDENT.findall((text_of(sid) or "") + " " + str(s.get("title") or ""))):
            if not m.isdigit() and len(m) >= 5:
                where.setdefault(m, set()).add(sid)
    out = []
    for ident in sorted((i for i, ids in where.items() if len(ids) == 1), key=_h):
        if len(out) >= PER_IDENT:
            break
        sid = next(iter(where[ident]))
        md = meta(sid)
        z = zones.zone_of(md, sid)
        out.append({"id": f"idn-{_h(ident):08x}", "q": f"Anything about {ident}?", "expect": [sid], "zone": z,
                    "kind": kind_of(z, md, sid), "origin": "identifier", "owner": md.get("owner")})
    return out


def curated(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The curated runbook cases, each expecting every runbook part whose title holds its section (a family)."""
    book = [s for s in sources if str(s.get("title") or "").lower().startswith("titan runbook")]
    out = []
    for q, section in CURATED:
        ids = sorted(str(s["source_id"]) for s in book if section.lower() in str(s.get("title") or "").lower())
        if ids:
            out.append({"id": f"cur-{_h(q):08x}", "q": q, "expect": ids, "zone": "system", "kind": "system",
                        "origin": "curated", "owner": None})
    return out


def harvest(sources: list[dict[str, Any]], meta: Callable[[str], dict[str, Any]], *, cited_cases: list[dict[str, Any]],
            path: Path | None = None, text_of: Callable[[str], str | None] | None = None) -> dict[str, int]:
    """Refresh the bank: generated cases rebuilt from today's KB, cited ones added, misses kept; a case whose
    expected items all left the KB retired."""
    live = {str(s["source_id"]) for s in sources}
    old = load(path)
    keep = [c for c in old if c.get("origin") == "miss" or (c.get("origin") == "cited" and meaningful(c.get("q")))]
    have = {c["id"] for c in keep}
    keep += [c for c in cited_cases if c["id"] not in have]
    keep += generate(sources, meta, text_of=text_of, ask_q=passage_question if text_of is not None else None)
    keep += curated(sources)
    if text_of is not None:
        keep += identifiers(sources, meta, text_of)
    kept = [c for c in keep if any(e in live for e in c.get("expect") or [])]
    save(kept, path)
    return {"cases": len(kept), "retired": len(keep) - len(kept),
            **{f"origin {o}": sum(c["origin"] == o for c in kept) for o in ("generated", "cited", "miss", "curated",
                                                                         "identifier")}}


def add(q: str, expect: list[str], meta: Callable[[str], dict[str, Any]], note: str = "", path: Path | None = None) -> dict:
    md = meta(expect[0])
    z = zones.zone_of(md, expect[0])
    c = {"id": f"miss-{_h(q):08x}", "q": q, "expect": expect, "zone": z, "kind": kind_of(z, md, expect[0]),
         "origin": "miss", "owner": md.get("owner"), "note": note}
    cases = [x for x in load(path) if x["id"] != c["id"]] + [c]
    save(cases, path)
    return c


def score(cases: list[dict[str, Any]], query: Callable[[str, dict[str, Any]], list[dict[str, Any]]],
          role_of: Callable[[str | None], str | None],
          about_titan: Callable[[str], bool] | None = None) -> dict[str, Any]:
    """Per kind: cases, found@K, wrong zone; and the misses (case ids only: never the question in a log). Wrong zone
    is judged on the plan a search makes (search=True, the runtime's about_titan), the one query() runs: judged on the
    per-turn plan, 32 of 34 wrong-zone cases (10-07) were system questions the search plan's runbook slot reaches."""
    by: dict[str, dict[str, int]] = {}
    misses = []
    for c in cases:
        b = by.setdefault(c["kind"], {"n": 0, "found": 0, "wrong_zone": 0})
        b["n"] += 1
        role = role_of(c.get("owner"))
        steps = zones.plan(c["q"], role, c.get("owner") or "p", search=True,
                           about_titan=about_titan(c["q"]) if about_titan else None)
        if c["zone"] not in {z for z, _ in steps}:
            b["wrong_zone"] += 1
        got = [str(h.get("source_id")) for h in query(c["q"], c)][:K]
        if set(got) & set(c["expect"]):
            b["found"] += 1
        else:
            misses.append(c["id"])
    return {"by_kind": by, "cases": len(cases), "found": sum(b["found"] for b in by.values()),
            "wrong_zone": sum(b["wrong_zone"] for b in by.values()), "misses": misses}


def against_baseline(r: dict[str, Any], path: Path | None = None) -> list[str]:
    """Kinds whose recall fell below the first score (which this records when there is none)."""
    p = Path(str(path or BANK) + ".baseline.json")
    rates = {k: v["found"] / v["n"] for k, v in r["by_kind"].items() if v["n"]}
    try:
        base = json.loads(p.read_text())
    except (OSError, ValueError):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"ts": time.time(), "rates": rates}))
        return []
    return [f"{k} {rates[k]:.0%} < baseline {b:.0%}" for k, b in base.get("rates", {}).items()
            if k in rates and rates[k] + 1e-9 < b]


# ── titan's own wiring ───────────────────────────────────────────────────────────────────────

def _live() -> tuple[Any, Callable[[str], dict[str, Any]], list[dict[str, Any]]]:
    import agent_runtime_lib as lib
    import kb_privacy as P
    return lib, (lambda sid: P.source_meta(lib._runtime_request, sid)), lib.kb_sources()


def _text_of() -> Callable[[str], str | None]:
    import agent_runtime_lib as lib
    import kb_privacy as P
    return lambda sid: P.source_text(lib._runtime_request, sid)


def nightly(stale_h: float = 20) -> dict[str, Any] | None:
    """Harvest when the bank is older than stale_h (or missing), then score. None when titan has no KB."""
    lib, meta, sources = _live()
    if not sources:
        return None
    if not BANK.exists() or time.time() - BANK.stat().st_mtime > stale_h * 3600:
        harvest(sources, meta, cited_cases=cited(Path(os.environ.get("RETRIEVAL_TRACE_DB", "/ai/logs/retrieval-trace.db")),
                                                 __import__("agent_paths").memory_dir()
                                                 / "titan_memory.db", meta), text_of=_text_of())
    import household_identity as hh
    try:
        roles = {str(p["id"]): str(p.get("role") or "") for p in hh.get_store().people()}
    except Exception:  # noqa: BLE001
        roles = {}

    def role_of(owner):
        return roles.get(str(owner or "")) or ("admin" if owner in (None, hh.ADMIN_PERSON_ID) else "adult")

    def query(q, case):
        lib._TURN_IO.person = {"id": case.get("owner") or hh.ADMIN_PERSON_ID, "name": "eval"}
        lib._TURN_IO.role = role_of(case.get("owner"))
        lib._TURN_IO.source = "eval"
        try:
            return lib._kb_query(q, K)
        finally:
            lib._TURN_IO.person = lib._TURN_IO.role = None
    r = score(load(), query, role_of, lib._about_titan)
    r["below_baseline"] = against_baseline(r)
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The KB eval bank from real questions (#862)")
    ap.add_argument("cmd", choices=["harvest", "score", "status", "add"])
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        cs = load()
        print(json.dumps({"cases": len(cs), "by_kind": {k: sum(c["kind"] == k for c in cs) for k in KINDS},
                          "by_origin": {o: sum(c["origin"] == o for c in cs) for o in ("generated", "cited", "miss", "curated")}}))
        return 0
    lib, meta, sources = _live()
    if a.cmd == "add":
        if len(a.rest) < 2:
            ap.error('add "<question>" <source_id>…')
        print(json.dumps(add(a.rest[0], a.rest[1:], meta, a.note)))
        return 0
    if a.cmd == "harvest":
        print(json.dumps(harvest(sources, meta, cited_cases=cited(
            Path(os.environ.get("RETRIEVAL_TRACE_DB", "/ai/logs/retrieval-trace.db")),
            __import__("agent_paths").memory_dir() / "titan_memory.db",
            meta), text_of=_text_of())))
        return 0
    print(json.dumps(nightly(stale_h=1e9), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
