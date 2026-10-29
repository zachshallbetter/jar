#!/usr/bin/env python3
# gate: hermetic   (no model, no network: the cloud and the local vision server are fakes)
"""Pictures without the cloud (#554): view_image, describe_image, name_images and extract_bill's pictures go to
Gemini when it's configured and to titan's own vision model (LOCAL_VISION_URL) when it isn't or fails; when
neither can, the tool says so plainly. extract_bill's reading is a function that files nothing.
Run: python3 scripts/test_local_vision.py"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-vision-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
os.environ["AGENT_ENDPOINT"] = "http://127.0.0.1:18010"   # a fake local brain (below)
os.environ.pop("LOCAL_LLM_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402

failed = 0


def check(label, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r} want={want!r}"))


class _Resp:
    def __init__(self, data): self.data = data
    def read(self): return self.data
    def __enter__(self): return self
    def __exit__(self, *a): return False


calls: list[str] = []
local_reply = {"text": "A bar chart; Tuesday's blue bar is the tallest."}
local_brain = {"text": ""}


def fake_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    calls.append(url)
    if url.startswith(lib.LOCAL_VISION_URL):
        if local_reply["text"] is None:
            raise OSError("connection refused")
        return _Resp(json.dumps({"choices": [{"message": {"content": local_reply["text"]}}]}).encode())
    if "generativelanguage" in url:
        raise OSError("cloud down")
    if url.startswith("http://127.0.0.1:18010"):
        return _Resp(json.dumps({"choices": [{"message": {"content": local_brain["text"]}}]}).encode())
    raise OSError("unexpected " + url)


lib.urllib.request.urlopen = fake_urlopen
png = _TMP / "chart.png"
png.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="))
b64 = base64.b64encode(png.read_bytes()).decode()

print("── who reads a picture ──")
text, who = lib._vision(b64, "image/png", "What's in it?")
check("no Gemini key: titan's own model answers", (who, "Tuesday" in text, any("generativelanguage" in c for c in calls)),
      ("local", True, False))
calls.clear()
os.environ["GEMINI_API_KEY"] = "test-key-not-real"
text, who = lib._vision(b64, "image/png", "What's in it?")
check("Gemini configured but failing: tried first, then titan's own",
      (who, "generativelanguage" in calls[0], calls[-1].startswith(lib.LOCAL_VISION_URL)), ("local", True, True))
os.environ.pop("GEMINI_API_KEY", None)
local_reply["text"] = None
try:
    lib._vision(b64, "image/png", "What's in it?")
    check("neither: raises", False, True)
except RuntimeError as exc:
    check("neither: a plain reason", ("isn't set up" in str(exc), "isn't running" in str(exc)), (True, True))
check("describe_image says it plainly", lib.execute_tool("describe_image", {"path": str(png)}).startswith("Vision error:"), True)

print("── describe_image and extract_bill without the cloud ──")
local_reply["text"] = "A bar chart; Tuesday's blue bar is the tallest."
out = lib.execute_tool("describe_image", {"path": str(png), "question": "Which day is tallest?"})
check("describe_image: the local answer, marked as the file's own text", ("Tuesday" in out, "⟦" in out or "untrusted" in out.lower()),
      (True, True))
local_reply["text"] = '```json\n{"doc_type": "bill", "vendor": "Riverside Water & Power", "amount": 142.37, ' \
                      '"currency": "USD", "due_date": "2026-10-21", "summary": "September water and power"}\n```'
fields, text = lib._extract_bill_fields(str(png), "bill")
check("a bill picture: fields from the local model, fence and all", (fields["vendor"], fields["amount"], fields["due_date"], text),
      ("Riverside Water & Power", 142.37, "2026-10-21", ""))
check("…and reading files nothing (no runtime call)", [c for c in calls if "/v1/nodes" in c], [])
txt = _TMP / "bill.txt"
txt.write_text("Riverside Water & Power\nAmount due: $142.37\nDue date: October 21, 2026\n")
lib._document_extract_text = lambda p, timeout=30: Path(p).read_text()
local_brain["text"] = '<think></think>{"doc_type": "bill", "vendor": "Riverside Water & Power", "amount": 142.37, ' \
                      '"due_date": "2026-10-21", "summary": "Water and power"}'
calls.clear()
fields, text = lib._extract_bill_fields(str(txt), "bill")
check("a text bill, no Gemini: titan's local brain reads the fields", (fields["vendor"], fields["amount"], "142.37" in text,
      any(c.startswith("http://127.0.0.1:18010") for c in calls), any("generativelanguage" in c for c in calls)),
      ("Riverside Water & Power", 142.37, True, True, False))
import inspect  # noqa: E402
check("both local prompts ask for null over a guessed date (10-07: the 8B filled in today as issued_date)",
      inspect.getsource(lib._extract_bill_fields).count("never fill in a date it doesn't state"), 2)
_ef, _rr, _eh, _et = lib._extract_bill_fields, lib._runtime_request, lib._ensure_hub, lib.execute_tool
lib._extract_bill_fields = lambda path, kind: ({"doc_type": "bill", "vendor": "Riverside Water & Power", "amount": 142.37,
                                                "currency": None, "issued_date": None, "due_date": "2026-10-21",
                                                "summary": None}, "Riverside Water & Power\nAmount due: $142.37")
lib._runtime_request = lambda *a, **k: {}
lib._ensure_hub = lambda *a, **k: True
lib.execute_tool = lambda name, args: "ok"
pdf = _TMP / "bill.pdf"
pdf.write_bytes(b"%PDF fake")
said = lib._execute_tool("extract_bill", {"path": str(pdf)})
lib._extract_bill_fields, lib._runtime_request, lib._ensure_hub, lib.execute_tool = _ef, _rr, _eh, _et
check("filing with null fields: no 'None' in the reply", ("None" in said, said.splitlines()[0].startswith("Filed a bill from")),
      (False, True))
print(f"{'PASS' if not failed else 'FAIL'}: {failed} failed")
sys.exit(1 if failed else 0)
