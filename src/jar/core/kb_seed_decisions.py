#!/usr/bin/env python3
"""Seed the recorded design decisions for titan, Jellybean and the runtimes into the KB.

These are the invariants a change should respect. The proactive loop's code review
(scripts/proactive.py) retrieves them for every git change, so a change that breaks one gets
flagged. Each entry says the rule and why. Upserts by title, so re-running updates in place.

Run on titan with the agent env sourced:
    set -a; . /ai/projects/titan-local-agent/agent.env; set +a
    python3 /ai/projects/titan-node/scripts/kb_seed_decisions.py
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DECISIONS = [
    ("Decision: Jellybean never takes the keyboard uninvited",
     """Rule: Jellybean's chat and command bar are non-activating panels. Only an explicit user gesture \
(clicking the bean or menu item, a hotkey, a card) may make a Jellybean window key or activate the app. \
Anything Jellybean does on its own (a confirm card, a peek, a proactive card) shows WITHOUT taking focus. \
Full-screen overlay windows are ordered in only while they have content.
Why: the user reported Jellybean grabbing the keyboard while they worked, and idle full-screen overlays \
drove Jellybean to 100% CPU and WindowServer load (fixed in 0.2.3 and 0.3.1).
Check: no NSApp.activate / makeKey outside user-initiated paths; no always-on full-screen windows."""),

    ("Decision: Mac approvals are enforced by Jellybean, not titan",
     """Rule: Jellybean decides whether a device action needs the user's approval. It re-derives risk locally \
from the tool name (never from the wire) and consults the user's per-domain trust tiers (Always, Ask once per \
session, Always ask, Never; TrustModel.swift since Jellybean 0.11). Titan cannot change trust settings, and no \
control or API command may widen what titan may do on the Mac. The device_trust and device_trust_tiers fields \
Jellybean sends with each turn flow one way (Mac to titan) and only shape titan's [DEVICE POLICY] prompt text \
so the model knows what will ask; nothing titan sends back is read as trust or risk.
Why: prompt-injected text (a web page, an email) must not be able to turn into unapproved shell commands.
Check: DeviceBridge risk map and TrustModel's gate are the only gate; new device tools get a risk tier and a
domain. Sending trust *to* titan is expected and not a bypass."""),

    ("Decision: household data stays on titan",
     """Rule: calendars, mail, downloads, browsing, scans and daily logs are processed on titan. Background \
(trigger) turns run on the local brain only; code review runs on the local code model. Hosted Jev reads \
chat messages only to route the turn, with secrets masked; Gemini gets masked content for hard chat turns. \
Connector data is never sent to Jev.
Why: the user's standing rule that personal data isn't sent to the hosted Jev; the local Decider exists \
to keep household decisions on titan."""),

    ("Decision: anything that borrows the clipboard restores every item",
     """Rule: a tool or test that writes the clipboard must save all pasteboard items and types first and \
