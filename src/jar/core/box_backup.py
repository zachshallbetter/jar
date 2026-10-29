#!/usr/bin/env python3
"""A box's encrypted backups with a recovery phrase (titan-node#407, first slice).

The owner writes a recovery phrase down once (at setup, #411). The phrase gives a key pair: scrypt(phrase, salt) is
an X25519 private key. The box keeps only the public half (BOX_BACKUP_PUB, default /ai/config/box-backup.pub.json),
so it seals a backup every night without the phrase, and nothing on the box can open one: a stolen box or a copied
backup is unreadable. Only the phrase restores, on this box or a new one. The phrase is printed once by `init` and
never written anywhere by titan; `verify` and `restore` read it from stdin (never from argv: process lists show argv).

Plaintext only ever lives in a fresh 0700 folder under BOX_BACKUP_STAGE (/ai/tmp/box-backup-stage), outside the
backup dir; an off-box copy must take only box-*.tbk. A backup is every connectors store and world file (sqlite through backup_store.copy: WAL-safe, quick_check) and every
memory file, as a tar.gz with manifest.json (sha256 and size of each file), sealed: a fresh X25519 key per backup,
HKDF-SHA256 to an AES-256-GCM key, 1 MiB chunks, each chunk's number and a last-chunk flag in its associated data (a
cut, reordered or edited file fails to open). File: BOX_BACKUP_DIR/box-<UTC stamp>.tbk, 0600.

    box_backup.py init [--replace]       a new phrase and the box's fingerprint (printed once, on a terminal only)
    box_backup.py check <file> [--sender FP]   no phrase: who made it, which key it's sealed to, the signature
    box_backup.py backup                 seal a backup now, then rotate
    box_backup.py list                   the backups, newest first: size, sealed-to key, which box made it
    box_backup.py status                 the newest backup's age and when the owner last opened one with the phrase
                                         (exit 1 when that's 30+ days or never: the box can't check its own backups)
    box_backup.py verify <file> [--sender FP]          phrase on stdin: open it, check every hash and store
    box_backup.py restore <file> <dir> [--sender FP]   phrase on stdin: unpack into a new 0700 <dir>, never over or
                                                       around the live stores or memory

Signed (#1188): each backup carries this box's Ed25519 public key and a signature over the whole file; verify and
restore open only a backup made by this box, or by the box whose fingerprint --sender names (from the card beside
the phrase), so a file someone else sealed to the same public key is refused before it's opened.
    box_backup.py rotate                 keep the newest per day for KEEP_DAILY days and per week for KEEP_WEEKLY weeks

Env: BOX_BACKUP_DIR (/ai/backups/box), BOX_BACKUP_STAGE (/ai/tmp/box-backup-stage), BOX_BACKUP_PUB, BOX_BACKUP_SIGN_KEY, BOX_BACKUP_VERIFIED, BOX_BACKUP_KEEP_DAILY (7), BOX_BACKUP_KEEP_WEEKLY (4);
the sources: CONNECTORS_DIR, AGENT_MEMORY_DIR.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import struct
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"TBK2"     # v2 (#1188): a JSON header (KDF params, key and sender fingerprints) and an Ed25519 signature
SIG_MAGIC = b"SIG1"   # the trailer after the last chunk; a chunk length never reads as this (it'd be > 1 GB)
MAX_HEADER = 4096
CHUNK = 1 << 20
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford base32: no I, L, O, U
PHRASE_GROUPS = 6                                 # 6 groups of 4 random characters: 120 bits, plus a check group
NAME_RX = re.compile(r"^box-(\d{8}T\d{6}Z)\.tbk$")
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "maxmem": 2 ** 26, "dklen": 32}
SCRYPT_LIMITS = {"n": 2 ** 20, "r": 16, "p": 4}   # a header can't ask for more work than this (a crafted file)


class BackupError(Exception):
    pass


def backup_dir() -> Path:
    return Path(os.environ.get("BOX_BACKUP_DIR", "/ai/backups/box"))


def stage_root() -> Path:
    """Where plaintext lives while a backup is made or opened: a 0700 folder OUTSIDE the backup dir, so anything that
    copies the backup dir off the box (it must take only box-*.tbk) never sees a half-made plaintext file."""
    return Path(os.environ.get("BOX_BACKUP_STAGE", "/ai/tmp/box-backup-stage"))


def _private_tmp(prefix: str) -> Path:
    """A fresh 0700 folder under stage_root(); leftovers over an hour old (a crashed run) are swept first."""
    root = stage_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    for old in root.iterdir():
        try:
            if time.time() - old.stat().st_mtime > 3600:
                shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink()
        except OSError:
            pass
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def pub_path() -> Path:
    return Path(os.environ.get("BOX_BACKUP_PUB", "/ai/config/box-backup.pub.json"))


def sign_key_path() -> Path:
    """The box's own Ed25519 signing key (0600): it signs each backup, so a restore can tell this box's backups from a
    file someone else sealed to the same public key."""
    return Path(os.environ.get("BOX_BACKUP_SIGN_KEY", "/ai/config/box-backup.sign.key"))


def fingerprint(raw: bytes) -> str:
    """12 hex characters, grouped for writing down: 3f2a-91c0-7b4e."""
    h = hashlib.sha256(raw).hexdigest()[:12]
    return f"{h[:4]}-{h[4:8]}-{h[8:]}"


# ── the phrase ───────────────────────────────────────────────────────────────────────────────

def _check_group(body: str) -> str:
    d = hashlib.sha256(body.encode()).digest()
    return "".join(ALPHABET[b % 32] for b in d[:4])


def new_phrase() -> str:
    """7 groups of 4 (the last one a check, so a mistyped phrase is caught before any scrypt)."""
    body = "".join(secrets.choice(ALPHABET) for _ in range(4 * PHRASE_GROUPS))
    full = body + _check_group(body)
    return "-".join(full[i:i + 4] for i in range(0, len(full), 4))


def normalize(phrase: str) -> str:
    """As written down: any case, spaces or dashes; O→0, I/L→1 (Crockford). Raises when the check group is wrong."""
    s = re.sub(r"[\s-]", "", str(phrase or "")).upper().translate(str.maketrans("OIL", "011"))
    if len(s) != 4 * (PHRASE_GROUPS + 1) or any(c not in ALPHABET for c in s):
        raise BackupError("that isn't a recovery phrase (7 groups of 4 letters and digits)")
    body, check = s[:-4], s[-4:]
    if _check_group(body) != check:
        raise BackupError("that recovery phrase has a typo (its last group doesn't match)")
    return body


def _private_key(phrase: str, salt: bytes, kdf: dict[str, int] | None = None) -> X25519PrivateKey:
    k = {**SCRYPT, **{x: int(v) for x, v in (kdf or {}).items() if x in ("n", "r", "p")}}
    if any(k[x] > SCRYPT_LIMITS[x] or k[x] < 1 for x in SCRYPT_LIMITS):
        raise BackupError("this backup asks for key-derivation work beyond the limits")
    k["maxmem"] = max(SCRYPT["maxmem"], 256 * k["n"] * k["r"])
    seed = hashlib.scrypt(normalize(phrase).encode(), salt=salt, **k)
    return X25519PrivateKey.from_private_bytes(seed)


def _raw(pub: X25519PublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def init(replace: bool = False) -> str:
    """A new phrase and the box's public key. Returns the phrase: the caller shows it once and forgets it."""
    p = pub_path()
    if p.exists() and not replace:
        raise BackupError(f"{p} exists: this box already has a recovery phrase. --replace makes a new phrase AND a new "
                          "box fingerprint; backups made before it then open only with the old phrase plus "
                          "--sender <the old card's fingerprint>, so keep the old card")
    phrase = new_phrase()
    salt = secrets.token_bytes(16)
    pub = _raw(_private_key(phrase, salt).public_key())
    signer = Ed25519PrivateKey.generate()
    sender = signer.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    sk = sign_key_path()
    sk.parent.mkdir(parents=True, exist_ok=True)
    tmp_sk = sk.with_suffix(".tmp")
    fd = os.open(tmp_sk, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(signer.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                      serialization.NoEncryption())).decode())
    tmp_sk.replace(sk)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"v": 2, "salt": base64.b64encode(salt).decode(), "pub": base64.b64encode(pub).decode(),
                               "fingerprint": fingerprint(pub), "kdf": {k: SCRYPT[k] for k in ("n", "r", "p")},
                               "sender": base64.b64encode(sender).decode(), "sender_fingerprint": fingerprint(sender),
                               "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}))
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    return phrase


