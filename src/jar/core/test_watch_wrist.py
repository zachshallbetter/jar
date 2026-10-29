#!/usr/bin/env python3
# gate: hermetic   (throwaway proactive store; no card handler that would act is reached on a refused tap)
"""From a wrist (a Watch tap relayed by the iPhone, via "watch"), a card answers only what's safe there (10-07 review,
#1013 rule 3): never an unlock, a mail move or its undo, a standing grant, a model switch, sitter mode, a re-share or a
cancel. Every action verb any card producer in scripts/ emits is classified (WATCH_OK_VERBS or WATCH_REFUSED_VERBS),
so a new producer's verb fails this test until someone decides, and fails closed at runtime meanwhile. Fictional people.
Run: python3 scripts/test_watch_wrist.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-wrist-"))
for k, v in {"PROACTIVE_DB": "proactive.db", "HOUSEHOLD_DB": "household.db", "LOCK_UP_DB": "lock_up.db",
             "TV_UNLOCK_DB": "tv_unlock.db", "SITTER_DB": "sitter.db", "FENCES_DB": "fences.db", "PUSH_DB": "push.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "AGENT_MEMORY_DIR": "memory",
             "KB_ACL_DB": "kb_acl.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "SPACES_DB": "spaces.db", "WORLD_DIR": "world"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("TITAN_ALERT_NTFY_URL", "APNS_KEY_FILE"):
    os.environ.pop(k, None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import proactive as P  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.300}"))


print("── every producer's verb is classified ──")
RX = re.compile(r"[\"']value[\"']\s*:\s*f?[\"']([A-Za-z_]+)[: ]")
NOT_ACTIONS = {"on"}   # agent_runtime_lib's tool parameter text ("value": "on or off; …"), not a card action
# producers whose value starts with a variable ("{v} take") or that live on branches not merged yet: named here
EXTRA = {"meeting", "missno", "share", "stop"}
found = set(EXTRA)
for f in sorted(HERE.glob("*.py")):
    if f.name.startswith("test_"):
        continue
    text = f.read_text()
    # only reply actions (a prompt's value is words the app asks with, never answered in code): "kind": "reply" within
    # a few lines of the value, either order
    found |= {m.group(1).lower() for m in RX.finditer(text)
              if re.search(r"[\"']kind[\"']\s*:\s*[\"']reply[\"']", text[max(0, m.start() - 160):m.end() + 160])}
found -= NOT_ACTIONS
check("the scan sees real producers (bill-due's paid, tv-unlock's unlock, mail-move's move, item-consent's always)",
      {"paid", "unlock", "move", "always"} <= found, True)
unclassified = sorted(found - P.WATCH_OK_VERBS - P.WATCH_REFUSED_VERBS)
check("no action verb is left unclassified (a new one must be put in WATCH_OK_VERBS or WATCH_REFUSED_VERBS)", unclassified, [])
check("the two lists don't overlap", sorted(P.WATCH_OK_VERBS & P.WATCH_REFUSED_VERBS), [])
check("the dangerous ones are refused", {"unlock", "move", "undo", "allow_once", "always", "switch", "start", "share"}
      <= P.WATCH_REFUSED_VERBS, True)

print("── at runtime, from a wrist ──")
SOURCES = {"tv-unlock": "unlock:r1", "mail-move": "move:p1", "item-consent": "always:c1", "model-switch": "switch:qwen",
           "sitter": "start:s1", "mail-unsub": "unsub:u1"}
import mail_unsubscribe  # noqa: E402
UNSUB_SENT: list = []
mail_unsubscribe.answer = lambda *a, **k: (UNSUB_SENT.append(a) or (True, "sent"))   # a spy: reached only if not refused
mail_unsubscribe._post = lambda *a, **k: UNSUB_SENT.append(("post",) + a)
for src, val in SOURCES.items():
    card = P.add_card(f"a {src} card", "", source=src, to=["p_alex"], key=f"wr-{src}",
                      actions=[{"id": "go", "label": "Do it", "kind": "reply", "value": val},
                               {"id": "no", "label": "Not now", "kind": "reply", "value": "decline:x"}])
    r = P.card_action(card["id"], "go", person="p_alex", role="adult", via="watch")
    left = next(c for c in P.list_cards(include_acked=True, person="p_alex", role="adult") if c["id"] == card["id"])
    check(f"{src}: '{val.split(':')[0]}' from the wrist is refused, the card stays for the phone",
          (r.get("ok"), r.get("said"), left["acked_at"]), (False, P.WATCH_PHONE_ONLY, None))
check("an unsubscribe tapped on the wrist reaches nothing: no answer, no POST", UNSUB_SENT, [])
check("…unsub is a refused verb", "unsub" in P.WATCH_REFUSED_VERBS, True)
unknown = P.add_card("a new kind", "", source="brand-new", to=["p_alex"], key="wr-new",
                     actions=[{"id": "go", "label": "Go", "kind": "reply", "value": "frobnicate:1"}])
check("an unknown verb fails closed from the wrist", P.card_action(unknown["id"], "go", person="p_alex", role="adult",
                                                                    via="watch").get("said"), P.WATCH_PHONE_ONLY)
check("…and the same tap from the phone itself isn't stopped by this rule",
      P.card_action(unknown["id"], "go", person="p_alex", role="adult").get("said") != P.WATCH_PHONE_ONLY, True)
lk = P.add_card("Lock up?", "", source="lock-up", to=["p_alex"], key="wr-lock",
                actions=[{"id": "lock", "label": "Lock", "kind": "reply", "value": "lock:r9"}])
r = P.card_action(lk["id"], "lock", person="p_alex", role="adult", via="watch")
check("Lock Up's lock still reaches home_security from the wrist (it refuses with its own audit row, #1013)",
      r.get("said") != P.WATCH_PHONE_ONLY, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
