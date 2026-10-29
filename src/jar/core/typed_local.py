#!/usr/bin/env python3
"""Typed answers from titan's local model (titan-node#941, from the #923 review of Thunc).

The local deciders (mail triage, mail bills, item envelopes) each asked llama-server for `json_object`, then cut
the JSON out of the reply with a regex and gave up on anything else. Here the answer's shape is a JSON schema that
llama-server turns into a grammar, so the reply can't come back in another shape; an `ensure` check (code, never
the model) can reject a well-formed answer, and the model gets one retry told why.

  obj, model = ask(prompt, schema, url, ensure=lambda o: None if o["x"] else "x is empty")

`ask` returns (the parsed answer, the server's model id), or (None, model) when no attempt passed. A network or
server error is raised, so a caller's own fallback (the next model URL, the title instead of a summary) still runs.
Household text stays on titan: only a localhost or private-network llama-server is ever called by the deciders.
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Callable

Post = Callable[[str, dict[str, Any], float], dict[str, Any]]


def enum(values) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def nullable(t: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [t, {"type": "null"}]}


def obj(props: dict[str, Any], required=()) -> dict[str, Any]:
    """An object with these properties; only `required` must appear (the others may be left out)."""
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def parse(text: str) -> dict[str, Any] | None:
    """The JSON object in a reply (a thinking block and stray prose around it are ignored), or None."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    for cand in (t, (re.search(r"\{.*\}", t, re.S) or [None])[0]):
        if not cand:
            continue
        try:
            v = json.loads(cand)
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    return None


def ask(prompt: str, schema: dict[str, Any], url: str, *, name: str = "answer",
        ensure: Callable[[dict[str, Any]], str | None] | None = None, max_tokens: int = 300,
        temperature: float = 0.0, timeout: float = 120, retries: int = 1, no_think: bool = True,
        post: Post | None = None) -> tuple[dict[str, Any] | None, str]:
    """One schema-constrained call, checked by `ensure`, retried `retries` times with the reason."""
    post = post or _post
    messages = [{"role": "user", "content": prompt + (" /no_think" if no_think else "")}]
    model = "local"
    for attempt in range(retries + 1):
        payload = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature,
                   "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}}
        if no_think:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        r = post(url, payload, timeout)
        model = str(r.get("model") or model)
        content = str(((r.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        got = parse(content)
        why = "the answer wasn't a JSON object" if got is None else (ensure(got) if ensure else None)
        if not why:
            return got, model
        if attempt < retries:
            messages = messages + [{"role": "assistant", "content": content[:2000]},
                                   {"role": "user", "content": f"That answer was rejected: {why}. Answer again, in "
                                                               "the same JSON shape." + (" /no_think" if no_think else "")}]
    return None, model
