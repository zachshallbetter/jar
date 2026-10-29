#!/usr/bin/env python3
"""How often an inline cite is right (jellybean#636): ask KB questions as eval turns with `eval_cites` (the switch can
stay off; nothing is remembered), and judge each cite titan would send: does its chunk state its sentence?

  check   in code: every number in the sentence is in the chunk, and at least 60% of the sentence's content words.
  judge   titan's own model (--judge-url, default the idle Qwen3.6 on :8198), yes or no, thinking off.
  agreed  the judge's yes; with --no-judge, the check's. Precision = agreed / emitted. The switch goes on only at
          >= 95% (Zach's bar on #636).

Runs on titan as the admin: questions from retrieval_eval's FOUND plus the runbook's (the KB holds it). Prints one line
per wrong cite and a summary, and appends a JSON line to /ai/logs/cite-precision.jsonl. Not in the deploy gate: it
asks the live agent.

  python3 scripts/cite_precision.py [--no-judge] [--questions file.txt] [--no-log]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ops_ports import AGENT_API_URL, CODE_MODEL_URL  # noqa: E402

API = os.environ.get("CITE_EVAL_API", AGENT_API_URL)
JUDGE_URL = os.environ.get("CITE_JUDGE_URL", CODE_MODEL_URL)
LOG = Path(os.environ.get("CITE_PRECISION_LOG", "/ai/logs/cite-precision.jsonl"))
RUNBOOK = [
    "How do I run the deploy gate before a titan change?",
    "Where does titan keep its scratch files?",
    "How are the runtimes started after a reboot?",
    "How do the runtimes authenticate callers?",
    "What is the deploy hold and how do I set one?",
    "Which GPU does the local model use, and what runs on :8198?",
    "How do phones get push notifications from titan?",
    "What happens to a household device's data on titan?",
    "How does the nightly household eval alert?",
    "Where are titan's backups kept and for how long?",
]
JUDGE = """/no_think
Passage:
{chunk}

Sentence: {sentence}

Does the passage state what the sentence says (the same facts; numbers and names must match)? Answer with JSON
{{"yes": true}} or {{"yes": false}} only."""


def _token() -> str:
    return Path(__import__("agent_paths").api_token_file()).read_text().strip()


def ask(question: str, timeout: int = 150) -> dict[str, Any]:
    """One eval turn; the stream's done event (with cite_evidence)."""
    body = {"prompt": question, "source": "eval", "remember": False, "eval_cites": True}
    req = urllib.request.Request(f"{API}/v1/prompt/stream", data=json.dumps(body).encode(),
                                 headers={"X-Agent-Token": _token(), "Content-Type": "application/json"})
    event = None
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event in ("done", "error"):
                d = json.loads(line[6:])
                if event == "error":
                    raise RuntimeError(str(d)[:200])
                return d
    return {}


def check(sentence: str, chunk: str) -> bool:
    """In code: the emitter's own rule (agent_runtime_lib.cite_check, titan-node#910), so the two can't disagree."""
    import agent_runtime_lib as lib
    return lib.cite_check(sentence, chunk)


def surface(c: dict[str, Any]) -> str:
    """Where a cite points: titan's runbook (and other system notes) or the household's KB."""
    import zones
    sid = str(c.get("source_id") or "")
    return "runbook" if sid and zones.zone_of({}, sid) == "system" or sid.startswith("note-runbook") else "kb"