def box_keys() -> dict[str, str]:
    """This box's two fingerprints, for the card beside the phrase (and for check)."""
    d = json.loads(pub_path().read_text())
    return {"key": d["fingerprint"], "sender": d["sender_fingerprint"]}


def _load_pub() -> dict[str, Any]:
    try:
        d = json.loads(pub_path().read_text())
    except FileNotFoundError:
        raise BackupError("this box has no recovery phrase yet (box_backup.py init)") from None
    if d.get("v") != 2:
        raise BackupError("this box's key file is from before backups were signed: run box_backup.py init --replace")
    return d


def _signer() -> Ed25519PrivateKey:
    try:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(sign_key_path().read_text().strip()))
    except FileNotFoundError:
        raise BackupError("this box has no signing key (box_backup.py init --replace)") from None


# ── sealing ──────────────────────────────────────────────────────────────────────────────────

def _aead_key(shared: bytes, eph: bytes, pub: bytes) -> AESGCM:
    k = HKDF(algorithm=hashes.SHA256(), length=32, salt=eph + pub, info=b"titan box backup v1").derive(shared)
    return AESGCM(k)


def seal(src: BinaryIO, dst: BinaryIO) -> None:
    """Header (MAGIC, length, canonical JSON), the chunks, then the trailer: SIG_MAGIC and an Ed25519 signature over
    sha256(header and every chunk as written)."""
    d = _load_pub()
    pub = X25519PublicKey.from_public_bytes(base64.b64decode(d["pub"]))
    signer = _signer()
    eph = X25519PrivateKey.generate()
    eph_raw = _raw(eph.public_key())
    aead = _aead_key(eph.exchange(pub), eph_raw, _raw(pub))
    prefix = secrets.token_bytes(8)
    meta = {"v": 2, "kdf": d["kdf"], "salt": d["salt"], "key": d["fingerprint"], "eph": base64.b64encode(eph_raw).decode(),
            "prefix": base64.b64encode(prefix).decode(), "sender": d["sender"],
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    body = json.dumps(meta, sort_keys=True, separators=(",", ":")).encode()
    header = MAGIC + struct.pack(">H", len(body)) + body
    h = hashlib.sha256(header)
    dst.write(header)
    n, buf = 0, src.read(CHUNK)
    while True:
        nxt = src.read(CHUNK)
        last = not nxt
        ct = aead.encrypt(prefix + struct.pack(">I", n), buf, header + struct.pack(">IB", n, last))
        piece = struct.pack(">I", len(ct)) + ct
        h.update(piece)
        dst.write(piece)
        if last:
            break
        n, buf = n + 1, nxt
    dst.write(SIG_MAGIC + signer.sign(h.digest()))


def _read_header(src: BinaryIO) -> tuple[bytes, dict[str, Any]]:
    head = src.read(6)
    if len(head) != 6 or head[:4] != MAGIC:
        raise BackupError("not a box backup (or one from before backups were signed)")
    ln = struct.unpack(">H", head[4:])[0]
    if ln > MAX_HEADER:
        raise BackupError("not a box backup (its header is too long)")
    body = src.read(ln)
    try:
        meta = json.loads(body)
        assert isinstance(meta, dict) and meta.get("v") == 2
    except Exception:  # noqa: BLE001
        raise BackupError("not a box backup (its header doesn't read)") from None
    return head + body, meta


def _chunks(src: BinaryIO, header: bytes) -> Any:
    """(n, ciphertext, last) for each chunk, then checks the trailer's signature against the header's sender. Raises
    on a cut file, a missing or bad signature."""
    meta = json.loads(header[6:])
    h = hashlib.sha256(header)
    n = 0
    while True:
        ln = src.read(4)
        if len(ln) != 4:
            raise BackupError("the backup is cut short")
        size = struct.unpack(">I", ln)[0]
        if size > CHUNK + 16:   # a chunk is at most CHUNK plus the GCM tag: never allocate what an untrusted file asks
            raise BackupError("not a box backup (a chunk claims to be larger than any chunk can be)")
        ct = src.read(size)
        if len(ct) != size:
            raise BackupError("the backup is cut short")
        h.update(ln + ct)
        nxt = src.peek(4)[:4] if hasattr(src, "peek") else None
        if nxt is None:
            pos = src.tell(); nxt = src.read(4); src.seek(pos)
        last = nxt == SIG_MAGIC
        yield n, ct, last
        if last:
            break
        n += 1
    trailer = src.read(4 + 64)
    if len(trailer) != 68 or trailer[:4] != SIG_MAGIC or src.read(1):
        raise BackupError("the backup's signature is missing or has extra data after it")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(meta["sender"])).verify(trailer[4:], h.digest())
    except (InvalidSignature, ValueError, KeyError):
        raise BackupError("the backup's signature doesn't match: the file was changed, or someone else sealed it") from None


