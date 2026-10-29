#!/usr/bin/env python3
# gate: hermetic
"""A box's factory reset (titan-node#411): console only, a dry run unless --erase; refuses off a box, without explicit
BOX_DATA_DIRS, with the agent API running, or with the backups inside a data folder; backs up first when the box has a
recovery key (and stops if that fails); empties the data folders (they stay), never follows a link out of them, and
re-opens setup with a new key. Everything in throwaway folders; the API check aims at spare local ports.
Run: python3 scripts/test_box_reset.py
"""
from __future__ import annotations

import contextlib
import http.server
import io
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-boxreset-")) / "deep" / "enough"   # past the depth guard on any host (/tmp is shallow on Linux)
_TMP.mkdir(parents=True)
DATA1, DATA2, OUTSIDE = _TMP / "box" / "stores", _TMP / "box" / "memory", _TMP / "outside"
for d in (DATA1, DATA2, OUTSIDE):
    d.mkdir(parents=True)
for k, v in {"HOUSEHOLD_DB": "box/stores/household.db", "BOX_SETUP_DIR": "box-setup",
             "BOX_BACKUP_PUB": "keys/pub.json", "BOX_BACKUP_SIGN_KEY": "keys/sign.key",
             "BOX_BACKUP_DIR": "backups", "BOX_BACKUP_STAGE": "stage"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["TITAN_PAIR_URLS"] = "http://10.0.0.9:7770"
os.environ["CONNECTORS_DIR"], os.environ["AGENT_MEMORY_DIR"] = str(DATA1), str(DATA2)   # a box: the backup's sources
os.environ.pop("BOX_DATA_DIRS", None)
os.environ.pop("BOX_SETUP", None)
sys.path.insert(0, str(Path(__file__).parent))
import box_backup  # noqa: E402
import box_setup  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


def spare_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


os.environ["AGENT_API_PORT"] = str(spare_port())   # nothing listens there: "the API is stopped"


def refused() -> str | None:
    try:
        box_setup.reset_plan()
        return None
    except box_setup.ResetRefused as exc:
        return str(exc)


def seed():
    import sqlite3
    (DATA1 / "household.db").unlink(missing_ok=True)
    con = sqlite3.connect(DATA1 / "household.db")   # a real store: the backup copies it with SQLite's backup API
    con.execute("CREATE TABLE people (id)")
    con.commit()
    con.close()
    (DATA1 / "sub").mkdir(exist_ok=True)
    (DATA1 / "sub" / "tasks.db").write_text("tasks")
    (DATA2 / "persona.md").write_text("persona")
    (OUTSIDE / "keep.txt").write_text("not the box's")
    link = DATA2 / "link-out"
    if not link.exists():
        link.symlink_to(OUTSIDE)


def cli(*args) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = box_setup.main(["box_setup.py", "factory-reset", *args])
    return rc, buf.getvalue()


seed()
print("── it refuses ──")
check("off a box (titan): refused", "this isn't a box" in (refused() or ""), True)
os.environ["BOX_SETUP"] = "1"
check("no BOX_DATA_DIRS: refused (there's no default)", "BOX_DATA_DIRS" in (refused() or ""), True)
os.environ["BOX_DATA_DIRS"] = "box/stores"
check("a relative folder: refused", "absolute" in (refused() or ""), True)
os.environ["BOX_DATA_DIRS"] = "/tmp"
check("a shallow folder like /tmp: refused", "absolute" in (refused() or ""), True)
os.environ["BOX_DATA_DIRS"] = f"{DATA1},{_TMP}"
check("a data folder that holds the backups: refused", "holds the backups" in (refused() or ""), True)
os.environ["BOX_DATA_DIRS"] = f"{DATA1},{DATA2}"


class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), Health)
threading.Thread(target=srv.serve_forever, daemon=True).start()
quiet = os.environ["AGENT_API_PORT"]
os.environ["AGENT_API_PORT"] = str(srv.server_address[1])
check("the agent API answering: refused", "stop it first" in (refused() or ""), True)
srv.shutdown()
os.environ["AGENT_API_PORT"] = quiet
check("…stopped: allowed", refused(), None)

print("── a dry run erases nothing ──")
rc, out = cli()
check("it lists each folder with counts and says dry run", (rc, str(DATA1.resolve()) in out, "Dry run" in out), (0, True, True))
check("…and every file is still there", ((DATA1 / "household.db").exists(), (DATA2 / "persona.md").exists()), (True, True))

