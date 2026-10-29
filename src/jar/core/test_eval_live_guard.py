#!/usr/bin/env python3
"""The household eval writes made-up facts through the live agent (a fake wifi password, door codes).
10-01: a sweep of every test_*.py ran it under a 120 s alarm, killed it before its rollback, and left
9 fixture notes in Zach's knowledge base. These checks keep that from happening again. Hermetic."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


# 1. Without --live it does nothing (no snapshot, no ssh, no agent call) and exits 0.
p = subprocess.run([sys.executable, str(HERE / "test_household_agent.py")], capture_output=True, text=True, timeout=30)
check("runs nothing without --live", p.returncode == 0 and p.stdout.startswith("Not run"), p.stdout[-200:] + p.stderr[-200:])

# 2. The nightly passes --live.
check("the nightly passes --live", '"--live"' in (HERE / "household_eval_nightly.py").read_text())

# 3. A signal raises SystemExit, so the finally (rollback, then verify) runs.
src = (HERE / "test_household_agent.py").read_text()
check("SIGTERM/SIGALRM unwind to the rollback",
      "signal.SIGALRM" in src and "raise SystemExit" in src and src.index("signal.signal(sig, _stop)") < src.index("finally:"))
check("rollback, then verify", src.index('kb_hygiene("rollback"') < src.index('kb_hygiene("verify"'))

# 4. verify: OK when nothing new; LEFT (exit 1, snapshot kept) when an eval note remains; synced mail ignored.
import kb_hygiene as k  # noqa: E402

snap_srcs = [{"source_id": "a", "source_type": "note"}]
with tempfile.TemporaryDirectory() as d:
    sp = Path(d) / "snap.json"
    sp.write_text(json.dumps({"sources": snap_srcs, "records": [{"node_id": "n1"}]}))
    k.list_records = lambda: [{"node_id": "n1"}]
    k.list_sources = lambda: snap_srcs + [{"source_id": "mail1", "source_type": "connector:mail"}]
    check("verify passes with only synced mail new", k.verify(sp, remove=False) == 0 and sp.exists())
    k.list_sources = lambda: snap_srcs + [{"source_id": "fixture", "source_type": "note"}]
    check("verify fails on a leftover note and keeps the snapshot", k.verify(sp, remove=True) == 1 and sp.exists())
    k.list_sources = lambda: snap_srcs
    k.list_records = lambda: [{"node_id": "n1"}, {"node_id": "n2"}]
    check("verify fails on a leftover record", k.verify(sp, remove=True) == 1)
    k.list_records = lambda: [{"node_id": "n1"}]
    check("verify passes clean and removes the snapshot", k.verify(sp, remove=True) == 0 and not sp.exists())

# 5. rollback never deletes synced items that arrived during the eval.
deleted: list[str] = []
with tempfile.TemporaryDirectory() as d:
    sp = Path(d) / "snap.json"
    sp.write_text(json.dumps({"sources": [{"source_id": "a", "source_type": "note", "updated_at": 1, "text": "x"}]}))
    k.list_sources = lambda: [{"source_id": "a", "source_type": "note", "updated_at": 1},
                              {"source_id": "mail1", "source_type": "connector:mail", "updated_at": 2},
                              {"source_id": "fixture", "source_type": "note", "updated_at": 2}]
    k.lib._runtime_request = lambda rt, path, method="GET", body=None: deleted.append(path) or {}
    k.rollback(sp)
check("rollback deletes the eval's note, not synced mail",
      any(p.endswith("/fixture") for p in deleted) and not any("mail1" in p for p in deleted), str(deleted))

# 6. Records: only chat-made nodes are the eval's. The sync's nodes (provenance.source = a connector)
#    are real: a new one passes verify and is never deleted; a type at the 500 cap is left alone.
SYNC = {"node_id": "e-sync", "node_type": "entity", "data": {"provenance": {"source": "mail"}}}
CHAT = {"node_id": "c-eval", "node_type": "contact", "data": {"provenance": {"source": "chat", "owner": "zach"}}}
OLD = {"node_id": "c-old", "node_type": "contact", "data": {"name": "Vet", "provenance": {"source": "chat"}}}
with tempfile.TemporaryDirectory() as d:
    sp = Path(d) / "snap.json"
    sp.write_text(json.dumps({"sources": snap_srcs, "records": [OLD]}))
    k.list_sources = lambda: snap_srcs
    k.list_records = lambda: [OLD, SYNC]
    check("a node the sync added during the run passes verify", k.verify(sp) == 0)
    k.list_records = lambda: [OLD, SYNC, CHAT]
    check("a node the eval added fails verify", k.verify(sp) == 1)
    calls: list[tuple] = []
    k.lib._runtime_request = lambda rt, path, method="GET", body=None: calls.append((method, path)) or {}
    k.rollback_records({"records": [OLD]})
    dels = [p for m, p in calls if m == "DELETE"]
    check("rollback deletes the eval's node, not the sync's", dels == ["/v1/nodes/c-eval"], str(calls))

    calls.clear()
    k.list_records = lambda: [SYNC]          # OLD missing from the list…
    k.rollback_records({"records": [OLD]})   # …but GET finds it (the fake returns {}), so no re-create
    check("a node missing from a list but still there isn't re-created",
          not [p for m, p in calls if m == "POST"], str(calls))

# list_records marks a type at the cap partial, and _chat_made then leaves it alone.
pages = {"contact": [CHAT] * k._REC_PAGE}
k2 = __import__("importlib").reload(k)
k2.lib._runtime_request = lambda rt, path, method="GET", body=None: {
    "nodes": pages.get(path.split("node_type=")[1].split("&")[0], [])}
got = k2.list_records()
check("a type at the 500 cap is never acted on", got and not any(k2._chat_made(n) for n in got))

# 7. A snapshot left by a killed run blocks the next run until a person checks it.
with tempfile.TemporaryDirectory() as d:
    sp = Path(d) / "pending.json"
    sp.write_text("{}")
    check("a pending snapshot refuses a new one", k2.snapshot(sp) == 1 and sp.read_text() == "{}")
check("the eval's snapshot path is fixed", '"/ai/tmp/household-eval-pending.json"' in src)

# 7. kb_hygiene's CLI parses for every subcommand (10-01, #399: a duplicated --remove made every command,
#    and so tonight's eval snapshot, crash in argparse before doing anything).
for sub in ("scan", "apply", "restore", "snapshot", "rollback", "verify"):
    r = subprocess.run([sys.executable, str(HERE / "kb_hygiene.py"), sub, "--help"], capture_output=True, text=True, timeout=30)
    check(f"kb_hygiene {sub} --help parses", r.returncode == 0 and "usage:" in r.stdout, (r.stderr or r.stdout)[-200:])

# 8. test_household.py (#400) writes testhh bills, a KB note and a filed bill to titan's live stores.
#    It does nothing without --live (it used to run on import), runs only on titan, shares the eval's
#    lock, and rolls back then verifies like the eval.
hh = HERE / "test_household.py"
p = subprocess.run([sys.executable, str(hh)], capture_output=True, text=True, timeout=30, cwd=str(HERE))
check("test_household: runs nothing without --live", p.returncode == 0 and p.stdout.startswith("Not run")
      and "PASS" not in p.stdout and "FAIL" not in p.stdout, p.stdout[-200:] + p.stderr[-200:])
p = subprocess.run([sys.executable, str(hh), "--live"], capture_output=True, text=True, timeout=30, cwd=str(HERE),
                   env={k_: v for k_, v in __import__("os").environ.items() if k_ != "TITAN_REMOTE_EXEC"})
if not __import__("socket").gethostname().startswith("titan"):
    check("test_household: --live off titan refuses", p.returncode == 1 and "runs on titan" in p.stdout,
          p.stdout[-200:] + p.stderr[-200:])
hsrc = hh.read_text()
check("test_household: nothing runs at import (all writes inside _run)",
      "def _run(" in hsrc and '_runtime_request("record", "/v1/nodes", "POST"' in hsrc.split("def _run(")[1].split("def main(")[0]
      and '\nA._runtime_request' not in hsrc and "\nimport agent_runtime_lib" not in hsrc)
check("test_household: snapshot, then rollback, then verify, signals unwind to them",
      hsrc.index('kb_hygiene("snapshot"') < hsrc.index("\n    finally:\n") < hsrc.index('kb_hygiene("rollback"')
      < hsrc.index('kb_hygiene("verify"') and "signal.SIGALRM" in hsrc and "raise SystemExit" in hsrc)
check("test_household: shares the eval's lock and has its own fixed snapshot path",
      '"/tmp/household-eval.lock"' in hsrc and '"/ai/tmp/household-test-pending.json"' in hsrc
      and '"/tmp/household-eval.lock"' in src)

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all eval-live-guard checks passed'}")
sys.exit(1 if failures else 0)
