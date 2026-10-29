#!/usr/bin/env python3
"""Local typed-decision server: Decider-2B behind the same POST /v1/systemone wire format as Jev.

Runs in the Decider venv (/ai/datasets/envs/decider: PyTorch 2.7.1+cu118, the last wheels that
still support the GTX TITAN X / sm_52) on GPU1 beside the agent's llama-server. decider-ai's own
serve.py forces bfloat16 on CUDA, which Maxwell can't compute, so this loads the model the way
the titan benchmark did (float16, eager, no CUDA graphs). Accuracy matched the Mac bf16 run.

One request at a time (a GPU shared with the agent must not see concurrent large batches), and
the state is capped so a long input can't push the GPU out of memory. Loopback only.

  POST /v1/systemone {state, questions, model?}  -> {"answers": {...}, "model": ..., "_meta": {...}}
  GET  /health                                    -> {"status": "ok", "model": ...}

  env: DECIDER_MODEL (Mapika/decider-2b)  DECIDER_MAX_STATE_CHARS (3000)  DECIDER_PORT (8031)
       DECIDER_TRIM (1; 0 skips returning the freed load-time CPU heap to the OS, #1544)
"""
from __future__ import annotations

import ctypes
import ctypes.util
import gc
import json
import os
import sys
import threading
import time

import torch
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

MODEL = os.environ.get("DECIDER_MODEL", "Mapika/decider-2b")
# Measured on GPU1 next to the agent (~1.2 GB free): 1500 chars fine, 4000 chars out of memory.
MAX_STATE_CHARS = int(os.environ.get("DECIDER_MAX_STATE_CHARS", "3000"))
# GPU1 is shared with the agent's llama-server; PyTorch's caching allocator otherwise keeps the peak
# of every long request (reserved crept from ~4.0 to ~4.9 GB, leaving ~130 MB on the card).
RESERVED_CAP_BYTES = int(float(os.environ.get("DECIDER_RESERVED_CAP_GB", "4.2")) * 1e9)

app = FastAPI(title="titan decider")
_lock = threading.Lock()
_decider = None


def _mem_kb(status_path: str = "/proc/self/status") -> dict:
    """VmRSS and VmSwap in kB from /proc/self/status; {} where there is no such file (not Linux)."""
    out = {}
    try:
        with open(status_path) as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("VmRSS", "VmSwap"):
                    out[key] = int(rest.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return out


def _malloc_trim() -> bool:
    """Ask glibc to hand freed heap pages back to the OS. Linux + glibc only; never raises."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        trim = getattr(libc, "malloc_trim", None)   # absent on musl
        if trim is None:
            return False
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return bool(trim(0))
    except Exception:   # noqa: BLE001 - a failed trim only costs host RAM, never the server
        return False


def _release_host_memory(environ=os.environ) -> None:
    """After the weights are on the GPU, return the freed CPU copy to the OS (#1544).

    The load reads the checkpoint into host RAM before moving it to cuda:0; glibc keeps those freed
    pages in the heap, so they sat in RSS and swap (1.24 GB RSS + 2.27 GB swap, steady since 09-30).
    Logs numbers only. DECIDER_TRIM=0 skips it.
    """
    if environ.get("DECIDER_TRIM", "1").strip() == "0":
        return
    before = _mem_kb()
    gc.collect()
    trimmed = _malloc_trim()
    after = _mem_kb()
    def mb(d, k):
        return f"{d[k] / 1024:.0f}" if k in d else "?"
    print(f"[decider] host memory MB: rss {mb(before, 'VmRSS')} -> {mb(after, 'VmRSS')}, "
          f"swap {mb(before, 'VmSwap')} -> {mb(after, 'VmSwap')}, trimmed={int(trimmed)}", flush=True)


def _load():
    global _decider
    from decider.infer import Decider
    t0 = time.time()
    _decider = Decider(MODEL, device="cuda:0", dtype=torch.float16, use_graphs=False)
    print(f"[decider] {MODEL} loaded in {time.time() - t0:.0f}s, "
          f"{torch.cuda.memory_allocated() / 1e9:.2f} GB", flush=True)
    # The server keeps no CPU-side copy of its own (only _decider), so the collect + trim is enough here.
    _release_host_memory()


@app.on_event("startup")
def _startup() -> None:
    _load()


@app.get("/health")
def health() -> dict:
    return {"status": "ok" if _decider is not None else "loading", "model": MODEL,
            "gpu_reserved_gb": round(torch.cuda.memory_reserved() / 1e9, 2) if _decider is not None else None}


@app.post("/v1/systemone")
async def systemone(request: Request) -> JSONResponse:
    body = await request.json()
    state, questions = body.get("state"), body.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise HTTPException(status_code=400, detail="questions must be a non-empty object")
    size = len(state if isinstance(state, str) else json.dumps(state))
    if size > MAX_STATE_CHARS:
        raise HTTPException(status_code=413, detail=f"state is {size} chars; the limit is {MAX_STATE_CHARS}")
    if _decider is None:
        raise HTTPException(status_code=503, detail="model loading")
    t0 = time.monotonic()
    try:
        with _lock:
            out = _decider.system_one(state, questions)
    except torch.cuda.OutOfMemoryError:
        raise HTTPException(status_code=503, detail="GPU out of memory for this input")
    except ValueError as exc:   # a malformed question (e.g. score criteria that aren't a list)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if torch.cuda.memory_reserved() > RESERVED_CAP_BYTES:
            torch.cuda.empty_cache()   # every path, including after an out-of-memory error
    answers = out["answers"] if isinstance(out, dict) and "answers" in out else out
    return JSONResponse({"answers": answers, "model": MODEL,
                         "_meta": {"latency_ms": int((time.monotonic() - t0) * 1000), "cost_usd": 0.0}})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DECIDER_PORT", "8031")), log_level="warning")
