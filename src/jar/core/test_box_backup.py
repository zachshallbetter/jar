#!/usr/bin/env python3
# gate: hermetic
"""A box's encrypted backups (titan-node#407): init prints a phrase once and keeps only the public key (no phrase or
private key on disk); a backup holds every store, world file and memory file, sealed (no plaintext in the file, no
plaintext left behind); verify and restore round-trip with the phrase (hashes and integrity_check); a wrong phrase,
a typo, an edited, cut or reordered file all refuse; restore never over the live stores or a non-empty folder;
rotate keeps the newest per day and per week. Every path is a temp folder: never /ai/backups (gate-tests-real-dirs).
Fictional data. Run: python3 scripts/test_box_backup.py
"""
from __future__ import annotations

import json
import time
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-boxbackup-"))
LIVE, MEM, OUT = _TMP / "connectors", _TMP / "memory", _TMP / "backups"
os.environ.update({"CONNECTORS_DIR": str(LIVE), "AGENT_MEMORY_DIR": str(MEM), "BOX_BACKUP_DIR": str(OUT),
                   "BOX_BACKUP_PUB": str(_TMP / "config" / "box-backup.pub.json"),
                   "BOX_BACKUP_STAGE": str(_TMP / "stage"), "BOX_BACKUP_SIGN_KEY": str(_TMP / "config" / "sign.key"),
                   "BOX_BACKUP_VERIFIED": str(_TMP / "config" / "verified.json")})
sys.path.insert(0, str(Path(__file__).parent))
import box_backup as bb  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def refuses(fn) -> str:
    try:
        fn()
    except bb.BackupError as exc:
        return str(exc)
    return "no refusal"


# A small household: two stores (one in WAL mode with an open writer), a world bubble, memory.
(LIVE / "world").mkdir(parents=True)
MEM.mkdir()
h = sqlite3.connect(LIVE / "household.db")
h.execute("PRAGMA journal_mode=WAL")
h.execute("CREATE TABLE people (id TEXT, name TEXT)")
h.executemany("INSERT INTO people VALUES (?,?)", [("p1", "Alex Hale"), ("p2", "Robin Hale"), ("p3", "Ava Hale")])
h.commit()   # left open: the WAL is live while the backup runs
t = sqlite3.connect(LIVE / "todos.db")
t.execute("CREATE TABLE todos (t TEXT)")
t.executemany("INSERT INTO todos VALUES (?)", [(f"Biscuit's walk {i} " + os.urandom(100).hex(),) for i in range(20000)])
t.commit()
t.close()
sqlite3.connect(LIVE / "world" / "home.db").execute("CREATE TABLE w (k)").connection.close()
(LIVE / "household.json").write_text('{"town": "Westview"}')
(MEM / "persona.md").write_text("Alex likes short answers.")

print("── the phrase ──")
p1 = bb.new_phrase()
check("7 groups of 4", [len(g) for g in p1.split("-")], [4] * 7)
check("written loosely still reads", bb.normalize(p1.lower().replace("-", " ")), bb.normalize(p1))
typo = p1[:5] + ("1" if p1[5] != "1" else "2") + p1[6:]
check("a typo is caught by the check group", "typo" in refuses(lambda: bb.normalize(typo)), True)
check("not a phrase", "isn't a recovery phrase" in refuses(lambda: bb.normalize("hello")), True)
check("no backup before init", "no recovery phrase" in refuses(bb.backup), True)

print("── init ──")
phrase = bb.init()
pub = json.loads(Path(os.environ["BOX_BACKUP_PUB"]).read_text())
check("only public material kept: salt, KDF, public key, the sender's public key, fingerprints, date", sorted(pub),
      ["created", "fingerprint", "kdf", "pub", "salt", "sender", "sender_fingerprint", "v"])
sk = Path(os.environ["BOX_BACKUP_SIGN_KEY"])
check("the signing key is its own 0600 file, never in the public file", (oct(sk.stat().st_mode & 0o777),
      sk.read_text().strip() in json.dumps(pub)), ("0o600", False))
check("the public key file is 0600", oct(Path(os.environ["BOX_BACKUP_PUB"]).stat().st_mode & 0o777), "0o600")
check("init twice refuses", "already has" in refuses(bb.init), True)
on_disk = b"".join(p.read_bytes() for p in _TMP.rglob("*") if p.is_file())
check("the phrase is nowhere on disk", bb.normalize(phrase).encode() in on_disk or phrase.encode() in on_disk, False)

