#!/usr/bin/env python3
"""titan's local voice (#554): Kokoro-82M (ONNX, on the CPU) behind a tiny localhost HTTP server, so the
`speak` tool can read things aloud without sending the text to Gemini.

Measured on titan 10-04 (#554): first audio 0.6–1.4 s against Gemini's 2.4–6.2 s, 2.7–4.3× real time,
~680 MB RAM, no GPU. Runs in its own venv (/ai/projects/kokoro/venv, kokoro-onnx 0.6.1) under
titan-kokoro.service, bound to 127.0.0.1 only.

  POST /v1/audio/speech  {"input": "text", "voice": "af_heart", "speed": 1.0}  → audio/wav (24 kHz, 16-bit mono)
  GET  /health                                                                → {"ok": true, "voices": N}

The model is loaded once; requests are served one at a time (onnxruntime uses every core for one).
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = os.environ.get("KOKORO_MODEL", "/ai/projects/kokoro/models/kokoro-v1.0.onnx")
VOICES = os.environ.get("KOKORO_VOICES", "/ai/projects/kokoro/models/voices-v1.0.bin")
HOST = os.environ.get("KOKORO_HOST", "127.0.0.1")
PORT = int(os.environ.get("KOKORO_PORT", "8040"))
DEFAULT_VOICE = os.environ.get("KOKORO_VOICE", "af_heart")
MAX_CHARS = 4000


def to_wav(samples, rate: int) -> bytes:
    """Float samples (-1…1) as a 16-bit mono WAV."""
    import numpy as np
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class Voice:
    """The loaded model, one synthesis at a time."""

    def __init__(self, engine=None):
        if engine is None:
            from kokoro_onnx import Kokoro
            engine = Kokoro(MODEL, VOICES)
        self.engine = engine
        self.lock = threading.Lock()
        try:
            self.voices = sorted(engine.get_voices())
        except Exception:  # noqa: BLE001
            self.voices = [DEFAULT_VOICE]

    def speak(self, text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0) -> bytes:
        voice = voice if voice in self.voices else DEFAULT_VOICE
        speed = max(0.5, min(2.0, float(speed)))
        with self.lock:
            samples, rate = self.engine.create(text, voice=voice, speed=speed, lang="en-us")
        return to_wav(samples, rate)


def handler(voice: Voice):
    class H(BaseHTTPRequestHandler):
        def _json(self, code: int, obj) -> None:
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path.rstrip("/") == "/health":
                return self._json(200, {"ok": True, "voices": len(voice.voices), "default": DEFAULT_VOICE})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path.rstrip("/") != "/v1/audio/speech":
                return self._json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 64_000)) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            text = str(body.get("input", "")).strip()
            if not text:
                return self._json(400, {"error": "input is required"})
            if len(text) > MAX_CHARS:
                return self._json(413, {"error": f"input over {MAX_CHARS} characters"})
            try:
                wav = voice.speak(text, str(body.get("voice") or DEFAULT_VOICE), body.get("speed", 1.0))
            except Exception as exc:  # noqa: BLE001
                return self._json(500, {"error": str(exc)[:200]})
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)

        def log_message(self, fmt, *args):   # one line per request, no text (it can be personal)
            sys.stderr.write("kokoro %s %s\n" % (self.command, self.path))

    return H


def main() -> int:
    if HOST not in ("127.0.0.1", "::1", "localhost"):
        print("refusing to bind anything but localhost", file=sys.stderr)
        return 2
    voice = Voice()
    print(f"kokoro: {MODEL} on {HOST}:{PORT}, {len(voice.voices)} voices", file=sys.stderr)
    ThreadingHTTPServer((HOST, PORT), handler(voice)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
