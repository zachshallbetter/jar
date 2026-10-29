#!/usr/bin/env python3
"""Does the right skill get picked? The Jev skill pick decides, per message, which one skill's full
text goes into the prompt (agent_api_server._skill_match). A skill whose text never loads can't be
improved by editing that text (09-30: media loaded for 5 of 10 of its own tasks), so measure the
pick first and tune the one-line descriptions it reads.

  python3 scripts/skillopt/pick_eval.py                      # every bank: recall, confusions
  python3 scripts/skillopt/pick_eval.py --skill media --description "..."   # score a candidate
  python3 scripts/skillopt/pick_eval.py --propose media [--n 4]             # local model proposes, all scored

Run on titan with the agent's environment (Jev key, AGENT_JEV_GATES): each question is one Jev call,
labelled source "eval". Candidates come from titan's local code model (:8198); nothing leaves titan
but the questions Jev already sees. Reports land in $SKILLOPT_OUT/pick/<timestamp>.json. SKILL.md is
never edited: adopting a description is a reviewed titan-node PR.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import agent_runtime_lib as lib  # noqa: E402
import agent_skills  # noqa: E402
from ops_ports import CODE_MODEL_URL  # noqa: E402

OUT_ROOT = Path(os.environ.get("SKILLOPT_OUT", "/ai/datasets/skillopt"))
OPTIMIZER_URL = os.environ.get("SKILLOPT_OPTIMIZER_URL", CODE_MODEL_URL + "/v1")
THRESHOLD = float(os.environ.get("AGENT_JEV_SKILL_MIN", "0.6"))

# Questions no skill should take (a pick here loads a skill's text for nothing).
NONE_QUESTIONS = ["What's 18 times 7?", "Write a haiku about autumn.", "What's the capital of Peru?",
                  "Tell me a joke.", "How do I say thank you in Spanish?",
                  "Is this polite? 'hey, send me the file already'"]


def questions() -> list[tuple[str, str]]:
    """(expected pick, rendered question) for every task bank plus NONE_QUESTIONS. The expected pick is
    the bank's skill unless the task says otherwise: a bank's negative tasks carry "pick" ("calendar",
    "none", or alternatives like "none|web-research")."""
    import harness
    out = []
    for bank in sorted((HERE / "tasks").glob("*.jsonl")):
        for t in harness.load_tasks(bank.stem):
            out.append((t.get("pick") or bank.stem, harness.render(t["question"])))
    return out + [("none", q) for q in NONE_QUESTIONS]


def descriptions(overrides: dict[str, str] | None = None) -> dict[str, str]:
    d = {s.name: s.description for s in agent_skills.load_skills()}
    d.update(overrides or {})
    return d


def verdicts(qs: list[tuple[str, str]], skills: dict[str, str], workers: int = 4) -> list[dict]:
    def one(item):
        want, q = item
        lib._TURN_IO.source = "eval"
        try:
            v = lib.jev_skill_verdict(q, skills)
        finally:
            lib._TURN_IO.source = None
        choice, sure = v if v else (None, 0.0)
        loaded = choice in skills and sure >= THRESHOLD
        allowed = want.split("|")
        return {"want": want, "question": q, "choice": choice, "sure": round(sure, 3),
                "hit": (loaded and choice in allowed) or ("none" in allowed and not loaded)}
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, qs))


def summary(rows: list[dict]) -> dict[str, dict]:
    """Per skill: recall (its own tasks load it), false loads (it loads for another bank's task or a
    none question), and what its misses went to."""
    out: dict[str, dict] = {}
    for want in sorted({w for r in rows for w in r["want"].split("|")}):
        own = [r for r in rows if r["want"] == want]
        misses: dict[str, int] = {}
        for r in own:
            if not r["hit"]:
                k = r["choice"] if r["sure"] >= THRESHOLD else f"unsure:{r['choice']}"
                misses[k] = misses.get(k, 0) + 1
        false = sum(1 for r in rows if want not in r["want"].split("|") and r["choice"] == want and r["sure"] >= THRESHOLD)
        out[want] = {"tasks": len(own), "hit": sum(r["hit"] for r in own),
                     "recall": round(sum(r["hit"] for r in own) / max(1, len(own)), 3),
                     "false_loads": false, "misses": misses}
    return out


def print_summary(s: dict[str, dict]) -> None:
    print(f"{'skill':<18}{'hit':>8}{'recall':>8}{'false':>7}  misses")
    for k, v in s.items():
        print(f"{k:<18}{v['hit']:>4}/{v['tasks']:<3}{v['recall']:>8.2f}{v['false_loads']:>7}  "
              + ", ".join(f"{m}×{n}" for m, n in sorted(v["misses"].items(), key=lambda x: -x[1])))


def propose(skill: str, rows: list[dict], skills: dict[str, str], n: int) -> list[str]:
    """Ask titan's local code model for n one-line descriptions that would make the pick load `skill`
    for its missed questions without taking other skills' questions."""
    import urllib.request
    missed = [r["question"] for r in rows if r["want"] == skill and not r["hit"]][:12]
    kept = [r["question"] for r in rows if r["want"] == skill and r["hit"]][:6]
    others = "\n".join(f"- {k}: {v}" for k, v in skills.items() if k != skill)
    prompt = (f"A router reads one-line skill descriptions and picks the one skill a household assistant needs "
              f"for a message. The '{skill}' skill's description is:\n{skills[skill]}\n\nIt is NOT picked for "
              f"these messages that need it:\n" + "\n".join(f"- {q}" for q in missed) +
              "\n\nIt IS picked for these (keep them):\n" + "\n".join(f"- {q}" for q in kept) +
              f"\n\nThe other skills (don't take their messages):\n{others}\n\n"
              f"Write {n} alternative descriptions for '{skill}', each one or two sentences: what it does and when "
              f"to use it, naming the kinds of questions above in general terms (not these exact messages). "
              f"Answer with a JSON list of {n} strings and nothing else.")
    body = {"model": "local", "messages": [{"role": "user", "content": prompt}], "temperature": 0.7,
            "max_tokens": 1500, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(OPTIMIZER_URL.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        text = json.loads(r.read())["choices"][0]["message"]["content"]
    start, end = text.find("["), text.rfind("]")
    cands = json.loads(text[start:end + 1]) if start >= 0 else []
    return [c.strip() for c in cands if isinstance(c, str) and 20 <= len(c.strip()) <= 400][:n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill")
    ap.add_argument("--description", help="score this description for --skill instead of the current one")
    ap.add_argument("--propose", metavar="SKILL", help="have the local model propose descriptions and score each")
    ap.add_argument("--n", type=int, default=4)
    a = ap.parse_args()
    if lib.jev_skill_verdict("ping", {"calendar": "events"}) is None:
        print("no Jev verdicts (AGENT_JEV_GATES off or no key): run with the agent's environment", file=sys.stderr)
        return 2
    qs = questions()
    report: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "threshold": THRESHOLD, "questions": len(qs)}
    base_skills = descriptions()
    over = {a.skill: a.description} if a.skill and a.description else {}
    rows = verdicts(qs, descriptions(over))
    report["baseline" if not over else "candidate"] = {"overrides": over, "summary": summary(rows), "rows": rows}
    print_summary(summary(rows))
    if a.propose:
        target = a.propose
        base = summary(rows)[target]
        report["proposals"] = []
        for cand in propose(target, rows, base_skills, a.n):
            s = summary(verdicts(qs, descriptions({target: cand})))
            others_false = sum(v["false_loads"] for k, v in s.items() if k != target)
            report["proposals"].append({"description": cand, "target": s[target], "summary": s})
            better = (s[target]["recall"] > base["recall"] and s[target]["false_loads"] <= base["false_loads"]
                      and all(s[k]["recall"] >= summary(rows)[k]["recall"] - 0.1 for k in s if k != target))
            print(f"\n{'BETTER' if better else 'no'}  recall {base['recall']:.2f}→{s[target]['recall']:.2f}  "
                  f"false {base['false_loads']}→{s[target]['false_loads']}  (others' false loads {others_false})\n  {cand}")
    out = OUT_ROOT / "pick" / (time.strftime("%Y%m%d-%H%M%S") + (f"-{a.propose}" if a.propose else "") + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