print("── backup ──")
f = bb.backup()
raw = f.read_bytes()
check("named box-<stamp>.tbk, 0600", (bool(bb.NAME_RX.match(f.name)), oct(f.stat().st_mode & 0o777)), (True, "0o600"))
check("sealed: no names, no store headers in the file", any(s in raw for s in (b"Alex Hale", b"SQLite format",
                                                                                b"Westview", b"persona")), False)
check("several chunks (the todos store is > 1 MiB)", len(raw) > bb.CHUNK, True)
check("no plaintext left behind", sorted(p.name for p in OUT.iterdir()), [f.name])

print("── verify and restore ──")
r = bb.verify(f, phrase)
check("verify: every file, nothing bad", (r["files"], r["bad"]),
      (5, []))
print("── list and status (#1187) ──")
d_ = bb.describe(f)
check("list: the sealed-to key and the box that made it (this one), from the header alone",
      (d_["key"], d_["sender"], d_["this_box"]), (bb.box_keys()["key"], bb.box_keys()["sender"], True))
junk = _TMP / "box-20300101T000000Z.tbk"
junk.write_bytes(b"not a backup")
check("…a file whose header doesn't read says so", "error" in bb.describe(junk), True)
junk.unlink()
st = bb.status()
check("status before the owner ever opened one: overdue (the box can't check its own backups)",
      (st["backups"], st["verified_days"], st["verify_due"]), (1, None, True))
real_read = bb._read_phrase
bb._read_phrase = lambda: phrase
check("`verify` with the right phrase: exit 0", bb.main(["box_backup.py", "verify", str(f)]), 0)
bb._read_phrase = real_read
vp = Path(os.environ["BOX_BACKUP_VERIFIED"])
check("…it notes when (time and file name only, 0600)", (sorted(json.loads(vp.read_text())), oct(vp.stat().st_mode & 0o777)),
      (["at", "file"], "0o600"))
st = bb.status()
check("…so status isn't overdue now", (round(st["verified_days"]), st["verify_due"]), (0, False))
check("…and is again after 30 days", bb.status(now=time.time() + 31 * 86400)["verify_due"], True)
bb._read_phrase = lambda: bb.new_phrase()
check("a failed verify (the wrong phrase) notes nothing", (bb.main(["box_backup.py", "verify", str(f)]) != 0,
      json.loads(vp.read_text())["file"]), (True, f.name))
bb._read_phrase = real_read

dest = _TMP / "restored"
r = bb.restore(f, phrase, dest)
check("restore: same files", sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()),
      ["manifest.json", "memory/persona.md", "stores/household.db", "stores/household.json", "stores/todos.db",
       "world/home.db"])
c = sqlite3.connect(dest / "stores" / "household.db")
check("the WAL's rows are in the copy", [r[0] for r in c.execute("SELECT name FROM people ORDER BY id")],
      ["Alex Hale", "Robin Hale", "Ava Hale"])
c.close()
check("memory comes back", (dest / "memory" / "persona.md").read_text(), "Alex likes short answers.")
check("restore over a non-empty folder refuses", "isn't empty" in refuses(lambda: bb.restore(f, phrase, dest)), True)
check("restore over the live stores refuses", "never restore over" in
      refuses(lambda: bb.restore(f, phrase, LIVE / "world" / "new")), True)
check("a wrong phrase refuses", "isn't the one this backup was sealed to" in refuses(lambda: bb.verify(f, bb.new_phrase())),
      True)