def _trusted_sender(meta: dict[str, Any], sender: str | None) -> None:
    """The backup must come from this box, or from the box whose sender fingerprint the owner gives (from the card
    beside the phrase). Anything else is refused before it's opened."""
    fp = fingerprint(base64.b64decode(meta["sender"]))
    want = (sender or "").strip().lower()
    if not want:
        try:
            want = json.loads(pub_path().read_text()).get("sender_fingerprint", "")
        except (FileNotFoundError, ValueError):
            want = ""
    if not want:
        raise BackupError("say which box made this backup: --sender <its sender fingerprint, from the card>")
    if want.replace("-", "") != fp.replace("-", ""):
        raise BackupError(f"this backup was made by a different box (sender {fp}), not the one you named")


def check(file: Path, sender: str | None = None) -> dict[str, Any]:
    """Without the phrase: the header, whether the signature holds, and whether this box (or the named sender) made
    it. Opens nothing."""
    with open(file, "rb") as src:
        header, meta = _read_header(src)
        out = {"created": meta.get("created"), "key": meta.get("key"), "kdf": meta.get("kdf"),
               "sender": fingerprint(base64.b64decode(meta["sender"])), "chunks": 0, "signed": False, "trusted": False}
        for n, _ct, _last in _chunks(src, header):
            out["chunks"] = n + 1
        out["signed"] = True
    try:
        _trusted_sender(meta, sender)
        out["trusted"] = True
    except BackupError:
        pass
    return out


