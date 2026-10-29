#!/usr/bin/env python3
# gate: hermetic
"""KB backups are kept 30 days, then shredded (#905; Zach, 10-04): kb_lifecycle's and envelope_pass's backups only,
aged by the later of the name's stamp and the file's mtime, overwritten before they are unlinked; a dry run only lists.
Temporary folders only; never the real backups.
Run: python3 scripts/test_backup_retention.py"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-retention-test-"))
os.environ["KB_LIFECYCLE_BACKUP_DIR"] = str(_TMP / "life")
os.environ["ENVELOPE_BACKUP_DIR"] = str(_TMP / "env")
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
sys.path.insert(0, str(Path(__file__).parent))
import backup_retention as R  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = time.time()
D = _TMP / "b"
D.mkdir()


def make(name: str, age_days: float, text: str = "Robin's mail: the garden club meets Thursday.\n") -> Path:
    p = D / name
    p.write_text(text)
    os.chmod(p, 0o600)
    t = NOW - age_days * 86400
    os.utime(p, (t, t))
    return p


def stamp(age_days: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(NOW - age_days * 86400))


old = make(f"kb-lifecycle-{stamp(31)}.jsonl", 31)
young = make(f"kb-lifecycle-{stamp(29)}.jsonl", 29)
renamed_late = make(f"kb-lifecycle-{stamp(40)}.jsonl", 2)        # old name, written 2 days ago: kept
odd = make("kb-lifecycle-manual.jsonl", 45)                       # no stamp: mtime decides
other = make(f"kb-cleanup-{stamp(60)}.jsonl", 60)                 # another writer's: not ours to shred
link = D / f"kb-lifecycle-{stamp(50)}-link.jsonl"
link.symlink_to(other)
os.utime(link, (NOW - 50 * 86400,) * 2, follow_symlinks=False)

print("── what is due ──")
check("dry run lists only our old files", R.prune(D, "kb-lifecycle-*.jsonl", now=NOW, apply=False),
      sorted([old.name, odd.name]))
check("…and touches nothing", all(p.exists() for p in (old, young, renamed_late, odd, other)), True)
check("the age is the later of name and mtime", round(R.age_s(renamed_late, NOW) / 86400), 2)

print("── shredded ──")
twin = D / "twin-of-old"
os.link(old, twin)   # a second name for the same bytes shows what was written over them
before = twin.read_bytes()
got = R.prune(D, "kb-lifecycle-*.jsonl", now=NOW, apply=True)
check("the due ones are shredded", got, sorted([old.name, odd.name]))
check("…unlinked", (old.exists(), odd.exists()), (False, False))
after = twin.read_bytes()
check("…overwritten first (same size, not the text)", (len(after) == len(before), after != before, b"garden" in after),
      (True, True, False))
check("younger ones stay", (young.exists(), renamed_late.exists()), (True, True))
check("another writer's backup stays", other.exists(), True)
check("a symlink is never followed or removed", (link.is_symlink(), other.read_text().startswith("Robin")), (True, True))
check("a missing folder: nothing", R.prune(_TMP / "nope", "*.jsonl"), [])
check("a 30-day-old backup is still kept", R.prune(D, "kb-lifecycle-*.jsonl", now=NOW + 1 * 86400, apply=False), [])

print("── the two writers ──")
import envelope_pass  # noqa: E402
import kb_lifecycle  # noqa: E402
check("targets: lifecycle's folder (its own, the cleanups', the mail store's), envelope backups; their own patterns",
      [(str(d), g) for d, g in R.targets()],
      [(str(_TMP / "life"), "kb-lifecycle-*.jsonl"), (str(_TMP / "life"), "kb-cleanup-*.jsonl"),
       (str(_TMP / "life"), "kb-delete-*.json"), (str(_TMP / "life"), "mail-server-*.db"),
       (str(_TMP / "env"), "envelopes-*.jsonl")])
check("kb_lifecycle prunes every pattern in its folder (the real clock)",
      "backup_retention.prune(BACKUP_DIR, g, apply=apply)" in Path(kb_lifecycle.__file__).read_text(), True)

print("── the one-off cleanups' backups (#905, 10-04) ──")
L = _TMP / "life"
L.mkdir(exist_ok=True)
olds = []
for name, age in ((f"kb-cleanup-{stamp(35)}.jsonl", 35), (f"kb-delete-web-f88de6d23433-{stamp(33)}.json", 33),
                  (f"kb-cleanup-{stamp(5)}.jsonl", 5), (f"kb-delete-web-0123456789ab-{stamp(2)}.json", 2),
                  (f"kb-saved-backfill-{stamp(90)}.json", 90), (f"coordinator-secret-backup-{stamp(90)}.json", 90)):
    p = L / name
    p.write_text("{}")
    os.utime(p, (NOW - age * 86400,) * 2)
    olds.append(p)
(L / f"kb-cleanup-{stamp(40)}").mkdir()   # a cleanup's backup folder: not a file, not ours to shred
kept_dir = L / f"kb-cleanup-{stamp(40)}"
due = {g: R.prune(L, g, now=NOW, apply=False) for g in R.LIFECYCLE_DIR_PATTERNS}
check("due: the old cleanup and delete backups only", sorted(sum(due.values(), [])), sorted([olds[0].name, olds[1].name]))
for g in R.LIFECYCLE_DIR_PATTERNS:
    R.prune(L, g, now=NOW, apply=True)
check("shredded those; the young ones, other writers' files and the folder stay",
      [p.exists() for p in olds] + [kept_dir.is_dir()], [False, False, True, True, True, True, True])

print("── the dry-run listing: names and ages only ──")
import io, contextlib  # noqa: E402,E401
old = L / f"kb-cleanup-{stamp(45)}.jsonl"
old.write_text("Robin's private note")
os.utime(old, (NOW - 45 * 86400,) * 2)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    R.main([])
check("the dry run names the file and its age, never its text, and shreds nothing",
      (f"{old.name} (45 d)" in buf.getvalue(), "Robin" in buf.getvalue(), old.exists()), (True, False, True))
check("envelope_pass prunes the folder it writes to", "backup_retention.prune(backup.parent if backup else BACKUP_DIR"
      in Path(envelope_pass.__file__).read_text(), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
