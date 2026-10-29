#!/usr/bin/env python3
# gate: hermetic
"""Action cards in replies (jellybean#201 slice 3): a card the turn made for the asker rides under the reply as an
`action` block, a snapshot of the card with its own buttons. At most 2, only the asker's (never another person's,
never past a fence, never on a shared device), nothing the card doesn't already show.

Run: python3 scripts/test_reply_action_blocks.py
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="titan-actblk-test-")
os.environ["PROACTIVE_DB"] = os.path.join(_TMP, "proactive.db")
os.environ["AGENT_TURN_LOG"] = os.path.join(_TMP, "turns.jsonl")
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_runtime_lib as lib  # noqa: E402
import proactive  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


DO = [{"label": "Do it", "kind": "prompt", "value": "Yes, do held action h-1a2b"}, {"label": "Not now", "kind": "dismiss"}]

print("\n── a turn's cards are noted, in order, only while tracking ──")
proactive.add_card("Before the turn", "x", to=["zach"], actions=DO)
proactive.track_cards()
held = proactive.add_card("Text Robin “running late”?", "Held: the number came from an email.", to=["zach"], actions=DO)
other = proactive.add_card("Robin: your dentist moved", "Thu 3 PM", to=["p_mir"], actions=DO)
sched = proactive.add_card("Turn off the porch light at 11 PM?", "Scheduled: waiting for you", to=["zach"],
                           actions=[{"label": "Cancel", "kind": "reply", "value": "cancel sa-1"}])
third = proactive.add_card("A third one", "", to=["zach"], actions=DO)
made = proactive.tracked_cards()
check("the four made this turn, in order (not the one from before)", made, [held["id"], other["id"], sched["id"], third["id"]])
check("tracking stops", (proactive.add_card("After", "", to=["zach"]) and proactive.tracked_cards()), [])

print("\n── only the asker's ──")
mine = proactive.cards_for_reply(made, "zach", "admin")
check("Zach's three, never Robin's (even for the admin)", [c["id"] for c in mine], [held["id"], sched["id"], third["id"]])
check("Robin sees only hers", [c["id"] for c in proactive.cards_for_reply(made, "p_mir", "adult")], [other["id"]])
check("a shared device: none", proactive.cards_for_reply(made, "household", "household"), [])
proactive.card_action(held["id"], held["actions"][1]["id"], person="zach", role="admin")
check("acted on already: gone from the reply", [c["id"] for c in proactive.cards_for_reply(made, "zach", "admin")],
      [sched["id"], third["id"]])

print("\n── the block ──")
fresh = proactive.add_card("Text Robin “running late”?", "Held: the number came from an email.\nSecond line", to=["zach"],
                           actions=DO + [{"label": "Bad", "kind": "teleport"}])
blocks = lib.reply_blocks("Okay, I held that text for you.", [], [], [], cards=[fresh, proactive.cards_for_reply([sched["id"]], "zach", "admin")[0], third])
check("at most 2", [b["type"] for b in blocks], ["action", "action"])
b = blocks[0]
check("a snapshot of the card: id, title, detail on one line", (b["card"], b["title"], b["detail"]),
      (fresh["id"], "Text Robin “running late”?", "Held: the number came from an email. Second line"))
check("the card's own buttons, as the deck card has them (the app acts through the card)",
      b["actions"], [{"id": "a0", "label": "Do it", "kind": "prompt", "value": "Yes, do held action h-1a2b"},
                     {"id": "a1", "label": "Not now", "kind": "dismiss", "value": ""}])
check("same as the deck card's buttons, nothing more", [{k: x[k] for k in ("id", "label", "kind", "value")} for x in fresh["actions"][:2]],
      b["actions"])
check("block ids follow the other blocks", lib.reply_blocks("x", [], [{"source_id": "kb1", "title": "T", "snippet": "s"}], [],
                                                             cards=[third])[1]["id"], "b2")
check("a card with no buttons isn't a block", lib.action_blocks([{"id": "pc-x", "title": "t", "actions": []}]), [])
check("no cards: no blocks", lib.reply_blocks("x", [], [], []), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
