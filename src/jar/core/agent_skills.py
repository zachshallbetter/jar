#!/usr/bin/env python3
"""
Titan Agent Skills loader
=========================
Reads SKILL.md files from scripts/skills/*/SKILL.md and builds the prompt
text for them. A turn's prompt carries every skill's name and description
(skills_index_prompt: the same every turn, so it stays in the cached prefix);
a skill's full guide is added after the prefix when the turn's skill was
picked (skill_guide_prompt), or read on demand with the skill_guide tool.

Usage
-----
  from agent_skills import load_skills, skills_index_prompt, skill_guide_prompt

  # One-time at startup:
  skills = load_skills()

  # In the static system message:
  index = skills_index_prompt(skills)
  # After it, for the skill this turn needs:
  guide = skill_guide_prompt(next(s for s in skills if s.name == "calendar"))

File format (SKILL.md)
----------------------
  ---
  name: skill-name          # kebab-case, matches directory name
  description: ...          # ≤1024 chars; Use when trigger conditions
  triggers: am i free, what's on {day}, push * back   # optional: the local skill pick (pick_by_triggers)
  ---

  ## Overview
  ...
  ## When to Use
  ...
  ## Core Process
  ...
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

_SKILLS_DIR = Path(__file__).parent / "skills"


@dataclass
class Skill:
    name: str
    description: str
    body: str          # full Markdown body below frontmatter
    path: Path
    triggers: tuple[str, ...] = ()   # phrasings that pick this skill when Jev can't (pick_by_triggers)


def load_skills(skills_dir: Path | None = None) -> list[Skill]:
    """
    Discover and parse all SKILL.md files under skills_dir.
    Returns skills sorted by name for deterministic prompt ordering.
    """
    root = skills_dir or _SKILLS_DIR
    skills: list[Skill] = []
    if not root.exists():
        return skills
    for skill_file in sorted(root.glob("*/SKILL.md")):
        skill = _parse_skill(skill_file)
        if skill:
            skills.append(skill)
    return skills


def _parse_skill(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_skill_text(text, path)


def parse_skill_text(text: str, path: Path | None = None) -> Skill | None:
    """Parse a whole SKILL.md (frontmatter + body) from text; None when it isn't one. Used by the
    file loader and by eval turns that bring a candidate skill (agent_api_server skill_overrides)."""
    path = path or Path("<text>")
    # Extract YAML frontmatter between --- delimiters
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not fm_match:
        print(f"[skills] skipped {path}: no YAML frontmatter block found", file=sys.stderr)
        return None
    fm_text = fm_match.group(1)
    body = text[fm_match.end():]

    name = _fm_field(fm_text, "name")
    description = _fm_field(fm_text, "description")
    if not name or not description:
        print(f"[skills] skipped {path}: missing 'name' or 'description' in frontmatter", file=sys.stderr)
        return None

    # Truncate description to 1024 chars per spec
    description = description[:1024].strip()

    triggers = tuple(t.strip().lower() for t in _fm_field(fm_text, "triggers").split(",") if t.strip())
    return Skill(name=name, description=description, body=body.strip(), path=path, triggers=triggers)


def _fm_field(fm: str, key: str) -> str:
    """Extract a scalar or block scalar value from minimal YAML frontmatter."""
    # Scalar: name: value
    scalar = re.search(rf"^{key}:\s*(.+)$", fm, re.MULTILINE)
    if scalar:
        return scalar.group(1).strip()
    # Block scalar (|  or  >): key: |\n  line1\n  line2
    block = re.search(rf"^{key}:\s*[|>]\s*\n((?:[ \t]+.+\n?)*)", fm, re.MULTILINE)
    if block:
        lines = [l.strip() for l in block.group(1).splitlines()]
        return " ".join(l for l in lines if l)
    return ""


def skills_system_prompt(
    skills: list[Skill],
    active: set[str] | None = None,
    include_body: bool = True,
) -> str:
    """
    Build the skills addendum for the system prompt.
    active=None → include all skills.
    include_body=False → description only (lower token cost, less guidance).
    """
    selected = [s for s in skills if active is None or s.name in active]
    if not selected:
        return ""

    parts = ["## Agent Skills\n"]
    parts.append(
        "The following skills guide how you approach specific task types. "
        "Read the relevant skill before acting on a matching request.\n"
    )
    for skill in selected:
        parts.append(f"### {skill.name}")
        parts.append(skill.description)
        if include_body and skill.body:
            parts.append(skill.body)
        parts.append("")

    return "\n".join(parts)


def skills_index_prompt(skills: list[Skill], guide_tool: bool = True) -> str:
    """Every skill's name and description, and how to get a skill's full guide. The same text every
    turn (for one set of skills), so it sits in the cached prompt prefix; the guide a turn needs
    comes after the prefix (skill_guide_prompt) or through the skill_guide tool."""
    if not skills:
        return ""
    how = ("When a request matches a skill, follow its guide: it's under [SKILL GUIDE] below when "
           "it was picked for this turn; otherwise call skill_guide with the skill's name first."
           if guide_tool else
           "When a request matches a skill, follow its guide under [SKILL GUIDE] below if there is one.")
    lines = ["## Agent Skills", "",
             "Each skill has a full guide: how to approach that kind of request, which tools to use "
             "first and what to confirm. " + how, ""]
    lines += [f"- **{s.name}**: {s.description}" for s in skills]
    return "\n".join(lines)


def skill_guide_prompt(skill: Skill) -> str:
    """One skill's full guide, for the per-turn part of the prompt or the skill_guide tool."""
    return f"[SKILL GUIDE: {skill.name}]\n{skill.body or skill.description}"


