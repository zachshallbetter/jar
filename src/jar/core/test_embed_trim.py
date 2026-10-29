#!/usr/bin/env python3
"""Memory embeddings trim inputs to what titan-embeddings accepts (agent_memory._embed). Hermetic.
Run: python3 scripts/test_embed_trim.py"""
import io, json, os, sys, tempfile, urllib.error
from pathlib import Path
os.environ["AGENT_MEMORY_DIR"] = tempfile.mkdtemp(prefix="titan-embed-")
sys.path.insert(0, str(Path(__file__).parent))
import agent_memory as am  # noqa: E402
passed = failed = 0
def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.160} expected={expected!r:.160}")

sent = []
class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False
def fake_urlopen(req, timeout=10):
    batch = json.loads(req.data)["input"]
    sent.append([len(t) for t in batch])
    if max(len(t) for t in batch) > 4000:   # like the real server: too long → 500
        raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)
    return Resp(json.dumps({"data": [{"embedding": [0.1, 0.2]} for _ in batch]}).encode())
import urllib.request
urllib.request.urlopen = fake_urlopen
mem = am.AgentMemory() if hasattr(am, "AgentMemory") else None
emb = getattr(mem, "_embed", None) or getattr(am, "_embed", None)
long = "word " * 3000
out = emb([long, "short fact"])
check("a long text is trimmed, then trimmed harder after a 500, and still embedded", (out is not None, sent), (True, [[6000, 10], [2000, 10]]))
sent.clear()
check("short texts go as they are", (emb(["a", "bb"]) is not None, sent), (True, [[1, 2]]))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
