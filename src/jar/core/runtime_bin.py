#!/usr/bin/env python3
"""Installed runtime binaries (titan-node#1023): units run /ai/runtimes/bin/<name>/current, never a build tree.

  /ai/runtimes/bin/<name>/
    current  -> releases/<stamp>-<sha12>/<name>     what the unit runs (ExecStart)
    previous -> releases/<stamp>-<sha12>/<name>     the rollback copy
    releases/<stamp>-<sha12>/<name>, BUILD          the last KEEP releases (BUILD: sha256, from, built_at, source)

A swap is one rename(2) of a fresh symlink over `current`, so the unit starts either the old binary or the new one,
never half of one; a running process keeps its inode until it's restarted. Installing a binary identical to
`current` does nothing (no restart needed). Pruning never touches what current or previous points at.

  runtime_bin.py install <name> <binary> [--source DESC] [--dry-run]   (deploy_rust_runtime.sh, after the build)
  runtime_bin.py rollback <name> [--dry-run]                         (current <-> previous; then restart the unit)
  runtime_bin.py status [<name>]                                     (targets and short hashes, every installed one)
  runtime_bin.py migrate <name> [--health URL] [--dry-run]           (once per unit: the live binary as it is → bin,
                                                                      ExecStart rewritten, restart, health; reverts on
                                                                      a failed health check)

Run on titan. Prints paths and short hashes only, never a unit's environment or a process's arguments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(os.environ.get("RUNTIME_BIN_ROOT", "/ai/runtimes/bin"))
UNIT_DIR = Path(os.environ.get("RUNTIME_UNIT_DIR", "/etc/systemd/system"))
KEEP = 3
_NAME_RX = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


def _check_name(name: str) -> str:
    if not _NAME_RX.fullmatch(str(name or "")):
        raise ValueError(f"not a runtime name: {name!r}")
    return name


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _target(link: Path) -> Path | None:
    try:
        return (link.parent / os.readlink(link)).resolve() if link.is_symlink() else None
    except OSError:
        return None


def swap(link: Path, target: Path, dry_run: bool = False) -> None:
    """Point `link` at `target` atomically: a new relative symlink beside it, renamed over it."""
    rel = os.path.relpath(Path(target).resolve(), link.parent.resolve())   # both resolved: /var vs /private/var
    if dry_run:
        print(f"  would point {link.name} -> {rel}")
        return
    tmp = link.with_name(f".{link.name}.new")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(rel, tmp)
    os.replace(tmp, link)


def prune(home: Path, keep: int = KEEP, dry_run: bool = False) -> list[str]:
    """Delete releases beyond the newest `keep`, never the ones current or previous point at."""
    rel = home / "releases"
    if not rel.is_dir():
        return []
    protect = {t.parent for t in (_target(home / "current"), _target(home / "previous")) if t}
    dirs = sorted((d for d in rel.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)
    gone = [d for d in dirs[keep:] if d.resolve() not in protect]
    for d in gone:
        if dry_run:
            print(f"  would prune {d.name}")
        else:
            shutil.rmtree(d)
    return [d.name for d in gone]


def install(name: str, binary: Path, root: Path = ROOT, keep: int = KEEP, source: str = "", now: float | None = None,
            dry_run: bool = False) -> dict[str, Any]:
    """Install `binary` as <name>'s current release; the old current becomes previous. Unchanged → nothing."""
    _check_name(name)
    binary = Path(binary)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise FileNotFoundError(f"not an executable file: {binary}")
    home = root / name
    digest = sha256(binary)
    cur = _target(home / "current")
    if cur and cur.is_file() and sha256(cur) == digest:
        return {"action": "unchanged", "sha": digest[:12], "current": str(cur)}
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else time.time()))
    rdir = home / "releases" / f"{stamp}-{digest[:12]}"
    dest = rdir / name
    if dry_run:
        print(f"  would copy {binary} -> {dest} (sha {digest[:12]})")
    else:
        rdir.mkdir(parents=True, exist_ok=True)
        tmp = rdir / f".{name}.part"
        shutil.copy2(binary, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
        (rdir / "BUILD").write_text(json.dumps({"sha256": digest, "from": str(binary), "source": source,
                                                "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                          time.gmtime(binary.stat().st_mtime)),
                                                "installed_at": stamp}, indent=1) + "\n")
    if cur:
        swap(home / "previous", cur, dry_run)
    swap(home / "current", dest, dry_run)
    if not cur:   # the first install: previous is the same file, so a rollback is a no-op, never a dangling link
        swap(home / "previous", dest, dry_run)
    pruned = prune(home, keep, dry_run)
    return {"action": "installed", "sha": digest[:12], "current": str(dest), "previous": str(cur or dest),
            "pruned": pruned}


def rollback(name: str, root: Path = ROOT, dry_run: bool = False) -> dict[str, Any]:
    """current <-> previous (the caller restarts the unit). Refused when either is missing or they're the same."""
    home = root / _check_name(name)
    cur, prev = _target(home / "current"), _target(home / "previous")
    if not cur or not prev or not prev.is_file():
        raise FileNotFoundError(f"{name}: no previous release to roll back to")
    if cur == prev:
        return {"action": "same", "current": str(cur)}
    swap(home / "current", prev, dry_run)
    swap(home / "previous", cur, dry_run)
    return {"action": "rolled back", "current": str(prev), "previous": str(cur)}


def status(root: Path = ROOT, name: str | None = None) -> list[dict[str, Any]]:
    out = []
    names = [name] if name else sorted(d.name for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
    for n in names:
        home = root / n
        row: dict[str, Any] = {"name": n}
        for k in ("current", "previous"):
            t = _target(home / k)
            row[k] = (t.parent.name if t else None)
            row[f"{k}_ok"] = bool(t and t.is_file() and os.access(t, os.X_OK))
        row["releases"] = len([d for d in (home / "releases").iterdir() if d.is_dir()]) if (home / "releases").is_dir() else 0
        out.append(row)
    return out


def unit_note(name: str, execln: str, root: Path = ROOT) -> str | None:
    """Preflight's rule for a Rust unit (#1023): None when it runs bin/<name>/current and that resolves to an
    executable; NOT-MIGRATED while it still runs its build tree; BIN-BROKEN when current doesn't resolve;
    UNIT!=rust for anything else (the clobber bug the old rule caught)."""
    line = str(execln or "").strip()
    exe = line.split("=", 1)[1].strip() if line.startswith("ExecStart=") else line
    exe = exe.split()[0] if exe else ""
    if exe == str(root / name / "current"):
        t = _target(root / name / "current")
        return None if t and t.is_file() and os.access(t, os.X_OK) else "BIN-BROKEN"
    if "target/release" in exe:
        return "NOT-MIGRATED"
    return "UNIT!=rust"


# ── migrating a unit (once): the live binary as it is, no rebuild ──

Runner = Callable[[list[str]], str]


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False).stdout.strip()


def exec_line(unit_text: str) -> str | None:
    lines = [ln for ln in unit_text.splitlines() if ln.startswith("ExecStart=")]
    return lines[0][len("ExecStart="):].strip() if len(lines) == 1 else None


def rewritten(unit_text: str, new_exec: str) -> str:
    return "\n".join(f"ExecStart={new_exec}" if ln.startswith("ExecStart=") else ln for ln in unit_text.splitlines()) + "\n"


def healthy(url: str, tries: int = 15, wait: float = 2.0, sleep: Callable[[float], None] = time.sleep) -> bool:
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except Exception:  # noqa: BLE001
            sleep(wait)
    return False


def migrate(name: str, health_url: str, root: Path = ROOT, unit_dir: Path = UNIT_DIR, run: Runner = _run,
            is_healthy: Callable[[str], bool] = healthy, write_unit: Callable[[Path, str], None] | None = None,
            dry_run: bool = False) -> dict[str, Any]:
    """Move one unit onto bin/<name>/current: copy the binary it is running now (by its process, no rebuild), rewrite
    ExecStart, daemon-reload, restart, health. A failed health check puts the old unit back and restarts it."""
    _check_name(name)
    unit = unit_dir / f"{name}.service"
    text = unit.read_text()
    old = exec_line(text)
    new_exec = str(root / name / "current")
    if old == new_exec:
        return {"action": "already migrated"}
    if not old or " " in old or not old.endswith(f"/target/release/{name}"):
        # arguments or an unexpected path: never guessed at (and never printed: an argument could be a secret)
        raise ValueError(f"{name}: ExecStart isn't a bare target/release/{name} path; migrate it by hand")
    pid = run(["systemctl", "show", "-p", "MainPID", "--value", f"{name}.service"])
    exe = ""
    if pid and pid != "0":
        # /proc/<pid>/exe of another user's process needs root (10-05: every unit refused without it)
        exe = run(["readlink", f"/proc/{pid}/exe"]) or run(["sudo", "-n", "readlink", f"/proc/{pid}/exe"])
    if not exe or exe.endswith(" (deleted)") or exe != old:
        # the running binary was replaced on disk (or isn't the unit's path): copying the file would install a
        # different binary than the one running, so refuse
        raise RuntimeError(f"{name}: the running binary isn't the file at its ExecStart (rebuilt since it started?)")
    got = install(name, Path(exe), root, source=f"migrated as-is from {exe}", dry_run=dry_run)
    if dry_run:
        print(f"  would rewrite {unit.name}: ExecStart -> {new_exec}; daemon-reload; restart; health {health_url}")
        return {**got, "action": "dry-run"}
    backup = root / name / "unit.before"
    backup.write_text(text)
    put = write_unit or (lambda p, t: subprocess.run(["sudo", "tee", str(p)], input=t, text=True, capture_output=True,
                                                     check=True))
    put(unit, rewritten(text, new_exec))
    run(["sudo", "systemctl", "daemon-reload"])
    run(["sudo", "systemctl", "restart", f"{name}.service"])
    if is_healthy(health_url):
        return {**got, "action": "migrated"}
    put(unit, text)
    run(["sudo", "systemctl", "daemon-reload"])
    run(["sudo", "systemctl", "restart", f"{name}.service"])
    return {**got, "action": "reverted", "healthy_after_revert": is_healthy(health_url)}


# ── freeing a port before a start (#1034) ──

_PID_RX = re.compile(r"pid=(\d+)")


def free_port(name: str, port: int, run: Runner = _run, sleep: Callable[[float], None] = time.sleep,
              grace: float = 5.0, dry_run: bool = False) -> list[dict[str, Any]]:
    """After `systemctl stop <name>`: whatever still listens on the port is an orphan (e.g. a legacy uvicorn) and is
    stopped by PID, TERM then KILL after `grace` seconds. The unit's own MainPID is never one (#1034: the old inline
    step found the unit's own process on its port and hard-killed it). Prints PIDs and process names only."""
    _check_name(name)
    port = int(port)
    if not 0 < port < 65536:
        raise ValueError("port")
    main = run(["systemctl", "show", "-p", "MainPID", "--value", f"{name}.service"]).strip()
    held = run(["sudo", "-n", "ss", "-Htlnp", f"sport = :{port}"])
    pids = sorted({p for p in _PID_RX.findall(held) if p not in (main, "0", "")}, key=int)
    out = []
    for pid in pids:
        comm = run(["ps", "-o", "comm=", "-p", pid]).strip() or "?"
        row = {"pid": int(pid), "comm": comm[:32], "port": port}
        if dry_run:
            print(f"  would stop orphan pid={pid} ({row['comm']}) on :{port}")
            out.append({**row, "action": "dry-run"})
            continue
        print(f"  stopping orphan pid={pid} ({row['comm']}) on :{port}")
        run(["sudo", "-n", "kill", "-TERM", pid])
        waited = 0.0
        while waited < grace and run(["ps", "-o", "pid=", "-p", pid]).strip():
            sleep(0.5)
            waited += 0.5
        if run(["ps", "-o", "pid=", "-p", pid]).strip():
            run(["sudo", "-n", "kill", "-KILL", pid])
            out.append({**row, "action": "killed"})
        else:
            out.append({**row, "action": "terminated"})
    return out


def _registry_health(name: str) -> str | None:
    reg = Path(__file__).resolve().parent / "runtime-registry.json"
    try:
        r = json.loads(reg.read_text())
    except (OSError, ValueError):
        return None
    for x in r.get("runtimes", []) + r.get("brokers", []):
        if x.get("name") == name:
            return x.get("health_url")
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install"); i.add_argument("name"); i.add_argument("binary"); i.add_argument("--source", default="")
    r = sub.add_parser("rollback"); r.add_argument("name")
    s = sub.add_parser("status"); s.add_argument("name", nargs="?")
    m = sub.add_parser("migrate"); m.add_argument("name"); m.add_argument("--health")
    fp = sub.add_parser("free-port"); fp.add_argument("name"); fp.add_argument("port", type=int)
    for p in (i, r, m, fp):
        p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "install":
            out: Any = install(a.name, Path(a.binary), source=a.source, dry_run=a.dry_run)
        elif a.cmd == "rollback":
            out = rollback(a.name, dry_run=a.dry_run)
        elif a.cmd == "status":
            out = status(ROOT, a.name)
        elif a.cmd == "free-port":
            out = free_port(a.name, a.port, dry_run=a.dry_run)
        else:
            url = a.health or _registry_health(a.name)
            if not url:
                raise ValueError(f"{a.name}: no health URL (registry or --health)")
            out = migrate(a.name, url, dry_run=a.dry_run)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    return 0 if not (isinstance(out, dict) and out.get("action") == "reverted") else 1


if __name__ == "__main__":
    sys.exit(main())
