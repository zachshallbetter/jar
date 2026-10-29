#!/usr/bin/env python3
"""The nightly self-exam (#373): titan is asked what the household told it (eval_bank.py's pairs) through the
real path, and graded in code.

Only the admin's own pairs, asked as the admin (the owner session, 10-02): someone else's pairs wait until an
eval turn can run as that person and its replies stay out of shared logs. The output and the log line carry the
pair's id, its kind and pass or fail, never the question, the answer or the reply. Results also go back into
the bank (asked / passed / last), the person's own store.

  python3 scripts/self_exam.py [--max 20] [--source nightly]      (on titan; the nightly runs it)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_bank  # noqa: E402

LOG = Path("/ai/logs/self-exam.jsonl")


def pair_id(ref: str) -> str:
    """A short id for a pair that says nothing about it (the ref names a bubble)."""
    return "p-" + hashlib.sha1(ref.encode()).hexdigest()[:10]


def _norm(t: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", str(t or "").lower()).split())


def passes(reply: str, answer: str) -> bool:
    """The stated answer is in the reply: as words, or, for a number (a phone, a code), as its digits."""
    a, r = _norm(answer), _norm(reply)
    if a and a in r:
        return True
    d = re.sub(r"\D", "", answer)
    return len(d) >= 3 and d in re.sub(r"\D", "", reply or "")


def exam(person: str, ask: Callable[[str], str], limit: int = 20, db: Path | None = None,
         now: float | None = None) -> list[dict[str, Any]]:
    """Ask up to `limit` of the person's pairs (the least recently asked first), grade each in code, and
    record it in the bank. → [{pair, kind, ok}] (no text)."""
    pairs = sorted(eval_bank.for_person(person, db), key=lambda p: (p.get("last_asked") or 0, p["ref"]))[:limit]
    out = []
    for p in pairs:
        try:
            reply = ask(p["question"])
            ok = passes(reply, p["answer"])
            err = None
        except Exception as exc:  # noqa: BLE001 - an error is a miss, said by kind only
            ok, err = False, type(exc).__name__
        eval_bank.record(p["ref"], ok, db, now)
        out.append({"pair": pair_id(p["ref"]), "kind": p["source"], "ok": ok, **({"error": err} if err else {})})
    return out


def turn_eval_ask(question: str) -> str:
    """One eval turn as the admin (turn_eval's token): nothing remembered, read tools only."""
    import turn_eval as te
    t = te.ask(question)
    if t.error:
        raise RuntimeError("turn error")
    return t.text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--max", type=int, default=20)
    ap.add_argument("--source", default="manual")
    a = ap.parse_args()
    import household_identity as hh
    person = hh.ADMIN_PERSON_ID
    res = exam(person, turn_eval_ask, a.max)
    passed = sum(1 for r in res if r["ok"])
    line = {"ts": time.time(), "source": a.source, "person": person, "asked": len(res), "passed": passed,
            "results": res}
    try:
        with LOG.open("a") as f:
            f.write(json.dumps(line) + "\n")
    except OSError:
        pass
    for r in res:
        print(f"  {r['pair']} {r['kind']}: {'ok' if r['ok'] else 'MISS'}" + (f" ({r['error']})" if r.get("error") else ""))
    print(f"self-exam: {passed}/{len(res)} of {person}'s own facts answered right")
    return 0


if __name__ == "__main__":
    sys.exit(main())
