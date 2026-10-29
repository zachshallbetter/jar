"""Reply tokens to the apps as the model writes them, only once they're safe (#676).

Off unless AGENT_STREAM_TOKENS=1. Without it the turn's text goes out once, final and filtered, as before.

The reply filters (agent_runtime_lib._run_agent_turn) work on the whole reply, and a word already on someone's
screen can't be taken back. So a streamed reply is held back until it's safe:
  - only whole sentences go out (up to a sentence end or a line break), and the first piece only once it's
    `MIN_FIRST` characters long, so a short "Let me check that." before a tool call usually never shows;
  - each candidate goes through the prefix-safe filters first: live credentials removed, ⟦untrusted⟧ markers
    stripped, internal tool names made plain, the vault's [[secret-N]] placeholders resolved. An unclosed private
    key block or marker is cut off; a round that writes <tool_call>/<tool_code> text shows nothing more (and stops
    the stream if some of that round already went out);
  - a sentence that claims or promises an action ("I've sent it", "I'll let you know when…") stops the stream:
    whether it's backed is only known at the end of the turn (_with_honest_outcome, _with_backed_claims,
    _with_kept_promises), so it and everything after it wait for `done`;
  - whatever comes next must extend what was shown. If a filter changed something already shown (a credential
    redacted, a tool name rewritten), a new round says something else (a claim retry, a second run), or a round
    turns into tool calls after showing text, the stream stops there.
`done` is always the reply: the apps replace the bubble's text with it. After a stop, no more deltas go out, so
nothing is shown twice or appended to.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Callable

MIN_FIRST = 120   # characters before the first piece goes out

# A sentence ends at . ! ? or … (closing quotes and brackets included) followed by whitespace, or at a line break.
_END_RX = re.compile(r"[.!?…][\"'”’)\]]*(?=\s)|\n")
_TOOL_TEXT_RX = re.compile(r"<tool_(?:call|code)>", re.I)
_KEY_OPEN_RX = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_KEY_CLOSE_RX = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")


def enabled() -> bool:
    return os.environ.get("AGENT_STREAM_TOKENS", "").strip().lower() in ("1", "true", "yes", "on")


def _unclosed_cut(text: str) -> int:
    """Where an unclosed private-key block or ⟦ marker begins (len(text) when there's none)."""
    cut = len(text)
    opens = list(_KEY_OPEN_RX.finditer(text))
    if opens and not _KEY_CLOSE_RX.search(text, opens[-1].end()):
        cut = min(cut, opens[-1].start())
    i = text.rfind("⟦")
    if i >= 0 and text.find("⟧", i) < 0:
        cut = min(cut, i)
    return cut


class ReplyStream:
    """One turn's streamed reply. The model's text goes in with feed(); `publish(shown)` gets the whole safe
    text so far each time it grows. Thread-safe (the model runs on the turn's thread)."""

    def __init__(self, publish: Callable[[str], None], filter: Callable[[str], str] = lambda t: t,
                 stops: Callable[[str], bool] = lambda s: False, min_first: int = MIN_FIRST) -> None:
        self._publish, self._filter, self._stops, self._min_first = publish, filter, stops, min_first
        self._lock = threading.Lock()
        self._raw = ""
        self._round_shown = False     # this round's text has gone out
        self._round_blocked = False   # this round turned out to be tool-call text: nothing more from it
        self._unshield: Callable[[str], str] = lambda t: t
        self.shown = ""        # what the apps have been sent
        self.stopped = False   # nothing more goes out; `done` carries the reply
        self.why = ""          # why it stopped (the eval counts these)

    def begin_round(self, unshield: Callable[[str], str] | None = None) -> None:
        """A model call starts: its text is new (what it must extend is what was shown)."""
        with self._lock:
            self._raw = ""
            self._round_shown = self._round_blocked = False
            self._unshield = unshield or (lambda t: t)

    def abandon_round(self) -> None:
        """The call turned into tool calls: its text was a preamble. Anything shown from it stops the stream."""
        with self._lock:
            if self._round_shown and not self.stopped:
                self._stop("tool call after text")
            self._raw = ""
            self._round_shown = self._round_blocked = False

    def feed(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            if self.stopped or self._round_blocked:
                return
            self._raw += chunk
            self._advance()

    def _stop(self, why: str) -> None:
        self.stopped, self.why = True, why

    def _advance(self) -> None:
        raw = self._raw
        if _TOOL_TEXT_RX.search(raw):
            # The model wrote a tool call as text (it's run, and another round answers): nothing more from this
            # round; if some of it already went out, the stream stops.
            self._round_blocked = True
            if self._round_shown:
                self._stop("tool-call text")
            return
        ends = [m.end() for m in _END_RX.finditer(raw)]
        if not ends:
            return
        upto, stop_after = ends[-1], False
        start = 0
        for e in ends:   # the first sentence that claims or promises an action, and everything after it, wait
            if self._stops(raw[start:e]):
                upto, stop_after = start, True
                break
            start = e
        cand = raw[:upto]
        cand = cand[:_unclosed_cut(cand)]
        safe = self._filter(self._unshield(cand)).rstrip()
        if stop_after:
            self._stop("claim")
        if not safe.startswith(self.shown):
            return self._stop("changed")
        if len(safe) == len(self.shown):
            return
        if not self.shown and len(safe) < self._min_first and not stop_after:
            return
        self.shown = safe
        self._round_shown = True
        self._publish(safe)


def next_delta(sent: str, current: str) -> tuple[str, str]:
    """What to send now: the part of `current` after `sent`, when `current` extends it. ("", sent) otherwise:
    a final text that doesn't extend what was streamed goes only in `done`, which the apps show instead."""
    if len(current) > len(sent) and current.startswith(sent):
        return current[len(sent):], current
    return "", sent
