#!/usr/bin/env python3
# gate: hermetic
"""The uploads guard covers listings too (titan-node#1071): a file someone sent titan (UPLOADS_DIR) never shows in
another person's file_list, whether they list a parent folder (like /ai/tmp), the uploads folder, its id folder, or
glob from above with **; a direct read through "..", a symlink or the full path is refused as before. run_shell runs
on the Mac (a device tool), where titan's uploads don't exist. Two fictional people; a throwaway folder tree.
Run: python3 scripts/test_uploads_guard.py
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-upguard-"))
PARENT = _TMP / "tmp"
PARENT.mkdir()
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "UPLOADS_DB": "uploads.db", "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["UPLOADS_DIR"] = str(PARENT / "uploads")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402
import uploads  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


_, a = uploads.save("p_alex", "alex-notes.txt", b"Alex's own notes.")
_, r = uploads.save("p_robin", "robin-bank-statement.txt", b"Robin's bank statement.")
(PARENT / "other.txt").write_text("not an upload")
(PARENT / "link-to-robin").symlink_to(r["path"])


def as_(pid):
    lib._TURN_IO.person, lib._TURN_IO.role, lib._TURN_IO.source = {"id": pid, "name": pid}, "admin", "user"


def tool(name, **args):
    return lib._execute_tool(name, args)


for me, mine, theirs in (("p_alex", a, r), ("p_robin", r, a)):
    other = "p_robin" if me == "p_alex" else "p_alex"
    print(f"── {me} ──")
    as_(me)
    out = tool("file_list", path=str(PARENT))
    check(f"{me}: listing the parent shows the uploads folder and other files", ("uploads/" in out, "other.txt" in out), (True, True))
    out = tool("file_list", path=str(PARENT / "uploads"))
    check(f"{me}: listing the uploads folder itself is refused, as before (never {other}'s id)",
          ("Refused" in out, theirs["id"] in out), (True, False))
    out = tool("file_list", path=str(PARENT), pattern="**/*")
    check(f"{me}: a ** glob from the parent: my file, never {other}'s",
          (mine["name"] in out, theirs["name"] in out, theirs["id"] in out), (True, False, False))
    out = tool("file_list", path=str(PARENT), pattern="uploads/*/*")
    check(f"{me}: a glob straight into the folders: mine only", (mine["name"] in out, theirs["name"] in out), (True, False))
    refused = tool("file_list", path=str(Path(theirs["path"]).parent))
    check(f"{me}: listing {other}'s id folder is refused", theirs["name"] in refused, False)
    for how, path in (("the full path", theirs["path"]), ("via ..", str(PARENT / "uploads" / ".." / "uploads" / theirs["id"] /
                                                                      theirs["name"]))):
        out = tool("file_read", path=path)
        check(f"{me}: reading {other}'s upload {how} is refused", ("Refused" in out, "statement" in out.lower() and other == "p_robin"
                                                                    or "notes." in out and other == "p_alex"), (True, False))
    out = tool("file_read", path=str(PARENT / "link-to-robin"))
    check(f"{me}: a symlink to Robin's upload: readable only by Robin", "Refused" in out, me != "p_robin")

print("── run_shell runs on the Mac, not titan ──")
check("run_shell is a device tool (the user's Mac), so titan's uploads aren't in its reach",
      ("run_shell" in lib._DEVICE_TOOLS, "Mac" in lib._DEVICE_TOOLS["run_shell"]["description"]), (True, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