def describe(file: Path) -> dict[str, Any]:
    """For `list` (#1187): the header only (no phrase, no signature walk): when it was made, the key it's sealed to
    and which box made it, and whether that's this box. {"error": …} for a file whose header doesn't read."""
    try:
        with open(file, "rb") as src:
            _h, meta = _read_header(src)
        sender = fingerprint(base64.b64decode(meta["sender"]))
    except (BackupError, KeyError, ValueError, OSError) as exc:
        return {"error": str(exc)}
    try:
        mine = box_keys()["sender"]
    except (OSError, ValueError, KeyError):
        mine = None
    return {"created": meta.get("created"), "key": meta.get("key"), "sender": sender, "this_box": sender == mine}


VERIFY_DUE_DAYS = 30   # the owner opens one backup with the phrase at least this often (#1187): the box can't


def verified_path() -> Path:
    """When the owner last opened a backup with the phrase (time and file name only, 0600): the box can't check its
    own backups, so `status` says when that's overdue."""
    return Path(os.environ.get("BOX_BACKUP_VERIFIED", "/ai/config/box-backup.verified.json"))


def _note_verified(file: Path, now: float | None = None) -> None:
    p = verified_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"at": time.time() if now is None else now, "file": file.name}, f)
    os.chmod(p, 0o600)


