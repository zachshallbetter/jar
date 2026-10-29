#!/usr/bin/env python3
# gate: hermetic
"""titan's voice for a reply (jellybean#709, the Mac's talk mode): POST /v1/speech {text} → audio/wav, Kokoro first
(X-Speech-Voice: local), Gemini as the fallback (gemini), 503 when neither speaks (the app uses its own device's voice).
Nothing is kept on titan. Every voice is a fake here; no model, no network.
Run: python3 scripts/test_speech.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-speech-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "AGENT_SPEECH_DIR": "speech"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
(_TMP / "speech").mkdir()
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.160} expected={expected!r:.160}")


WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24
said: list = []
lib._kokoro_wav = lambda text, timeout=None: said.append(("kokoro", text)) or WAV
lib._gemini_wav = lambda text: said.append(("gemini", text)) or WAV

print("── the voice, in order ──")
check("Kokoro first: local", lib.speech_wav("Hello"), (WAV, "local"))
check("…Gemini never asked", [w for w, _ in said], ["kokoro"])
lib._kokoro_wav = lambda text, timeout=None: None
check("Kokoro down: Gemini's", lib.speech_wav("Hello"), (WAV, "gemini"))
lib._gemini_wav = lambda text: None
check("neither: None (the app speaks itself)", lib.speech_wav("Hello"), None)

print("── the route ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}
lib._kokoro_wav = lambda text, timeout=None: WAV
r = c.post("/v1/speech", json={"text": "  It's 54 degrees\n and sunny. "}, headers=M)
check("audio/wav, local, not cached", (r.status_code, r.headers.get("content-type"), r.headers.get("x-speech-voice"),
                                       r.headers.get("cache-control"), r.content[:4]), (200, "audio/wav", "local", "no-store", b"RIFF"))
heard = []
lib._kokoro_wav = lambda text, timeout=None: heard.append(text) or WAV
c.post("/v1/speech", json={"text": "  It's 54 degrees\n and sunny. "}, headers=M)
check("the text is spoken as one line", heard, ["It's 54 degrees and sunny."])
lib._kokoro_wav = lambda text, timeout=None: None
lib._gemini_wav = lambda text: WAV
check("the fallback says so", c.post("/v1/speech", json={"text": "Hi"}, headers=M).headers.get("x-speech-voice"), "gemini")
lib._gemini_wav = lambda text: None
r = c.post("/v1/speech", json={"text": "Hi"}, headers=M)
check("neither voice: 503, said plainly", (r.status_code, r.json()), (503, {"error": "titan's voice isn't available right now"}))
check("no text: 400", c.post("/v1/speech", json={"text": "  "}, headers=M).status_code, 400)
check("too long: 400", c.post("/v1/speech", json={"text": "x" * (lib.SPEECH_MAX + 1)}, headers=M).status_code, 400)
check("every role may ask (a kid's Mac, the kitchen TV)", all(hh.route_allowed(r, "POST", "/v1/speech") for r in ("adult", "kid", "household")), True)

print("── the cloud is for grown-ups' devices only ──")
asked = []
lib._kokoro_wav = lambda text, timeout=None: None
lib._gemini_wav = lambda text: asked.append(text) or WAV
check("an adult's device, Kokoro down: Gemini speaks", lib.speech_wav("Hi", cloud=True), (WAV, "gemini"))
asked.clear()
check("a kid's or a household device, Kokoro down: nothing (its own voice), and Gemini never asked",
      (lib.speech_wav("Hi", cloud=False), asked), (None, []))
s = c.post("/v1/pair/start", json={"new_person": {"name": "Ava", "role": "kid"}}, headers=M).json()
KID = {"X-Agent-Token": c.post("/v1/pair/claim", json={"code": s["code"], "device_name": "Ava's iPad", "platform": "ios"}).json()["token"]}
r = c.post("/v1/speech", json={"text": "Bedtime story"}, headers=KID)
check("through the route: a kid gets 503, the cloud untouched", (r.status_code, asked), (503, []))
lib._kokoro_wav = lambda text, timeout=None: WAV
check("…and Kokoro when it's up", c.post("/v1/speech", json={"text": "Bedtime story"}, headers=KID).headers.get("x-speech-voice"), "local")
lib._kokoro_wav = lambda text, timeout=None: None
r = c.post("/v1/speech", json={"text": "Hi"}, headers=M)
check("the admin's cloud fallback says it left the house", (r.headers.get("x-speech-voice"), r.headers.get("x-egress")), ("gemini", "gemini"))

print("── what titan keeps: counts in memory, never who or what ──")
h = c.get("/health").json().get("speech") or {}
check("GET /health counts calls by voice and kind of device (so far: 7 answered)",
      (h.get("local", 0) + h.get("gemini", 0) + h.get("unavailable", 0), h.get("adult", 0) + h.get("kid_or_household", 0)), (7, 7))
check("…the kid's two calls are counted as a kid's or a household device's, the cloud never", (h.get("kid_or_household"), h.get("gemini")), (2, 2))
check("only those keys: no person, no device, no text", set(h) == {"local", "gemini", "adult", "kid_or_household", "rate_limited", "unavailable", "ms"}, True)
check("…and ms is durations per voice only (#554)", all(set(v) == {"n", "p50_ms", "p90_ms", "max_ms"} and set(h["ms"]) <= {"local", "gemini"}
      for v in h["ms"].values()), True)
check("no speech log is written anywhere under the test's state", [p.name for p in _TMP.rglob("*speech*.jsonl")], [])

print("── abuse limits ──")
check("20 a minute per device, then not", ([lib.speech_allowed("dev-x", now=1000.0 + i) for i in range(21)].count(True),
                                          lib.speech_allowed("dev-x", now=1061.0)), (20, True))
check("another device has its own budget", lib.speech_allowed("dev-y", now=1000.0), True)
lib._kokoro_wav = lambda text, timeout=None: WAV
codes = [c.post("/v1/speech", json={"text": "x"}, headers=KID).status_code for _ in range(lib.SPEECH_RATE + 2)]
check("through the route: past the limit, 429", codes[-1], 429)
check("…and counted", (c.get("/health").json()["speech"]["rate_limited"] >= 1), True)
check("unauthenticated: refused", c.post("/v1/speech", json={"text": "x"}).status_code, 401)

check("nothing is written on titan (the speech folder stays empty)", list((_TMP / "speech").glob("*")), [])


print("── #554: time to audio per voice, and which voice the speak tool used ──")
lib._SPEECH_MS.clear()
for ms in (400, 600, 800, 1000, 3000):
    lib.time_speech("local", ms)
lib.time_speech("gemini", 2500)
st = lib.speech_stats()
check("/health speech.ms: n, p50, p90, max per voice", (st["ms"]["local"], st["ms"]["gemini"]["n"]),
      ({"n": 5, "p50_ms": 800, "p90_ms": 3000, "max_ms": 3000}, 1))
check("…and the counts are still there", "local" in st and "unavailable" in st, True)
for _ in range(lib.SPEECH_MS_KEEP + 50):
    lib.time_speech("local", 1)
check("only the last SPEECH_MS_KEEP are kept", lib.speech_stats()["ms"]["local"]["n"], lib.SPEECH_MS_KEEP)
_sl = lib._speak_local
lib._speak_local = lambda text, timeout=None: "/ai/tmp/speech-x.wav"
lib._TURN_INFO.info = {}
out = lib._execute_tool("speak", {"text": "Dinner is ready."})
check("the speak tool records local and its time in the turn log", (out.startswith("Speech synthesized"),
      lib._TURN_INFO.info.get("speak_voice"), isinstance(lib._TURN_INFO.info.get("speak_ms"), int)), (True, "local", True))
lib._speak_local = lambda text, timeout=None: None
_g = lib._gemini
def _no_gemini(*a, **k):
    raise RuntimeError("no key")
lib._gemini = _no_gemini
lib._TURN_INFO.info = {}
lib._execute_tool("speak", {"text": "Dinner is ready."})
check("both voices failing is recorded as failed", lib._TURN_INFO.info.get("speak_voice"), "failed")
lib._speak_local, lib._gemini = _sl, _g
src = Path(lib.__file__).read_text()
check("speak_voice and speak_ms are turn-log keys", ('"speak_voice", "speak_ms"' in src), True)
api = (Path(lib.__file__).parent / "agent_api_server.py").read_text()
check("the route times each spoken reply and /health shows it", ("_lib.time_speech(voice, ms)" in api,
      '"speech": _lib.speech_stats()' in api), (True, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
