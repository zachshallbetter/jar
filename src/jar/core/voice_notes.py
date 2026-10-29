#!/usr/bin/env python3
"""Voice notes to the household (jellybean#313): a short recording on someone's iPhone, delivered as a card with Play
to everyone it's for (people's phones, the TV and the household's shared devices for "household"), with a push.

  audio      an upload (uploads.py, POST /v1/files): .m4a or .aac by their first bytes, at most a minute
             (VOICE_MAX_SECONDS; uploads.VOICE_MAX_BYTES), kept a week (uploads.VOICE_TTL_SECONDS), served only to
             its sender and its audience, never read by the agent's tools: no model ever hears it
  words      none, unless the sender chose to send a transcript (made on their phone); then it's the card's text
  who        anyone with a person's own device may send; a kid only while the household lets kids send (an adult's
             setting, on by default: household.json "kids_send_voice_notes"). A shared household device doesn't send.
  to         "household" (everyone and the shared devices), "adults", or a list of people
  Keep       (titan-node#859, Zach 10-04) a card with a transcript has a Keep button. A recipient who presses it gets
             the transcript in their own knowledge base, private to them (never the sender's or anyone else's, never
             the audio); until then, and for everyone who doesn't, nothing of it outlives the card. A kid or a shared
             device doesn't write the knowledge base, so Keep is refused there.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import uploads

VOICE_MAX_SECONDS = 60
KEEP_ACTION = {"id": "keep", "label": "Keep", "kind": "reply", "value": "keep"}
KEEP_ROLES = ("admin", "adult")
PREFS = Path(os.environ.get("HOUSEHOLD_PREFS", "/ai/datasets/connectors/household.json"))
_KEY = "kids_send_voice_notes"


def _prefs_path() -> Path:
    return Path(os.environ.get("HOUSEHOLD_PREFS") or PREFS)


def kids_may_send() -> bool:
    try:
        data = json.loads(_prefs_path().read_text())
    except (OSError, ValueError):
        data = {}
    v = data.get(_KEY) if isinstance(data, dict) else None
    return True if v is None else bool(v)


def set_kids_may_send(on: bool, role: str | None) -> bool:
    """An adult's choice for the household."""
    if role not in ("admin", "adult"):
        raise PermissionError("Only an adult can choose whether kids send voice notes.")
    p = _prefs_path()
    try:
        data = json.loads(p.read_text())
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data[_KEY] = bool(on)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)
    return bool(on)


def recipients(people: list[dict[str, Any]], viewer: str | None) -> list[dict[str, Any]]:
    """Who `viewer` can pick for a note: the household's people, in its order, without the viewer and without the
    shared devices (those get "household" notes)."""
    me = str(viewer or "").lower()
    return [{"id": str(p["id"]), "name": p.get("name") or str(p["id"]), "role": p.get("role")} for p in people
            if p.get("role") != "household" and str(p["id"]).lower() != me]


def send(sender: str, role: str | None, body: Any, people: list[dict[str, Any]],
         add_card: Any = None, now: float | None = None) -> tuple[int, dict[str, Any]]:
    """POST /v1/voice-notes: (HTTP status, body). 201 {card_id, to}."""
    now = time.time() if now is None else now
    me = str(sender or "").lower()
    if role == "household" or not me:
        return 403, {"error": "a voice note comes from a person's own device"}
    if role == "kid" and not kids_may_send():
        return 403, {"error": "Kids' voice notes are off for this household."}
    if not isinstance(body, dict):
        return 400, {"error": "expected a JSON object"}
    rec = uploads.for_owner(str(body.get("file_id") or ""), me, now)
    if not rec or rec["type"] not in uploads.AUDIO_TYPES:
        return 400, {"error": "file_id must be a voice note (.m4a or .aac) you uploaded"}
    try:
        secs = int(round(float(body.get("seconds") or 0)))
    except (TypeError, ValueError):
        return 400, {"error": "seconds must be a number"}
    if secs > VOICE_MAX_SECONDS:
        return 400, {"error": f"a voice note is at most {VOICE_MAX_SECONDS} seconds"}
    to = body.get("to")
    everyone = {str(p["id"]).lower(): p for p in people}
    if to == "household":
        ids, aud = sorted(everyone), "household"
    elif to == "adults":
        ids, aud = sorted(i for i, p in everyone.items() if p.get("role") in ("admin", "adult")), "adults"
    elif isinstance(to, list) and to:
        ids = sorted({str(i).lower() for i in to})
        if not set(ids) <= set(everyone):
            return 400, {"error": "to names someone who isn't in the household"}
        aud = None
    else:
        return 400, {"error": 'to is "household", "adults" or a list of people'}
    ids = [i for i in ids if i != me]
    if not ids:
        return 400, {"error": "nobody to send it to"}
    uploads.set_audience(rec["id"], me, ids)
    name = str((everyone.get(me) or {}).get("name") or "Someone").split(" ")[0]
    transcript = " ".join(str(body.get("transcript") or "").split())[:1000]
    if add_card is None:
        import proactive
        add_card = proactive.add_card
    expires = max(60.0, rec["ts"] + uploads.ttl(rec["type"]) - now)
    card = add_card(f"Voice note from {name}", transcript, kind="info", urgency=2, source="voice-note",
                    to=aud or ids, sender=name, expires_in=expires,
                    actions=[KEEP_ACTION] if transcript else None,
                    data={"kind": "voice_note", "file_id": rec["id"], "seconds": max(1, secs)})
    return 201, {"card_id": card["id"], "to": ids}


def keep_id(card_id: str, person: str) -> str:
    """The kept transcript's KB id: one per card and person (pressing Keep twice keeps it once)."""
    import hashlib
    import kb_privacy
    return kb_privacy.scoped_id("voice-", hashlib.sha1(str(card_id).encode()).hexdigest()[:12], person)


def keep(card: dict[str, Any], person: str | None, role: str | None, index: Any = None,
         now: float | None = None) -> tuple[bool, str]:
    """A recipient's Keep (the card's `keep` button, titan-node#859): its transcript into their own knowledge base,
    private to them. (done, what to say). The card stays: Play still works until it expires."""
    if role not in KEEP_ROLES or not person:
        return False, "Keeping voice notes is for adults on their own devices."
    words = " ".join(str(card.get("detail") or "").split())
    if not words:
        return False, "This voice note has no words to keep, only the recording."
    sender = str(card.get("from") or card.get("sender") or "someone")   # list_cards names the sender `from`
    at = float(card.get("created_at") or (now or time.time()))
    import local_time
    day = local_time.clock(at, "%b %-d, %Y")
    title = f"Voice note from {sender}, {day}"
    if index is None:
        import agent_runtime_lib as lib
        index = lib._kb_index
    index(keep_id(str(card.get("id") or ""), person), title, f"{title}\n{words}", "voice-note",
          {"via": "voice-note", "sender": sender, "card_id": card.get("id"),
           "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)),
           "kept_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time()))},
          owner=person, audience="private")
    return True, f"Kept: the words of {sender}'s voice note are in your knowledge base, just for you."
