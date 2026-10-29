#!/usr/bin/env python3
"""Item envelopes (titan-node#390, design: docs/envelopes-and-scenarios.md §6). Zach, 10-01: "summarize those
envelopes to write to the knowledge base instead of storing every single word of things like emails, txt messages,
files."

One synced item (a mail, a message, a file) becomes:
  record           its pointer (source, upstream id, item id), F1 provenance carried over unchanged (owner, audience,
                   bubble, facet, observed_at), and the digest of the original (`sha256:` over its text): never
                   the text itself;
  fences           the fences the ORIGINAL text touches (fences.touches) and its subject, recorded at summarising
                   time: a summary can drop the very word a fence matches on, so the envelope carries them, and
                   reading it applies them as if it were the original;
  interpretation   the summary, one or two plain lines, labelled `lossy-disclosed`, with its evaluator
                   (`model:<name>`) and when. Checked in code first: every number in it must be in the original
                   (a model's made-up date or amount is refused, the envelope falls back to the title), and it's
                   redacted (redact.redact_personal: no account numbers or codes).

Local model only ("household data stays on titan"): the summariser is titan's own (:8010, or the code model on :8198
when GPU1 is busy). Nothing here reaches Gemini or Jev.

Behind ITEM_ENVELOPES=1 (default off). Until the report on #390 is in and Zach decides, nothing replaces stored text:
scripts/envelope_eval.py builds envelopes in a scratch store and measures them against the full-text baseline.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

VERSION = 1
SUMMARY_MAX = 300
SUMMARISER_URL = os.environ.get("ENVELOPE_SUMMARISER_URL", "http://127.0.0.1:8010")
# What's carried from the item's metadata exactly as it is: whose it is and who may see it.
CARRIED = ("owner", "audience", "bubble", "facet", "observed_at", "synced_at", "source", "upstream_id", "item_id",
           "device", "account", "kind")

PROMPT = """/no_think
Summarise this {what} for a household's knowledge base in one or two plain sentences: who it's from or about, what
it's about, and any date, time, place or amount it states, and anything it asks the reader to do. Use only what it
says. Leave out account, order, confirmation and card numbers and any codes. Return JSON only: {{"summary": "..."}}

TITLE: {title}
TEXT:
{text}"""

_WHAT = {"connector:mail": "email", "connector:messages": "text message", "connector:downloads": "downloaded file",
         "connector:browsing": "browsing digest", "note": "note"}
_NUM_RX = re.compile(r"\d[\d,.:/-]*\d|\d")


def enabled() -> bool:
    return os.environ.get("ITEM_ENVELOPES", "0") == "1"


def _digits(s: str) -> set[str]:
    return {re.sub(r"\D", "", m) for m in _NUM_RX.findall(s or "") if re.sub(r"\D", "", m)}


def numbers_ok(summary: str, original: str) -> bool:
    """Every number in the summary is in the original (as its digits: "$1,234.50" ↔ "1234.50", "10/9" ↔ "10/9").
    A year or a day the model added is a made-up fact."""
    have = _digits(original)
    # A short number (a day, an hour) must be one of the original's own; only a long one may sit inside a longer run
    # ("1234" of "123450"): a "9" isn't backed by the 9 inside an account number.
    return all(d in have or (len(d) >= 4 and any(d in h for h in have)) for d in _digits(summary))


def _chat(prompt: str, url: str, max_tokens: int = 300) -> tuple[dict[str, Any] | None, str]:
    """The model's {"summary": …} for one item: schema-constrained, one retry when it's empty (#941)."""
    import typed_local as tl
    schema = tl.obj({"summary": {"type": "string"}}, required=("summary",))
    return tl.ask(prompt, schema, url, name="envelope", max_tokens=max_tokens, timeout=180, no_think=False,
                  ensure=lambda o: None if " ".join(str(o.get("summary") or "").split()) else "the summary is empty")


def summarise(title: str, text: str, source_type: str, *, url: str | None = None,
              chat: Callable[[str, str], tuple[dict[str, Any] | None, str]] | None = None) -> tuple[str, str, str]:
    """(summary, evaluator, how) for one item's text, checked in code. how: "model", or "title" when the model's
    summary failed a check (a number not in the original, empty, or too long) or the model couldn't be reached."""
    import redact
    chat = chat or (lambda p, u: _chat(p, u))
    try:
        got, model = chat(PROMPT.format(what=_WHAT.get(source_type, "item"), title=title[:200], text=text[:4000]),
                          url or SUMMARISER_URL)
    except Exception:  # noqa: BLE001 - the model is busy or down: the title stands in, and says so
        return redact.redact_personal(title)[:SUMMARY_MAX], "code:title", "title"
    s = " ".join(str((got or {}).get("summary") or "").split())
    if not s or len(s) > SUMMARY_MAX * 2 or not numbers_ok(s, f"{title}\n{text}"):
        return redact.redact_personal(title)[:SUMMARY_MAX], f"model:{model}", "title"
    return redact.redact_personal(s)[:SUMMARY_MAX], f"model:{model}", "model"


def build(item: dict[str, Any], *, url: str | None = None, chat: Any = None, fences: Any = None,
          now: float | None = None) -> dict[str, Any]:
    """One item → its envelope. `item`: {source_id, source_type, title, text, metadata}. The text itself is read
    here (for the digest, the fences, the summary) and never kept."""
    import provenance
    md = dict(item.get("metadata") or {})
    text = str(item.get("text") or "")
    title = str(item.get("title") or "")
    summary, evaluator, how = summarise(title, text, str(item.get("source_type") or ""), url=url, chat=chat)
    touched, subject = [], None
    try:
        fs = fences
        if fs is None:
            import fences as _f
            fs = _f.get()
        touched = sorted(int(f["id"]) for f in fs.touches(f"{title}\n{text}"))
        subject = fs.subject_in(f"{title}\n{text}")
    except Exception:  # noqa: BLE001 - no fences store here: none recorded (and reading applies none)
        pass
    return {
        "kind": "item_envelope", "version": VERSION,
        "record": {"source_id": item.get("source_id"), "source_type": item.get("source_type"), "title": title,
                   "digest": provenance.digest({"title": title, "text": text}),
                   **{k: md[k] for k in CARRIED if k in md}},
        "fences": {"touched": touched, "subject": subject},
        "interpretation": {"summary": summary, "label": "lossy-disclosed", "evaluator": evaluator, "how": how,
                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time()))},
    }


def index_text(env: dict[str, Any]) -> str:
    """What the knowledge base would index for an envelope: its title and summary, and where it came from."""
    r, i = env["record"], env["interpretation"]
    src = str(r.get("source_type") or "").replace("connector:", "")
    when = str(r.get("observed_at") or "")[:10]
    return f"{r.get('title') or ''}\n{i['summary']}\n({src}{', ' + when if when else ''})"
