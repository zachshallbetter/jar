#!/usr/bin/env python3
"""titan's own identity for a kid's Watch (10-07 review W1): so the Watch hands its token only to the real titan.

The Watch reaches titan over plain HTTP at a few addresses (a LAN IP, titan-node.local, Tailscale), and the first that
answers used to get its token: on a friend's Wi-Fi that reuses the address, a stranger's device did. Now titan has an
Ed25519 key of its own, made by titan on first use (SERVER_IDENTITY_KEY, 0600, never leaves titan, never logged).

The contract (the Watch side is iOS's, jellybean KidClient):
  1. Pairing: GET /v1/watch/pair/request/{id}'s 200 adds
       "server_identity": {"alg": "ed25519", "key": <base64 of the 32-byte public key>, "fp": <sha256 hex of it>}
     The Watch stores `key` with its token (Keychain, this device only) and pins it from then on.
  2. Before sending its token to an address (each launch, and whenever it switches address), the Watch makes 32 random
     bytes and calls  POST /v1/watch/hello {"nonce": <base64 of them>}  (no token; public; rate-limited per source)
       → 200 {"alg": "ed25519", "key": …, "fp": …, "sig": <base64 Ed25519 signature>}
     The signature is over  b"titan-watch-hello-v1\0" + nonce  with titan's key. The Watch verifies `sig` against the
     key it PINNED (never the key in the answer), and only then sends X-Agent-Token to that address.
  3. A 401 counts as "unpaired" only from an address that passed step 2; from anything else it's "not titan".
  4. A Watch paired before this has no pinned key: it pins the key from its first successful step 2 on the address it
     already uses (trust on first use, the same as pairing itself), and from then on as above.
The first pairing stays trust-on-first-use over HTTP (an accepted risk shared with #157): a host faking the whole
pairing gets no real token, because a parent confirms the code on the real titan.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

ALG = "ed25519"
DOMAIN = b"titan-watch-hello-v1\0"
NONCE_MIN, NONCE_MAX = 16, 64


def key_path() -> Path:
    return Path(os.environ.get("SERVER_IDENTITY_KEY", "/ai/datasets/connectors/server-identity.key"))


# On a household box (#407 item 3) this key is the box's identity too (the same key type as coordinator auth), and a
# box with a TPM keeps it sealed: made there, it's stored only as a systemd credential encrypted to the TPM
# (`systemd-creds encrypt --with-key=tpm2`), and decrypted into memory when used. Without a TPM, or on titan, it's the
# plain 0600 file as before. Sealing is detected, never required. A sealed key that won't decrypt is an error, never a
# reason to make a new key (that would silently change the box's identity). Rotation is out of scope (#1449).
CRED_NAME = "server-identity"
_KEY = None


def sealed_path() -> Path:
    p = key_path()
    return p.with_name(p.name + ".cred")


def _box() -> bool:
    return os.environ.get("BOX_SETUP") == "1"


def _has_tpm() -> bool:
    """A TPM systemd can seal to, on a box only. Any failure to tell is "no"."""
    if not _box():
        return False
    import subprocess
    try:
        return subprocess.run(["systemd-creds", "has-tpm2", "--quiet"], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def storage() -> str:
    """"tpm" when the key is sealed to the TPM, else "file" (0600)."""
    return "tpm" if sealed_path().exists() else "file"


def _seal(raw: bytes) -> bool:
    import subprocess
    p = sealed_path()
    try:
        r = subprocess.run(["systemd-creds", "encrypt", "--with-key=tpm2", f"--name={CRED_NAME}", "-", str(p)],
                           input=raw, capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001
        return False
    if r.returncode != 0 or not p.exists():
        p.unlink(missing_ok=True)
        return False
    os.chmod(p, 0o600)
    return True


def _unseal() -> bytes:
    import subprocess
    r = subprocess.run(["systemd-creds", "decrypt", f"--name={CRED_NAME}", str(sealed_path()), "-"],
                       capture_output=True, timeout=30)
    if r.returncode != 0 or len(r.stdout) < 32:
        raise RuntimeError("the sealed identity key couldn't be decrypted")   # never echoes the credential
    return r.stdout[:32]


def _private():
    global _KEY
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if _KEY is not None:
        return _KEY
    p = key_path()
    if sealed_path().exists():
        _KEY = Ed25519PrivateKey.from_private_bytes(_unseal())
        return _KEY
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        raw = Ed25519PrivateKey.generate().private_bytes(*_raw_private())
        if _has_tpm() and _seal(raw):
            _KEY = Ed25519PrivateKey.from_private_bytes(raw)
            return _KEY
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
        except FileExistsError:
            pass   # another worker made it first: use theirs
    _KEY = Ed25519PrivateKey.from_private_bytes(p.read_bytes()[:32])
    return _KEY


def _raw_private():
    from cryptography.hazmat.primitives import serialization as s
    return s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption()


def public_key() -> bytes:
    from cryptography.hazmat.primitives import serialization as s
    return _private().public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)


def identity() -> dict[str, str]:
    """What the Watch pins at pairing."""
    k = public_key()
    return {"alg": ALG, "key": base64.b64encode(k).decode(), "fp": hashlib.sha256(k).hexdigest()}


def hello(nonce_b64: str) -> dict[str, str]:
    """POST /v1/watch/hello: titan's signature over the domain and the Watch's nonce. ValueError for a bad nonce."""
    try:
        nonce = base64.b64decode(str(nonce_b64 or ""), validate=True)
    except (ValueError, TypeError):
        raise ValueError("nonce must be base64") from None
    if not NONCE_MIN <= len(nonce) <= NONCE_MAX:
        raise ValueError(f"nonce must be {NONCE_MIN}–{NONCE_MAX} bytes")
    sig = _private().sign(DOMAIN + nonce)
    return {**identity(), "sig": base64.b64encode(sig).decode()}


def verify(pinned_key_b64: str, nonce: bytes, sig_b64: str) -> bool:
    """The Watch's check, in Python (for tests and for any other client that wants it)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pinned_key_b64)).verify(base64.b64decode(sig_b64), DOMAIN + nonce)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
