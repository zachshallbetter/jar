#!/usr/bin/env python3
"""jellybean#201 slice 2 + #248's titan half. A picture generate_image made becomes an images block, served only to
the person who made it; create_calendar_event takes location, for, purpose and from. Hermetic: temp images db and
files.
# gate: hermetic
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_T = Path(tempfile.mkdtemp())
os.environ["GENERATED_IMAGES_DB"] = str(_T / "images.db")
os.environ["GENERATED_DIR"] = str(_T)
import agent_runtime_lib as lib  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1024).to_bytes(4, "big") + (768).to_bytes(4, "big") + b"\x08\x02"
tmp = _T
real = True

lib._TURN_INFO.info = {}
lib._TURN_IO.person = {"id": "robin"}
lib._viewer = lambda: ("robin", "adult")
if real:
    f = tmp / "generated-0a1b2c3d.png"
    f.write_bytes(PNG)
    lib._note_image(str(f), "A lighthouse at dusk")
    imgs = lib._TURN_INFO.info.get("_images")
    check("noted for the turn", imgs == [{"file": str(f), "caption": "A lighthouse at dusk"}], str(imgs))
    check("recorded as its maker's", lib.generated_owner(f.name) == "robin")
    blocks = lib.reply_blocks("Here it is.", [], [], imgs)
    b = blocks[-1] if blocks else {}
    check("an images block in the agreed shape", b.get("type") == "images" and b["items"][0]["ref"] ==
          {"file": str(f), "url": "/v1/files/generated/" + f.name} and (b["items"][0]["width"], b["items"][0]["height"]) == (1024, 768),
          str(blocks))
    f.unlink()
    check("a file that's gone: no block", lib.reply_blocks("x", [], [], imgs) == [])
else:
    print("  (skipped the /ai/tmp file checks: no writable /ai/tmp here)")
check("only /ai/tmp/generated-*.png, never another path", lib.reply_blocks("x", [], [], [
    {"file": "/etc/passwd"}, {"file": str(_T) + "/../etc/generated-0a1b2c3d.png"}, {"file": str(_T) + "/speech-0a1b2c3d.wav"}]) == [])
check("an unknown or odd name has no owner", lib.generated_owner("generated-ffffffff.png") is None
      and lib.generated_owner("../x.png") is None)
lib._note_image("/home/zach/generated-0a1b2c3d.png", "x")
check("a file outside /ai/tmp is never recorded", lib.generated_owner("generated-0a1b2c3d.png") in (None, "robin"))

spec = lib._DEVICE_TOOLS["create_calendar_event"]["parameters"]
check("create_calendar_event takes location, for, purpose and from (#248)",
      all(k in spec for k in ("location", "for", "purpose", "from")), str(list(spec)))
check("…with the guidance: no address from a text, no guessed person",
      "never an address from a text" in spec["location"] and "never guessed" in spec["for"])

import household_identity as hh  # noqa: E402
check("the route is open to people, and scoped by owner inside",
      hh.route_allowed("kid", "GET", "/v1/files/generated/generated-0a1b2c3d.png"))

# #1251 D (Zach, 10-08): cloud-only pictures; with no cloud the tool says so plainly, never a key or a stack trace
_real_gemini = lib._gemini
for err in (RuntimeError("GEMINI_API_KEY is not set"), OSError("network is unreachable"), TimeoutError("timed out")):
    def _down(*a, _e=err, **k):
        raise _e
    lib._gemini = _down
    out = lib.execute_tool("generate_image", {"prompt": "A lighthouse at dusk"})
    check(f"no cloud ({type(err).__name__}): a plain answer, no key name", out.startswith("Not made: making pictures needs the cloud")
          and "GEMINI" not in out, out)
# JEV's review of #1284: the service saying no isn't "unreachable", and a picture made but not saved is never re-sent
import io as _io  # noqa: E402
import urllib.error as _ue  # noqa: E402
for code, retry in ((403, False), (429, True)):
    def _no(*a, _c=code, **k):
        raise _ue.HTTPError("https://example.invalid", _c, "nope", {}, _io.BytesIO(b""))
    lib._gemini = _no
    out = lib.execute_tool("generate_image", {"prompt": "A lighthouse at dusk"})
    check(f"HTTP {code}: refused or rate-limited, said plainly (not 'unreachable')",
          out.startswith("Not made: the image service refused or rate-limited it") and f"HTTP {code}" in out
          and "isn't reachable" not in out, out)
    check(f"…HTTP {code}: " + ("offers to try again later" if retry else "doesn't offer to try again"),
          ("offer to try again in a while" in out) if retry else ("don't offer to try again" in out), out)
import base64 as _b64x  # noqa: E402
lib._gemini = lambda *a, **k: {"candidates": [{"content": {"parts": [{"inlineData": {"data": _b64x.b64encode(b"png").decode()}}]}}]}
_gen_dir = lib.GENERATED_DIR
lib.GENERATED_DIR = "/dev/null/not-a-dir"
try:
    out = lib.execute_tool("generate_image", {"prompt": "A lighthouse at dusk"})
finally:
    lib.GENERATED_DIR = _gen_dir
check("made but not saved: its own message, and never an offer to make it again",
      out.startswith("Made, but titan couldn't save the picture") and "don't offer to make it again" in out
      and "try again" not in out, out)
lib._gemini = _real_gemini

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed")
sys.exit(1 if failures else 0)
