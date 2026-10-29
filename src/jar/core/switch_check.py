#!/usr/bin/env python3
"""Switches whose values can't be what was meant (titan-node#940).

systemd's EnvironmentFile keeps an inline "# comment" as part of a value, so on 10-04 two switches were silently off
for a day: FETCH_BROWSER_POOL ("1  # #882") and AGENT_GEMINI_THINKING ("low# …"). titan_preflight.sh flags such lines
in the files; this is the running service's own check, once at startup: a switch whose value carries a comment or a
stray quote, or an enumerated switch holding a value its code doesn't know. Key names only, never values.

  suspicious(env) → [key, …]
"""
from __future__ import annotations

import os
import re
from typing import Mapping

# Our switches: the prefixes titan's own settings use. Credentials and addresses are never looked at.
_PREFIXES = ("AGENT_", "CITE_", "ROOM_", "HA_", "ITEM_", "MAIL_", "KB_", "WORLD_", "FENCES", "NOTE_", "TASK_",
             "FAMILY_", "SCHEDULE", "FETCH_", "ENVELOPE", "EFFORT", "JEV_", "TITAN_")
_NEVER = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASS\b|URL|DSN|CREDENTIAL|_FILE$|_DIR$|_DB$|_PATH$|_LOG$", re.I)
# Enumerated switches: the values their code acts on (anything else falls back to a default without a word).
ENUMS: dict[str, frozenset[str]] = {
    "AGENT_GEMINI_THINKING": frozenset({"default", "low", "high", "auto"}),
}
_COMMENT = re.compile(r"\s#|['\"]\s*#|^#")


def suspicious(env: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    out = []
    for k, v in env.items():
        if not k.startswith(_PREFIXES) or _NEVER.search(k):
            continue
        s = str(v)
        if k in ENUMS:
            if s.strip().strip("'\"").lower() not in ENUMS[k] or s.strip() != s.strip().strip("'\""):
                out.append(k)
            continue
        if _COMMENT.search(s) or (len(s) >= 2 and s[0] in "'\"" and s[-1] != s[0]) or s.endswith(("'#", '"#')):
            out.append(k)
    return sorted(out)


def warn(env: Mapping[str, str] | None = None) -> str | None:
    """The startup line, or None when every switch looks as meant."""
    bad = suspicious(env)
    if not bad:
        return None
    return ("[switches] these values can't be what was meant (an inline comment or a stray quote in the env file? "
            "systemd keeps it in the value; titan-node#940): " + ", ".join(bad))
