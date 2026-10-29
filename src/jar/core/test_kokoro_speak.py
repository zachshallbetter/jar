#!/usr/bin/env python3
# gate: hermetic   (a fake Kokoro engine and a fake voice server on localhost; Gemini stubbed; nothing leaves)
"""titan's local voice (#554): scripts/kokoro_server.py with a fake engine, and the `speak` tool using it
first and Gemini only when it isn't there; the egress ledger says gemini only for a fallback. Hermetic.

Run: python3 scripts/test_kokoro_speak.py
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-kokoro-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "AGENT_SPEECH_DIR": "speech"}.items():
    os.environ[k] = str(_TMP / v)
(_TMP / "speech").mkdir()
os.environ["MEMORY_HYBRID"] = "0"
os.environ["GEMINI_API_KEY"] = "test-key-not-real"
os.environ["AGENT_SPEAK_LOCAL"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import egress  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import kokoro_server as ks  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}: {detail}")


# --- The server, with a fake engine (no model)
class FakeEngine:
    def __init__(self):
        self.calls = []

    def get_voices(self):
        return ["af_heart", "am_michael"]

    def create(self, text, voice, speed, lang):
        import numpy as np
        self.calls.append((text, voice, speed, lang))
        return np.zeros(2400, dtype=np.float32), 24000


engine = FakeEngine()
voice = ks.Voice(engine)
srv = ThreadingHTTPServer(("127.0.0.1", 0), ks.handler(voice))
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"


def post(path, obj):
    req = urllib.request.Request(base + path, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


code, ctype, body = post("/v1/audio/speech", {"input": "The recycling goes out tonight.", "voice": "am_michael", "speed": 1.1})
check("server: a WAV for the text (24 kHz, 16-bit mono)", code == 200 and ctype == "audio/wav" and body[:4] == b"RIFF"
      and body[24:28] == (24000).to_bytes(4, "little"), f"{code} {ctype} {body[:12]!r}")
check("server: the voice and speed asked for, US English", engine.calls[-1] == ("The recycling goes out tonight.", "am_michael", 1.1, "en-us"),
      str(engine.calls[-1]))
post("/v1/audio/speech", {"input": "x", "voice": "nobody", "speed": 9})
check("server: an unknown voice is the default, the speed is clamped", engine.calls[-1][1:3] == ("af_heart", 2.0), str(engine.calls[-1]))
check("server: no input is a 400", post("/v1/audio/speech", {"input": "  "})[0] == 400, "")
check("server: too long is a 413", post("/v1/audio/speech", {"input": "a" * 5000})[0] == 413, "")
with urllib.request.urlopen(base + "/health", timeout=5) as r:
    health = json.loads(r.read())
check("server: /health says ok and the voices", health.get("ok") and health.get("voices") == 2, str(health))
os.environ["KOKORO_HOST"] = "0.0.0.0"
import importlib  # noqa: E402
importlib.reload(ks)
check("server: refuses to bind anything but localhost", ks.main() == 2, "")
os.environ["KOKORO_HOST"] = "127.0.0.1"

# --- The speak tool: local first
gemini_calls = []


def fake_gemini(model, parts, gen_config=None, timeout=90):
    egress.note("gemini")
    gemini_calls.append(model)
    import base64
    return {"candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(b"\0\0" * 2400).decode()}}]}}]}


lib._gemini = fake_gemini
os.environ["KOKORO_URL"] = base
sink = []
prev = egress.begin(sink)
out = lib.execute_tool("speak", {"text": "Good morning."})
egress.begin(prev)
saved = out.split("saved to ", 1)[-1].strip()
check("speak: titan's own voice when it's there, saved under the speech dir", out.startswith("Speech synthesized")
      and saved.startswith(str(_TMP / "speech")) and Path(saved).read_bytes()[:4] == b"RIFF", out)
check("speak: nothing left the house (no gemini call, no egress)", gemini_calls == [] and "gemini" not in sink, f"{gemini_calls} {sink}")

os.environ["KOKORO_URL"] = "http://127.0.0.1:9"   # nothing listens
sink = []
prev = egress.begin(sink)
out = lib.execute_tool("speak", {"text": "Good morning."})
egress.begin(prev)
check("speak: the local voice down → Gemini, as before, and the ledger says so",
      out.startswith("Speech synthesized") and gemini_calls == ["gemini-3.1-flash-tts-preview"] and "gemini" in sink, f"{out} {sink}")

os.environ["KOKORO_URL"] = base
os.environ["AGENT_SPEAK_LOCAL"] = "0"
gemini_calls.clear()
lib.execute_tool("speak", {"text": "Hi."})
check("speak: AGENT_SPEAK_LOCAL=0 turns the local voice off (Gemini only)", gemini_calls == ["gemini-3.1-flash-tts-preview"], str(gemini_calls))
os.environ["AGENT_SPEAK_LOCAL"] = "1"
check("speak: no text is still an error", lib.execute_tool("speak", {"text": " "}).startswith("Error"), "")

# #937: a spoken reply's WAV doesn't outlive SPEECH_TTL_S; only speak's own files are touched.
import time as _t  # noqa: E402
sp = _TMP / "speech"
old = sp / "speech-0ld00000.wav"; old.write_bytes(b"RIFF")
fresh = sp / "speech-fresh000.wav"; fresh.write_bytes(b"RIFF")
other = sp / "keep-me.wav"; other.write_bytes(b"RIFF")
link = sp / "speech-link0000.wav"; link.symlink_to(other)
past = _t.time() - lib.SPEECH_TTL_S - 60
for f in (old, other):
    os.utime(f, (past, past))
os.utime(link, (past, past), follow_symlinks=False)
nxt = lib._speech_path()
check("#937: a WAV older than the TTL is removed", not old.exists(), str(sorted(x.name for x in sp.iterdir())))
check("#937: a fresh WAV is kept (it may not have been played yet)", fresh.exists(), "")
check("#937: other files and symlinks are left alone", other.exists() and link.is_symlink(), "")
check("#937: the next path is a new speech-*.wav in AGENT_SPEECH_DIR",
      nxt.startswith(str(sp)) and os.path.basename(nxt).startswith("speech-") and not os.path.exists(nxt), nxt)

srv.shutdown()
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
