#!/usr/bin/env python3
# gate: hermetic   (pure: SKILL.md files and the SkillOpt task banks, no model, no network)
"""The local skill pick (#402): when Jev gives no verdict (a box, no key, an error), each SKILL.md's
`triggers` pick at most one guide. Scored on SkillOpt's task banks (every question's expected skill,
negatives included), the calendar questions the box missed in #402 runs 2 and 3, and questions no
skill should take. Hermetic.

Run: python3 scripts/test_skill_triggers.py
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "skillopt"))

import agent_skills  # noqa: E402
import harness  # noqa: E402

ok = fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}: {detail}")


skills = agent_skills.load_skills()
by_name = {s.name: s for s in skills}
pick = lambda q: agent_skills.pick_by_triggers(q, skills)  # noqa: E731

# Parsing
check("every skill has triggers", all(s.triggers for s in skills), str({s.name: len(s.triggers) for s in skills}))
check("triggers are lowercased phrases", all(t == t.lower() and t.strip() for s in skills for t in s.triggers), "")
s = agent_skills.parse_skill_text("---\nname: x\ndescription: d\n---\nbody")
check("a SKILL.md without triggers still loads (none)", s is not None and s.triggers == (), repr(s))

# The bank: every expected pick (bank or a task's "pick", "none" or alternatives).
bank = []
for f in sorted((HERE / "skillopt" / "tasks").glob("*.jsonl")):
    for t in harness.load_tasks(f.stem):
        bank.append((t.get("pick") or f.stem, harness.render(t["question"])))
right = wrong = 0
wrongs = []
for expected, q in bank:
    got = pick(q) or "none"
    if got in expected.split("|"):
        right += 1
    elif got != "none":
        wrong += 1
        wrongs.append(f"{q!r} → {got} (expected {expected})")
check("bank: never the wrong guide", wrong == 0, "; ".join(wrongs))
check("bank: the right guide for ≥ 95%", right >= 0.95 * len(bank), f"{right}/{len(bank)}")

# The box's misses (#402 runs 2 and 3): calendar questions that never say "calendar".
for q in ["Am I free tomorrow at 3:30 PM?", "When is my dentist appointment?", "List tomorrow's appointments in order.",
          "What's on Thursday?", "Do I have anything on Saturday?", "What time is piano tomorrow?",
          "Am I busy tomorrow afternoon?", "What’s on this weekend?"]:
    check(f"box miss → calendar: {q}", pick(q) == "calendar", str(agent_skills.trigger_scores(q, skills)))

# No guide: questions no skill takes, and the ones a loose trigger would have caught.
for q in ["What's 18 times 7?", "Write a haiku about autumn.", "Tell me a joke.", "Can you remind me how photosynthesis works?",
          "The roads are busy today, any tips?", "What lesson can we take from that?", "What should I cook tonight?",
          "What's the best way to learn piano?", "I'm free now, what should we do?", "What's on the menu tonight?",
          "Explain how a heat pump works in simple terms.", "What's a good time of day to post on social media?",
          "How many steps have I taken today?"]:
    check(f"no guide: {q}", pick(q) is None, str(agent_skills.trigger_scores(q, skills)))

# Rules
check("a longer phrasing outweighs a word", pick("Remind me later to look up that podcast episode.") == "calendar",
      str(agent_skills.trigger_scores("Remind me later to look up that podcast episode.", skills)))
tie = [agent_skills.Skill("a", "d", "", Path("a"), ("alpha",)), agent_skills.Skill("b", "d", "", Path("b"), ("beta",))]
check("a tie loads no guide", agent_skills.pick_by_triggers("alpha and beta", tie) is None, "")
check("* spans a few words", pick("Push the team dinner back an hour.") == "calendar", "")
check("paths keep their dots; a sentence's period goes", agent_skills._normalise("See ~/x/server.log.") == " see ~/x/server.log ", "")
check("curly apostrophes count", pick("What’s on Thursday?") == "calendar", "")

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