print("── tampering ──")
bad = OUT / "edited.bin"
b = bytearray(raw)
b[len(b) // 2] ^= 1
bad.write_bytes(bytes(b))
check("one flipped bit refuses", "didn't open" in refuses(lambda: bb.verify(bad, phrase)), True)
bad.write_bytes(raw[:bb.CHUNK + 200])
check("cut short refuses", refuses(lambda: bb.verify(bad, phrase)) != "no refusal", True)
H = 6 + int.from_bytes(raw[4:6], "big")   # MAGIC, length, JSON header
first_len = int.from_bytes(raw[H:H + 4], "big")
c0 = raw[H:H + 4 + first_len]
rest = raw[H + 4 + first_len:]
n1 = int.from_bytes(rest[:4], "big")
c1 = rest[:4 + n1]
bad.write_bytes(raw[:H] + c1 + c0 + rest[4 + n1:])
check("chunks swapped refuses", refuses(lambda: bb.verify(bad, phrase)) != "no refusal", True)
bad.write_bytes(raw[:H] + c0)   # drop every chunk after the first: the first wasn't sealed as the last
check("trailing chunks dropped refuses", refuses(lambda: bb.verify(bad, phrase)) != "no refusal", True)
bad.unlink()
check("no verify leftovers", sorted(p.name for p in OUT.iterdir()), [f.name])

print("── rotate ──")
f.unlink()
base = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)
for d in range(40):   # one a day for 40 days, two on the newest day
    (OUT / f"box-{(base - timedelta(days=d)).strftime('%Y%m%dT%H%M%SZ')}.tbk").write_bytes(b"x")
(OUT / f"box-{(base + timedelta(hours=5)).strftime('%Y%m%dT%H%M%SZ')}.tbk").write_bytes(b"x")
(OUT / "notes.txt").write_text("not a backup")
gone = bb.rotate(7, 4)
left = [p.name for p in bb.listing()]
check("newest kept", left[0], "box-20261007T080000Z.tbk")
check("7 days (newest per day), then the weeks not already kept (4 weeks back: 2 more)", left[7:],
      ["box-20260927T030000Z.tbk", "box-20260920T030000Z.tbk"])
check("the same day's older one went", "box-20261007T030000Z.tbk" in left, False)
check("anything else is left alone", (OUT / "notes.txt").exists(), True)

print("── the CLI reads the phrase from stdin ──")
bb.backup()
newest = bb.listing()[0]
env = dict(os.environ)
out = subprocess.run([sys.executable, str(Path(__file__).with_name("box_backup.py")), "verify", str(newest)],
                     input=phrase + "\n", capture_output=True, text=True, env=env)
check("verify via stdin", (out.returncode, out.stdout.startswith("ok: 5 files")), (0, True))
check("the phrase isn't echoed", phrase in out.stdout + out.stderr, False)
h.close()

print("── review fixes (JEV, 10-07) ──")
import io  # noqa: E402
import tarfile  # noqa: E402


def sealed_tar(entries) -> Path:
    """A backup sealed with this box's key whose tar holds the given (name, bytes) entries."""
    raw_tgz = io.BytesIO()
    with tarfile.open(fileobj=raw_tgz, mode="w:gz") as t:
        for name, data in entries:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    out = OUT / "box-20990101T000000Z.tbk"
    with open(out, "wb") as d:
        bb.seal(io.BytesIO(raw_tgz.getvalue()), d)
    return out


evil = sealed_tar([("./manifest.json", b'{"v":1,"created":"x","files":{}}'), ("../b2/owned.txt", b"x")])
dest2 = _TMP / "b"
check("restore refuses ../b2 (a sibling whose name starts like the target)",
      ("strange entry" in refuses(lambda: bb.restore(evil, phrase, dest2)), (_TMP / "b2").exists()), (True, False))
evil.unlink()
victim = _TMP / "victim.txt"
victim.write_text("keep me")
dest3 = _TMP / "r3"
(_TMP / "r3.tgz").symlink_to(victim)   # where the old code put its temp file
bb.restore(newest, phrase, dest3)
check("restore doesn't follow a link planted beside the target", victim.read_text(), "keep me")
check("plaintext is staged outside the backup dir, and cleaned up",
      (sorted(p.name for p in OUT.iterdir() if not p.name.startswith("box-") and p.name != "notes.txt"), sorted((_TMP / "stage").iterdir())),
      ([], []))
check("the stage folder is 0700", oct((_TMP / "stage").stat().st_mode & 0o777), "0o700")
(OUT / "box-20000101T000000Z.part").write_bytes(b"half")
old = _TMP / "stage" / "stage-crashed"
old.mkdir()
os.utime(old, (0, 0))
bb.backup()
check("a crashed run's .part and stale stage are swept at the next run",
      ((OUT / "box-20000101T000000Z.part").exists(), old.exists()), (False, False))