restore them after (not just the plain-text string). Prefer not touching the clipboard at all.
Why: a text-only save/restore destroyed an image the user had copied (2026-09-29).
Check: NSPasteboard writes are paired with a full pasteboardItems save/restore."""),

    ("Decision: coordinator auth is EdDSA on titan; Railway still dual-verifies",
     """Rule: titan's runtimes verify coordinator JWTs with the Ed25519 public key only; HS256 is retired there. \
The AcmeProductCo runtime repos' main branches auto-deploy to Railway, whose services mostly lack the \
public key, so EdDSA-only code must not be merged to main until Railway has the key and its callers sign EdDSA.
Why: merging state#6 took the four Railway State runtimes down for ~22 minutes (2026-09-29).
Check: before merging auth or boot-requirement changes in a runtime repo, check that repo's Railway variables \
and read the deploy logs after (Railway shows crash loops as SUCCESS)."""),

    ("Decision: Jellybean replies are brief in the panel, full on request",
     """Rule: the compact chat asks titan for detail=brief (answer first, about 80 words, offer more); the \
Conversation window asks for detail=full. A reply that hits the length cap ends on a complete sentence and \
is marked truncated so the app offers Continue.
Why: the user: "jellybean really shouldn't be returning novels ... never necessary to give them all of it \
unless they ask." """),

    ("Decision: Jellybean versions, signing and install location",
     """Rule: versions stay 0.x: a feature bumps the minor, a fix the patch. build-app.sh signs with the Apple \
Development identity (stable signature, so macOS privacy grants survive rebuilds); ad-hoc only as a fallback. \
The app the user runs lives at ~/Applications/Jellybean.app; only one instance may run. HTTP to titan \
needs the tailnet ATS exception in Info.plist; new privacy-protected access needs its usage string.
Why: ad-hoc signatures silently dropped Accessibility/Screen Recording grants on every rebuild; ATS blocked \
the HTTP transport until 0.2.4."""),

    ("Decision: sync sources are opt-in, with provenance and forget",
     """Rule: every Jellybean sync source (calendar, mail, downloads, browsing) is off by default and asks for \
access only when the user turns it on. Items carry source, source_id, observed_at and account; titan's \
connectors ledger makes resync idempotent and can forget a source exactly. Browsing never sends page \
contents; mail sends headers and a snippet unless the user enables full bodies.
Why: the user chose continuous sync with a per-source off switch (2026-09-29)."""),

    ("Decision: proactive loop tiers — interrupt only when it matters",
     """Rule: events posted to /v1/triggers are triaged by the local Decider (dropped only when it is confident \
they are noise), then investigated or code-reviewed locally. Results are NO_ACTION (nothing), PASSIVE \
(timeline only; the agent sees it next turn) or NOTIFY (a card in Jellybean). Unclear results are passive. \
Git and check results go to the separate dev-activity stream, never the household timeline (whose last \
entries appear in every turn); a turn sees dev activity only when it is about code.
Why: titan's own design (sense, triage, act, surface) and the user's wish not to be interrupted while working."""),

    ("Decision: be careful with Vercel",
     """Rule: don't deploy to Vercel, create preview deployments or add traffic to Vercel-hosted sites without \
asking the user first; check whether a repo auto-deploys to Vercel before merging.
Why: the Pro plan's included credit was exhausted and on-demand charges had started (2026-09-29)."""),

    ("Decision: firewall default-deny; model servers on localhost",
     """Rule: titan's nftables firewall drops inbound traffic except tailscale0, SSH from the LAN, ICMP, \
Tailscale's UDP port and DHCP. Model servers (agent :8010, embeddings :8015, Decider :8031, code model \
:8198) bind 127.0.0.1; the agent API (:7770) is token-gated. Never flush the whole ruleset (Tailscale's \
rules live in iptables-nft).
Why: every runtime and model server used to listen on 0.0.0.0 with no firewall."""),

    ("Decision: ASE commits carry no AI attribution",
     """Rule: the ASE repository must show no sign it was AI-built: commits are authored as the user, with no \
Co-Authored-By trailers or AI mentions in commits, PR titles or bodies, and neutral branch names. The \
overnight autonomous coder is excluded from ASE until its scrubbing is proven.
Why: the user's rule for the public ASE repo."""),
]


def _norm(t: str) -> str:
    return "".join(str(t or "").split())


