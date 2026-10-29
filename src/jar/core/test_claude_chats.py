#!/usr/bin/env python3
"""
Unit tests for Claude conversations (claude_chats.py): the claude.ai export and Claude Code
parsers, secrets removed before storing, each person's chats searched for them alone, the
search_claude_chats tool and the API. Hermetic: temp databases, no runtimes, no network.

Run: python3 scripts/test_claude_chats.py
"""
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-claude-chats-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "CLAUDE_CHATS_DB": "claude_chats.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha-token")
os.environ.pop("TITAN_DISCOVERY", None)
os.environ.pop("GEMINI_API_KEY", None)
os.environ["AGENT_TURN_SOURCE"] = "user"
sys.path.insert(0, os.path.dirname(__file__))

import agent_runtime_lib as lib  # noqa: E402
import claude_chats as cc  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")
    return ok


# ── fixtures ──────────────────────────────────────────────────────────────────────────────────
EXPORT = [
    {"uuid": "c-ff", "name": "Fantasy football waiver picks", "created_at": "2026-09-30T19:40:00Z",
     "updated_at": "2026-09-30T19:52:00Z", "chat_messages": [
         {"sender": "human", "created_at": "2026-09-30T19:40:00Z", "text": "",
          "content": [{"type": "text", "text": "Who should I pick up on waivers this week? I lost my running back."}],
          "attachments": [{"file_name": "roster.png"}]},
         {"sender": "assistant", "created_at": "2026-09-30T19:41:00Z", "text": "",
          "content": [{"type": "text", "text": "With your running back out, grab Jaylen Wright from waivers."},
                      {"type": "tool_use", "name": "web_search", "input": {"q": "waivers"}}]},
     ]},
    {"uuid": "c-soup", "name": "", "created_at": "2026-09-12T01:00:00Z", "updated_at": "2026-09-12T01:05:00Z",
     "chat_messages": [
         {"sender": "human", "created_at": "2026-09-12T01:00:00Z", "text": "A good lentil soup for a cold night please"},
         {"sender": "assistant", "created_at": "2026-09-12T01:01:00Z", "text": "Try a red lentil soup with cumin."}]},
    {"uuid": "c-empty", "name": "Nothing said", "chat_messages": []},
]


def export_zip(convs) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("data-2026-10-01/conversations.json", json.dumps(convs))
        z.writestr("data-2026-10-01/users.json", "[]")
    return b.getvalue()


def L(**d):
    return json.dumps(d)


