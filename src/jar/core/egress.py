"""What left the house during a turn (#409): content-free kinds of external calls, per turn.

A turn opens a sink (agent_runtime_lib._run_agent_turn); anything that sends data off titan during the
turn, on the turn's own thread, calls note(kind). The turn log records the list as `egress`, the API's
`done` event carries it (the apps' "left the house" badge), and the weekly turn report counts it per
person. Kinds name a destination, never what was sent:

  gemini      Google Gemini (the chat brain, image understanding, bills, speech, image generation,
              web grounding, summaries)
  ai_gateway  Vercel AI Gateway (OpenAI-compatible cloud brain fallback / pin)
  jev         Jev, the hosted decision model (routing, skill pick, fact gates, file pick, jev_decide)
  web_search  a web search engine (Brave; Gemini grounding also notes gemini)
  fetch_url   a web page the turn fetched
  weather     Open-Meteo (a place name or coordinates)
  push        Apple's push service (a card's title and text, to the person's devices)
  github      GitHub (an issue for Claude's work queue; the daily read of the admin's starred repos)
  claude      a Claude session, through the Mac's send_to_claude
  mail        the person's own mail server (mail_server.py, #955): a read of mail it already holds

Calls the person's own devices make (their Mac reading files, the iPhone's travel time) aren't titan's
egress and aren't listed. Background work outside a turn (the nightly letter, memory extraction with
MEMORY_EXTRACT_BRAIN=gemini) has no sink and isn't counted per turn.
"""
from __future__ import annotations

import threading
from typing import Any

KINDS = ("gemini", "ai_gateway", "jev", "web_search", "fetch_url", "weather", "push", "github", "claude", "mail", "youtube")
_LOCAL = threading.local()


def begin(sink: list[str] | None) -> Any:
    """Point this thread's notes at `sink` (the turn's list), or stop recording with None. Returns the
    previous sink, so a turn run inside another turn can put it back."""
    prev = getattr(_LOCAL, "sink", None)
    _LOCAL.sink = sink
    return prev


def note(kind: str) -> None:
    """Record that this turn sent something to `kind` (once per kind). A no-op outside a turn."""
    sink: Any = getattr(_LOCAL, "sink", None)
    if isinstance(sink, list) and kind not in sink:
        sink.append(kind)


def current() -> list[str] | None:
    sink = getattr(_LOCAL, "sink", None)
    return list(sink) if isinstance(sink, list) else None