def status(now: float | None = None) -> dict[str, Any]:
    """The newest backup's age and the last owner verify's age, in days; `verify_due` when it's VERIFY_DUE_DAYS or
    more (or never) and there's a backup to check."""
    now = time.time() if now is None else now
    backups = listing()
    newest = (now - backups[0].stat().st_mtime) / 86400 if backups else None
    try:
        last = (now - float(json.loads(verified_path().read_text())["at"])) / 86400
    except (OSError, ValueError, KeyError, TypeError):
        last = None
    return {"backups": len(backups), "newest_days": newest, "verified_days": last,
            "verify_due": bool(backups) and (last is None or last >= VERIFY_DUE_DAYS)}


def unseal(src: BinaryIO, dst: BinaryIO, phrase: str, sender: str | None = None) -> None:
    """Decrypts into dst; the caller keeps dst private and uses it only if this returns (the signature is checked
    after the last chunk, so a bad file raises before anything is extracted)."""
    header, meta = _read_header(src)
    _trusted_sender(meta, sender)
    salt, eph_raw = base64.b64decode(meta["salt"]), base64.b64decode(meta["eph"])
    prefix = base64.b64decode(meta["prefix"])
    priv = _private_key(phrase, salt, meta.get("kdf"))
    if fingerprint(_raw(priv.public_key())) != meta.get("key"):
        raise BackupError("that recovery phrase isn't the one this backup was sealed to")
    aead = _aead_key(priv.exchange(X25519PublicKey.from_public_bytes(eph_raw)), eph_raw, _raw(priv.public_key()))
    for n, ct, last in _chunks(src, header):
        try:
            dst.write(aead.decrypt(prefix + struct.pack(">I", n), ct, header + struct.pack(">IB", n, last)))
        except Exception:  # noqa: BLE001 - InvalidTag: the file was changed
            raise BackupError("the backup didn't open: the file was changed") from None


# ── what goes in ─────────────────────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


_SIDECARS = ("-wal", "-shm", "-journal")


def sources() -> list[tuple[str, Path]]:
    """What a backup holds: the top-level files of these folders (stores, world, memory). Nothing below them."""
    import agent_paths
    live = Path(os.environ.get("CONNECTORS_DIR", "/ai/datasets/connectors"))
    return [("stores", live), ("world", live / "world"), ("memory", agent_paths.memory_dir())]


def uncovered(dirs: list[Path]) -> list[Path]:
    """The files under `dirs` a backup wouldn't hold (a factory reset erases them all): anything that isn't a
    top-level file of a source folder, or a SQLite sidecar of one. Links aren't followed (the reset doesn't either)."""
    held = set()
    for _, d in sources():
        if d.is_dir():
            held |= {f.resolve() for f in d.iterdir() if f.is_file() and not f.is_symlink()}
    out = []
    for root in dirs:
        for f in sorted(Path(root).rglob("*")):
            if f.is_symlink() or not f.is_file():
                continue
            r = f.resolve()
            base = r.with_name(r.name[:-len(next(x for x in _SIDECARS if r.name.endswith(x))):]) \
                if r.name.endswith(_SIDECARS) else r
            if base not in held:
                out.append(f)
    return out


