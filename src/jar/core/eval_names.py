#!/usr/bin/env python3
"""Evals speak fictional names; the live household has real ones (#878). The repo's eval cases, checks and fixtures
use fictional names only (Robin, Ava, Leo, Jordan, Hale; Maple Health, Cedar Dental, Corner Grocery, Birch). On
titan, a 0600 map (EVAL_NAMES_FILE, default /ai/datasets/connectors/eval_names.json, {"fictional": "real", ...}) translates at the
boundary: what an eval sends to the live agent goes out with real names (`out`), and everything that comes back
(replies, tool arguments, live reads) comes back with fictional ones (`back`) before any check reads it. Without the
map (a laptop, a hermetic test) both are the identity, so nothing real is ever needed off titan.

Word-bounded and case-preserving ("Ava's", "ava", "AVA"); identifiers are never touched (letters, digits and _ bound a
word). Longer names first ("Maple Health" before "Maple").
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT = Path("/ai/datasets/connectors/eval_names.json")


@lru_cache(maxsize=1)
def _map() -> dict[str, str]:
    p = Path(os.environ.get("EVAL_NAMES_FILE") or DEFAULT)
    try:
        m = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in m.items() if k and v}


def _rx(words: list[str]) -> re.Pattern | None:
    if not words:
        return None
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9_])({alts})(?![A-Za-z0-9_])", re.I)


def _case_like(src: str, dst: str) -> str:
    if src.isupper() and len(src) > 1:
        return dst.upper()
    if src.islower():
        return dst.lower()
    return dst


def _translate(x: Any, table: dict[str, str]) -> Any:
    if not table:
        return x
    rx = _rx(list(table))
    low = {k.lower(): v for k, v in table.items()}

    def tr(s: str) -> str:
        # the name as the map spells it maps as the map spells it (an all-caps short name keeps its own spelling); other casings follow the text's
        return rx.sub(lambda m: table[m.group(0)] if m.group(0) in table else _case_like(m.group(0), low[m.group(0).lower()]), s)

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return tr(v)
        if isinstance(v, list):
            return [walk(i) for i in v]
        if isinstance(v, tuple):
            return tuple(walk(i) for i in v)
        if isinstance(v, dict):
            return {walk(k) if isinstance(k, str) else k: walk(i) for k, i in v.items()}
        return v
    return walk(x)


def out(x: Any) -> Any:
    """Fictional → real: what an eval sends to the live agent."""
    return _translate(x, _map())


def back(x: Any) -> Any:
    """Real → fictional: what comes back from the live agent or a live read, before a check reads it."""
    return _translate(x, {v: k for k, v in _map().items()})


def active() -> bool:
    return bool(_map())
