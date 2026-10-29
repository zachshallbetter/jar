#!/usr/bin/env python3
# gate: hermetic
"""One default per shared setting (agent_paths.py, 10-05 housekeeping): the environment wins; unset or blank,
every caller gets the same default, and none of them is a path on the Mac.
Run: python3 scripts/test_agent_paths.py"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_paths  # noqa: E402

failed = 0


def check(name: str, got: object, want: object) -> None:
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


NAMES = ("AGENT_MEMORY_DIR", "AGENT_API_TOKEN_FILE", "AGENT_SCRIPT_DIR", "AGENT_SESSION", "HOME")
saved = {k: os.environ.get(k) for k in NAMES}
try:
    for k in NAMES[:4]:
        os.environ.pop(k, None)
    os.environ["HOME"] = "/tmp/fixture-home"
    check("memory: under $HOME when unset", agent_paths.memory_dir(), Path("/tmp/fixture-home/.config/jellybean/memory"))
    check("memory db: titan_memory.db in it", agent_paths.memory_db().name, "titan_memory.db")
    check("token file: the conventional path when unset", agent_paths.api_token_file(), "/ai/config/agent_api_token.txt")
    check("script dir: this folder when unset (never a Mac path)", agent_paths.script_dir(), Path(agent_paths.__file__).resolve().parent)
    check("session: titan-local-agent when unset", agent_paths.session(), "titan-local-agent")
    for k in NAMES[:4]:
        os.environ[k] = "  "
    check("blank counts as unset (session)", agent_paths.session(), "titan-local-agent")
    check("blank counts as unset (token file)", agent_paths.api_token_file(), "/ai/config/agent_api_token.txt")
    os.environ.update({"AGENT_MEMORY_DIR": "/tmp/m", "AGENT_API_TOKEN_FILE": "/tmp/t", "AGENT_SCRIPT_DIR": "/tmp/s",
                       "AGENT_SESSION": "s1"})
    check("env wins: memory", agent_paths.memory_dir(), Path("/tmp/m"))
    check("env wins: token file", agent_paths.api_token_file(), "/tmp/t")
    check("env wins: script dir", agent_paths.script_dir(), Path("/tmp/s"))
    check("env wins: session", agent_paths.session(), "s1")
    here = Path(__file__).resolve().parent
    stray = [p.name for p in here.glob("*.py") if p.name != "test_agent_paths.py"
             and "/Users/zachshallbetter/Projects/titan-node" in p.read_text(encoding="utf-8", errors="ignore")]
    check("no script defaults to the Mac checkout", stray, [])
finally:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print(f"{'PASS' if not failed else 'FAIL'}: {failed} failed")
sys.exit(1 if failed else 0)
