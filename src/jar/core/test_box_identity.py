#!/usr/bin/env python3
# gate: hermetic   (temp key paths; systemd-creds faked; titan's real key untouched)
"""A box's identity (#407 item 3) is server_identity's Ed25519 key (the one a kid's Watch already pins). On a box with
a TPM it's made sealed (systemd-creds, tpm2) and never stored in the clear; without a TPM, or on titan, it's the 0600
file as before; sealing is detected, never required, and titan never even asks. A sealed key that won't decrypt is an
error, never a new key. GET /v1/box/identity gives the public half only, admin only. Fictional; no real TPM.
Run: python3 scripts/test_box_identity.py
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-box-identity-"))
sys.path.insert(0, str(Path(__file__).parent))
import server_identity as S  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


CALLS: list[list[str]] = []
TPM = {"present": True, "decrypt_ok": True}
_run = subprocess.run


def fake_creds(cmd, *a, input=None, **k):
    """systemd-creds, faked: has-tpm2 by TPM["present"]; encrypt writes the input XOR'd (a stand-in for sealing, so
    the credential never holds the key in the clear); decrypt undoes it, or fails."""
    if not cmd or cmd[0] != "systemd-creds":
        return _run(cmd, *a, input=input, **k)
    CALLS.append(cmd[1:])
    if cmd[1] == "has-tpm2":
        return subprocess.CompletedProcess(cmd, 0 if TPM["present"] else 1, b"", b"")
    if cmd[1] == "encrypt":
        Path(cmd[-1]).write_bytes(b"CRED" + bytes(b ^ 0x5A for b in input))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    if cmd[1] == "decrypt":
        if not TPM["decrypt_ok"]:
            return subprocess.CompletedProcess(cmd, 1, b"", b"tpm changed")
        blob = Path(cmd[-2]).read_bytes()[4:]
        return subprocess.CompletedProcess(cmd, 0, bytes(b ^ 0x5A for b in blob), b"")
    raise AssertionError(cmd)


subprocess.run = fake_creds


def fresh(name: str, box: bool, tpm: bool = True) -> None:
    os.environ["SERVER_IDENTITY_KEY"] = str(_TMP / name / "server-identity.key")
    if box:
        os.environ["BOX_SETUP"] = "1"
    else:
        os.environ.pop("BOX_SETUP", None)
    TPM.update(present=tpm, decrypt_ok=True)
    S._KEY = None
    CALLS.clear()


def private_raw() -> bytes:
    from cryptography.hazmat.primitives import serialization as s
    return S._private().private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())


print("── titan (no BOX_SETUP): the plain 0600 file, and never a TPM call ──")
fresh("titan", box=False)
ident = S.identity()
p = Path(os.environ["SERVER_IDENTITY_KEY"])
check("made as the 0600 file", (p.exists(), oct(p.stat().st_mode & 0o777), S.storage()), (True, "0o600", "file"))
check("titan never asks systemd-creds anything", CALLS, [])

print("── a box with a TPM: sealed, never in the clear ──")
fresh("box-tpm", box=True)
ident = S.identity()
raw = private_raw()
p = Path(os.environ["SERVER_IDENTITY_KEY"])
cred = S.sealed_path()
check("sealed: a credential, no plain key file", (cred.exists(), p.exists(), S.storage()), (True, False, "tpm"))
check("…the credential is 0600 and doesn't hold the key in the clear", (oct(cred.stat().st_mode & 0o777),
      raw in cred.read_bytes()), ("0o600", False))
check("…detected, then sealed with tpm2", [c[0] for c in CALLS], ["has-tpm2", "encrypt"])
S._KEY = None
check("a restart decrypts it: the same identity", S.identity(), ident)
check("…and the same key signs what the Watch checks", S.verify(ident["key"], b"n" * 16,
      base64.b64decode(S.hello(base64.b64encode(b"n" * 16).decode())["sig"]) and S.hello(base64.b64encode(b"n" * 16).decode())["sig"]),
      True)
S._KEY = None
TPM["decrypt_ok"] = False
try:
    S.identity()
    outcome = "a key"
except RuntimeError as exc:
    outcome = str(exc)
check("a sealed key that won't decrypt: an error, never a new key (the identity never changes by accident)",
      (outcome, Path(os.environ["SERVER_IDENTITY_KEY"]).exists()), ("the sealed identity key couldn't be decrypted", False))

print("── a box without a TPM: the 0600 file (sealing optional) ──")
fresh("box-notpm", box=True, tpm=False)
S.identity()
check("no TPM: the plain 0600 file, no credential", (Path(os.environ["SERVER_IDENTITY_KEY"]).exists(),
      S.sealed_path().exists(), S.storage()), (True, False, "file"))
check("…asked once, then nothing sealed", [c[0] for c in CALLS], ["has-tpm2"])

print("── the route: public half only, admin only ──")
src = (Path(__file__).parent / "agent_api_server.py").read_text()
route = src[src.index('@app.get("/v1/box/identity")'):src.index('@app.get("/v1/box/identity")') + 700]
check("it answers identity() (alg, public key, fingerprint) and storage, nothing private",
      ("server_identity.identity" in route, "storage()" in route, "_private" in route), (True, True, False))
fresh("route", box=True)
body = json.dumps({**S.identity(), "storage": S.storage()})
check("…the private key appears nowhere in it", base64.b64encode(private_raw()).decode() in body, False)
import household_identity as hh  # noqa: E402
check("not in ROUTE_ROLES: admin only", any(rx.fullmatch("/v1/box/identity") for _, rx, _ in hh.ROUTE_ROLES), False)

subprocess.run = _run
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
