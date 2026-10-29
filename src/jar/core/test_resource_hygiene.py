#!/usr/bin/env python3
# gate: hermetic
"""Resource hygiene (the 10-10 review): what titan opens it closes, what it keeps it bounds, set-up runs once.
  - a failed turn's token counts leave _session_token_stats (they leaked one entry per failed turn)
  - links.db's mode, WAL and schema are set once per process and file, not on every connect; the linker's schema too;
    _db_key counts again only after a commit to the file; the hooks(source) and chat_links(owner, type) indexes exist
  - generated pictures past GENERATED_TTL_S go, file and row; newer ones and other files stay
  - file_read streams a page of a file (never the whole file in memory) and cuts a page of enormous lines, with the
    same notes and pages as before
  - /health's memory turn count is counted off the event loop and kept HEALTH_TURNS_TTL_S
  - turns run on their own pool (_TURN_POOL), in a copy of the caller's context, as asyncio.to_thread ran them
  - the grow-only maps are pruned: session approvals, the speech rate limiter, the Mac inbox's known set
Fictional people only; every store is a throwaway temp dir.
Run: TZ=UTC python3 scripts/test_resource_hygiene.py"""
from __future__ import annotations

import asyncio
import contextvars
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-res-hygiene-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "LINKS_DB": "links.db",
             "LINK_SALT_FILE": "link_salt", "GENERATED_DIR": "generated", "GENERATED_IMAGES_DB": "generated_images.db",
             "UPLOADS_DIR": "uploads", "UPLOADS_DB": "uploads.db", "AGENT_SPEECH_DIR": "speech"}.items():
    os.environ[k] = str(_TMP / v)
(_TMP / "generated").mkdir()
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import agent_api_server as api  # noqa: E402
import chat_links  # noqa: E402
import cross_device  # noqa: E402
import hooks  # noqa: E402
import household_identity as hh  # noqa: E402
import linker  # noqa: E402

lib = api._lib
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


# ── a failed turn's token counts don't stay behind ──────────────────────────────────────────────────────────────
print("── token counts of a failed turn ──")
alex = hh.owner_identity()


def build_and_count(question, history, tools, ws_id, turn_id=None, **_k):
    api._session_token_stats[turn_id] = api._TokenCounts(estimated_input_chars=10, recall_injected_chars=0,
                                                          history_avoided_chars=28)   # 7 tokens saved
    return [{"role": "user", "content": question}], 0


def boom(messages):
    raise BrokenPipeError("[Errno 32] Broken pipe")


saved_build, saved_run, saved_mem = api._build_messages, lib._run_agent_turn, api._get_memory
api._build_messages, lib._run_agent_turn = build_and_count, boom
api._get_memory = lambda: None
api._turns["t-fail"] = {"source": "eval", "remember": False, "identity": alex}
before = api._token_savings_done
try:
    api._run_turn("t-fail", "What's on Robin's calendar?", [], True, None)
except BrokenPipeError:
    pass
check("the failed turn's entry is gone", "t-fail" in api._session_token_stats, False)
check("…and its savings still count in /health's total, as they did while they leaked",
      api._token_savings_done - before, 7)
lib._run_agent_turn = lambda messages: "Robin has swim at 4."
api._turns["t-ok"] = {"source": "eval", "remember": False, "identity": alex}
before = api._token_savings_done
check("a finished turn answers as before", api._run_turn("t-ok", "And Leo?", [], True, None), "Robin has swim at 4.")
check("…its entry gone and counted once", ("t-ok" in api._session_token_stats, api._token_savings_done - before),
      (False, 7))
check("…and its token counts are on the turn", api._turns["t-ok"].get("token_counts", {}).get("estimated_savings_tokens"),
      7)
api._build_messages, lib._run_agent_turn, api._get_memory = saved_build, saved_run, saved_mem


# ── links.db: set up once per process and file ─────────────────────────────────────────────────────────────────
print("── links.db set-up once ──")
traced: list[str] = []
_real_connect = sqlite3.connect


def tracing_connect(*a, **k):
    con = _real_connect(*a, **k)
    con.set_trace_callback(traced.append)
    return con


