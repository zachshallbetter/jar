#!/usr/bin/env python3
# gate: hermetic   (temp stores and env; nothing live)
"""box_fixture (#1399 P4): eval_as naming a golden-household person is an impersonation path, refused unless all hold:
BOX_FIXTURE=1, an eval turn from the admin token, the loader's marker (household.json's digest) in the box's stores, and
a person who is one of the fixture's people AND in this box's household. Refused requests never run as the asker
(agent_api_server raises EvalAsRefused → 403). The deploy refuses an agent.env that sets BOX_FIXTURE.
Run: python3 scripts/test_box_fixture.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
T = Path(tempfile.mkdtemp(prefix="titan-box-fixture-"))
os.environ["HOUSEHOLD_DB"] = str(T / "household.db")
os.environ.pop("BOX_FIXTURE", None)
sys.path.insert(0, str(HERE))
import box_fixture as F  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


class Store:
    def __init__(self, people):
        self.people = people

    def person(self, pid):
        return self.people.get(pid)


FIX = Store({"p_robin": {"id": "p_robin", "name": "Robin Hale", "role": "adult"},
             "p_ava": {"id": "p_ava", "name": "Ava Hale", "role": "kid"}})
ROBIN = {"person": "p_robin"}

print("── refused unless every condition holds ──")
check("the live config (no BOX_FIXTURE): refused", F.person(ROBIN, "eval", "admin", FIX), (None, "not a fixture box"))
os.environ["BOX_FIXTURE"] = "1"
check("BOX_FIXTURE=1 but no marker in the stores: refused", F.person(ROBIN, "eval", "admin", FIX)[1],
      "the stores carry no fixture marker")
(T / F.MARKER).write_text("sha256:" + "0" * 64 + "\n")
check("a hand-made marker (not the golden household's digest): refused", F.person(ROBIN, "eval", "admin", FIX)[1],
      "the stores' fixture marker doesn't match the golden household")
F.write_marker(T)
check("…the loader's marker is 0600", oct((T / F.MARKER).stat().st_mode & 0o777), "0o600")
check("not an eval turn: refused", F.person(ROBIN, "user", "admin", FIX)[0], None)
check("an eval turn from an adult's token: refused", F.person(ROBIN, "eval", "adult", FIX)[0], None)
check("a real household's id (not one of the fixture's people): refused even in a fixture box",
      F.person({"person": "zach"}, "eval", "admin", Store({"zach": {"name": "Z", "role": "admin"}})),
      (None, "only the golden household's people may be named"))
check("a fixture id missing from this box's household: refused", F.person({"person": "p_leo"}, "eval", "admin", FIX),
      (None, "that person isn't in this box's household"))
check("all of it: run as that person, their role from the store", F.person(ROBIN, "eval", "admin", FIX),
      ({"id": "p_robin", "name": "Robin Hale", "role": "adult"}, ""))
check("the fixture's people are the golden household's", sorted(F.FIXTURE_IDS),
      ["p_alex", "p_ava", "p_jordan", "p_leo", "p_robin"])

print("── through the API: a refused request is 403, never the asker ──")
os.environ.pop("BOX_FIXTURE", None)
src = (HERE / "agent_api_server.py").read_text()
body = src[src.index("def _eval_as("):src.index("def _parse_client(")]
check("_eval_as hands a {person} request to box_fixture and raises EvalAsRefused on a refusal",
      ("box_fixture.person(" in body, "raise EvalAsRefused(why)" in body), (True, True))
sites = src.count("_eval_as(body") - src.count("def _eval_as(body")
handled = len(re.findall(r"try:\n\s+[^\n]*_eval_as\(body[^\n]*\n(?:[^\n]*\n)?\s+except EvalAsRefused as exc:", src))
check("every call site catches it and answers 403", (sites, handled, src.count('status_code=403, detail=f"eval_as refused: {exc}"')),
      (3, 3, 3))
check("…and a refused turn's record is dropped (never left as a turn in flight)",
      src.count("_turns.pop(turn_id, None)   # never left behind as a turn in flight"), 2)

print("── the deploy refuses an agent.env that sets BOX_FIXTURE ──")
deploy = (HERE / "deploy_titan_node.sh").read_text()
m = re.search(r"grep -qE '([^']+)' /ai/projects/titan-local-agent/agent.env", deploy)
check("the guard is in the deploy", bool(m), True)
for line, want in (("BOX_FIXTURE=1", True), ("export BOX_FIXTURE='1'", True), ("  BOX_FIXTURE = 0", True),
                   ("# BOX_FIXTURE=1", False), ("NOT_BOX_FIXTURE=1", False), ("MAIL_SERVER=1", False)):
    env = T / "agent.env"
    env.write_text(line + "\n")
    hit = subprocess.run(["grep", "-qE", m.group(1), str(env)]).returncode == 0 if m else None
    check(f"{line!r}: {'refused' if want else 'deploys'}", hit, want)

print("── the fixture box's env pins every store inside the box, and hides the live dirs ──")
box = (HERE / "sandbox_box.sh").read_text()
blk = box[box.index('if [ "$fixture" = 1 ]; then\n  # Only the fixture'):box.index('if [ "$first_run" = 1 ]; then\n  household_db')]
for k, v in (("BOX_FIXTURE", "1"), ("MAIL_SERVER", "0"), ("LINKS_DB", '"$S/links.db"'),
             ("REFERENCES_DB", '"$S/references.db"'), ("MAIL_SERVER_DB", '"$S/mail_server.db"')):
    check(f"{k}={v} in the fixture env", f"{k}={v}" in blk, True)
launch_env = box[box.index("launch=(env "):box.index("  bwrap --dev-bind")]
check("MEMORY_DB is inside the box, in every box's launch (Box's review; #1464: test_sandbox_box covers each mode)",
      re.search(r'MEMORY_DB="\$B/[^"]+"', launch_env) is not None and "MEMORY_DB=" not in blk, True)
for d in ("/ai/datasets", "/ai/logs", "/ai/projects/titan-local-agent", '"$HOME/.config/jellybean"'):
    check(f"{d} hidden (tmpfs)", f"--tmpfs {d}" in blk, True)
check("every runtime a dead port", all(f"{r}" in blk for r in ("RETRIEVAL", "RECORD", "INGESTION")) and "TITAN_${r}_URL=http://127.0.0.1:9" in blk, True)
check("the hide list reaches the launch", '${hide[@]+"${hide[@]}"} --chdir "$B/code"' in box, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
