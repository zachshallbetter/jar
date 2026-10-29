#!/usr/bin/env python3
# gate: hermetic   (pure function over a dict; no environment is read)
"""switch_check (#940): a switch value that carries a comment or a stray quote, or an enumerated switch with a value
its code doesn't know, is named at startup; credentials and paths are never looked at. Hermetic.

Run: python3 scripts/test_switch_check.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import switch_check as sc  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  {detail}")


env = {"AGENT_GEMINI_THINKING": "low# pin it", "FETCH_BROWSER_POOL": "1  # #882", "CITE_INLINE": "1",
       "ROOM_HOLD": "'1'#x", "MAIL_SERVER": "1", "HA_TOKEN": "abc # not a switch", "AGENT_API_TOKEN": "x #y",
       "KB_BACKUP_DIR": "/ai/x #y", "PATH": "/usr/bin # z", "AGENT_STREAM_TOKENS": "0", "WORLD_MODEL": "1"}
got = sc.suspicious(env)
check("10-04's two cases are named", {"AGENT_GEMINI_THINKING", "FETCH_BROWSER_POOL"} <= set(got), str(got))
check("a quoted value with a comment after it is named", "ROOM_HOLD" in got, str(got))
check("clean switches aren't", not {"CITE_INLINE", "MAIL_SERVER", "AGENT_STREAM_TOKENS", "WORLD_MODEL"} & set(got), str(got))
check("tokens, keys, paths and other programs' variables are never looked at",
      not {"HA_TOKEN", "AGENT_API_TOKEN", "KB_BACKUP_DIR", "PATH"} & set(got), str(got))
for v, bad in (("low", False), ("'low'", True), ("auto", False), ("High", False), ("lo w", True), ("", True)):
    check(f"AGENT_GEMINI_THINKING={v!r} {'named' if bad else 'fine'}", ("AGENT_GEMINI_THINKING" in sc.suspicious({"AGENT_GEMINI_THINKING": v})) == bad)
line = sc.warn(env) or ""
check("the startup line names keys only, never a value", "pin it" not in line and "#882" not in line and "FETCH_BROWSER_POOL" in line, line)
check("nothing to say → None", sc.warn({"CITE_INLINE": "1"}) is None)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
