#!/usr/bin/env python3
# gate: hermetic   (temp dirs only; never starts a box)
"""scripts/sandbox_box.sh: the copy step takes every memory file (persona.md too, #770) and WAL-safe sqlite copies;
the script refuses the live port and checks its syntax.
Run: python3 scripts/test_sandbox_box.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sandbox_copy as sc  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


root = Path(tempfile.mkdtemp())
live, mem, box = root / "connectors", root / "memory", root / "box"
(live / "world").mkdir(parents=True)
mem.mkdir()
box.mkdir()
db = sqlite3.connect(live / "todos.db")
db.execute("PRAGMA journal_mode=WAL")
db.execute("CREATE TABLE t (x)")
db.execute("INSERT INTO t VALUES ('only in the WAL so far')")
db.commit()   # still open: the row sits in todos.db-wal, not yet checkpointed
(live / "household.json").write_text("{}")
sqlite3.connect(live / "world" / "p_1.db").close()
sqlite3.connect(mem / "titan_memory.db").close()
(mem / "persona.md").write_text("persona text")
(mem / "notes.txt").write_text("another memory file")

copied = sc.copy_all(str(box), str(live), str(mem))
check("every memory file, persona.md included", sorted(copied["memory"]), ["notes.txt", "persona.md", "titan_memory.db"])
check("no WAL/SHM side files", sorted(n for k in copied.values() for n in k if n.endswith(("-wal", "-shm"))), [])
check("the WAL's row is in the copy (backup API)",
      sqlite3.connect(box / "stores" / "todos.db").execute("SELECT x FROM t").fetchall(), [("only in the WAL so far",)])
check("non-db stores and world files too", ("household.json" in copied["stores"], "p_1.db" in copied["world"]), (True, True))
check("cards, schedules and pushes start empty", all((box / "stores" / n).exists() for n in sc.EMPTY_STORES), True)
db.close()

sc.main([str(box), str(live), str(mem), "7773", "abc1234", "on", "on"])
m = json.loads((box / "manifest.json").read_text())
check("manifest: switches and what was copied", (m["port"], m["code"], m["profile"], m["jev"], m["cloud"],
      "persona.md" in m["copied"]["memory"]), (7773, "abc1234", "live", "on", "on", True))
sc.main([str(box), str(live), str(mem), "7774", "abc1234", "off", "off", "box"])
check("manifest: the box profile is recorded", json.loads((box / "manifest.json").read_text())["profile"], "box")

sh = str(Path(__file__).with_name("sandbox_box.sh"))
check("the script parses", subprocess.run(["bash", "-n", sh]).returncode, 0)
r = subprocess.run(["bash", sh, "up", "7770"], capture_output=True, text=True)
check("refuses the live port", (r.returncode, "7770 is live" in r.stderr), (2, True))
src = Path(sh).read_text()
check("every background process detaches stdin/stdout (or `up | tail` hangs)",
      src.count("< /dev/null") >= 3, True)
check("down stops by recorded PID, never by pattern", ("pkill" in src, "pgrep" in src), (False, False))
check("--profile box sets no Jev and no Gemini", 'box) jev=off cloud=off' in src, True)
check("every profile: a dummy HA token, discovery and autonomy off",
      ('stub-token-not-real' in src, "TITAN_DISCOVERY=0" in src, "--no-discovery" in src, "AGENT_TASK_AUTONOMY=0" in src),
      (True, True, True, True))
r = subprocess.run(["bash", sh, "up", "7773", "--profile", "nope"], capture_output=True, text=True)
check("an unknown profile is refused", r.returncode, 2)

print("── every box mode keeps the memory DB inside the box (#1464) ──")
import re  # noqa: E402
box_sh = (Path(__file__).parent / "sandbox_box.sh").read_text()
parser = box_sh[box_sh.index("while [ $# -gt 0 ]; do"):]
parser = parser[:parser.index("done")]
modes = sorted(set(re.findall(r"^\s+(--[a-z-]+)\)", parser, re.M)))
check("the modes the script accepts (a new one must be thought about here)", modes,
      ["--cloud", "--code", "--fe-graph", "--first-run", "--fixture", "--hot", "--jev", "--no-watchdog", "--profile"])
launch = box_sh[box_sh.index("launch=(env "):box_sh.index("  bwrap --dev-bind")]
pin = re.search(r'MEMORY_DB="(\$B/[^"]+)"', launch)
check("the launch pins MEMORY_DB inside the box, for every mode", pin.group(1) if pin else None, "$B/mem/titan_memory.db")
check("…after the modes' own settings, so no mode can undo it",
      "MEMORY_DB=" in launch and launch.index('"${extra[@]}"') < launch.index("MEMORY_DB="), True)
check("…next to AGENT_MEMORY_DIR, the same folder", 'AGENT_MEMORY_DIR="$B/mem"' in launch, True)
outside = [m for m in re.findall(r'MEMORY_DB=("?[^ )\n]+)', box_sh) if not m.strip('"').startswith("$B/")]
check("no mode sets MEMORY_DB anywhere but the box", outside, [])

print("── the modules that read the memory DB follow the box's memory dir (#1464) ──")
here = Path(__file__).parent
for mod in ("chat_links", "miss_proposals"):
    for label, env, want in (("AGENT_MEMORY_DIR set (a box)", {"AGENT_MEMORY_DIR": str(root / "boxmem")},
                              str(root / "boxmem" / "titan_memory.db")),
                             ("MEMORY_DB set: it wins", {"AGENT_MEMORY_DIR": str(root / "boxmem"), "MEMORY_DB": str(root / "x.db")},
                              str(root / "x.db")),
                             ("neither (titan, as live runs): the same file as before", {},
                              str(Path.home() / ".config/jellybean/memory/titan_memory.db"))):
        e = {k: v for k, v in os.environ.items() if k not in ("AGENT_MEMORY_DIR", "MEMORY_DB")}
        e.update(env)
        r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(here)!r}); import {mod}; print({mod}.MEMORY_DB)"],
                           env=e, capture_output=True, text=True, timeout=60)
        check(f"{mod}: {label}", r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:], want)
hard = [p.name for p in here.glob("*.py") if not p.name.startswith("test_") and p.name != "agent_paths.py"
        and re.search(r"""["'][^"'\n]*\.config/jellybean/memory[^"'\n]*["']""", p.read_text())]   # a string, not a comment
check("no module but agent_paths hard-codes the live memory path", sorted(hard), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