sqlite3.connect = tracing_connect
try:
    db1 = _TMP / "links-a" / "links.db"
    for _ in range(3):
        hooks.connect(db1).close()
    schema_runs = sum(1 for s in traced if "CREATE TABLE IF NOT EXISTS hooks" in s)
    wal_runs = sum(1 for s in traced if "journal_mode" in s)
    check("three connects to one file: the schema and WAL pragma ran once", (schema_runs, wal_runs), (1, 1))
    check("the file is private (0640)", oct(db1.stat().st_mode & 0o777), "0o640")
    traced.clear()
    db2 = _TMP / "links-b" / "links.db"
    hooks.connect(db2).close()
    check("another file is set up on its own", sum(1 for s in traced if "CREATE TABLE IF NOT EXISTS hooks" in s), 1)
    traced.clear()
    db2.unlink()
    for side in ("-wal", "-shm"):
        Path(str(db2) + side).unlink(missing_ok=True)
    con = hooks.connect(db2)
    check("a file deleted and made again is set up again (tables there)",
          con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='hooks'").fetchone()[0], 1)
    con.close()
    # ext4 hands a deleted file's inode straight back (the gate on titan): a remembered key on an empty file
    db3 = _TMP / "links-c" / "links.db"
    db3.parent.mkdir()
    _real_connect(db3).execute("CREATE TABLE unrelated (x)").connection.close()   # made by another opener
    k3 = hooks._file_key(str(db3))
    hooks._READY.add(k3)
    hooks._OPENED.add(k3)
    con = hooks.connect(db3)
    check("a remembered file whose schema isn't there (a reused inode) is set up again",
          (con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='hooks'").fetchone()[0],
           con.execute("PRAGMA journal_mode").fetchone()[0]), (1, "wal"))
    linker._READY.add(k3)
    check("…and the linker's tables too", (linker.statements(con), con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='link_log'").fetchone()[0]), ({}, 1))
    con.close()
    traced.clear()
    con = hooks.connect(db1)
    linker.statements(con)
    linker.statements(con)
    check("the linker's schema: once per file", sum(1 for s in traced if "CREATE TABLE IF NOT EXISTS links" in s), 1)
    con.close()
    mem = sqlite3.connect(":memory:")
    hooks.ensure(mem)
    hooks.ensure(mem)
    check("an in-memory database is always set up (no file to remember)",
          mem.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='hooks'").fetchone()[0], 1)
finally:
    sqlite3.connect = _real_connect

print("── the indexes ──")
con = hooks.connect(db1)
idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
check("hooks(source) is indexed", "idx_hooks_source" in idx, True)
plan = " ".join(str(r) for r in con.execute("EXPLAIN QUERY PLAN SELECT item_id FROM hooks WHERE source=?", ("chat",)))
check("…and a by-source read uses it", "idx_hooks_source" in plan, True)


def plan_of(q, *args):
    return " ".join(str(r) for r in con.execute("EXPLAIN QUERY PLAN " + q, args))


check("the selective reads keep their own indexes (a source's two items; a day's text hooks)",
      ("item_id=?" in plan_of("SELECT DISTINCT item_id FROM hooks WHERE source=? AND owner=? AND item_id IN (?,?)",
                              "mail", "p_alex", "a", "b"),
       "idx_hooks_key" in plan_of("SELECT COUNT(DISTINCT item_id) FROM hooks WHERE source='text' AND kind='date:item' "
                                  "AND key=?", "k")), (True, True))
chat_links.ensure(con)
cols = [r[2] for r in con.execute("PRAGMA index_info(idx_chat_links_owner)")]
check("chat_links(owner, type) is indexed", cols, ["owner", "type"])
con.close()

print("── _db_key counts only after a commit ──")
con = hooks.connect(db1)
hooks.index(con, "mail-1", "mail", [hooks.Hook("party:x", "k1", None, {})], "p_alex", "private")
hooks.index(con, "mail-2", "mail", [hooks.Hook("party:x", "k2", None, {})], "p_robin", "private")
counts: list[str] = []
con.set_trace_callback(lambda s: counts.append(s) if "COUNT(*)" in s else None)
k1 = linker._db_key(con)
k2 = linker._db_key(con)
# two counts a key here (hooks, link_statements; no topics table in this file)
check("unchanged: the same key, counted once", (k1 == k2, len(counts)), (True, 2))
other = _real_connect(db1)
other.execute("INSERT INTO link_statements (a, b, related, by, at) VALUES ('mail-1', 'mail-2', 1, 'p_alex', 0)")
other.commit()
k3 = linker._db_key(con)
check("another connection's commit: counted again, a new key", (k3 != k2, len(counts)), (True, 4))
first = other.execute("SELECT MIN(rowid) FROM hooks").fetchone()[0]
other.execute("DELETE FROM hooks WHERE rowid=?", (first,))   # not the highest rowid: only the count shows it
other.commit()
k4 = linker._db_key(con)
check("a delete is seen (the count changed)", k4 != k3, True)
fresh = _real_connect(db1)
check("…and the key is what counting gives", k4[1:], tuple(
    v for t in ("hooks", "link_statements", "topics")
    for v in (fresh.execute(f"SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM {t}").fetchone()
              if t != "topics" else (None, None))))
con.execute("INSERT INTO hooks (item_id, source, kind, key, made_at) VALUES ('mail-3', 'mail', 'k', 'v', 0)")
k5 = linker._db_key(con)
check("this connection's own unsaved row is counted (never the cached key)", k5 != k4, True)
con.rollback()
for c in (con, other, fresh):
    c.close()


# ── generated pictures: swept after their time ───────────────────────────────────────────────────────────────
print("── generated pictures ──")
now = time.time()
gen = Path(lib.GENERATED_DIR)
old, new, other_file = gen / "generated-0000000a.png", gen / "generated-0000000b.png", gen / "notes-ava.png"
for f in (old, new, other_file):
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
long_ago = now - lib.GENERATED_TTL_S - 3600
os.utime(old, (long_ago, long_ago))
os.utime(other_file, (long_ago, long_ago))
lib.IMAGES_DB.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(lib.IMAGES_DB) as c:
    c.execute("CREATE TABLE IF NOT EXISTS images (name TEXT PRIMARY KEY, owner TEXT NOT NULL, ts REAL NOT NULL)")
    c.execute("INSERT INTO images VALUES (?,?,?)", (old.name, "p_jordan", long_ago))
    c.execute("INSERT INTO images VALUES (?,?,?)", (new.name, "p_jordan", now - 86400))
check("the sweep removes one file", lib.sweep_generated(now), 1)
check("the old picture is gone, the newer one and other files stay",
      (old.exists(), new.exists(), other_file.exists()), (False, True, True))
check("the old row is gone, the newer one stays", (lib.generated_owner(old.name), lib.generated_owner(new.name)),
      (None, "p_jordan"))
check("the TTL is 30 days (the chat history links them)", lib.GENERATED_TTL_S, 30 * 86400)
check("no images db yet: nothing to do, nothing made",
      (lambda saved: (setattr(lib, "IMAGES_DB", _TMP / "none" / "x.db"), lib.sweep_generated(now),
                      (_TMP / "none").exists(), setattr(lib, "IMAGES_DB", saved))[1:3])(lib.IMAGES_DB), (0, False))


# ── file_read: streamed, the same pages ───────────────────────────────────────────────────────────────────────
print("── file_read ──")
lib._TURN_IO.person, lib._TURN_IO.role, lib._TURN_IO.source = {"id": "p_hale", "name": "Hale"}, "admin", "user"


def file_read(path, **args):
    return lib._execute_tool("file_read", {"path": str(path), **args})


def old_file_read(path, sl=None, el=None):   # the tool's pages before streaming, for comparison
    all_lines = open(path, "r", encoding="utf-8", errors="replace").readlines()
    total = len(all_lines)
    if sl is not None or el is not None:
        s = max(0, int(sl or 1) - 1)
        e = min(total, int(el or total))
        return f"[lines {s+1}–{min(e, total)} of {total}]\n" + "".join(all_lines[s:e])
    return ((f"[{total} lines total — showing first 2000; use start_line/end_line to page]\n" if total > 2000 else "")
            + "".join(all_lines[:2000]))


log = _TMP / "leo.log"
log.write_text("".join(f"line {i} for Leo\n" for i in range(1, 5001)) + "no newline at the end")
same = []
for sl, el in ((None, None), (1, 10), (4990, None), (None, 3), (4999, 6000), (7000, 7010), (10, -5), (0, 0), (3, 2)):
    args = {k: v for k, v in (("start_line", sl), ("end_line", el)) if v is not None}
    same.append(file_read(log, **args) == old_file_read(log, sl, el))
check("every page is what the tool gave before (first 2000, ranges, past the end, negative end)", same, [True] * 9)
real_readlines = []
_open = open


class Spy:
    def __init__(self, f):
        self.f = f

    def __enter__(self):
        self.f.__enter__()
        return self

    def __exit__(self, *a):
        return self.f.__exit__(*a)

    def readlines(self, *a):
        real_readlines.append(1)
        return self.f.readlines(*a)

    def __getattr__(self, n):
        return getattr(self.f, n)

    def __iter__(self):
        return iter(self.f)


import builtins  # noqa: E402
builtins.open = lambda *a, **k: Spy(_open(*a, **k))
try:
    file_read(log, start_line=2, end_line=3)
finally:
    builtins.open = _open
check("the file is never read whole (no readlines)", real_readlines, [])
wide = _TMP / "robin-minified.js"
wide.write_text("x" * (lib.FILE_READ_MAX_CHARS + 500_000))
out = file_read(wide)
check("a page of one enormous line is cut, and says so",
      (len(out) < lib.FILE_READ_MAX_CHARS + 200, "cut at 2,000,000 characters" in out), (True, True))
(_TMP / "empty.txt").write_text("")
check("an empty file reads as before", file_read(_TMP / "empty.txt"), old_file_read(_TMP / "empty.txt"))
big_doc = _TMP / "ava-scan.pdf"
with _open(big_doc, "wb") as f:
    f.truncate(lib.FILE_READ_DOC_MAX_BYTES + 1)
check("a document over the size limit is refused before extraction", file_read(big_doc).startswith("Error: "), True)


# ── /health's turn count: off the loop, kept 30 s ─────────────────────────────────────────────────────────────
print("── /health memory_turns ──")


class CountingMem:
    calls: list[str] = []

    def turn_count(self):
        CountingMem.calls.append(threading.current_thread().name)
        return 42


api._get_memory, saved_avail = (lambda: CountingMem()), api._MEMORY_AVAILABLE
api._MEMORY_AVAILABLE = True
api._health_turns.update(at=float("-inf"), n=0)
loop_thread = threading.current_thread().name


async def two_probes():
    a = await api._memory_turns()
    b = await api._memory_turns()
    return a, b


check("two probes: the count, asked once", (asyncio.run(two_probes()), len(CountingMem.calls)), ((42, 42), 1))
check("…not on the event loop's thread", CountingMem.calls[0] != loop_thread, True)
api._health_turns["at"] -= api.HEALTH_TURNS_TTL_S + 1
asyncio.run(api._memory_turns())
check("past the TTL it's counted again", len(CountingMem.calls), 2)
api._get_memory, api._MEMORY_AVAILABLE = saved_mem, saved_avail


# ── turns on their own pool ───────────────────────────────────────────────────────────────────────────────────
print("── the turn pool ──")
who_var: contextvars.ContextVar[str] = contextvars.ContextVar("who", default="nobody")
saved_turn = api._run_turn
api._run_turn = lambda *a: (threading.current_thread().name, who_var.get(), a)


async def run_one():
    who_var.set("Jordan")
    return await asyncio.get_running_loop().run_in_executor(api._TURN_POOL, api._turn_call("t-1", "q", [], True, None))


name, seen_who, got_args = asyncio.run(run_one())
check("a turn runs on a turn thread", name.startswith("turn_"), True)
check("…in a copy of the caller's context, as to_thread ran it", seen_who, "Jordan")
check("…with its arguments", got_args, ("t-1", "q", [], True, None))
check("the pool's size", api._TURN_POOL._max_workers, api.TURN_WORKERS)
api._run_turn = saved_turn


# ── grow-only maps ───────────────────────────────────────────────────────────────────────────────────────────
print("── grow-only maps ──")
clock = [1000.0]
ap = cross_device.Approvals(now=lambda: clock[0])
ap.grant("s-alex", "calendar")
clock[0] += cross_device.SESSION_APPROVAL_S + 1
ap.grant("s-robin", "mail")
check("an expired approval goes with the next grant", sorted(ap._until), [("s-robin", "mail")])
check("a live one still holds", ap.granted("s-robin", "mail"), True)
lib._SPEECH_CALLS.clear()
for i in range(70):
    lib.speech_allowed(f"dev-{i}", now=100.0)
lib.speech_allowed("dev-leo", now=200.0)
check("idle devices leave the speech limiter", sorted(lib._SPEECH_CALLS), ["dev-leo"])
check("the limit itself is unchanged", [lib.speech_allowed("dev-ava", now=300.0) for _ in range(lib.SPEECH_RATE + 1)][-2:],
      [True, False])
api._inbox_known.clear()
for i in range(api.INBOX_KNOWN_MAX + 5):
    api._inbox_note(("p_alex", f"mac-{i}"))
check("the inbox's known set is bounded", len(api._inbox_known) <= api.INBOX_KNOWN_MAX, True)
check("…and keeps the newest", ("p_alex", f"mac-{api.INBOX_KNOWN_MAX + 4}") in api._inbox_known, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
