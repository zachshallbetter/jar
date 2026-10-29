#!/usr/bin/env python3
# gate: hermetic   (throwaway stores; parses requests, runs no turn)
"""Only the admin's turns can be eval turns (10-07 review W5). An eval turn remembers nothing, takes fixtures, tool pins
and skill overrides, and is left out of the turn reports: from anyone else, "source": "eval" is an ordinary turn of
theirs. "temporary" keeps its own rule (_temporary_refused). Fictional people.
Run: python3 scripts/test_eval_source.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-eval-source-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "PUSH_DB": "push.db", "CONNECTORS_LEDGER": "ledger.db", "SERVER_IDENTITY_KEY": "id/key"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402


def who(role, via="device"):
    return hh.Identity({"id": f"p_{role}", "name": "Alex", "role": role}, None, role, via)


E, T = {"source": "eval"}, {"source": "temporary"}
check("the admin's eval is an eval (turn_eval, jb ask, the A/B boxes)",
      (api._parse_source(E, who("admin", "master")), api._parse_source(E, who("admin"))), ("eval", "eval"))
check("an adult's, a kid's, a kid's Watch's, a shared screen's: an ordinary turn",
      [api._parse_source(E, who(r, v)) for r, v in (("adult", "device"), ("kid", "device"), ("kid", "watch"),
                                                      ("household", "device"))], [None, None, None, None])
check("no identity at all: never an eval", api._parse_source(E), None)
check("so an adult's eval_tools pins and fixtures are ignored too",
      (api._parse_eval_pins({"eval_tools": ["web_search"]}, api._parse_source(E, who("adult"))),
       api._parse_eval_fixtures({"eval_fixtures": {"x": "y"}}, api._parse_source(E, who("adult")))), ((None, None), None))
check("temporary is unchanged (its own adult-only rule applies after)",
      (api._parse_source(T, who("adult")), api._parse_source(T, who("kid"))), ("temporary", "temporary"))
src = (HERE / "agent_api_server.py").read_text()
check("every caller passes the asker", re.findall(r"_parse_source\(body\)", src), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
