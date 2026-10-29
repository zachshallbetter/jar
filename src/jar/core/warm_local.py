#!/usr/bin/env python3
"""Warm the local model's prompt cache after a deploy.

A deploy that changes the tool prompt changes the start of every local-model prompt, so the first
local turn afterwards re-reads ~9-12k tokens at ~187 tok/s on the TITAN X (~50 s for a cold device
turn). One tiny request per prompt shape puts the new prefix in llama-server's cache.

Shapes (#404), warmed in this order: the Mac's device turn, the iPhone's (with the Mac's tools when
the Mac is reachable from it), then plain chat last, so the one slot holds the chat prefix the
router's warmth check (#277) looks at. These are the shapes the turn log shows reaching the local
model since chat moved to Gemini (#278). Three prefixes (~36k tokens, ~5 GiB of KV) fit the RAM
prompt cache (--cache-ram, 8 GiB by default); each extra shape costs about a minute of GPU1 and can
evict another, so add one only when the turn log's `prefix`/`role` show it going local cold.

Prints each shape's prefix id (agent_runtime_lib.prefix_id), the same id the turn log records, so a
cold local turn (`prompt_cached` small) can be matched to the shape that wasn't warm.

Run on titan with the agent env sourced: python3 scripts/warm_local.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# (label, client, device): the order is the warm-up order; chat stays last.
SHAPES = (("mac", None, True), ("iphone", "iphone", True), ("chat", None, False))


def shape_prefix(api, lib, client: str | None, device: bool) -> tuple[str | None, list[dict]]:
    """The shape's prefix id (as the turn log records it, from the turn's own first message) and what
    to send: that first system message (the part every turn of the shape shares; the per-turn
    message, [SITUATION] and history follow it) as the local brain shapes it, plus a stub question."""
    msgs = api.turn_prefix_messages(api._hh.owner_identity(), client=client, device=device)
    first = [m for m in msgs if m.get("role") == "system"][:1]
    return lib.prefix_id(first), lib._local_messages(first + [{"role": "user", "content": "hi"}])


def too_big(lib, url: str, msgs: list[dict]) -> str | None:
    """Why this shape can't be warmed, or None. Tokenizes the prompt (llama-server /tokenize: CPU, no
    generation) against the slot's context, so a shape over the window is skipped and logged instead of
    costing a guaranteed HTTP 400 on every deploy (10-01: "iphone failed: HTTP 400", 16.8k > 16,384).
    Unknown (an older server, an error): None, and the warm-up tries."""
    try:
        n_ctx = (lib._http_json(f"{url}/props", timeout=10).get("default_generation_settings") or {}).get("n_ctx")
        text = "\n".join(str(m.get("content") or "") for m in msgs)
        n = len(lib._http_json(f"{url}/tokenize", timeout=30, payload={"content": text}).get("tokens") or [])
    except Exception:  # noqa: BLE001
        return None
    if n_ctx and n > n_ctx - 512:   # the room a real turn needs on top of the prefix, at the least
        return f"{n:,} tokens > {n_ctx:,} ctx"
    return None


def main() -> int:
    import agent_api_server as api
    import agent_runtime_lib as lib
    url = (os.environ.get("AGENT_ENDPOINT") or "http://127.0.0.1:8010").rstrip("/")
    done = []
    t0 = time.time()
    for label, client, device in SHAPES:
        t = time.time()
        try:
            pid, msgs = shape_prefix(api, lib, client, device)
            why = too_big(lib, url, msgs)
            if why:
                done.append(f"{label} {pid} skipped ({why})")
                continue
            lib._http_json(f"{url}/v1/chat/completions", timeout=180, payload={
                "model": "local", "max_tokens": 1, "temperature": 0, "messages": msgs})
            done.append(f"{label} {pid} ({time.time() - t:.0f} s)")
        except Exception as exc:  # noqa: BLE001
            done.append(f"{label} failed: {str(exc)[:80]}")
    print(f"local cache warm: {', '.join(done)}; {time.time() - t0:.0f} s in all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
