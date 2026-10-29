#!/usr/bin/env python3
"""
Unit tests for preflight_check.host_disk_problems: a missing fstab mount, a full filesystem,
and SMART failing / unreadable / asleep are each reported (and healthy disks are not).
Hermetic: fstab, mounts, statvfs, lsblk and smartctl are all faked.

Run: python3 scripts/test_preflight_disks.py
"""
import io
import os
import sys
import types
sys.path.insert(0, os.path.dirname(__file__))

import preflight_check as pc


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


FSTAB = """# comment
UUID=a / ext4 defaults 0 1
UUID=b /ai/datasets ext4 defaults 0 2
UUID=c /ai/tmp ext4 defaults,nofail 0 2
/swap.img none swap sw 0 0
"""


def run():
    ok = True
    real_open, real_ismount, real_statvfs, real_run, real_sh = open, os.path.ismount, os.statvfs, pc.subprocess.run, pc.sh
    mounted = {"/", "/ai/tmp"}
    usage = {"/": 95, "/ai/tmp": 10}
    smart = {"/dev/sda": (0, "PASSED"), "/dev/sdb": (8, "FAILED!"), "/dev/sdc": (2, ""), "/dev/sdd": (2, "Device is in STANDBY mode")}

    def fake_open(path, *a, **k):
        return io.StringIO(FSTAB) if path == "/etc/fstab" else real_open(path, *a, **k)

    def fake_statvfs(mp):
        return types.SimpleNamespace(f_blocks=100, f_bavail=100 - usage[mp])

    def fake_run(cmd, **kw):
        rc, out = smart[cmd[-1]]
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")

    try:
        import builtins
        builtins.open = fake_open
        os.path.ismount = lambda mp: mp in mounted
        os.statvfs = fake_statvfs
        pc.subprocess.run = fake_run
        pc.sh = lambda c: "\n".join(smart)
        got = pc.host_disk_problems()
    finally:
        import builtins
        builtins.open = real_open
        os.path.ismount, os.statvfs, pc.subprocess.run, pc.sh = real_ismount, real_statvfs, real_run, real_sh

    print("\n── host disk problems ──")
    ok &= check("missing mount reported", "mount:/ai/datasets" in got, True)
    ok &= check("full root reported", got.get("full:/"), "/ is 95% full")
    ok &= check("healthy /ai/tmp not reported", "full:/ai/tmp" in got, False)
    ok &= check("SMART failing", got.get("smart:/dev/sdb"), "/dev/sdb SMART health FAILING")
    ok &= check("SMART unreadable", got.get("smart:/dev/sdc"), "/dev/sdc SMART health unreadable (rc=2)")
    ok &= check("healthy disk not reported", "smart:/dev/sda" in got, False)
    ok &= check("sleeping disk not reported", "smart:/dev/sdd" in got, False)
    ok &= check("nothing else", len(got), 4)

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