SESSION = [
    L(type="user", isSidechain=False, cwd="/Users/zach/Projects/titan-node", gitBranch="claude-chats",
      timestamp="2026-10-01T05:00:00Z", message={"role": "user", "content": "Make titan able to read my Claude chats. <system-reminder>secret plumbing</system-reminder>"}),
    L(type="assistant", timestamp="2026-10-01T05:00:05Z", message={"content": [{"type": "thinking", "thinking": "hmm"}]}),
    L(type="assistant", timestamp="2026-10-01T05:00:06Z", message={"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}),
    L(type="user", timestamp="2026-10-01T05:00:07Z", message={"content": [{"type": "tool_result", "content": "huge output"}]}),
    L(type="assistant", timestamp="2026-10-01T05:01:00Z", message={"content": [{"type": "text", "text": "Built a per-person FTS5 store for the chats."}]}),
    L(type="user", isMeta=True, timestamp="2026-10-01T05:01:01Z", message={"content": "Caveat: The messages below were generated"}),
    L(type="user", timestamp="2026-10-01T05:01:02Z", message={"content": "<command-name>/clear</command-name><command-args></command-args>"}),
    L(type="user", isSidechain=True, timestamp="2026-10-01T05:01:03Z", message={"content": "subagent prompt"}),
    L(type="user", timestamp="2026-10-01T05:02:00Z", message={"content": "Here's my key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 use it"}),
    L(type="custom-title", customTitle="Claude chats on titan", sessionId="s-1"),
    L(type="pr-link", prUrl="https://github.com/zachshallbetter/titan-node/pull/248", prNumber=248),
    "not json at all",
]

print("\n── secrets never stored ──")
for label, text, gone in [
    ("Anthropic key", "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 here", "sk-ant-api03"),
    ("GitHub token", "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB", "ghp_"),
    ("AWS key id", "AKIAIOSFODNN7EXAMPLE in the env", "AKIAIOSFODNN7EXAMPLE"),
    ("private key", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaA\n-----END OPENSSH PRIVATE KEY-----", "b3BlbnNzaA"),
    ("password: value", "the wifi password: Bluebird4471! ok", "Bluebird4471"),
    ("JWT", "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJ6YWNoIn0.c2lnbmF0dXJlc2lnbmF0dXJl", "eyJhbGci"),
]:
    out = cc.redact(text)
    check(f"{label} removed", (gone in out, cc.REDACTED in out), (False, True))
check("a long word or a row of x's isn't a key", cc.redact("x" * 60 + " Supercalifragilisticexpialidocious" * 2), "x" * 60 + " Supercalifragilisticexpialidocious" * 2)
check("a git SHA and ordinary words stay", cc.redact("merged at 8d0a3fd9c1e2b3a4f5d6e7c8b9a0f1e2d3c4b5a6 today"),
      "merged at 8d0a3fd9c1e2b3a4f5d6e7c8b9a0f1e2d3c4b5a6 today")

print("\n── claude.ai export ──")
convs = cc.parse_claude_ai_export(export_zip(EXPORT))
check("from the zip: every conversation", [c["conv_id"] for c in convs], ["c-ff", "c-soup", "c-empty"])
ff = convs[0]
check("text blocks only, attachments named", [(m["role"], m["text"]) for m in ff["messages"]],
      [("user", "Who should I pick up on waivers this week? I lost my running back.\n[attached: roster.png]"),
       ("claude", "With your running back out, grab Jaylen Wright from waivers.")])
check("an untitled chat is titled by its first words", convs[1]["title"], "A good lentil soup for a cold night please")
check("conversations.json alone works too", len(cc.parse_claude_ai_export(json.dumps(EXPORT).encode())), 3)
for bad, why in [(b"PK\x03\x04garbage", "corrupt zip"), (json.dumps({"x": 1}).encode(), "not a list")]:
    try:
        cc.parse_claude_ai_export(bad)
        check(f"{why} refused", False, True)
    except (ValueError, zipfile.BadZipFile):
        check(f"{why} refused", True, True)

print("\n── Claude Code session ──")
s = cc.condense_claude_code("s-1", SESSION)
check("prompts and Claude's words; no tools, thinking, meta, subagents or reminders",
      [(m["role"], m["text"]) for m in s["messages"]],
      [("user", "Make titan able to read my Claude chats."),
       ("claude", "Built a per-person FTS5 store for the chats."),
       ("user", "/clear"),
       ("user", "Here's my key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 use it"),
       ("note", "Pull requests: https://github.com/zachshallbetter/titan-node/pull/248")])
check("its title, project and branch", (s["title"], s["place"]), ("Claude chats on titan", "Claude Code · titan-node (claude-chats)"))
check("when", (s["started"], s["updated"]), ("2026-10-01T05:00:00Z", "2026-10-01T05:02:00Z"))

print("\n── each person's own ──")
check("Zach's export stored", cc.store("zach", "claude-ai", convs), {"written": 2, "unchanged": 0, "empty": 1})
check("again: nothing changes", cc.store("zach", "claude-ai", convs), {"written": 0, "unchanged": 2, "empty": 1})
check("Zach's session stored", cc.store("zach", "claude-code", [s])["written"], 1)
mir_conv = {"conv_id": "c-ff", "title": "Fantasy football trade", "place": "claude.ai", "started": "2026-09-30T20:00:00Z",
            "updated": "2026-09-30T20:10:00Z", "messages": [{"role": "user", "text": "Should I trade my quarterback for two wide receivers in fantasy football?"}]}
check("Robin's own, same id, kept apart", cc.store("p_mir", "claude-ai", [mir_conv])["written"], 1)
z = cc.search("zach", "waiver running back")
check("titles are searched too", "waiver picks" in cc.search("zach", "fantasy football"), True)
check("Zach finds his", "“Fantasy football waiver picks” — claude.ai" in z and "[id: claude-ai:c-ff]" in z, True)
check("… never Robin's, though he's the admin", "trade" in z.lower() or "quarterback" in z.lower(), False)
m = cc.search("p_mir", "fantasy football")
check("Robin finds hers", "Fantasy football trade" in m and "waiver" not in m.lower(), True)
check("the stored key is gone", "sk-ant-api03" in cc.read("zach", "claude-code:s-1"), False)
check("no AND match → any word", "lentil" in cc.search("zach", "lentil casserole"), True)
check("nothing at all says so", cc.search("zach", "zebra xylophone").startswith("Nothing in your 3 Claude conversations"), True)
check("someone with none is told how to send them", "Export data" in cc.search("p_none", "football"), True)
check("by source", "Claude chats on titan" in cc.search("zach", "titan", source="claude-code")
      and "lentil" not in cc.search("zach", "soup titan", source="claude-code"), True)
check("reading another's id: not yours", cc.read("p_mir", "claude-code:s-1"), "No conversation of yours has that id.")
r = cc.read("zach", "claude-ai:c-ff")
check("reading one: title, where, the words", (r.startswith("“Fantasy football waiver picks” — claude.ai"), "Jaylen Wright" in r), (True, True))
long = {"conv_id": "long", "title": "Long one", "place": "claude.ai", "started": "", "updated": "",
        "messages": [{"role": "user", "text": "First: plan the garden."}] +
                    [{"role": "claude", "text": f"step {i} " + "x" * 900} for i in range(30)] +
                    [{"role": "user", "text": "Final: tomatoes by the fence."}]}
cc.store("zach", "claude-ai", [long])
r = cc.read("zach", "claude-ai:long")
check("a long one: its start, its end, and what's skipped",
      ("First: plan the garden." in r, "Final: tomatoes by the fence." in r, "messages in between" in r, len(r) < 7000),
      (True, True, True, True))
check("forget: dry run first", cc.forget("p_mir", "claude-ai"), {"owner": "p_mir", "source": "claude-ai", "conversations": 1, "removed": False})
cc.forget("p_mir", None, apply=True)
check("forget: gone, and only hers", ("Nothing" in cc.search("p_mir", "fantasy") or "Export data" in cc.search("p_mir", "fantasy"),
                                      "waiver" in cc.search("zach", "waiver")), (True, True))

print("\n── the tool ──")
store = hh.HouseholdStore(_TMP / "people.db")
hh.reset_store(store)
rob = store.add_person("Robin", "adult")["id"]
cc.store(rob, "claude-ai", [mir_conv])


def as_(role, pid=None, name=None):
    lib._TURN_IO.role = role
    lib._TURN_IO.person = {"id": pid, "name": name} if pid else None


T = lib.execute_tool
as_("adult", rob, "Robin")
out = T("search_claude_chats", {"query": "fantasy football"})
check("Robin's turn: hers", ("Fantasy football trade" in out, "waiver" in out.lower()), (True, False))
as_(None)
check("Zach's turn: his", "waiver picks" in T("search_claude_chats", {"query": "fantasy football"}), True)
check("reading by id", "Jaylen Wright" in T("search_claude_chats", {"conversation": "claude-ai:c-ff"}), True)
check("no query", T("search_claude_chats", {}), "Error: query (or conversation) is required.")
as_("household")
out = T("search_claude_chats", {"query": "football"})
check("a shared device is refused, sees nobody's", ("waiver" in out.lower() or "trade" in out.lower(), "Fantasy" in out), (False, False))
as_(None)
check("adults only", sorted(set(hh.TOOL_ROLES.get("search_claude_chats", ())) - {"admin"}), ["adult"])

print("\n── append: only new lines (#1140) ──")
ap = _TMP / "append.db"
cc.store("alex", "claude-code", [cc.condense_claude_code("whole", SESSION)], ap)
want = cc.read("alex", "claude-code:whole", path=ap).split("\n", 1)
splits_ok = []
for cut in range(len(SESSION) + 1):
    sid = f"cut-{cut}"
    a = cc.append_claude_code("alex", sid, 0, SESSION[:cut], ap)
    b = cc.append_claude_code("alex", sid, cut, SESSION[cut:], ap)
    got = cc.read("alex", f"claude-code:{sid}", path=ap).split("\n", 1)
    splits_ok.append((a["have"], b["have"], got[0].split("—")[0] == want[0].split("—")[0],
                      got[1:] == want[1:]) == (cut, len(SESSION), True, True))
check("any split, appended, reads the same as the whole session (title, words, PR note, order)", all(splits_ok), True)
three = [SESSION[:2], SESSION[2:6], SESSION[6:]]
n = 0
for part in three:
    n = cc.append_claude_code("alex", "three", n, part, ap)["have"]
check("three appends", cc.read("alex", "claude-code:three", path=ap).split("\n", 1)[1:], want[1:])
check("a gap → conflict with what titan holds, nothing changed",
      (cc.append_claude_code("alex", "three", n + 5, [L(type="user", message={"content": "late"})], ap),
       "late" in cc.read("alex", "claude-code:three", path=ap)), ({"conflict": True, "have": len(SESSION)}, False))
check("a resend of lines titan has → conflict too (no duplicates)",
      cc.append_claude_code("alex", "three", 2, SESSION[2:], ap), {"conflict": True, "have": len(SESSION)})
check("an unknown session holds 0", cc.append_claude_code("alex", "new", 3, SESSION, ap), {"conflict": True, "have": 0})
cc.store("alex", "claude-code", [{k: v for k, v in cc.condense_claude_code("legacy", SESSION).items()
                                  if k not in ("raw_lines", "state")}], ap)
check("a session stored before line counts → have 0", cc.append_claude_code("alex", "legacy", 4, SESSION[4:], ap)["have"], 0)
cc.append_claude_code("alex", "legacy", 0, SESSION, ap)
check("…and an append from 0 rewrites it whole (no duplicates)", cc.read("alex", "claude-code:legacy", path=ap).split("\n", 1)[1:],
      want[1:])
r1 = cc.store("alex", "claude-code", [cc.condense_claude_code("three", SESSION[:5])], ap)
check("replace still replaces (a rewrite or truncation), and resets the count",
      (r1["written"], cc.append_claude_code("alex", "three", 99, [], ap)["have"]), (1, 5))
check("…after which appends go on from there", cc.append_claude_code("alex", "three", 5, SESSION[5:], ap)["have"], len(SESSION))
before = cc.status("alex", ap)["sources"]["claude-code"]["conversations"]
check("lines with no words yet: counted, not a conversation",
      (cc.append_claude_code("alex", "quiet", 0, [SESSION[1], SESSION[2]], ap)["have"],
       cc.status("alex", ap)["sources"]["claude-code"]["conversations"]), (2, before))
cc.append_claude_code("alex", "quiet", 2, [SESSION[0]], ap)
check("…its first prompt still names it later", "Make titan able" in cc.read("alex", "claude-code:quiet", path=ap), True)
check("secrets in appended lines are never stored", "sk-ant-api03" in (ap.read_bytes().decode("utf-8", "ignore")), False)
check("another person's append never touches Alex's session",
      (cc.append_claude_code("robin", "three", len(SESSION), [L(type="user", message={"content": "robin's"})], ap),
       "robin's" in cc.read("alex", "claude-code:three", path=ap)), ({"conflict": True, "have": 0}, False))

print("\n── the API ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role):
    s_ = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    return {"X-Agent-Token": c.post("/v1/pair/claim", json={"code": s_["code"], "device_name": f"{name}'s iPhone",
                                                             "platform": "ios"}).json()["token"]}


SAB, AVA = pair("Jordan", "adult"), pair("Ava", "kid")
r = c.post("/v1/claude-chats/import", content=export_zip(EXPORT[:1]), headers={**SAB, "Content-Type": "application/zip"})
check("an adult imports her export", (r.status_code, r.json().get("written"), r.json().get("conversations")), (200, 1, 1))
check("… it's hers", (c.get("/v1/claude-chats", headers=SAB).json()["sources"]["claude-ai"]["conversations"]), 1)
sab_id = st.people()[-2]["id"] if st.people()[-2]["name"] == "Jordan" else next(p["id"] for p in st.people() if p["name"] == "Jordan")
check("… and searched only as her", "waiver" in cc.search(sab_id, "waiver"), True)
r = c.post("/v1/claude-chats/sessions", json={"sessions": [{"session_id": "s-9", "lines": SESSION}]}, headers=SAB)
check("Claude Code sessions from her Mac", (r.status_code, r.json()["written"]), (200, 1))
tail = {"session_id": "s-9", "mode": "append", "lines": [L(type="user", timestamp="2026-10-01T06:00:00Z",
                                                              message={"content": "one more question about the waiver"})]}
r = c.post("/v1/claude-chats/sessions", json={"sessions": [{**tail, "from_line": len(SESSION)}]}, headers=SAB)
check("append from where titan is", (r.status_code, r.json().get("appended")), (200, 1))
r = c.post("/v1/claude-chats/sessions", json={"sessions": [{**tail, "from_line": 3}]}, headers=SAB)
check("a gap → 409 with what titan holds", (r.status_code, r.json().get("have"), r.json().get("conflicts")),
      (409, len(SESSION) + 1, [{"session_id": "s-9", "have": len(SESSION) + 1}]))
r = c.post("/v1/claude-chats/sessions", json={"sessions": [{**tail, "from_line": len(SESSION) + 1},
                                                           {**tail, "session_id": "s-10", "from_line": 7}]}, headers=SAB)
check("a batch: the good one lands, the gap is listed", (r.status_code, r.json().get("appended"), r.json().get("conflicts")),
      (409, 1, [{"session_id": "s-10", "have": 0}]))
for bad in ({**tail, "from_line": "3"}, {**tail, "from_line": -1}, {**tail, "from_line": True}, {**tail, "from_line": 0, "session_id": ""},
            {**tail}):
    check(f"a bad append → 400 ({str(bad.get('from_line'))[:6]})",
          c.post("/v1/claude-chats/sessions", json={"sessions": [bad]}, headers=SAB).status_code, 400)
check("a kid can't append", c.post("/v1/claude-chats/sessions", json={"sessions": [{**tail, "from_line": 0}]},
                                   headers=AVA).status_code, 403)
check("too many sessions → 400", c.post("/v1/claude-chats/sessions", json={"sessions": [{"session_id": str(i), "lines": []}
                                                                                       for i in range(51)]}, headers=SAB).status_code, 400)
check("not an export → 400", c.post("/v1/claude-chats/import", content=b'{"x": 1}', headers=SAB).status_code, 400)
check("a kid can't send or read", (c.post("/v1/claude-chats/import", content=export_zip(EXPORT), headers=AVA).status_code,
                                   c.get("/v1/claude-chats", headers=AVA).status_code), (403, 403))
check("forget her own: dry run, then apply", (c.delete("/v1/claude-chats?source=claude-code", headers=SAB).json()["removed"],
                                              c.delete("/v1/claude-chats?source=claude-code&apply=true", headers=SAB).json()["removed"]),
      (False, True))
check("bad source → 400", c.delete("/v1/claude-chats?source=gmail", headers=SAB).status_code, 400)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