print("── #1188: header, signature, sender, check ──")
import base64 as _b64  # noqa: E402
import shutil as _sh  # noqa: E402
newest = bb.listing()[0]
raw2 = newest.read_bytes()
H2 = 6 + int.from_bytes(raw2[4:6], "big")
meta = json.loads(raw2[6:H2])
check("the header names its KDF, salt, key and sender, and nothing secret",
      sorted(meta), ["created", "eph", "kdf", "key", "prefix", "salt", "sender", "v"])
check("…key fingerprint = this box's", meta["key"], bb.box_keys()["key"])
r = bb.check(newest)
check("check without the phrase: signed, from this box", (r["signed"], r["trusted"], r["sender"]),
      (True, True, bb.box_keys()["sender"]))
tamp = OUT / "tamper.bin"
tamp.write_bytes(raw2[:-1] + bytes([raw2[-1] ^ 1]))
check("a changed signature refuses (check and verify)", ("signature doesn't match" in refuses(lambda: bb.check(tamp)),
      "signature doesn't match" in refuses(lambda: bb.verify(tamp, phrase))), (True, True))
tamp.write_bytes(raw2 + b"x")
check("extra bytes after the signature refuse", "extra data" in refuses(lambda: bb.check(tamp)), True)
tamp.write_bytes(raw2[:-68])
check("no signature at all refuses", refuses(lambda: bb.check(tamp)) != "no refusal", True)
tamp.unlink()
# Someone else seals a file to THIS box's public key with their own signing key: refused before it's opened.
mine_sign = Path(os.environ["BOX_BACKUP_SIGN_KEY"]).read_text()
mine_pub = Path(os.environ["BOX_BACKUP_PUB"]).read_text()
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives import serialization as _ser  # noqa: E402
other = Ed25519PrivateKey.generate()
Path(os.environ["BOX_BACKUP_SIGN_KEY"]).write_text(_b64.b64encode(other.private_bytes(
    _ser.Encoding.Raw, _ser.PrivateFormat.Raw, _ser.NoEncryption())).decode())
d = json.loads(mine_pub)
other_pub = other.public_key().public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw)
d.update(sender=_b64.b64encode(other_pub).decode(), sender_fingerprint=bb.fingerprint(other_pub))
Path(os.environ["BOX_BACKUP_PUB"]).write_text(json.dumps(d))
forged = bb.backup(now=datetime(2030, 1, 1, tzinfo=timezone.utc))
Path(os.environ["BOX_BACKUP_SIGN_KEY"]).write_text(mine_sign)
Path(os.environ["BOX_BACKUP_PUB"]).write_text(mine_pub)
check("a file sealed to this box's key but signed by another box is refused, before it's opened",
      ("different box" in refuses(lambda: bb.restore(forged, phrase, _TMP / "r-forged")), (_TMP / "r-forged").exists()),
      (True, False))
check("…unless the owner names that box (restoring another box's backups, fingerprint from its card)",
      bb.verify(forged, phrase, sender=bb.fingerprint(other_pub))["bad"], [])
check("check says so too", bb.check(forged)["trusted"], False)
check("list marks it as another box's", bb.describe(forged)["this_box"], False)
forged.unlink()
_sh.copy(os.environ["BOX_BACKUP_PUB"], _TMP / "pub.keep")
os.remove(os.environ["BOX_BACKUP_PUB"])
check("on a new box (no key file), restore needs --sender", "say which box" in refuses(lambda: bb.verify(newest, phrase)), True)
check("…and works with it", bb.verify(newest, phrase, sender=bb.fingerprint(_b64.b64decode(meta["sender"])))["bad"], [])
_sh.copy(_TMP / "pub.keep", os.environ["BOX_BACKUP_PUB"])
greedy = dict(meta, kdf={"n": 2 ** 24, "r": 8, "p": 1})
body = json.dumps(greedy, sort_keys=True, separators=(",", ":")).encode()
tamp.write_bytes(bb.MAGIC + len(body).to_bytes(2, "big") + body + raw2[H2:])
check("a header asking for too much key-derivation work is refused (no 16 GB scrypt)",
      "beyond the limits" in refuses(lambda: bb.verify(tamp, phrase)), True)
tamp.unlink()

print("── #1188: restore guards, init on a terminal ──")
mem_child = MEM / "restored"
check("restore inside the memory dir is refused", "never restore over" in refuses(lambda: bb.restore(newest, phrase, mem_child)),
      True)
check("…and around it (a parent of the live stores)", refuses(lambda: bb.restore(newest, phrase, _TMP)) != "no refusal",
      True)
