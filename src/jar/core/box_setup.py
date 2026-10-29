#!/usr/bin/env python3
"""A box's first run (titan-node#411): plug in, open the setup page, name the owner, add the people, pair the phones,
say whose calendars are whose. Five minutes from unboxing to useful.

Off unless BOX_SETUP=1 (a box's profile), so titan itself never shows it. While a box isn't set up it keeps a one-time
setup key in BOX_SETUP_DIR/setup-key (0600): the sticker on the box, or `python3 box_setup.py url` on its console, is
http://<box>:7770/setup#k=<key>. The key travels in the URL's fragment (never sent to the server or its log); the page
sends it as X-Setup-Key. Every /v1/setup/* call needs it; a wrong key counts against the pairing claim limits.

The Jellybean app's path is one call: it scans the sticker's QR and sends POST /v1/setup/claim {name, device_name,
platform} with the key, and gets the owner's device token (docs/box-first-run.md). The page is the path without
the app. Setup reuses what's there: the people store (add_person, update_person) and pairing (#157: a 6-digit code and the
titan://pair link the Jellybean app opens; the app finds the box over mDNS). Setup ends when the owner's first device
is paired, or on POST /v1/setup/finish; then the key file is deleted and every setup route answers 404. After that the
owner's Jellybean adds people and pairs devices as on titan (Settings › Household).

Calendars and mail are connected from the phone after pairing (their tokens stay on the box); setup only says whose
calendar is whose (the people store's `calendars`, by title).

Factory reset (docs/box-first-run.md: "holding the button re-opens it and wipes the data directory, with #407's backup
offered first"): `python3 box_setup.py factory-reset` shows what it would erase; `--erase` does it. Console only (no
route). It refuses unless BOX_SETUP=1, BOX_DATA_DIRS names the box's data folders explicitly (no default: on titan
it's unset, so it can never run there), the agent API is stopped, and the backup folder is outside every one of them.
With a recovery key it seals a backup first and stops if that fails (`--no-backup` skips it). Then it empties each
data folder (the folders stay), re-opens setup with a new key and prints the sticker URL.

CLI: python3 box_setup.py url | state | factory-reset [--erase] [--no-backup]
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

import durable
import household_identity as hh
from ops_ports import AGENT_API_PORT

ADD_ROLES = ("adult", "kid", "household")   # the owner is the one admin; more admins are made in Settings later
KEY_TTL_S = 30 * 60   # the key works for 30 minutes from its first use; a power-cycle (the server starting) re-arms it
_lock = threading.RLock()
_op_lock = threading.Lock()   # one setup action at a time: check and act together (two claims can't both win)
limiter = hh.ClaimLimiter()


class SetupError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def enabled() -> bool:
    return os.environ.get("BOX_SETUP", "0").strip() == "1"


def _dir() -> Path:
    return Path(os.environ.get("BOX_SETUP_DIR", "/ai/config/box-setup"))


def _key_path() -> Path:
    return _dir() / "setup-key"


def _done_path() -> Path:
    return _dir() / "done.json"


def _owner_paired(store: hh.HouseholdStore) -> bool:
    return any(not d["revoked"] for p in store.people() if p["role"] == "admin" for d in p["devices"])


def _used_path() -> Path:
    return _dir() / "key-first-used"


def needs_setup(store: hh.HouseholdStore) -> bool:
    """On a box that hasn't finished setup and whose owner has no paired device yet."""
    if not enabled() or _done_path().exists():
        return False
    if _owner_paired(store):
        _finish(store, "owner paired")
        return False
    return True


def rearm() -> None:
    """The server starting (a power-cycle) gives the same key a fresh 30 minutes."""
    _used_path().unlink(missing_ok=True)


def _key_live() -> bool:
    """Starts the 30 minutes at the first right key; False once they're up."""
    u = _used_path()
    if not u.exists():
        durable.atomic_write(u, str(time.time()))
        return True
    try:
        return time.time() - float(u.read_text().strip()) < KEY_TTL_S
    except ValueError:
        return False