print("── a file the backup wouldn't hold: refused, nothing erased (10-10, #1469 review) ──")
phrase = box_backup.init()
box_setup._done_path().parent.mkdir(parents=True, exist_ok=True)
rc, out = cli()
check("the dry run says so", ("1 files here aren't in a backup" in out, str((DATA1 / "sub").resolve()) in out), (True, True))
rc, out = cli("--erase")
check("--erase refuses: a file in a subfolder (a backup holds top-level files only)",
      (rc, "aren't in a backup" in out, (DATA1 / "sub" / "tasks.db").exists(), (DATA1 / "household.db").exists()),
      (1, True, True, True))
check("…and made no backup", list(Path(os.environ["BOX_BACKUP_DIR"]).glob("box-*.tbk")), [])
(DATA1 / "sub" / "tasks.db").unlink(missing_ok=True)
if (DATA1 / "sub").is_dir():
    (DATA1 / "sub").rmdir()
seed_mem = DATA2 / "persona.md"
if not seed_mem.exists():   # a mutant that let the erase run: put the box back so the rest still reads clearly
    seed()
    (DATA1 / "sub" / "tasks.db").unlink()
    (DATA1 / "sub").rmdir()
os.environ["CONNECTORS_DIR"] = str(_TMP / "not-the-stores")
rc, out = cli("--erase")
check("CONNECTORS_DIR pointing elsewhere: the stores aren't covered, refused (never an empty backup, then the erase)",
      (rc, "aren't in a backup" in out, (DATA1 / "household.db").exists()), (1, True, True))
os.environ["CONNECTORS_DIR"] = str(DATA1)
(DATA1 / "household.db-wal").write_text("wal")
check("a covered store's SQLite sidecar counts as covered", box_backup.uncovered([DATA1]), [])
(DATA1 / "household.db-wal").unlink()

print("── with a recovery key: backup first, and no erase if it fails ──")
Path(os.environ["BOX_BACKUP_SIGN_KEY"]).rename(_TMP / "sign.key.aside")
rc, out = cli("--erase")
check("the backup can't be signed: not reset, nothing erased", (rc, (DATA1 / "household.db").exists(), "Not reset" in out),
      (1, True, True))
(_TMP / "sign.key.aside").rename(os.environ["BOX_BACKUP_SIGN_KEY"])
old_key = box_setup.ensure_key()
box_setup._done_path().write_text("{}")
rc, out = cli("--erase")
backups = sorted(Path(os.environ["BOX_BACKUP_DIR"]).glob("box-*.tbk"))
check("erased, with one sealed backup made first", (rc, len(backups), "Erased" in out), (0, 1, True))
check("the data folders are empty but still there", (DATA1.is_dir(), DATA2.is_dir(), list(DATA1.iterdir()),
      list(DATA2.iterdir())), (True, True, [], []))
check("a link out of a data folder: the link goes, its target stays", (OUTSIDE / "keep.txt").exists(), True)
new_key = box_setup._key_path().read_text().strip()
check("setup is open again with a NEW key, and the URL printed", (box_setup._done_path().exists(), new_key != old_key,
      f"#k={new_key}" in out), (False, True, True))
opened = io.BytesIO()
with open(backups[0], "rb") as f:
    box_backup.unseal(f, opened, phrase)
check("the backup opens with the recovery phrase", opened.getvalue()[:2], b"\x1f\x8b")
box_backup.restore(backups[0], phrase, _TMP / "restored")
import json  # noqa: E402
check("…and holds what was erased (the store and the memory file)",
      sorted(json.loads((_TMP / "restored" / "manifest.json").read_text())["files"]),
      ["memory/persona.md", "stores/household.db"])

print("── --no-backup, and no recovery key at all ──")
seed()
rc, out = cli("--erase", "--no-backup")
check("--no-backup: erased, no second backup", (rc, len(list(Path(os.environ["BOX_BACKUP_DIR"]).glob("box-*.tbk")))), (0, 1))
for f in (Path(os.environ["BOX_BACKUP_PUB"]), Path(os.environ["BOX_BACKUP_SIGN_KEY"])):
    f.unlink()
seed()
rc, out = cli("--erase")
check("no recovery key: erased without a backup (nothing to seal to)", (rc, "Backup:" in out, list(DATA1.iterdir())), (0, False, []))

src = (Path(__file__).parent / "agent_api_server.py").read_text()
check("console only: no API route for a reset", "factory" in src.lower(), False)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
