#!/usr/bin/env python3
# gate: hermetic
"""household_reset.py on a fake house: the dry run's actions per store and table, the person-made and unlisted rows
kept, the device rows wiped by default (kept with --keep-device-items), --apply refused while a pass runs, the backup
taken before anything is wiped, the KB and records deleted through their runtimes, the ledger epoch bumped last.
Temporary stores; postgres and the runtimes are stand-ins. Run: python3 scripts/test_household_reset.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-reset-"))
STORES = _TMP / "connectors"
STORES.mkdir()
os.environ["RESET_STORES_DIR"] = str(STORES)
os.environ["RESET_BACKUP_DIR"] = str(_TMP / "backups")
os.environ["CONNECTORS_LEDGER"] = str(STORES / "ledger.db")
os.environ["KB_ACL_DB"] = str(_TMP / "kb_acl.db")
sys.path.insert(0, str(Path(__file__).resolve().parent))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def make(rel, tables):
    p = STORES / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    for t, (cols, rows) in tables.items():
        con.execute(f"CREATE TABLE {t} ({', '.join(cols)})")
        con.executemany(f"INSERT INTO {t} VALUES ({','.join('?' * len(cols))})", rows)
    con.commit()
    con.close()


make("proactive.db", {"cards": (["id"], [("c1",), ("c2",)]), "card_mutes": (["k"], [("m",)]),
                      "triggers": (["id"], [("t",)]), "brand_new": (["x"], [(1,)])})
make("mail_bills.db", {"cards": (["id"], [("b",)]), "seen": (["id"], [("s",)]),
                       "obligation_claims": (["id"], [("oc-1",)])})
make("links.db", {"hooks": (["item_id"], [("mail-1",), ("dev-ab",)]), "links": (["a"], [("x",)]),
                  "link_statements": (["a"], [("said",)])})
import connectors_runner as cx  # noqa: E402  (the real ledger schema)
_led = cx._db(STORES / "ledger.db")
for src, sid in (("calendar", "e1"), ("mail", "m1"), ("github_stars", "g1")):
    _led.execute("INSERT INTO items (source, source_id, content_hash, item_id, synced_at, owner, upstream_id) "
                 "VALUES (?,?,?,?,?,?,?)", (src, sid, "h", f"cx-{src}-{sid}", 0, "robin", sid))
_led.execute("INSERT INTO versions (source, source_id, item_id, change, at) VALUES ('calendar','e1','cx-calendar-e1','created',0)")
_led.execute("INSERT INTO series (source, series_key, at) VALUES ('calendar','k',0)")
_led.execute("INSERT INTO tombstones VALUES ('robin','mail','m0',0)")
_led.execute("INSERT INTO sources_off VALUES ('robin','browsing',0)")
_led.commit()
_led.close()
make("world/zach.db", {"claims": (["id"], [("w1",), ("w2",)]), "note_reviews": (["id"], [("r",)]),
                       "merges": (["id"], [("m",)])})
make("household.db", {"people": (["id"], [("robin",)])})
make("todos.db", {"todos": (["id"], [("t1",)])})

import household_reset as R  # noqa: E402

PG = {"retrieval": [["connector:calendar", "3"], ["note", "2"], ["knowledge_record", "4"]],
      "queries": [["retrieval_queries", "9"]],
      "nodes": [["appointment", "calendar", "cx-calendar-", "5"], ["bill", "mail", "", "2"], ["entity", "-", "", "7"],
                ["github_star", "github_stars", "", "3"], ["smoke", "-", "", "1"]],
      "ids_kb": [["cx-calendar-aa"], ["cx-calendar-bb"]], "ids_nodes": [["cx-calendar-cc"], ["bill-mail-dd"]]}


def fake_psql(unit, sql):
    if "retrieval_queries" in sql:
        return PG["queries"]
    if "GROUP BY 1 ORDER BY 1" in sql and "retrieval_sources" in sql:
        return PG["retrieval"]
    if "FROM nodes GROUP BY" in sql:
        return PG["nodes"]
    if "SELECT source_id FROM retrieval_sources" in sql:
        return PG["ids_kb"]
    if "SELECT node_id FROM nodes" in sql:
        return PG["ids_nodes"]
    return []


R.psql = fake_psql
deleted = []
R._runtime_delete = lambda service, path: deleted.append((service, path)) or True
import kb_privacy  # noqa: E402
dropped = []
kb_privacy.drop_acl = lambda sid: dropped.append(sid)
dumps = []
R.pg_dump = lambda dest: dumps.append(dest)


def act(rows, store, table):
    return next(((r["action"], r["rows"]) for r in rows if r["store"] == store and r["table"] == table), None)


print("── the dry run (devices resend: the default) ──")
rows = R.dry_run(True)
check("derived cards wiped; mutes and triggers kept", (act(rows, "proactive.db", "cards"), act(rows, "proactive.db", "card_mutes"),
                                                     act(rows, "proactive.db", "triggers")), (("wipe", 2), ("keep", 1), ("keep", 1)))
check("a table the plan doesn't name is kept", act(rows, "proactive.db", "brand_new"), ("keep", 1))
check("a person's word on a bill is kept", act(rows, "mail_bills.db", "obligation_claims"), ("keep", 1))
check("link corrections kept; every hook wiped when devices resend", (act(rows, "links.db", "link_statements"),
                                                                       act(rows, "links.db", "hooks")), (("keep", 1), ("wipe", 2)))
check("ledger: the device sources' rows wiped, the rest and the tombstones kept",
      (act(rows, "ledger.db", "items"), act(rows, "ledger.db", "tombstones"), act(rows, "ledger.db", "sources_off")),
      (("wipe", 2), ("keep", 1), ("keep", 1)))
check("world: derived claims wiped, Zach's own reviews and merges kept",
      (act(rows, "world/zach.db", "claims"), act(rows, "world/zach.db", "note_reviews"), act(rows, "world/zach.db", "merges")),
      (("wipe", 2), ("keep", 1), ("keep", 1)))
check("whole stores on the keep list are kept", (act(rows, "household.db", "*"), act(rows, "todos.db", "*")),
      (("keep", 1), ("keep", 1)))
check("KB: device copies wiped, notes and runbook seeds kept", (act(rows, "postgres retrieval_runtime", "retrieval_sources[connector:calendar]"),
      act(rows, "postgres retrieval_runtime", "retrieval_sources[note]"),
      act(rows, "postgres retrieval_runtime", "retrieval_sources[knowledge_record]")), (("wipe", 3), ("keep", 2), ("keep", 4)))
check("records: device, derived, stars and test junk wiped; sourceless entities kept",
      [r["action"] for r in rows if r["store"] == "postgres record_runtime"], ["wipe", "wipe", "keep", "wipe", "wipe"])
check("counts only: nothing in a row is a value from a store", all(set(r) <= {"store", "table", "action", "rows", "note", "where"}
                                                                    for r in rows), True)

print("── --keep-device-items ──")
keep = R.dry_run(False)
check("device rows kept: ledger, the device's hooks, the KB copies, the device records",
      (act(keep, "ledger.db", "items")[0], act(keep, "links.db", "hooks"),
       act(keep, "postgres retrieval_runtime", "retrieval_sources[connector:calendar]")[0],
       next(r["action"] for r in keep if "cx-calendar-" in r["table"])), ("keep", ("wipe", 1), "keep", "keep"))

print("── --apply ──")
R.running_units = lambda units=R.MUST_BE_STOPPED, state=None: ["titan-mail-bills.timer"]
try:
    R.apply(True)
    check("refused while a pass is running", "applied", "refused")
except SystemExit as exc:
    check("refused while a pass is running, naming it", "titan-mail-bills.timer" in str(exc), True)
check("…and nothing was wiped or backed up", (sqlite3.connect(STORES / "proactive.db").execute("SELECT COUNT(*) FROM cards")
                                              .fetchone()[0], dumps), (2, []))
before = cx.ledger_epoch()
R.running_units = lambda units=R.MUST_BE_STOPPED, state=None: []
done = R.apply(True)
backup_dirs = list((_TMP / "backups").glob("pre-reset-*"))
check("a named backup first: SQLite copies plus the postgres dumps", (len(backup_dirs), len(dumps),
      (backup_dirs[0] / "proactive.db").exists() if backup_dirs else False), (1, 1, True))
check("…the backup still holds what was wiped", sqlite3.connect(backup_dirs[0] / "proactive.db").execute(
    "SELECT COUNT(*) FROM cards").fetchone()[0], 2)
check("the backup folder is private", oct(backup_dirs[0].stat().st_mode & 0o777), "0o700")
c = lambda rel, t: sqlite3.connect(STORES / rel).execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
check("wiped: cards, hooks, the device ledger rows, world claims", (c("proactive.db", "cards"), c("links.db", "hooks"),
      c("ledger.db", "items"), c("world/zach.db", "claims")), (0, 0, 1, 0))
check("kept: mutes, claims, statements, tombstones, reviews, people", (c("proactive.db", "card_mutes"),
      c("mail_bills.db", "obligation_claims"), c("links.db", "link_statements"), c("ledger.db", "tombstones"),
      c("world/zach.db", "note_reviews"), c("household.db", "people")), (1, 1, 1, 1, 1, 1))
check("KB and records deleted through their runtimes, each KB copy with its sharing row",
      (sorted(p.rsplit("/", 1)[-1] for s, p in deleted if s == "retrieval"), dropped,
       sorted(p.rsplit("/", 1)[-1] for s, p in deleted if s == "record")),
      (["cx-calendar-aa", "cx-calendar-bb"], ["cx-calendar-aa", "cx-calendar-bb"], ["bill-mail-dd", "cx-calendar-cc"]))
check("the ledger epoch bumped last, so the Macs resend", (done.get("ledger_epoch_bumped"), cx.ledger_epoch() != before),
      (1, True))

print("── the postgres dumps ──")
check("retrieval_runtime's dump leaves out the okb_* tables its user can't read (empty, never reset)",
      R.pg_dump_cmd("retrieval_runtime", Path("/x/r.pgdump")), ["pg_dump", "-Fc", "-f", "/x/r.pgdump", "--exclude-table=okb_*"])
check("record_runtime's dump is whole", R.pg_dump_cmd("record_runtime", Path("/x/n.pgdump")), ["pg_dump", "-Fc", "-f", "/x/n.pgdump"])
check("the excluded tables' row counts are logged (pg_stat), counts only",
      R.excluded_counts("retrieval_runtime", "u", pg=lambda unit, sql: [["okb_chunks", "0"], ["okb_namespaces", "0"]]),
      {"okb_chunks": 0, "okb_namespaces": 0})
check("…none for an undumped name", R.excluded_counts("record_runtime", "u", pg=lambda *a: 1 / 0), {})
check("…a failure to tell is None, never an exception",
      R.excluded_counts("retrieval_runtime", "u", pg=lambda *a: 1 / 0), {"okb_*": None})
check("the reset never touches an excluded table", any("okb_" in r["table"] for r in rows), False)

print("── units checked ──")
check("the agent API and the passes must be stopped", {"titan-agent-api.service", "titan-mail-server.timer",
                                                        "titan-mail-bills.timer"} <= set(R.MUST_BE_STOPPED), True)
states = {"titan-agent-api.service": "inactive", "titan-mail-bills.timer": "inactive", "titan-mail-bills.service": "activating"}
import importlib  # noqa: E402
R2 = importlib.reload(__import__("household_reset"))
check("a timer stopped but its service mid-run still counts", R2.running_units(("titan-agent-api.service", "titan-mail-bills.timer"), state=lambda u: states.get(u, "inactive")),
      ["titan-mail-bills.service"])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
