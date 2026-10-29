#!/usr/bin/env python3
"""Chat @-tags (jellybean#715; Zach decided 10-04, the titan contract is on that issue, approved by Coordination 10-05).

A message that starts with @code, @home or @research (any case, the first only) is for that "panel": the tag pins the
turn's tools (strictly: nothing outside them and the core, no widening), its skill, and for @research a deeper turn
(effort high, 8 rounds under the normal turn budget). Anything else, an unknown @word or a tag mid-sentence, is plain
text. The tag is removed from what the model reads; the turn log keeps its name.

A tag never grants a tool. What a tagged turn may use is its tools that the person's role already has (household_identity's
role table, the same as any turn); a tag whose main tool the role lacks is refused, and GET /v1/me lists only the tags
the caller's role can use (`available`).
"""
from __future__ import annotations

import re
from typing import Any

TAGS: dict[str, dict[str, Any]] = {
    "code": {"label": "Claude on your Mac", "where": "mac", "needs": "run_agent",
             "tools": ("run_agent", "agent_status", "agent_discard"), "skill": None,
             "note": "The person addressed @code: this is for Claude on their Mac (run_agent and its status)."},
    "home": {"label": "Home", "needs": "home_control",
             "tools": ("home_status", "home_control", "home_why", "lock_up"), "skill": "home-automation",
             "note": "The person addressed @home: answer with the house's devices."},
    "research": {"label": "Deep research", "needs": "web_search",
                 "tools": ("web_search", "fetch_url", "kb_retrieve"), "skill": "web-research",
                 "note": "The person addressed @research: research it properly on the web, read the sources you cite, "
                         "and say where each fact came from.", "effort": "high", "rounds": 8},
}
_TAG_RX = re.compile(r"^\s*@(code|home|research)(?=$|[\s.,:;!?])[\s.,:;!?]*", re.I)


def parse(text: str) -> tuple[str | None, str]:
    """(tag, the message without it), or (None, the message unchanged)."""
    m = _TAG_RX.match(str(text or ""))
    if not m:
        return None, str(text or "")
    return m.group(1).lower(), str(text)[m.end():]


def _allows(role: str | None, tool: str, allow: Any = None) -> bool:
    if allow is not None:
        return bool(allow(tool))       # the turn's own check (agent_runtime_lib._role_allows: role table + switches)
    if role == "admin":
        return True
    import household_identity as hh
    return hh.role_allows_tool(role, tool)


def available(role: str | None) -> list[dict[str, str]]:
    """The tags this role can use today: only those whose main tool the role already has."""
    out = []
    for name, t in TAGS.items():
        if _allows(role, t["needs"]):
            out.append({"tag": name, "label": t["label"], **({"where": t["where"]} if t.get("where") else {})})
    return out


def tools_for(tag: str, role: str | None, core: frozenset[str] | set[str], allow: Any = None) -> set[str] | None:
    """The tools a tagged turn may use: the core and the tag's tools, each only if the role already has it. None when
    the role can't use the tag at all."""
    t = TAGS[tag]
    if not _allows(role, t["needs"], allow):
        return None
    return {n for n in (set(core) | set(t["tools"])) if _allows(role, n, allow)}