def _gather(stage: Path) -> dict[str, Any]:
    """Copies of every store, world file and memory file under stage/ (stores/, world/, memory/), and the manifest."""
    import backup_store
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)   # a box with no stores yet seals an empty backup (10-10)
    files: dict[str, dict[str, Any]] = {}
    for kind, src_dir in sources():
        if not src_dir.is_dir():
            continue
        (stage / kind).mkdir(parents=True, exist_ok=True)
        for src in sorted(src_dir.iterdir()):
            if not src.is_file() or src.name.endswith(_SIDECARS):
                continue
            out = stage / kind / src.name
            if src.suffix == ".db":
                backup_store.copy(src, out)
            else:
                shutil.copy2(src, out)
            files[f"{kind}/{src.name}"] = {"sha256": _sha256(out), "size": out.stat().st_size}
    return {"v": 1, "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "files": files}


def backup(now: datetime | None = None) -> Path:
    """Seal a backup into BOX_BACKUP_DIR (plaintext only ever in a 0700 temp folder, removed after)."""
    _load_pub()   # no key: refuse before copying anything
    _signer()
    dest = backup_dir()
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    out = dest / f"box-{stamp}.tbk"
    for part in dest.glob("box-*.part"):   # a crashed run's half-written file
        part.unlink(missing_ok=True)
    stage = _private_tmp("stage-")
    try:
        manifest = _gather(stage / "b")
        (stage / "b" / "manifest.json").write_text(json.dumps(manifest, indent=1))
        tgz = stage / "b.tgz"
        with tarfile.open(tgz, "w:gz") as t:
            t.add(stage / "b", arcname=".")
        part = out.with_suffix(".part")
        fd = os.open(part, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with open(tgz, "rb") as s, os.fdopen(fd, "wb") as d:
            seal(s, d)
        part.replace(out)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return out


# ── opening ──────────────────────────────────────────────────────────────────────────────────

def _safe_extract(tgz: Path, into: Path) -> None:
    """Files and folders only, every one inside `into` (a path check, not a string prefix: ../b2 isn't inside b), then
    tarfile's own "data" filter as a second wall (no links, devices, absolute paths or set-uid bits)."""
    root = into.resolve()
    with tarfile.open(tgz, "r:gz") as t:
        for m in t.getmembers():
            p = (into / m.name).resolve()
            if not (m.isfile() or m.isdir()) or not (p == root or p.is_relative_to(root)):
                raise BackupError(f"refusing a strange entry in the backup: {m.name}")
        t.extractall(into, filter="data")


def _open(file: Path, phrase: str, into: Path, sender: str | None = None) -> dict[str, Any]:
    tmp = _private_tmp("open-")
    tgz = tmp / "b.tgz"   # in a fresh 0700 folder: nothing there to follow or overwrite
    try:
        fd = os.open(tgz, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with open(file, "rb") as s, os.fdopen(fd, "wb") as d:
            unseal(s, d, phrase, sender)
        into.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(into, 0o700)   # household data: the restore target is the service user's only
        _safe_extract(tgz, into)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    manifest = json.loads((into / "manifest.json").read_text())
    bad = [name for name, f in manifest["files"].items()
           if not (into / name).is_file() or _sha256(into / name) != f["sha256"]]
    for name in manifest["files"]:
        if name.endswith(".db") and name not in bad:
            c = sqlite3.connect(f"file:{into / name}?mode=ro&immutable=1", uri=True)   # no -wal/-shm beside it
            try:
                if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    bad.append(name)
            finally:
                c.close()
    return {"created": manifest["created"], "files": len(manifest["files"]), "bad": bad}


def verify(file: Path, phrase: str, sender: str | None = None) -> dict[str, Any]:
    tmp = _private_tmp("verify-")
    try:
        return _open(file, phrase, tmp / "b", sender)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def restore(file: Path, phrase: str, into: Path, sender: str | None = None) -> dict[str, Any]:
    import agent_paths
    if into.exists() and any(into.iterdir()):
        raise BackupError(f"{into} isn't empty: restore into a new folder, then move the stores in with the agent stopped")
    target = into.resolve()
    for live in (Path(os.environ.get("CONNECTORS_DIR", "/ai/datasets/connectors")), agent_paths.memory_dir()):
        live = live.resolve()
        if target == live or live in target.parents or target in live.parents:
            raise BackupError("never restore over (or around) the live stores or memory")
    return _open(file, phrase, into, sender)


# ── keeping ──────────────────────────────────────────────────────────────────────────────────

def listing() -> list[Path]:
    d = backup_dir()
    return sorted((p for p in d.iterdir() if NAME_RX.match(p.name)), reverse=True) if d.is_dir() else []


def rotate(keep_daily: int | None = None, keep_weekly: int | None = None) -> list[Path]:
    """The newest backup of each of the last keep_daily days that have one, and of each of the last keep_weekly ISO
    weeks; the newest backup always. Removes the rest (only box-*.tbk files). → the removed paths."""
    kd = int(os.environ.get("BOX_BACKUP_KEEP_DAILY", "7")) if keep_daily is None else keep_daily
    kw = int(os.environ.get("BOX_BACKUP_KEEP_WEEKLY", "4")) if keep_weekly is None else keep_weekly
    all_ = listing()
    keep: set[Path] = set(all_[:1])
    days: dict[str, Path] = {}
    weeks: dict[tuple[int, int], Path] = {}
    for p in all_:   # newest first: the first seen per day/week is its newest
        t = datetime.strptime(NAME_RX.match(p.name).group(1), "%Y%m%dT%H%M%SZ")
        days.setdefault(t.strftime("%Y%m%d"), p)
        weeks.setdefault(tuple(t.isocalendar()[:2]), p)
    keep.update(list(days.values())[:kd])
    keep.update(list(weeks.values())[:kw])
    gone = [p for p in all_ if p not in keep]
    for p in gone:
        p.unlink()
    return gone


def _read_phrase() -> str:
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass("Recovery phrase: ")
    return sys.stdin.readline().strip()


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and not (i and argv[i - 1] == "--sender")]
    sender = _flag(argv, "--sender")
    try:
        if cmd == "init":
            if not sys.stdout.isatty() and "--to-stdout" not in argv:
                # a phrase in a log or a pipe is a phrase written down where the owner didn't choose
                raise BackupError("init prints the recovery phrase once; run it on a terminal "
                                  "(or pass --to-stdout if you really mean to capture it)")
            phrase = init(replace="--replace" in argv)
            keys = box_keys()
            print("Write these down and keep them somewhere safe. titan doesn't keep the phrase; without it no backup "
                  "can be opened. The fingerprint says which box's backups to trust when restoring elsewhere.\n\n"
                  f"    recovery phrase:     {phrase}\n    box fingerprint:     {keys['sender']}\n\n"
                  "If this replaced an older phrase, keep the old card too: backups made before today open only "
                  "with the old phrase and --sender <the old card's fingerprint>.\n")
        elif cmd == "backup":
            out = backup()
            gone = rotate()
            print(f"sealed {out.name} ({out.stat().st_size} bytes); rotated out {len(gone)}")
        elif cmd == "list":
            for p in listing():
                d = describe(p)
                print(f"{p.name}  {p.stat().st_size}  " + (f"unreadable: {d['error']}" if "error" in d else
                      f"key {d['key']}  from box {d['sender']} ({'this box' if d['this_box'] else 'ANOTHER BOX'})"))
        elif cmd == "status":
            st = status()
            print(f"{st['backups']} backups; newest "
                  + ("none" if st["newest_days"] is None else f"{st['newest_days']:.1f} days old")
                  + "; last opened with the phrase " + ("never" if st["verified_days"] is None else f"{st['verified_days']:.0f} days ago")
                  + (f". Overdue: open one with `box_backup.py verify <file>` (every {VERIFY_DUE_DAYS} days)." if st["verify_due"] else "."))
            return 1 if st["verify_due"] else 0
        elif cmd == "check" and len(args) > 2:
            r = check(Path(args[2]), sender)
            print(f"{'ok' if r['signed'] and r['trusted'] else 'NOT TRUSTED'}: made {r['created']} by box {r['sender']}"
                  f" ({'this box' if r['trusted'] else 'not this box or the one named'}), sealed to key {r['key']}, "
                  f"{r['chunks']} chunks, signature holds")
            return 0 if r["trusted"] else 1
        elif cmd == "verify" and len(args) > 2:
            r = verify(Path(args[2]), _read_phrase(), sender)
            if not r["bad"]:
                _note_verified(Path(args[2]))   # the owner opened one: `status` counts from now (#1187)
            print(f"{'ok' if not r['bad'] else 'BAD'}: {r['files']} files from {r['created']}" +
                  (f"; failed: {', '.join(r['bad'])}" if r["bad"] else ""))
            return 1 if r["bad"] else 0
        elif cmd == "restore" and len(args) > 3:
            r = restore(Path(args[2]), _read_phrase(), Path(args[3]), sender)
            print(f"restored {r['files']} files from {r['created']} into {args[3]}" +
                  (f"; failed: {', '.join(r['bad'])}" if r["bad"] else ""))
            return 1 if r["bad"] else 0
        elif cmd == "rotate":
            print(f"rotated out {len(rotate())}")
        else:
            print(__doc__)
            return 2
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
