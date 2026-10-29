#!/usr/bin/env python3
# gate: hermetic   (no model, no device, throwaway stores: the declarations, the mapping and the checks per asker)
"""One file tool, `files` with a mode (#1251 decision C, Zach 10-08): contents = search_file_contents, name = find_files,
titan = file_list.

What it holds:
  - who may read which file doesn't change: for every asker (the admin, another adult, a kid, a shared screen) on every
    client (a Mac, an iPhone with their Mac, an iPhone alone), a `files` call in each mode comes back exactly as the old
    tool's call did, and the device gets exactly the same requests (the old tool's name and arguments: the Mac's tool
    names never change). A kid and a shared screen are refused the same way in every mode, with nothing sent to any
    device; another adult is refused titan's listing as file_list refused them;
  - another person's upload never shows in a titan listing through `files`, as through file_list (#1071);
  - only `files` is declared (the local prompt, Gemini's declarations, the turn's specs), with only the modes the asker
    may use; the old names are hidden aliases that still run, through their own checks;
  - a narrowed turn (a tag, a task step, a background turn) runs a `files` mode only when its old tool is in the set;
  - turn_eval's fixtures (keyed by the old names) answer a `files` call; a scheduled `files` call is stored as its old
    tool; required_reads and claim_checks rows still match.
Fictional people only.
Run: python3 scripts/test_file_tool.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-filetool-"))
PARENT = _TMP / "tmp"
PARENT.mkdir()
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "UPLOADS_DB": "uploads.db", "KB_ACL_DB": "kb_acl.db", "PROACTIVE_DB": "proactive.db",
             "SCHEDULED_DB": "scheduled.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["UPLOADS_DIR"] = str(PARENT / "uploads")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import claim_checks  # noqa: E402
import file_tool as ft  # noqa: E402
import household_identity as hi  # noqa: E402
import required_reads  # noqa: E402
import uploads  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


IO = lib._TURN_IO
SENT: list[tuple[str, dict]] = []


def device(req):
    SENT.append((req["tool"], dict(req["args"])))
    return {"result": f"[{req['tool']} ran on this person's own Mac]"}


PEOPLE = {"admin": {"id": "p_alex", "name": "Alex"}, "adult": {"id": "p_robin", "name": "Robin"},
          "kid": {"id": "p_leo", "name": "Leo"}, "household": {"id": "d_kitchen", "name": "Kitchen"}}
PHONE = lib.PHONE_DEVICE_TOOLS_LEGACY


def turn(role, client):
    """A fresh turn for this asker on this client ("mac", "iphone+mac", "iphone")."""
    for k in ("outcomes", "failed_calls", "tools_used", "remote", "eval_fixtures", "eval_tools", "eval_device"):
        if hasattr(IO, k):
            delattr(IO, k)
    IO.person, IO.role, IO.source = PEOPLE[role], role, "user"
    IO.outcomes, IO.failed_calls, IO.tools_used = [], {}, set()
    IO.device = device
    if client == "mac":
        IO.client, IO.device_tools = None, None
    else:
        IO.client, IO.device_tools = "iphone", PHONE
        if client == "iphone+mac":
            IO.remote = {"tools": lib.mac_tools_for_phone(PHONE), "from": "the iPhone", "device": "the Mac"}
    lib._TURN_INFO.info = {}
    SENT.clear()


def run(role, client, name, args):
    turn(role, client)
    out = lib.execute_tool(name, dict(args))
    return out, list(SENT)


# one call per mode, with the old tool's own arguments
CALLS = {
    "contents": ({"mode": "contents", "query": "plain_failure", "path": "~/Projects/demo"},
                 ("search_file_contents", {"query": "plain_failure", "path": "~/Projects/demo"})),
    "name": ({"mode": "name", "query": "resume", "path": "~/Documents"},
             ("find_files", {"query": "resume", "folder": "~/Documents"})),
    "titan": ({"mode": "titan", "path": str(PARENT)}, ("file_list", {"path": str(PARENT)})),
}
_, theirs = uploads.save("p_robin", "robin-bank-statement.txt", b"Robin's statement.")
_, mine = uploads.save("p_alex", "alex-notes.txt", b"Alex's notes.")
(PARENT / "other.txt").write_text("not an upload")

print("── the mapping ──")
check("each mode is its old tool, with that tool's own arguments", {m: ft.resolve(a)[:2] for m, (a, _) in CALLS.items()},
      {m: old for m, (_, old) in CALLS.items()})
check("the text protocol's args wrapper and a mode word", ft.resolve({"args": {"mode": "text", "query": "x", "path": "~/a"}})[:2],
      ("search_file_contents", {"query": "x", "path": "~/a"}))
check("no mode and no telling shape: asked again, nothing runs", (ft.resolve({"query": "resume"})[0],
      ft.resolve({"query": "resume"})[2].startswith("Not run: no mode was given.")), (None, True))
check("an unknown mode: asked again", ft.resolve({"mode": "everything", "query": "x"})[2].startswith("Not run: “everything” isn't a mode"), True)

print("── views x viewers: every mode is its old tool, for every asker on every client ──")
WANT_MODES = {("admin", "mac"): ["contents", "name", "titan"], ("adult", "mac"): ["contents", "name"],
              ("kid", "mac"): [], ("household", "mac"): [],
              ("admin", "iphone+mac"): ["contents", "name", "titan"], ("adult", "iphone+mac"): ["contents", "name"],
              ("kid", "iphone+mac"): [], ("admin", "iphone"): ["titan"], ("adult", "iphone"): [], ("kid", "iphone"): []}
for (role, client), want in WANT_MODES.items():
    turn(role, client)
    specs = lib._turn_tool_specs()
    offered = [m for m in ft.MODES if m in (specs.get("files") or {}).get("parameters", {}).get("mode", "").split(" | ")]
    check(f"{role} on {client}: offered exactly the modes whose old tool they had", offered, want)
    check(f"{role} on {client}: no old file tool is declared", sorted(set(specs) & ft.ALIASES), [])
    for mode, (args, (old, old_args)) in CALLS.items():
        new = run(role, client, "files", args)
        was = run(role, client, old, old_args)
        check(f"{role} on {client}, mode {mode}: the same answer and the same device requests as {old}", new, was)

print("── a kid and a shared screen: refused the same way in every mode, nothing sent ──")
for role in ("kid", "household"):
    for client in ("mac", "iphone+mac"):
        got = {mode: run(role, client, "files", args) for mode, (args, _) in CALLS.items()}
        check(f"{role} on {client}: every mode is the role refusal, no device request",
              got, {mode: (hi.REFUSAL, []) for mode in CALLS})

print("── another adult: titan's listing refused as file_list refused it; their Mac reads go to their own device ──")
check("Robin (adult), mode titan: the role refusal, as file_list", run("adult", "mac", "files", CALLS["titan"][0]),
      (hi.REFUSAL, []))
check("Robin (adult), mode contents: one request, the Mac's own tool name", run("adult", "mac", "files", CALLS["contents"][0])[1],
      [CALLS["contents"][1]])

print("── another person's upload never shows through files, as through file_list (#1071) ──")
lst = run("admin", "mac", "files", {"mode": "titan", "path": str(PARENT), "glob": "**/*"})[0]
check("Alex lists the parent folder with a ** glob: his own upload, never Robin's",
      ("alex-notes.txt" in lst, "robin-bank-statement.txt" in lst), (True, False))
check("…exactly what file_list shows him", lst, run("admin", "mac", "file_list", {"path": str(PARENT), "pattern": "**/*"})[0])
ref = run("admin", "mac", "files", {"mode": "titan", "path": str(Path(theirs["path"]).parent)})[0]
check("Robin's upload folder named outright: refused, as file_list refuses it",
      (ref.startswith("Refused: that file was sent to titan by someone else"),
       ref == run("admin", "mac", "file_list", {"path": str(Path(theirs["path"]).parent)})[0]), (True, True))

print("── the machine guard names what the model knows ──")
out = run("admin", "mac", "files", {"mode": "titan", "path": "~/Documents"})[0]
check("a Mac path in titan mode: not done, and pointed at the Mac's modes", (out.startswith("Not done: ~/Documents is a path on "
      "the user's Mac, and files (mode titan) works on titan."), "files mode contents or name" in out), (True, True))
out = run("admin", "mac", "files", {"mode": "contents", "query": "x", "path": "/ai/projects"})
check("a titan path in contents mode: exactly as search_file_contents had it (no change to any check)",
      out, run("admin", "mac", "search_file_contents", {"query": "x", "path": "/ai/projects"}))
check("a titan path in name mode: exactly as find_files had it", run("admin", "mac", "files", {"mode": "name", "query": "x", "path": "/ai/x"}),
      run("admin", "mac", "find_files", {"query": "x", "folder": "/ai/x"}))

print("── what each brain is declared ──")
turn("admin", "mac")
prompt = lib.build_tool_system_prompt()
check("the local prompt: files, in full, and none of the old three", ("- **files**:" in prompt,
      [n for n in ft.ALIASES if f"- **{n}**:" in prompt]), (True, []))
check("files is shown in full to the local brain", "files" in lib.LOCAL_FULL_TOOLS, True)
decls = {d["name"]: d for d in lib._gemini_function_declarations()}
check("Gemini: files with mode, query and path; none of the old three",
      ("files" in decls, {"mode", "query", "path"} <= set(decls["files"]["parameters"]["properties"]), sorted(set(decls) & ft.ALIASES)),
      (True, True, []))
check("a narrowed declaration (only find_files in the set): files with mode name only",
      {d["name"]: d["parameters"]["properties"]["mode"]["description"] for d in lib._gemini_function_declarations({"find_files", "get_datetime"})
       if d["name"] == "files"}, {"files": "name"})
check("tool_help on an old name: the files entry", lib._tool_help({"name": "find_files"}).startswith("- **files**:"), True)
check("the local gate: files runs, an old name still runs", (lib._local_tool_gate("files"), lib._local_tool_gate("find_files"),
      lib._local_tool_gate("search_file_contents"), lib._local_tool_gate("file_list")), (None, None, None, None))
turn("adult", "mac")
check("an adult's local gate: titan's file_list is no tool of theirs", lib._local_tool_gate("file_list").startswith("No tool named"), True)
turn("kid", "mac")
check("a kid's prompt and declarations: no files at all", ("- **files**:" in lib.build_tool_system_prompt(),
      "files" in {d["name"] for d in lib._gemini_function_declarations()}), (False, False))

print("── a narrowed turn runs a files mode only when its old tool is in the set ──")
check("a background turn's set (file_list, no Mac reads): titan mode yes, contents and name no",
      {m: lib._call_allowed("files", a, lib.BACKGROUND_TOOLS) for m, (a, _) in CALLS.items()},
      {"contents": False, "name": False, "titan": True})
check("no set: everything (the asker's checks still apply at the chokepoint)", lib._call_allowed("files", CALLS["name"][0], None), True)
check("an old name in a set is itself", (lib._call_allowed("find_files", {}, {"find_files"}), lib._call_allowed("find_files", {}, {"x"})),
      (True, False))

print("── eval fixtures, scheduling, retries ──")
turn("admin", "mac")
IO.source, IO.eval_fixtures = "eval", {"search_file_contents": "[fixture: 1 match]"}
check("turn_eval's fixture for search_file_contents answers files mode contents",
      lib.execute_tool("files", dict(CALLS["contents"][0])), "[fixture: 1 match]")
turn("admin", "mac")
lib.execute_tool("files", dict(CALLS["name"][0]))
check("the turn log: the old tool's name ran, and the mode counted", (lib._TURN_INFO.info.get("tool_names"),
      lib._TURN_INFO.info.get("files_modes")), (["find_files"], ["name"]))
import scheduled  # noqa: E402
turn("admin", "mac")
got = scheduled.tool("schedule_action", {"tool_name": "files", "tool_args": {"mode": "titan", "path": "/ai/tmp"},
                                         "at": "in 30 minutes", "what": "list titan's tmp"})
check("a scheduled files call is stored as its mode's tool (so its checks then are that tool's)",
      (got.startswith("Scheduled"), [(r["tool"], r["args"]) for r in scheduled.list_for("p_alex", "admin")]),
      (True, [("file_list", {"path": "/ai/tmp"})]))
check("a scheduled files call with no mode: not scheduled", scheduled.tool("schedule_action", {
      "tool_name": "files", "tool_args": {"query": "x"}, "at": "in 30 minutes"}).startswith("Not scheduled: no mode was given."), True)
check("required_reads: an old reader in a row is asked for as files, and a files call counts as it",
      (required_reads._declared_as("search_file_contents", {"files"}), required_reads._ran({"find_files"}) >= {"files"}),
      ("files", True))
check("claim_checks: a denied file read with files declared names files", claim_checks.false_no_access(
      "I don't have access to your files on the Mac.", {"files", "read_file"}, set()), [("files", "files")])
check("…and none once its mode's tool ran", claim_checks.false_no_access(
      "I don't have access to your files on the Mac.", {"files", "read_file"}, {"search_file_contents"}), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
