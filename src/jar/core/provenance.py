#!/usr/bin/env python3
"""One provenance shape and one digest for titan's stores (titan-node#393; design: docs/envelopes-and-scenarios.md §5).

Five shapes grew up separately: connector items, mail-bill records, chat records, KB note metadata and world
evidence. They spell the same things differently, and `source_id` means the Mac's id in one place and titan's
item id in another. This module is the one shape new code writes and the one reader for the old shapes:

  source       where it came from: "mail", "calendar", "chat", "note", "world"…
  upstream_id  the id in the origin system (the Mac's message id)
  item_id      titan's id for the item (connectors' cx-…)
  observed_at  when the source says it was true (ISO 8601); never ingest time
  synced_at    when titan got it
  owner, audience, bubble, facet   whose it is and who may see it
  kind         observed | declared | derived (Reflective Rust's classes: never promoted from one to another)
  digest       "sha256:<hex>" of the canonical JSON of what it describes

canonical_json follows protocol-kernel pkernel/digest.py (sorted keys, (",", ":"), UTF-8); the digest format
is Reflective Rust's. One difference from RFC 8785: floats use Python's shortest repr, stable within titan.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any

KINDS = ("observed", "declared", "derived")
CORE = ("source", "upstream_id", "item_id", "observed_at", "synced_at", "owner", "audience", "bubble", "facet", "kind",
        "digest")
_DIGEST_RX = re.compile(r"^sha256:[0-9a-f]{64}$")


def _default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    return str(o)


def canonical_json(x: Any) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def digest(x: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(x).encode("utf-8")).hexdigest()


def _nfc(x: Any) -> Any:
    import unicodedata
    if isinstance(x, str):
        return unicodedata.normalize("NFC", x)
    if isinstance(x, dict):
        return {_nfc(k): _nfc(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_nfc(v) for v in x]
    return x


def _ints(x: Any) -> Any:
    """Integral floats as ints (50.0 → 50): the model sends either, and Swift's JSONSerialization prints 50."""
    if isinstance(x, float) and x.is_integer():
        return int(x)
    if isinstance(x, dict):
        return {k: _ints(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_ints(v) for v in x]
    return x


def args_digest(tool: str, args: dict[str, Any]) -> str:
    """The digest a grant binds a device action to (#395/#382). The contract, for the apps to match byte for byte:
    canonical JSON of {"tool": <tool>, "args": <args>}: keys sorted, no whitespace, "/" NOT escaped, non-ASCII
    raw UTF-8 (never \\u escapes), every string NFC-normalised, integral floats as integers (50.0 → 50);
    then "sha256:" + lowercase hex. ARGS_DIGEST_VECTORS pins the cases."""
    return digest(_ints(_nfc({"tool": tool, "args": args or {}})))


# The contract's test vectors: titan, the Mac and the iPhone must all produce these digests.
ARGS_DIGEST_VECTOR = ({"tool": "send_message", "args": {"to": "Ava", "text": "Dinner is at 6 — café"}},
                      "sha256:94ee7f923a07d5f4e54d9c8b0dfc91e21fb8f742e1d26584dd152eec5e532809")
ARGS_DIGEST_VECTORS = [
    ARGS_DIGEST_VECTOR,
    # an integral float hashes as the integer (Swift prints 50.0 as 50)
    ({"tool": "home_control", "args": {"device": "living room lights", "action": "set_brightness", "value": {"brightness": 50.0}}},
     "sha256:54d68ffd5eaa88c3b78ebb87a6475d5b34832649f0d595662dc0ab179ad17ed7"),
    # slashes are never escaped (Swift needs .withoutEscapingSlashes)
    ({"tool": "move_files", "args": {"paths": ["~/Desktop/a.txt"], "to": "~/Documents/"}},
     "sha256:fe0a70be62129050b7d621c7c2c79d03a7fae2b84ea0c1e7202c35a026ff35eb"),
]


def is_digest(s: Any) -> bool:
    return bool(_DIGEST_RX.match(str(s or "")))


def _iso(v: Any) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    s = str(v).strip()
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"not an ISO date: {s!r}") from None
    return s


def make(source: str, *, upstream_id: str | None = None, item_id: str | None = None, observed_at: Any = None,
         synced_at: Any = None, owner: str | None = None, audience: str | None = "private", bubble: str | None = None,
         facet: str | None = None, kind: str = "observed", of: Any = None, reference: str | None = None,
         **extra: Any) -> dict[str, Any]:
    """A provenance record. `of`: what it describes, hashed into `digest`. `reference`: the id of the reference
    (references.py, #446) a derived fact came from: the item stays on its device, the fact points back to it. Extra
    fields (a store's own extension, e.g. world evidence's origin/prior/by) are kept as given; they may not reuse a
    core name."""
    if reference is not None and not re.match(r"^rf-[0-9a-f]{16}$", str(reference)):
        raise ValueError("reference must be a reference id (rf-…)")
    if not source:
        raise ValueError("provenance needs a source")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
    clash = set(extra) & set(CORE)
    if clash:
        raise ValueError(f"extension fields may not reuse core names: {sorted(clash)}")
    out = {"source": str(source), "upstream_id": upstream_id, "item_id": item_id, "observed_at": _iso(observed_at),
           "synced_at": _iso(synced_at), "owner": owner, "audience": audience, "bubble": bubble, "facet": facet,
           "kind": kind, "digest": digest(of) if of is not None else None, "reference": reference, **extra}
    return {k: v for k, v in out.items() if v is not None}


def normalize(prov: dict[str, Any] | None) -> dict[str, Any]:
    """Any of titan's provenance shapes as the one shape (for readers). Unknown fields are kept."""
    p = dict(prov or {})
    if not p:
        return {}
    out: dict[str, Any] = {}
    if "ref" in p and "label" in p:                      # world evidence {source, kind, ref, origin, label, …}
        out = {"source": p.get("source") or "world", "item_id": p.get("ref"), "observed_at": p.get("observed_at"),
               "owner": p.get("owner"), "audience": p.get("audience"), "kind": "declared" if p.get("by") else "observed",
               "evidence_kind": p.get("kind"), **{k: p[k] for k in ("origin", "label", "prior", "by") if k in p}}
    elif "saved" in p or ("origin" in p and "source" not in p):   # KB note metadata {saved, origin, owner, audience}
        out = {"source": p.get("origin") or "note", "observed_at": p.get("saved"), "owner": p.get("owner"),
               "audience": p.get("audience"), "kind": "declared"}
    else:                                                # connectors, mail bills, chat records
        sid = p.get("source_id")
        is_item = isinstance(sid, str) and sid.startswith("cx-")
        out = {"source": p.get("source"), "upstream_id": p.get("upstream_id") or (None if is_item else sid),
               "item_id": p.get("item_id") or (sid if is_item else None), "observed_at": p.get("observed_at"),
               "synced_at": p.get("synced_at"), "owner": p.get("owner"), "audience": p.get("audience"),
               "kind": p.get("kind") or ("declared" if p.get("source") == "chat" else
                                         "derived" if p.get("extracted_by") else "observed")}
        out.update({k: v for k, v in p.items() if k not in out and k != "source_id"})
    for k in ("bubble", "facet", "digest"):
        if p.get(k) is not None:
            out[k] = p[k]
    return {k: v for k, v in out.items() if v is not None}
