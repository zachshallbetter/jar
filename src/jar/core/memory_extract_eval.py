#!/usr/bin/env python3
"""The memory extractor on a fictional bank (#1090): does it keep new facts, or echo the known ones?

Each window in scripts/evalsets/memory_extract.jsonl is a few USER lines (and the assistant's question, if any) over
the same block of 24 known facts. The window runs through agent_memory's real _extract_facts_l1 (its filters, REPLACES
rules and near-duplicate check) on a throwaway memory DB; only the model call is pointed at --url and, per variant,
the prompt's contract is changed:
  v0    the prompt as it is
  none  v0 + "never repeat or restate a known fact; nothing new → exactly NONE"
  json  only new facts, as JSON {"new": [...], "replaces": [{"n": <number>, "fact": ...}]}, turned back into lines
Scored per window: expected new facts stored, corrections made (the right known fact forgotten), echoes in the raw
output (a line that is a known fact again), false facts stored (matching nothing expected), false REPLACES (a known
fact forgotten that the window never corrected), and NONE answered on windows with nothing new.

  python3 scripts/memory_extract_eval.py --url http://127.0.0.1:8198 [--variant v0 none json] [--repeat 2] [-v]
Fictional names only; nothing touches live memory.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BANK = HERE / "evalsets" / "memory_extract.jsonl"
KNOWN = [
    "Robin is the user's wife.", "Leo is the user's son and he is 8.", "Ava is the user's daughter and she is 12.",
    "Jordan is the user's sister.", "Ava goes to Lincoln Middle School.", "The user drinks coffee every morning.",
    "Robin goes to a yoga class on Tuesdays.", "The family car is a gray minivan.", "Leo plays soccer on Saturdays.",
    "Robin works from home on Mondays.", "Jordan's birthday is in March.", "The user prefers aisle seats on flights.",
    "The family lives in Portland.", "The user's commute is by bike.", "Ava is learning Spanish.",
    "The family has a vegetable garden.", "The user is allergic to penicillin.", "Leo's best friend is Sam.",
    "Robin's parents live in Boise.", "The user likes jazz.", "The house has a guest room.",
    "The user works as an architect.", "Groceries are delivered on Sundays.", "The family hikes most weekends.",
]
CONTRACTS = {
    "none": ("Never repeat, restate or paraphrase a known fact: the known facts are already stored. Write only what "
             "is new, or a correction. If nothing is new and nothing was corrected, output exactly NONE and nothing "
             "else.\n\n"),
    "json": ("Answer with JSON only: {\"new\": [<each NEW fact as one standalone sentence>], \"replaces\": "
             "[{\"n\": <known fact number>, \"fact\": <the corrected fact>}]}. A known fact is already stored: never "
             "put it in \"new\". If nothing is new and nothing was corrected, answer {\"new\": [], \"replaces\": []}."
             "\n\n"),
}
_ASKS_NONE = "If there is nothing like that, output exactly NONE.\n\n"


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split())


def _is_echo(line: str) -> bool:
    words = set(_norm(line).split())
    for k in KNOWN:
        kw = set(_norm(k).split())
        if kw and len(words & kw) / len(kw | words) >= 0.7:
            return True
    return False


def _json_to_lines(text: str) -> str:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return text
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return text
    out = [str(f) for f in (d.get("new") or []) if str(f).strip()]
    out += [f"REPLACES {r.get('n')}: {r.get('fact')}" for r in (d.get("replaces") or []) if isinstance(r, dict)]
    return "\n".join(out) or "NONE"


def run(url: str, variants: list[str], repeat: int, verbose: bool) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="titan-memx-"))
    os.environ.update({"AGENT_MEMORY_DIR": str(tmp / "mem"), "HOUSEHOLD_DB": str(tmp / "household.db"),
                       "MEMORY_HYBRID": "0", "MEMORY_GROUP_FACTS": "0", "AGENT_ENDPOINT": url,
                       "MEMORY_EXTRACT_URL": url,   # the model under test first, not :8198 (the default since #1090)
                       "MEMORY_EXTRACT_BRAIN": "local"})
    os.environ.pop("GEMINI_API_KEY", None)
    sys.path.insert(0, str(HERE))
    import agent_memory as am
    bank = [json.loads(l) for l in BANK.read_text().splitlines() if l.strip()]
    results = {}
    for variant in variants:
        tot = dict(windows=0, new_expected=0, new_kept=0, fix_expected=0, fix_made=0, echoes=0, raw_lines=0,
                   false_facts=0, false_replaces=0, none_windows=0, none_clean=0, said_none=0, ms=0)
        for rep in range(repeat):
            for w in bank:
                db = tmp / f"{variant}-{rep}-{w['id']}"
                db.mkdir(parents=True, exist_ok=True)
                am._MEMORY_DIR, am._DB_PATH, am._REFS_DIR = db, db / "titan_memory.db", db / "refs"   # set at import
                mem = am.AgentMemory()
                for k in reversed(KNOWN):   # newest first in the block, like live: the order the prompt numbers them
                    mem.add_fact("c", k, confidence=1.0, person_id=am.OWNER)
                for q in w.get("asked") or []:
                    mem.add_message("c", "assistant", q)
                for u in w["user"]:
                    mem.add_message("c", "user", u)
                raw_box: list[str] = []
                orig = mem._llm_call

                def call(prompt: str, max_tokens: int = 512, _o=orig) -> str:
                    if variant in CONTRACTS:
                        prompt = prompt.replace(_ASKS_NONE, _ASKS_NONE + CONTRACTS[variant])
                    t0 = time.time()
                    if url == "oracle":
                        nums = {t: n for n, t in re.findall(r"^\[(\d+)\] (.+)$", prompt, re.M)}
                        text = "\n".join(re.sub(r"\{(.+?)\}", lambda m: nums.get(m.group(1), "0"), l) for l in w["ideal"])
                    else:
                        text = _o(prompt, max_tokens)
                    tot["ms"] += int((time.time() - t0) * 1000)
                    raw_box.append(text)
                    return _json_to_lines(text) if variant == "json" else text
                mem._llm_call = call
                mem._extract_facts_l1("c", person_id=am.OWNER)
                raw = _json_to_lines(raw_box[0]) if raw_box and variant == "json" else (raw_box[0] if raw_box else "")
                lines = [l.strip(" -•*") for l in raw.splitlines() if l.strip()]
                now = mem.get_facts(limit=1000, person_id=am.OWNER)
                stored_new = [f for f in now if f not in KNOWN]
                forgotten = [k for k in KNOWN if k not in now]
                tot["windows"] += 1
                tot["raw_lines"] += len(lines)
                tot["echoes"] += sum(1 for l in lines if not l.upper().startswith(("REPLACES", "NONE")) and _is_echo(l))
                tot["said_none"] += int(any(l.upper() == "NONE" for l in lines) or raw.strip().upper() == "NONE")
                hit = lambda f, keys: all(k.lower() in f.lower() for k in keys)   # noqa: E731
                for keys in w["new"]:
                    tot["new_expected"] += 1
                    tot["new_kept"] += int(any(hit(f, keys) for f in stored_new))
                expected_fix = w.get("replaces") or []
                for word in expected_fix:
                    tot["fix_expected"] += 1
                    tot["fix_made"] += int(any(word.lower() in k.lower() for k in forgotten))
                tot["false_replaces"] += sum(1 for k in forgotten if not any(word.lower() in k.lower() for word in expected_fix))
                tot["false_facts"] += sum(1 for f in stored_new if not any(hit(f, keys) for keys in w["new"]))
                if not w["new"] and not expected_fix:
                    tot["none_windows"] += 1
                    tot["none_clean"] += int(not stored_new and not forgotten)
                if url == "oracle":
                    import household_identity  # noqa: F401  (roster empty in the temp household DB)
                    ul = "\n".join("USER: " + u for u in w["user"])
                    for l in w["ideal"]:
                        if l == "NONE":
                            continue
                        rep = l.startswith("REPLACES")
                        line = re.sub(r"^REPLACES \{.+?\}:\s*", "", l)
                        why = ("too short" if len(line) <= 12 else "_keep_extracted" if not am._keep_extracted(line, ul)
                               else "contradicts_roster (_MEMBERSHIP)" if am.contradicts_roster(line, []) else
                               "one_off" if (am.one_off(line) and not rep) else
                               "_near_duplicate" if (not rep and am._near_duplicate(line, KNOWN)) else None)
                        stored = line in now
                        if why or not stored:
                            print(f"  DROPPED {w['id']}: {line!r} — {why or 'other (stored? ' + str(stored) + ')'}")
                if verbose:
                    hitv = lambda f, keys: all(k.lower() in f.lower() for k in keys)   # noqa: E731
                    bad_facts = [f for f in stored_new if not any(hitv(f, keys) for keys in w["new"])]
                    bad_forgot = [k for k in forgotten if not any(word.lower() in k.lower() for word in expected_fix)]
                    if bad_facts or bad_forgot:
                        print(f"  {variant} {rep} {w['id']}:")
                        for f in bad_facts:
                            print(f"      false fact: {f!r}")
                        for k in bad_forgot:
                            print(f"      false REPLACES (forgot): {k!r}")
                        for l in lines:
                            if l.upper().startswith("REPLACES"):
                                print(f"      raw: {l!r}")
        results[variant] = tot
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="an OpenAI-compatible llama-server (the code model, say), or oracle")
    ap.add_argument("--variant", nargs="+", default=["v0", "none", "json"])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    res = run(a.url, a.variant, a.repeat, a.verbose)
    for v, t in res.items():
        print(f"{v:<5} new {t['new_kept']}/{t['new_expected']}  fixes {t['fix_made']}/{t['fix_expected']}  "
              f"nothing-new clean {t['none_clean']}/{t['none_windows']}  echoes {t['echoes']}/{t['raw_lines']} lines  "
              f"false facts {t['false_facts']}  false REPLACES {t['false_replaces']}  NONE said {t['said_none']}  "
              f"model {t['ms'] // max(t['windows'], 1)} ms/window")
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