r4 = _TMP / "r4"
bb.restore(newest, phrase, r4)
check("the restore target is 0700", oct(r4.stat().st_mode & 0o777), "0o700")
out = subprocess.run([sys.executable, str(Path(__file__).with_name("box_backup.py")), "init", "--replace"],
                     capture_output=True, text=True, env=env)
check("init with stdout not a terminal refuses (the phrase never lands in a log or pipe)",
      (out.returncode, "run it on a terminal" in out.stderr, out.stdout), (1, True, ""))
out = subprocess.run([sys.executable, str(Path(__file__).with_name("box_backup.py")), "check", str(newest)],
                     capture_output=True, text=True, env=env)
check("the check command (no phrase)", (out.returncode, out.stdout.startswith("ok: made")), (0, True))

print("── #1188: the nightly unit ──")
unit = (Path(__file__).resolve().parent.parent / "server-setup" / "titan-box-backup.service").read_text()
keys = {l.split("=", 1)[0] for l in unit.splitlines() if "=" in l and not l.startswith("#")}
check("no agent.env (it needs none of the agent's secrets)", "EnvironmentFile" in keys, False)
check("sandboxed: private umask and /tmp, read-only system, no new privileges",
      all(x in unit for x in ("UMask=0077", "PrivateTmp=yes", "ProtectSystem=strict", "NoNewPrivileges=yes")), True)
rw = next(l for l in unit.splitlines() if l.startswith("ReadWritePaths="))
check("writable: only the backup and stage dirs, and the live stores and memory (SQLite's -shm)",
      sorted(p.lstrip("-") for p in rw.split("=", 1)[1].split()),
      sorted(["/ai/backups/box", "/ai/tmp/box-backup-stage", "/ai/datasets/connectors", "/home/zach/.config/jellybean/memory"]))
check("…and every path it uses is set in the unit", all(f"Environment={k}=" in unit for k in
      ("CONNECTORS_DIR", "AGENT_MEMORY_DIR", "BOX_BACKUP_DIR", "BOX_BACKUP_STAGE", "BOX_BACKUP_PUB", "BOX_BACKUP_SIGN_KEY")), True)
check("the timer still ships, not enabled by this repo", (Path(__file__).resolve().parent.parent / "server-setup" /
      "titan-box-backup.timer").exists(), True)

print("── #1188 follow-ups (JEV) ──")
import tracemalloc  # noqa: E402
huge = OUT / "huge.bin"
huge.write_bytes(raw2[:H2] + (0xFFFFFFF0).to_bytes(4, "big") + b"x" * 32)
tracemalloc.start()
msg = refuses(lambda: bb.check(huge))
peak = tracemalloc.get_traced_memory()[1]
tracemalloc.stop()
check("a crafted ~4 GB chunk length is refused before any allocation (check needs no phrase)",
      ("larger than any chunk" in msg, peak < 50 * 2 ** 20), (True, True))
first = int.from_bytes(raw2[H2:H2 + 4], "big")
huge.write_bytes(raw2[:H2 + 4 + first // 2])
check("a chunk shorter than its length says: cut short", "cut short" in refuses(lambda: bb.check(huge)), True)
huge.write_bytes(raw2[:H2] + bb.SIG_MAGIC + raw2[-64:])
check("a signature where the first chunk should be: refused", refuses(lambda: bb.check(huge)) != "no refusal", True)
huge.unlink()
check("init twice says --replace changes the fingerprint too, and to keep the old card",
      ("new box fingerprint" in (m := refuses(bb.init)), "old card" in m), (True, True))
src = Path(bb.__file__).read_text()
check("the printed card says to keep the old card after a replace", "keep the old card too" in src, True)

print("── a box with no stores yet (10-10: a fresh box's factory reset crashed on the missing stage folder) ──")
os.environ.update({"CONNECTORS_DIR": str(_TMP / "no-stores-yet"), "AGENT_MEMORY_DIR": str(_TMP / "no-memory-yet")})
empty = bb.backup(now=datetime(2031, 1, 1, tzinfo=timezone.utc))
check("it seals an empty backup instead of failing", empty.exists(), True)
opened = bb.restore(empty, phrase, _TMP / "restored-empty")
check("…which opens to a manifest with no files", json.loads((_TMP / "restored-empty" / "manifest.json").read_text()).get("files"),
      {})

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
