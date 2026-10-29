#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""#676: the reply streamed to the apps as Gemini writes it, only once it's safe. Hermetic: no network, no titan,
no real credentials (a fake one is planted in the environment), no turn log.

  python3 scripts/test_reply_stream.py
"""
from __future__ import annotations

import io
import json
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
FAKE_TOKEN = "sk-TEST0123456789abcdefSTREAM"
os.environ["STREAM_TEST_TOKEN"] = FAKE_TOKEN              # a live credential redact_credentials must remove
os.environ["AGENT_API_TOKEN_FILE"] = os.path.join(tempfile.mkdtemp(), "none.txt")   # never the real token file
os.environ["GEMINI_API_KEY"] = "test-key-not-real-0000000000"
os.environ.pop("AGENT_STREAM_TOKENS", None)
os.environ["AGENT_TURN_LOG"] = os.path.join(tempfile.mkdtemp(), "turns.jsonl")

import agent_runtime_lib as lib  # noqa: E402
import reply_stream as rs  # noqa: E402

lib._credential_cache = (0.0, frozenset())

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


def ok(label, cond):
    check(label, bool(cond), True)


def stream(min_first=rs.MIN_FIRST):
    shown: list[str] = []
    s = rs.ReplyStream(shown.append, filter=lib.stream_filter, stops=lib.stream_stops, min_first=min_first)
    return s, shown


def feed_in(s, text, size):
    for i in range(0, len(text), size):
        s.feed(text[i:i + size])


LONG = ("Your calendar is busy tomorrow morning, with the dentist at nine and the school drop-off before it. "
        "The afternoon is open from one until four, so that's the best window for the plumber. ")

# --- Off by default ---------------------------------------------------------------------------------------------
check("off unless AGENT_STREAM_TOKENS", rs.enabled(), False)
os.environ["AGENT_STREAM_TOKENS"] = "1"
check("on with AGENT_STREAM_TOKENS=1", rs.enabled(), True)
os.environ.pop("AGENT_STREAM_TOKENS")

# --- Hold-back: whole sentences, and the first piece only once it's long enough --------------------------------
s, shown = stream()
s.begin_round()
s.feed("Your calendar is busy tomorrow morning, with the dentist")
check("hold-back: nothing before a sentence ends", shown, [])
s.feed(" at nine. ")
check("hold-back: a short first sentence waits (a preamble before a tool call usually never shows)", shown, [])
feed_in(s, LONG[len("Your calendar is busy tomorrow morning, with the dentist at nine. "):], 7)
ok("hold-back: once long enough, whole sentences go out", shown and shown[-1].endswith("."))
ok("hold-back: every piece extends the one before", all(b.startswith(a) for a, b in zip(shown, shown[1:])))
ok("hold-back: the unfinished sentence's words never go out", all(t.endswith((".", "!", "?")) for t in shown))

# --- A live credential split across chunks never reaches the apps -----------------------------------------------
text = LONG + f"The token in that file is {FAKE_TOKEN}, which titan keeps to itself. Anything else?"
for size in (3, 5, 11, 17):
    s, shown = stream()
    s.begin_round()
    feed_in(s, text, size)
    leaked = any(FAKE_TOKEN[i:i + 8] in t for t in shown for i in range(len(FAKE_TOKEN) - 7))
    check(f"credential split in {size}-char chunks: no part of it is ever shown", leaked, False)
s, shown = stream()
s.begin_round()
s.feed(LONG + "The token is ")
s.feed(FAKE_TOKEN[:10])
s.feed(FAKE_TOKEN[10:] + ". More. ")
ok("credential: the sentence holding it is shown only redacted",
   any("[redacted: credential]" in t for t in shown) and not any(FAKE_TOKEN in t for t in shown))

# --- An unbacked claim waits for done, and done's correction replaces the streamed text -------------------------
claim = LONG + "I've sent the payment to the plumber for Thursday's visit. It'll show up in your bank by Friday. "
s, shown = stream()
s.begin_round()
feed_in(s, claim, 9)
check("claim: the stream stops at the sentence that claims an action", s.why, "claim")
ok("claim: the claim itself is never streamed", not any("I've sent" in t for t in shown))
ok("claim: what came before it was", shown and shown[-1] == LONG.strip())
final = lib.stream_filter(claim).strip() + "\n\nCorrection: that didn't go through. send_payment said: “declined”"
piece, sent = rs.next_delta(shown[-1], final)
ok("claim: done's text (with the correction) follows what was shown, so the rest goes as one last delta",
   piece.startswith(" I've sent") and piece.endswith("“declined”") and sent == final)
rewritten = "I couldn't send the payment: the bank declined it. " + LONG
check("claim: a final text that doesn't extend the stream sends no delta (done replaces it)",
      rs.next_delta(shown[-1], rewritten), ("", shown[-1]))
promise = LONG + "I'll let you know when the plumber confirms. "
s, shown = stream()
s.begin_round()
feed_in(s, promise, 13)
ok("claim: a promise to act later waits too", s.why == "claim" and not any("I'll let you know" in t for t in shown))

# --- Rounds: a preamble before a tool call, a retry that says something else -----------------------------------
s, shown = stream()
s.begin_round()
s.feed("Let me check your calendar. ")
s.abandon_round()
check("round: a short preamble before a tool call is never shown, and doesn't stop the stream", (shown, s.stopped), ([], False))
s.begin_round()
feed_in(s, LONG, 10)
ok("round: the answer after the tools streams", shown and shown[-1] == LONG.strip())
s, shown = stream(min_first=10)
s.begin_round()
s.feed("Here's a long preamble that went out. ")
s.abandon_round()
check("round: text shown from a round that then calls tools stops the stream", s.why, "tool call after text")
s, shown = stream()
s.begin_round()
feed_in(s, LONG, 12)
s.begin_round()   # a claim retry or second run: a different answer
s.feed("Actually, the dentist moved to ten, so the morning is lighter than I said before. " * 2)
check("round: a second run that doesn't extend what was shown stops the stream", s.why, "changed")
n = len(shown)
s.feed("More words. ")
check("round: nothing more after a stop", len(shown), n)

# --- Tool-call text, markers, private keys -----------------------------------------------------------------------
s, shown = stream(min_first=1)
s.begin_round()
s.feed('Checking. <tool_call>{"tool": "list_files"}</tool_call> Done. ')
check("tool text: a round with <tool_call> text shows nothing more, and nothing of it was shown", (s.stopped, shown), (False, []))
s, shown = stream(min_first=1)
s.begin_round()
s.feed("Here is part one. ")
s.feed('<tool_call>{"tool": "list_files"}</tool_call> ')
check("tool text: after some of the round went out, <tool_call> text stops the stream", s.why, "tool-call text")
check("unclosed marker: cut before it", rs._unclosed_cut("Hello. ⟦untrusted source=mail n=1a2b3c4d"), len("Hello. "))
key = "Here it is.\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3Nza\n"
check("unclosed key block: cut before it", rs._unclosed_cut(key), len("Here it is.\n"))
s, shown = stream(min_first=1)
s.begin_round()
feed_in(s, key + "MORE\n-----END OPENSSH PRIVATE KEY-----\nThat's the key. ", 6)
ok("key block: never shown, whole or in part", not any("AAAAB3" in t or "MORE" in t for t in shown))
s, shown = stream(min_first=1)
s.begin_round()
s.feed("The mail says ⟦untrusted source=mail from=Mom n=1a2b3c4d⟧Dinner at six.⟦/untrusted n=1a2b3c4d⟧ Sounds good. ")
ok("markers: stripped from what's shown", shown and "⟦" not in shown[-1] and "Dinner at six" in shown[-1])

# --- The vault's placeholders are resolved before the filters (and so redacted if they're credentials) ----------
s, shown = stream(min_first=1)
s.begin_round(lambda t: t.replace("[[secret-1]]", FAKE_TOKEN))
s.feed("Your key [[secret-1]] is set. Fine. ")
ok("vault: a placeholder is resolved, then redacted", shown and FAKE_TOKEN not in shown[-1] and "[[secret-1]]" not in shown[-1])

# --- next_delta ----------------------------------------------------------------------------------------------------
check("next_delta: the new part", rs.next_delta("Hello.", "Hello. World."), (" World.", "Hello. World."))
check("next_delta: nothing new", rs.next_delta("Hello.", "Hello."), ("", "Hello."))
check("next_delta: from nothing (streaming off: the final text, once)", rs.next_delta("", "All of it."), ("All of it.", "All of it."))

# --- Gemini's stream assembled into the usual response ----------------------------------------------------------
def sse(*chunks):
    return [("data: " + json.dumps(c) + "\n").encode() for c in chunks] + [b"\n"]


pieces: list[str] = []
resp = lib._assemble_stream(sse(
    {"candidates": [{"content": {"parts": [{"text": "Your calendar "}]}}]},
    {"candidates": [{"content": {"parts": [{"text": "is busy.", "thoughtSignature": "sig-1"}]}}]},
    {"candidates": [{"content": {"parts": [{"thought": True, "text": "private reasoning"}]}}]},
    {"candidates": [{"content": {"parts": [{"functionCall": {"name": "list_calendar_events", "args": {}}, "thoughtSignature": "sig-2"}]},
                     "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 10}}), pieces.append)
parts = resp["candidates"][0]["content"]["parts"]
check("assemble: text pieces joined into one part, its signature kept", parts[0], {"text": "Your calendar is busy.", "thoughtSignature": "sig-1"})
check("assemble: thought text kept as its own part, never handed on", (parts[1].get("thought"), "private reasoning" in "".join(pieces)), (True, False))
check("assemble: a function call kept as sent, with its thoughtSignature", parts[2], {"functionCall": {"name": "list_calendar_events", "args": {}}, "thoughtSignature": "sig-2"})
check("assemble: finish reason and usage", (resp["candidates"][0]["finishReason"], resp["usageMetadata"]), ("STOP", {"promptTokenCount": 10}))
check("assemble: each text piece handed on as it came", pieces, ["Your calendar ", "is busy."])

# --- The Gemini loop with a sink: the preamble round is dropped, the answer streams, nested runs never stream ----
class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


calls: list[str] = []
answer = LONG + "Want me to text the plumber?"
script = [
    sse({"candidates": [{"content": {"parts": [{"text": "Let me look. "}]}}]},
        {"candidates": [{"content": {"parts": [{"functionCall": {"name": "list_calendar_events", "args": {}}, "thoughtSignature": "s"}]}}]}),
    sse(*[{"candidates": [{"content": {"parts": [{"text": answer[i:i + 15]}]}}]} for i in range(0, len(answer), 15)]),
]
real_urlopen, real_exec = lib.urllib.request.urlopen, lib.execute_tool
seen_sink_in_tool: list[object] = []


def fake_urlopen(req, timeout=None):
    calls.append(req.full_url)
    return FakeResp(b"".join(script[len(calls) - 1]))


def fake_exec(name, args):
    seen_sink_in_tool.append(getattr(lib._TURN_IO, "reply_sink", "unset"))
    return "Dentist 9:00; school drop-off 8:15."


lib.urllib.request.urlopen, lib.execute_tool = fake_urlopen, fake_exec
try:
    s, shown = stream()
    lib._TURN_IO.reply_sink = s
    out = lib._run_gemini_agent([{"role": "system", "content": "You are titan."}, {"role": "user", "content": "How's tomorrow?"}],
                                max_rounds=2)
finally:
    lib.urllib.request.urlopen, lib.execute_tool = real_urlopen, real_exec
    lib._TURN_IO.reply_sink = None
ok("loop: every round used the streaming endpoint", calls and all(":streamGenerateContent?alt=sse" in u for u in calls))
check("loop: the final answer is returned as before", out, answer)
ok("loop: the preamble before the tool call was never shown", not any("Let me look" in t for t in shown))
ok("loop: the answer streamed, and done's text extends it", shown and out.startswith(shown[-1]))
check("loop: a tool (and any run inside it) sees no sink", seen_sink_in_tool, [None])
check("loop: the sink is the turn's again afterwards (a claim retry streams)", s.stopped, False)

# --- A <tool_call> text round, then the answer (JEV's review of #678: the text-protocol loop binds `call`) --------
text_round = {"candidates": [{"content": {"parts": [{"text": '<tool_call>{"tool": "get_datetime"}</tool_call>'}]}}]}
answer_round = {"candidates": [{"content": {"parts": [{"text": answer}]}}]}
plain: list[dict] = []
real_call = lib._gemini_call
lib._gemini_call = lambda body, model=None, timeout=None: [text_round, answer_round][len(plain)] if not plain.append(1) else None
lib.execute_tool = lambda name, args: "Friday, 2 October 2026, 10:00"
try:
    out_off = lib._run_gemini_agent([{"role": "user", "content": "What day is it?"}], max_rounds=2)
except Exception as exc:  # noqa: BLE001
    out_off = f"ERROR: {type(exc).__name__}: {exc}"
finally:
    lib._gemini_call, lib.execute_tool = real_call, real_exec
check("text tool call, no sink (the flag off): the turn answers after it", out_off, answer)
calls.clear()
script = [sse(text_round), sse(*[{"candidates": [{"content": {"parts": [{"text": answer[i:i + 20]}]}}]} for i in range(0, len(answer), 20)])]
lib.urllib.request.urlopen, lib.execute_tool = fake_urlopen, (lambda name, args: "Friday, 2 October 2026, 10:00")
try:
    s, shown = stream()
    lib._TURN_IO.reply_sink = s
    out_on = lib._run_gemini_agent([{"role": "user", "content": "What day is it?"}], max_rounds=2)
except Exception as exc:  # noqa: BLE001
    out_on = f"ERROR: {type(exc).__name__}: {exc}"
finally:
    lib.urllib.request.urlopen, lib.execute_tool = real_urlopen, real_exec
    lib._TURN_IO.reply_sink = None
check("text tool call, streamed: the turn answers after it", out_on, answer)
ok("text tool call, streamed: the <tool_call> text never shown, the answer streamed",
   not any("tool_call" in t for t in shown) and shown and out_on.startswith(shown[-1]))

# --- Consistency eval: random chunkings of varied replies; what's shown is always a prefix of done's text, or the
#     stream stopped ------------------------------------------------------------------------------------------------
corpus = [
    LONG,
    LONG + "Want me to text the plumber?",
    claim,
    promise,
    LONG + f"Your key is {FAKE_TOKEN}. Keep it safe. ",
    LONG + "I checked with list_events and search_knowledge for this. ",
    "Short answer. " * 20,
    LONG + "Here it is:\n- the dentist at nine\n- drop-off at 8:15\n- the plumber after one\n",
]
rng = random.Random(676)
runs = stopped = streamed_chars = final_chars = 0
bad: list[str] = []
for text in corpus:
    final = lib._with_credentials_removed(lib.stream_filter(text)).strip()
    for _ in range(25):
        s, shown = stream()
        s.begin_round()
        i = 0
        while i < len(text):
            n = rng.randint(1, 24)
            s.feed(text[i:i + n])
            i += n
        runs += 1
        stopped += s.stopped
        if shown:
            streamed_chars += len(shown[-1])
            final_chars += len(final)
            if not final.startswith(shown[-1]) and not s.stopped:
                bad.append(text[:40])
check("eval: what was shown is always a prefix of done's text, unless the stream stopped", bad, [])
print(f"eval: {runs} streams, {stopped} stopped early, {streamed_chars / max(1, final_chars):.0%} of the final text streamed "
      "before done (the rest arrives with done)")

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