def plan(existing: list[dict], texts: dict[str, str], seeded: dict[str, bool] | None = None) -> dict[str, list]:
    """{'write': [(title, content)], 'same': [title], 'retire': [(source_id, title)]}. A Decision whose text is
    unchanged isn't rewritten; a "Decision: …" record no longer in DECISIONS is retired (#351), but only one
    this seeder wrote (`seeded[sid]`: origin kb_seed_decisions.py and no owner). A person's own note titled
    "Decision: …" is never touched by a deploy."""
    titles = {t for t, _ in DECISIONS}
    by_title = {s["title"]: s["source_id"] for s in existing if s.get("source_type") == "knowledge_record"}
    p: dict[str, list] = {"write": [], "same": [], "retire": []}
    for title, content in DECISIONS:
        sid = by_title.get(title)
        if sid and _norm(texts.get(sid, "")) == _norm(content):
            p["same"].append(title)
        else:
            p["write"].append((title, content))
    p["retire"] = [(sid, t) for t, sid in by_title.items()
                   if t.startswith("Decision:") and t not in titles and (seeded or {}).get(sid)]
    return p


def _seeded(ids: list[str]) -> dict[str, bool]:
    """Which records this seeder wrote: chunk metadata origin "kb_seed_decisions.py", and no owner in the sharing
    table (titan's own notes have none). Anything unsure counts as not ours."""
    import urllib.parse
    import agent_runtime_lib as lib
    import kb_privacy
    acl = kb_privacy.acl_rows(ids)
    out = {}
    for sid in ids:
        try:
            r = lib._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}/chunks", "GET")
            meta = ((r.get("chunks") or [{}])[0] or {}).get("metadata") or {}
        except Exception:  # noqa: BLE001
            meta = {}
        out[sid] = meta.get("origin") == "kb_seed_decisions.py" and (acl.get(sid) or (None, None))[0] is None
    return out


def retire(items: list[tuple[str, str]], backup_root: Path = Path("/ai/datasets/backups")) -> Path | None:
    """Back up, then delete, retired Decision records. The backup holds each record's text and sharing row."""
    if not items:
        return None
    import json
    import os
    import time
    import urllib.parse
    import agent_runtime_lib as lib
    import kb_hygiene
    import kb_privacy
    d = backup_root / f"decisions-retired-{time.strftime('%Y%m%d-%H%M%SZ', time.gmtime())}"
    d.mkdir(parents=True, mode=0o700)
    acl = kb_privacy.acl_rows([sid for sid, _ in items])
    rows = [{"source_id": sid, "title": t, "text": kb_hygiene.source_text(sid), "acl": acl.get(sid)} for sid, t in items]
    (d / "backup.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    os.chmod(d / "backup.json", 0o600)
    for sid, _ in items:
        lib._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}", "DELETE")
    return d


def seed(dry_run: bool = False, quiet: bool = False) -> int:
    import kb_hygiene
    existing = kb_hygiene.list_sources()
    texts = {s["source_id"]: kb_hygiene.source_text(s["source_id"]) for s in existing
             if s.get("source_type") == "knowledge_record" and str(s.get("title", "")).startswith("Decision:")}
    p = plan(existing, texts, _seeded([s["source_id"] for s in existing if s.get("source_type") == "knowledge_record"
                                       and str(s.get("title", "")).startswith("Decision:")]))
    written = 0
    for title, content in p["write"]:
        if dry_run:
            print(f"  would write {title}")
            continue
        new_id = "note-decision-" + (re.sub(r"[^a-z0-9]+", "-", title.lower().replace("decision:", "")).strip("-")[:60])
        try:
            kb_hygiene.upsert_by_title(title, content, "knowledge_record",
                                       {"domain": "decisions", "tags": ["decisions", "architecture"],
                                        "origin": "kb_seed_decisions.py"}, new_id, existing)
            if not quiet:
                print(f"  ✓ {title}")
            written += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {title}: {exc}")
    for sid, title in p["retire"]:
        print(f"  {'would retire' if dry_run else 'retire'} {title}")
    backup = None if dry_run else retire(p["retire"])
    print(f"decisions: {written} written, {len(p['same'])} unchanged, {len(p['retire'])} retired"
          + (f" (backup {backup})" if backup else "") + (" (dry run)" if dry_run else ""))
    return written


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Seed titan's Decisions; retire ones no longer listed (backed up).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    seed(dry_run=a.dry_run, quiet=a.quiet)