def skill_names(skills: list[Skill]) -> list[str]:
    return [s.name for s in skills]


# ---------------------------------------------------------------------------
# The local skill pick (titan-node#402): which one guide a turn needs when Jev gives no verdict (off,
# no key, an error, unsure). Each SKILL.md lists its `triggers`, the ways people ask ("am i free",
# "what's on {day}"), so the box finds the calendar guide for questions that never say "calendar".
# ---------------------------------------------------------------------------

# {day}: when people say it. *: up to three words in between ("push the dinner back").
_DAY = (r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tonight|tomorrow|"
        r"this (?:morning|afternoon|evening|week|weekend)|next week|the weekend)")


def _normalise(text: str) -> str:
    t = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    # Dots and slashes stay inside a word (server.log, ~/Projects); a sentence's own punctuation goes.
    words = (w.strip(".'-") for w in re.sub(r"[^\w'~/.-]+", " ", t).split())
    return " " + " ".join(w for w in words if w) + " "


def _trigger_re(trigger: str) -> "re.Pattern[str]":
    parts = []
    for word in trigger.split():
        if word == "*":
            parts.append(r"(?:\S+ ){0,3}")
        else:
            parts.append(re.escape(word).replace(re.escape("{day}"), _DAY) + " ")
    return re.compile(r"(?<= )" + "".join(parts))


_TRIGGER_CACHE: dict[str, "re.Pattern[str]"] = {}


def trigger_scores(question: str, skills: list[Skill]) -> dict[str, int]:
    """Each skill's score for `question`: the words in its triggers that matched (a longer phrasing says
    more than one word). Skills with none are left out. A skill's name isn't a trigger of its own: "social
    media" isn't a question for the media skill."""
    q = _normalise(question)
    scores: dict[str, int] = {}
    for sk in skills:
        total = 0
        for t in sk.triggers:
            rx = _TRIGGER_CACHE.get(t) or _TRIGGER_CACHE.setdefault(t, _trigger_re(t))
            if rx.search(q):
                total += len([w for w in t.split() if w != "*"])
        if total:
            scores[sk.name] = total
    return scores


def pick_by_triggers(question: str, skills: list[Skill]) -> str | None:
    """The one skill whose triggers fit `question` best, or None: nothing matched, or two skills tie (an
    unclear question loads no guide, as before; the model can still read one with skill_guide)."""
    scores = trigger_scores(question, skills)
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]