def judge(sentence: str, chunk: str) -> bool:
    body = json.dumps({"messages": [{"role": "user", "content": JUDGE.format(chunk=chunk[:1500], sentence=sentence)}],
                       "max_tokens": 20, "temperature": 0, "response_format": {"type": "json_object"},
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(JUDGE_URL + "/v1/chat/completions", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())["choices"][0]["message"].get("content") or "{}"
    return bool(json.loads(out).get("yes"))


def lower_bound(agreed: int, n: int) -> float | None:
    """A 95% lower bound on precision: the rule of three when every cite is right (1 - 3/n), else Wilson's."""
    if not n:
        return None
    if agreed == n:
        return round(max(0.0, 1 - 3 / n), 3)
    import math
    z, p = 1.96, agreed / n
    return round((p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n), 3)


def recall(answers: list[tuple[str, int]]) -> dict[str, Any]:
    """Cites per answer that had evidence, by surface: [(surface of its first source, cites)] → per surface the answers,
    how many got any cite, and cites per answer."""
    out: dict[str, Any] = {}
    for name in ("kb", "runbook"):
        part = [n for s, n in answers if s == name]
        if part:
            out[name] = {"answers": len(part), "cited": sum(1 for n in part if n), "per_answer": round(sum(part) / len(part), 2)}
    return out


def score(cites: list[dict[str, Any]], decide: Callable[[str, str], bool]) -> dict[str, Any]:
    """Counts and the wrong ones: cites are cite_evidence items ({n, after, sentence, chunk})."""
    agreed, wrong = 0, []
    for c in cites:
        if decide(c["sentence"], c["chunk"]):
            agreed += 1
        else:
            wrong.append({"after": c["after"], "sentence": c["sentence"][:160]})
    n = len(cites)
    return {"emitted": n, "agreed": agreed, "precision": round(agreed / n, 3) if n else None, "wrong": wrong}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-judge", action="store_true", help="the check in code decides")
    ap.add_argument("--questions", help="one question a line (default: retrieval_eval's FOUND and the runbook's)")
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--out", help="also write this pass's record (JSON) here")
    ap.add_argument("--combine", nargs="+", metavar="RECORD", help="sum several passes' records and print the total")
    a = ap.parse_args()
    if a.combine:
        recs = [json.loads(Path(p).read_text()) for p in a.combine]
        n, agreed = sum(r["emitted"] for r in recs), sum(r["agreed"] for r in recs)
        surf: dict[str, list[int]] = {}
        for r in recs:
            for k, v in (r.get("by_surface") or {}).items():
                surf.setdefault(k, [0, 0])
                surf[k][0] += v["agreed"]
                surf[k][1] += v["emitted"]
        rc: dict[str, list[int]] = {}
        for r in recs:
            for k, v in (r.get("recall") or {}).items():
                rc.setdefault(k, [0, 0, 0])
                rc[k][0] += v["answers"]
                rc[k][1] += v["cited"]
                rc[k][2] += round(v["per_answer"] * v["answers"])
        print(json.dumps({"passes": len(recs), "emitted": n, "agreed": agreed,
                          "precision": round(agreed / n, 3) if n else None, "lower_bound": lower_bound(agreed, n),
                          "by_surface": {k: f"{a_}/{b}" for k, (a_, b) in surf.items()},
                          "recall": {k: {"answers": x, "cited": y, "per_answer": round(z / x, 2) if x else None}
                                     for k, (x, y, z) in rc.items()}}, indent=1))
        return 0
    if a.questions:
        qs = [q.strip() for q in Path(a.questions).read_text().splitlines() if q.strip()]
    else:
        import retrieval_eval
        qs = [q for _, q, _ in retrieval_eval.FOUND] + RUNBOOK
    cites: list[dict[str, Any]] = []
    answers: list[tuple[str, int]] = []
    asked = failed = with_sources = 0
    for q in qs:
        asked += 1
        try:
            d = ask(q)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  turn failed: {q[:60]} ({type(exc).__name__})")
            continue
        with_sources += bool(d.get("sources"))
        mine = [c for c in d.get("cite_evidence") or [] if c.get("sentence") and c.get("chunk")]
        cites += mine
        srcs = [str(x.get("source_id") or "") for x in d.get("sources") or []]
        if srcs:
            answers.append((surface({"source_id": srcs[0]}), len(mine)))
    decide = check if a.no_judge else judge
    out = score(cites, decide)
    code = score(cites, check)
    by_surface = {}
    for name in ("kb", "runbook"):
        part = [c for c in cites if surface(c) == name]
        if part:
            r = score(part, decide)
            by_surface[name] = {"emitted": r["emitted"], "agreed": r["agreed"], "precision": r["precision"]}
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "questions": asked, "failed": failed, "with_sources": with_sources,
           "emitted": out["emitted"], "agreed": out["agreed"], "precision": out["precision"],
           "judge": "check" if a.no_judge else "model", "check_precision": code["precision"], "by_surface": by_surface,
           "lower_bound": lower_bound(out["agreed"], out["emitted"]), "recall": recall(answers)}
    for w in out["wrong"]:
        print(f"  wrong: …{w['after']!r}: {w['sentence']}")
    print(f"cite precision: {out['agreed']}/{out['emitted']} = {out['precision']} ({rec['judge']}; check in code: "
          f"{code['precision']}) over {asked} questions, {with_sources} with sources, {failed} failed")
    for name, r in by_surface.items():
        print(f"  {name}: {r['agreed']}/{r['emitted']} = {r['precision']}")
    print(f"  lower bound (95%): {rec['lower_bound']}")
    for name, r in rec["recall"].items():
        print(f"  recall {name}: {r['cited']}/{r['answers']} answers with evidence got a cite, {r['per_answer']} cites per answer")
    if a.out:
        Path(a.out).write_text(json.dumps(rec))
    if not a.no_log:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