def ensure_key() -> str:
    """The setup key, made on first use (32 url-safe characters, 0600)."""
    p = _key_path()
    with _lock:
        if p.exists():
            return p.read_text().strip()
        p.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(24)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n")
        return key


def _finish(store: hh.HouseholdStore, why: str) -> None:
    """Setup is over: every unused code made during it goes (an adult's, a kid's or a screen's would outlive it for 10
    minutes, #1188), then the key."""
    store.drop_all_open_codes()
    with _lock:
        d = _done_path()
        if not d.exists():
            durable.atomic_write(d, json.dumps({"done_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "why": why}))
        _key_path().unlink(missing_ok=True)
        _used_path().unlink(missing_ok=True)


def check(store: hh.HouseholdStore, presented: str | None, ip: str) -> None:
    """Raise SetupError unless setup is open and the key is right: 404 when there's no setup to do (titan, or a box
    that's set up), 401 for a wrong or missing key, 429 when one address keeps guessing."""
    if not needs_setup(store):
        raise SetupError(404, "not found")
    if presented and hmac.compare_digest(presented.strip().encode(), ensure_key().encode()):
        if not _key_live():
            raise SetupError(410, "this setup key's 30 minutes are up: unplug the box and plug it back in, then start again")
        return   # the right key gets in even while guesses are limited: guessing can't lock the owner out
    if not limiter.admit(ip):   # only wrong keys count (the page itself refreshes every 15 s)
        raise SetupError(429, "too many tries; wait a few minutes")
    limiter.failed()
    raise SetupError(401, "that setup key isn't this box's (it's on the sticker, or run box_setup.py url)")


def guarded(store: hh.HouseholdStore, presented: str | None, ip: str, fn: Any) -> Any:
    """Every setup route: the key check and the action under one lock, setup re-checked inside it, so two requests
    racing (two owner claims) can't both act on an open setup."""
    with _op_lock:
        check(store, presented, ip)
        return fn()


def state(store: hh.HouseholdStore) -> dict[str, Any]:
    """What's done so far: the owner's name, everyone with their role, calendars and paired devices."""
    people = [{"id": p["id"], "name": p["name"], "role": p["role"], "calendars": p.get("calendars") or [],
               "devices": [d["name"] for d in p["devices"] if not d["revoked"]]} for p in store.people()]
    owner = next((p for p in people if p["id"] == hh.ADMIN_PERSON_ID), None)
    return {"needs_setup": True, "owner": owner, "people": people,
            "steps": {"owner_named": (_dir() / "owner-named").exists(),
                      "people_added": len(people) > 1,
                      "calendars": any(p["calendars"] for p in people),
                      "recovery": _has_recovery(),
                      "home": _home_connected(),
                      "owner_paired": False}}


def _has_recovery() -> bool:
    import box_backup
    return box_backup.pub_path().exists()


def _home_connected() -> bool:
    import box_ha
    return box_ha.token_path().exists()


def home(store: hh.HouseholdStore) -> dict[str, Any]:
    """Connect the house (docs/box-first-run.md step 5): onboard the box's own Home Assistant (box_ha.onboard: its owner
    account and token made on the box, no laptop), then say what it found: {connected, devices: {kind: count},
    hands_off: [{entity_id, name, kind}]}. Locks, alarms, garage and gate doors and cameras are listed so the owner knows
    they're seen; setup wires no control for them (#413). Asking again after it's connected only re-reads the list."""
    import box_ha
    try:
        if not _home_connected():
            box_ha.onboard()
        return {"connected": True, **box_ha.inventory()}
    except box_ha.OnboardError as exc:
        raise SetupError(409 if "already" in str(exc) else 503, str(exc)) from None


def recovery(store: hh.HouseholdStore) -> dict[str, Any]:
    """The box's backup recovery phrase (#407's box_backup.init), made here and shown ONCE (#411 step: "write this
    down"): {phrase, groups, key, sender} (the two fingerprints for the card beside it). The box keeps only the public
    key; the phrase is never stored or logged, so asking again is 409, not the same phrase. Later backups open only
    with it (box_backup.py restore)."""
    import box_backup
    if _has_recovery():
        raise SetupError(409, "This box already has a recovery phrase; it's shown only once.")
    try:
        phrase = box_backup.init()
    except box_backup.BackupError as exc:
        raise SetupError(409, str(exc)) from None
    except OSError as exc:   # the key folder isn't writable: say so, not a bare 500 (the 10-09 sandbox found it)
        raise SetupError(503, f"The box couldn't save its backup key ({type(exc).__name__}).") from None
    return {"phrase": phrase, "groups": len(phrase.split("-")), **box_backup.box_keys()}


def name_owner(store: hh.HouseholdStore, name: Any) -> dict[str, Any]:
    """The owner is the seeded admin (its id stays "zach" internally; only the name is theirs)."""
    out = store.update_person(hh.ADMIN_PERSON_ID, name=name)
    (_dir() / "owner-named").touch()
    return out


def add_person(store: hh.HouseholdStore, name: Any, role: Any) -> dict[str, str]:
    if str(role or "").strip().lower() not in ADD_ROLES:
        raise SetupError(400, f"role must be one of: {', '.join(ADD_ROLES)}")
    return store.add_person(name, role)


def pair(store: hh.HouseholdStore, person_id: Any, label: Any = None) -> dict[str, Any]:
    """A pairing code for anyone in the household (#157's own codes: 6 digits, single use, 10 minutes). The owner has
    at most one open code: a new one replaces an unused older one, and setup closing deletes it."""
    pid = str(person_id or "")
    if pid == hh.ADMIN_PERSON_ID:
        store.drop_open_codes(pid)
    return store.start_pairing(pid, label)


def set_calendars(store: hh.HouseholdStore, person_id: Any, calendars: Any) -> dict[str, Any]:
    if not isinstance(calendars, list):
        raise SetupError(400, "calendars must be a list of calendar names")
    return store.update_person(str(person_id or ""), calendars=calendars)


def claim(store: hh.HouseholdStore, body: dict[str, Any], client_ip: str) -> dict[str, Any]:
    """The app's owner claim (docs/box-first-run.md step 2): {name?, device_name, platform} with the setup key →
    #157's claim answer {token, device, person, servers} for the owner. One call; it closes setup (the owner is
    paired), so it works once."""
    if str(body.get("platform") or "").strip().lower() not in hh.PLATFORMS:
        raise SetupError(400, f"platform must be one of: {', '.join(hh.PLATFORMS)}")   # before a code is made
    if body.get("name"):
        name_owner(store, body["name"])
    store.drop_open_codes(hh.ADMIN_PERSON_ID)
    code = store.start_pairing(hh.ADMIN_PERSON_ID, body.get("device_name"))["code"]
    try:   # keyed: not through the claim limiter (a stranger's junk codes must not lock the owner's claim out)
        out = store._claim({"code": code, "device_name": body.get("device_name"), "platform": body.get("platform")})
    except Exception:
        store.drop_open_codes(hh.ADMIN_PERSON_ID)   # never leave the code it made
        raise
    needs_setup(store)   # the owner is paired now: setup closes and the key goes
    return out


def finish(store: hh.HouseholdStore) -> dict[str, Any]:
    _finish(store, "finished on the setup page")
    return {"needs_setup": False}


def setup_urls() -> list[str]:
    key = ensure_key()
    return [f"{u}/setup#k={key}" for u in hh.server_urls()]


class ResetRefused(Exception):
    pass


def data_dirs() -> list[Path]:
    """BOX_DATA_DIRS: the box's data folders, comma separated, absolute. No default."""
    raw = [x.strip() for x in os.environ.get("BOX_DATA_DIRS", "").split(",") if x.strip()]
    return [Path(x) for x in raw]


def _api_running() -> bool:
    import urllib.request
    port = os.environ.get("AGENT_API_PORT", str(AGENT_API_PORT))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
            return True
    except Exception:  # noqa: BLE001 - nothing answering is what we want
        return False


def reset_plan() -> dict[str, Any]:
    """What a factory reset would erase: {dirs: [{path, files, bytes}], backup: bool}. Raises ResetRefused with the
    reason when it mustn't run."""
    if not enabled():
        raise ResetRefused("BOX_SETUP isn't on: this isn't a box.")
    dirs = data_dirs()
    if not dirs:
        raise ResetRefused("BOX_DATA_DIRS isn't set: name the box's data folders explicitly.")
    import box_backup
    keep = [box_backup.backup_dir().resolve(), box_backup.stage_root().resolve()]
    out = []
    for d in dirs:
        if not d.is_absolute() or len(d.resolve().parts) < 4:
            raise ResetRefused(f"{d}: give an absolute path at least three folders deep.")
        if not d.is_dir():
            raise ResetRefused(f"{d} isn't a folder.")
        r = d.resolve()
        for k in keep:
            if k == r or r in k.parents:
                raise ResetRefused(f"{d} holds the backups ({k}); keep them outside the data folders.")
        files = [f for f in r.rglob("*") if f.is_file() or f.is_symlink()]
        out.append({"path": str(r), "files": len(files), "bytes": sum(f.lstat().st_size for f in files)})
    if _api_running():
        raise ResetRefused("The agent API is running: stop it first (the reset deletes its open stores).")
    missed = box_backup.uncovered([Path(d["path"]) for d in out])
    return {"dirs": out, "backup": box_backup.pub_path().exists(), "uncovered": len(missed),
            "uncovered_in": sorted({str(f.parent) for f in missed})[:3]}


def factory_reset(backup: bool = True) -> dict[str, Any]:
    """Erase the box's household data and re-open setup. Returns {erased: plan, backup: path|None, setup_url}."""
    import box_backup
    import shutil
    plan = reset_plan()
    made = None
    if backup and plan["backup"]:
        if plan["uncovered"]:   # the backup wouldn't hold them, and the erase would delete them: refuse, erase nothing
            raise ResetRefused(f"{plan['uncovered']} files in the data folders aren't in a backup (in "
                               f"{', '.join(plan['uncovered_in'])}): point CONNECTORS_DIR / AGENT_MEMORY_DIR at them, "
                               "or reset with --no-backup knowing they're gone.")
        made = str(box_backup.backup())   # raises on failure: nothing erased
    for d in plan["dirs"]:
        for child in Path(d["path"]).iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    with _lock:
        for f in (_done_path(), _used_path(), _key_path(), _dir() / "owner-named"):
            f.unlink(missing_ok=True)
    ensure_key()
    return {"erased": plan["dirs"], "backup": made, "setup_url": setup_urls()}


def _reset_cli(argv: list[str]) -> int:
    try:
        plan = reset_plan()
    except ResetRefused as exc:
        print(f"Not resetting: {exc}")
        return 1
    for d in plan["dirs"]:
        print(f"  {d['path']}: {d['files']} files, {d['bytes'] / 1e6:.1f} MB")
    if plan["backup"] and plan["uncovered"] and "--no-backup" not in argv:
        print(f"  {plan['uncovered']} files here aren't in a backup (in {', '.join(plan['uncovered_in'])}): "
              "--erase refuses until they are, or with --no-backup.")
    if "--erase" not in argv:
        print("Dry run: nothing erased. Add --erase to erase these and re-open setup"
              + (" (a sealed backup is made first)." if plan["backup"] and "--no-backup" not in argv else "."))
        return 0
    try:
        out = factory_reset(backup="--no-backup" not in argv)
    except ResetRefused as exc:
        print(f"Not resetting: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - a failed backup stops the reset before anything is erased
        print(f"Not reset: {type(exc).__name__}: {exc}")
        return 1
    if out["backup"]:
        print(f"Backup: {out['backup']}")
    print("Erased. Setup is open again:\n" + "\n".join(out["setup_url"]))
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "url"
    if cmd == "factory-reset":
        return _reset_cli(argv)
    store = hh.get_store()
    if not enabled():
        print("BOX_SETUP isn't on: this isn't a box.")
        return 1
    if not needs_setup(store):
        print("This box is set up.")
        return 0
    if cmd == "url":
        print("\n".join(setup_urls()))
    elif cmd == "state":
        print(json.dumps(state(store), indent=2))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
