#!/usr/bin/env python3
# gate: hermetic
"""Calendar without the Mac (#1612). 10-08 to 10-13: calendar changes waited on an asleep Mac and timed out, an
approved held change re-created an old time, and an event missing from a sync was taken as deleted. Here:
  1. a calendar write no device could take is held (durable, device_wait) with a card, and the person is told plainly
     "I'll add it when your Mac or iPhone is back"; a Mac that's back gets it; the iPhone runs it on Do it;
  2. a held calendar write whose event time passed (or that waited past the bound) expires with a card, never fires
     late; a newer change on the same event supersedes it;
  3. a partial or failed calendar sync never deletes anything on titan; only a delete signal or a complete window does.
Fictional people only (Robin, Alex, Ava, Leo); temporary stores; pinned clocks; run under TZ=UTC and
TZ=America/Los_Angeles. Run: python3 scripts/test_calendar_without_mac.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_TMP = Path(tempfile.mkdtemp(prefix="titan-cal-nomac-"))
os.environ["HELD_ACTIONS_DB"] = str(_TMP / "held.db")
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
os.environ["KB_ACL_DB"] = str(_TMP / "kb_acl.db")
os.environ["CONNECTORS_LEDGER"] = str(_TMP / "ledger.db")
os.environ["SPACES_DB"] = str(_TMP / "spaces.db")
os.environ["CAPABILITY_SWITCHES"] = str(_TMP / "switches.json")
os.environ["MCP_SERVERS"] = str(_TMP / "none.json")
os.environ["AGENT_TURN_SOURCE"] = "user"
os.environ["AGENT_TZ"] = "America/Los_Angeles"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import calendar_hold as ch  # noqa: E402
import connectors_runner as cx  # noqa: E402
import cross_device as xd  # noqa: E402
import held_actions  # noqa: E402
import proactive  # noqa: E402

held_actions.DB = _TMP / "held.db"
LA = ZoneInfo("America/Los_Angeles")
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def la(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LA).timestamp()


# Pinned clock: Thu Oct 15 2026, 9:00 AM Pacific, for every module that reads time.time().
T0 = la(2026, 10, 15, 9, 0)
CLOCK = {"now": T0}
_fake_time = types.SimpleNamespace(time=lambda: CLOCK["now"], monotonic=__import__("time").monotonic)
held_actions.time = _fake_time
ch.time = _fake_time

CARDS: list[dict] = []
proactive.add_card = lambda title, detail="", **kw: CARDS.append({"title": title, "detail": detail, **kw}) or {}


def cards_for(pid):
    return [c for c in CARDS if c.get("to") == [pid]]


def as_person(pid, role="adult", device=None, said=None):
    io = lib._TURN_IO
    io.person = {"id": pid, "name": pid.title()}
    io.role, io.source, io.client = role, "user", "mac"
    io.device_tools = io.remote = io.scheduled = io.confirmed_call = None
    io.said = io.last_said = said
    io.shown = ""
    io.device = device


def device(answer):
    calls = []

    def hook(req):
        calls.append(req)
        return answer(req) if callable(answer) else dict(answer)
    hook.calls = calls
    return hook


ASLEEP = {"result": xd.NO_WAKE, "completed": False}
OK_ADD = {"result": "Created 'Leo pickup' on Fri Oct 16 at 3:10 PM on the Home calendar."}
EV = {"title": "Leo pickup", "start": "2026-10-16T15:10:00"}


def pending(owner=None):
    return held_actions.pending(owner=owner, tools=ch.CALENDAR_WRITES, now=CLOCK["now"])


print("── household clock, whatever the host's TZ ──")
check("when_text is Pacific", ch.when_text("create_calendar_event", EV), "Fri Oct 16, 3:10 PM")
check("stale_at: the event's start, Pacific", ch.stale_at("create_calendar_event", EV), la(2026, 10, 16, 15, 10))
check("a date only: the end of that day", ch.stale_at("delete_calendar_event", {"title": "x", "on": "2026-10-16"}),
      la(2026, 10, 17))
check("a repeating one: no event-time bound",
      ch.stale_at("create_calendar_event", {**EV, "repeat": {"every": "week"}}), None)
ANSWERS = (xd.NO_WAKE, "Device action was not completed: " + xd.NO_ANSWER,
           "Device action was not completed: Confirmation timed out.",
           "No device is connected to this turn, so device actions can't run here.", xd.DECLINED, "Created 'x'.")
check("never taken (safe to send later)", [ch.not_delivered(t) for t in ANSWERS], [True, False, False, True, False, False])
check("taken, never answered (may have happened)", [ch.maybe_done(t) for t in ANSWERS],
      [False, True, True, False, False, False])
check("a scheduled-style run: only a non-delivery retries", [ch.classify(False, t) for t in ANSWERS[:4]],
      ["unreachable", "failed", "failed", "unreachable"])

print("── 1. no device: held, told plainly, never a silent timeout ──")
lib._device_bridge_enabled = lambda: True
hook = device(ASLEEP)
as_person("robin", device=hook, said="add Leo pickup tomorrow at 3:10")
out = lib._device_request("create_calendar_event", dict(EV))
check("the reply tells them plainly", "I'll add it when your Mac or iPhone is back" in out, True)
check("not counted as done", lib._TURN_IO.device_completed, False)
rows = pending("robin")
check("one durable held action, waiting for a device", [(r["tool"], r["kind"]) for r in rows],
      [("create_calendar_event", "device_wait")])
hid = rows[0]["id"]
card = cards_for("robin")[-1]
check("a card to Robin only, saying when", (card["title"], card["to"], "Fri Oct 16, 3:10 PM" in card["detail"]),
      ("Waiting for your Mac or iPhone", ["robin"], True))
check("its buttons: Do it now and Don't", [a["value"] for a in card["actions"]],
      [f"Yes, do held action {hid}", f"Drop held action {hid}"])
check("the card never names the title", "Leo" in card["title"] + card["detail"], False)

for msg, ans in (("a delete", ASLEEP), ("an update", {"error": "No device is connected to this turn."})):
    tool = "delete_calendar_event" if msg == "a delete" else "update_calendar_event"
    as_person("robin", device=device(ans), said="move the dentist to 4")
    out = lib._device_request(tool, {"title": "Dentist", "on": "2026-10-20", "start": "2026-10-20T16:00:00"})
    check(f"{msg} with no device: held too", ("when your Mac or iPhone is back" in out,
                                               any(r["tool"] == tool for r in pending("robin"))), (True, True))

as_person("robin", device=device(ASLEEP), said="what's on today")
out = lib._device_request("list_calendar_events", {"start": "2026-10-15"})
check("a read is never held", out.startswith("I'll") or "held" in out, False)

as_person("robin", device=device({"result": xd.DECLINED, "completed": False}), said="add Ava swim at 5")
n = len(pending("robin"))
lib._device_request("create_calendar_event", {"title": "Ava swim", "start": "2026-10-16T17:00:00"})
check("a decline is an answer, not held", len(pending("robin")), n)

as_person("robin", device=device(ASLEEP), said="add Ava swim at 5")
lib._TURN_IO.source = "eval"
lib._TURN_IO.eval_device = "fake"
lib._device_request("create_calendar_event", {"title": "Ava swim", "start": "2026-10-16T17:00:00"})
lib._TURN_IO.eval_device = None
check("an eval turn never holds", len(pending("robin")), n)
as_person("robin", device=device(ASLEEP))
lib._TURN_IO.scheduled = "sa-1"
lib._device_request("create_calendar_event", {"title": "Ava swim", "start": "2026-10-16T17:00:00"})
check("a scheduled action (titan's own run) never holds", len(pending("robin")), n)
lib._TURN_IO.scheduled = None
as_person("robin", role="household", device=device(ASLEEP), said="add Ava swim at 5")
lib._device_request("create_calendar_event", {"title": "Ava swim", "start": "2026-10-16T17:00:00"})
check("a shared screen's turn never holds", len(pending("robin")), n)
as_person("robin", device=device({"error": "Stopped by the user.", "completed": False}), said="add Ava swim at 5")
out = lib._device_request("create_calendar_event", {"title": "Ava swim", "start": "2026-10-16T17:00:00"})
check("Stop is released at once, never held or checked",
      (out.startswith("Device action was not completed: Stopped by the user"),
       len(held_actions.pending(owner="robin", now=CLOCK["now"]))), (True, n))
check("the runtime's calendar writes are calendar_hold's", lib._CALENDAR_WRITES, ch.CALENDAR_WRITES)

print("── privacy: Robin's held calendar action never shows to Alex ──")
check("Alex's pending list is empty", pending("alex"), [])
check("Alex can't read it", held_actions.row(hid, "alex"), None)
as_person("alex", device=device(OK_ADD))
check("Alex's Do it is refused", lib._run_held_action(hid), "There's no held action with that id for you.")
check("Alex's Don't is refused", lib._drop_held_action(hid), "There's nothing held with that id for you any more.")
check("no card ever went to Alex", cards_for("alex"), [])
check("still Robin's and pending", [r["id"] for r in pending("robin") if r["id"] == hid], [hid])

print("── a device back: the Mac takes it, or the iPhone runs it on Do it ──")
sent = []
live = {"robin": False}
check("Mac still away: nothing sent", (ch.drain(live=lambda o: live.get(o, False),
                                                send=lambda r: sent.append(r) or ("done", "")), sent), ([], []))
live["robin"] = True
res = ch.drain(live=lambda o: live.get(o, False), send=lambda r: sent.append(r["id"]) or ("unreachable", ""))
check("it went again before taking it: one try, the rest wait", (len(sent), [r["status"] for r in res]),
      (1, ["unreachable"]))
check("back to pending, retried no sooner than RETRY_S", (held_actions.row(sent[0], "robin")["used_at"],
      held_actions.row(sent[0], "robin")["next_try_at"]), (None, CLOCK["now"] + ch.RETRY_S))
sent.clear()
CLOCK["now"] = T0 + ch.RETRY_S + 1
res = ch.drain(live=lambda o: live.get(o, False), send=lambda r: sent.append(r["id"]) or ("done", "Created."))
# three were held: the add, the dentist delete and the dentist move; the move superseded the delete (same event)
check("the Mac is back: each sent once, oldest first", (sent[0] == hid, len(sent), {r["status"] for r in res}),
      (True, 2, {"done"}))
check("done: nothing pending, a Done card to Robin", (pending("robin"), cards_for("robin")[-1]["title"].startswith("Done")),
      ([], True))
sent.clear()
ch.drain(live=lambda o: True, send=lambda r: sent.append(r["id"]) or ("done", ""))
check("never sent twice", sent, [])

# the Mac asks first and nobody's there: a Do it card instead, and it isn't sent again
as_person("robin", device=device(ASLEEP), said="add Ava swim at 5")
lib._device_request("create_calendar_event", {"title": "Ava swim", "start": "2026-10-16T17:00:00"})
h2 = pending("robin")[-1]["id"]
ch.drain(live=lambda o: True, send=lambda r: ("needs_ok", ""))
r2 = held_actions.row(h2, "robin")
check("the Mac wants a yes: now a confirm hold, with a Do it card", (r2["kind"], r2["used_at"],
      cards_for("robin")[-1]["actions"][0]["value"]), ("confirm", None, f"Yes, do held action {h2}"))
sent.clear()
ch.drain(live=lambda o: True, send=lambda r: sent.append(1) or ("done", ""))
check("not sent again in the background", sent, [])
# Robin taps Do it on her iPhone: the phone runs calendar writes itself
phone = device({"result": "Created 'Ava swim'."})
as_person("robin", device=phone)
lib._TURN_IO.client, lib._TURN_IO.device_tools = "iphone", lib.PHONE_DEVICE_TOOLS
out = lib._run_held_action(h2)
check("Do it on the iPhone runs it there, once", (len(phone.calls), phone.calls[0]["tool"] if phone.calls else None,
                                                  "Created" in out), (1, "create_calendar_event", True))
check("and it's done", lib._run_held_action(h2), "That held action was already done.")
check("the iPhone runs calendar writes (the fallback is real)",
      ch.CALENDAR_WRITES <= lib.PHONE_DEVICE_TOOLS, True)

print("── Don't calls it off ──")
as_person("robin", device=device(ASLEEP), said="add Leo dentist at 2")
lib._device_request("create_calendar_event", {"title": "Leo dentist", "start": "2026-10-19T14:00:00"})
h3 = pending("robin")[-1]["id"]
as_person("robin")
check("Robin's Don't", lib._drop_held_action(h3), "Called off: nothing was changed.")
sent.clear()
ch.drain(live=lambda o: True, send=lambda r: sent.append(1) or ("done", ""))
check("a called-off one is never sent", sent, [])
check("nor run by a later Do it", lib._run_held_action(h3).startswith("That held action was called off"), True)
check("the Don't message is recognised exactly", (held_actions.asked_drop(f"Drop held action {h3}"),
      held_actions.asked_drop(f"please drop held action {h3}")), (h3, None))

print("── taken but never answered: never resent, the day is read back ──")
CLOCK["now"] = T0
for label, ans in (("the Mac took it and never answered", {"error": xd.NO_ANSWER}),
                   ("the asking device timed out", {"error": "Confirmation timed out."})):
    CARDS.clear()
    hook = device(ans)
    as_person("robin", device=hook, said="add Jordan visit Friday at 4")
    out = lib._device_request("create_calendar_event", {"title": "Jordan visit", "start": "2026-10-16T16:00:00"})
    check(f"{label}: told it may or may not have happened", ("may or may not" in out, "Don't try it again" in out,
                                                             "when your Mac or iPhone is back" in out), (True, True, False))
    rows_c = [r for r in held_actions.pending(owner="robin", now=CLOCK["now"]) if r["kind"] == "check"]
    check(f"{label}: a check, not a held write", len(rows_c), 1)
    hc = rows_c[0]["id"]
    c = cards_for("robin")[-1]
    check(f"{label}: the card offers a read, never a resend", (c["title"], [a["value"] for a in c["actions"] or []]),
          ("Not sure it went through", ["What's on my calendar on Fri Oct 16?"]))
    as_person("robin", device=device(OK_ADD))
    check(f"{label}: Do it on a check runs nothing", lib._run_held_action(hc),
          "That was a check, not something to do; nothing was run.")
    sent.clear()
    reads = []
    res = ch.drain(live=lambda o: True, send=lambda r: sent.append(r["id"]) or ("done", ""),
                   read=lambda r, day: reads.append((r["owner"], day)) or ("done", "4:00 PM Jordan visit"))
    check(f"{label}: the Mac back reads the day, never sends the write", (sent, reads, [x["status"] for x in res]),
          ([], [("robin", "2026-10-16")], ["checked"]))
    check(f"{label}: the read-back goes to Robin only", (cards_for("robin")[-1]["title"], cards_for("alex")),
          ("Checked your calendar for Fri Oct 16", []))
    reads.clear()
    ch.drain(live=lambda o: True, send=lambda r: ("done", ""), read=lambda r, day: reads.append(day) or ("done", ""))
    check(f"{label}: read back once", reads, [])
as_person("robin", device=device({"error": xd.NO_ANSWER}), said="add Jordan visit Friday at 4")
lib._device_request("create_calendar_event", {"title": "Jordan visit", "start": "2026-10-16T16:00:00"})
hc = [r for r in held_actions.pending(owner="robin", now=CLOCK["now"]) if r["kind"] == "check"][-1]["id"]
reads = []
ch.drain(live=lambda o: True, send=lambda r: ("done", ""), read=lambda r, day: reads.append(day) or ("unreachable", ""))
check("a check whose Mac went again waits for the next try", (held_actions.row(hc, "robin")["used_at"], len(reads)),
      (None, 1))
ch.expire_stale(now=CLOCK["now"] + ch.WAIT_MAX_S + 60)
check("a check never done within a day ends with a card", [c["title"] for c in CARDS if c.get("key") == f"held-{hc}"][-1],
      "Couldn't check your calendar")

print("── which event: identity, never a title alone ──")
CLOCK["now"] = T0
as_person("robin", device=device(ASLEEP), said="add Dentist Monday at 10 and Tuesday at 10")
lib._device_request("create_calendar_event", {"title": "Dentist", "start": "2026-10-19T10:00:00"})
lib._device_request("create_calendar_event", {"title": "Dentist", "start": "2026-10-20T10:00:00"})
dent = [r for r in pending("robin") if r["args"].get("title") == "Dentist"]
check("Dentist Monday and Dentist Tuesday both stay held", sorted(r["args"]["start"][:10] for r in dent),
      ["2026-10-19", "2026-10-20"])
mon = next(r["id"] for r in dent if r["args"]["start"].startswith("2026-10-19"))
tue = next(r["id"] for r in dent if r["args"]["start"].startswith("2026-10-20"))
as_person("robin", device=device({"result": "Moved “Dentist” to 11:00 AM."}), said="move Monday's dentist to 11")
lib._device_request("update_calendar_event", {"title": "Dentist", "on": "2026-10-19", "start": "2026-10-19T11:00:00"})
check("a change to Monday's ends only Monday's older hold",
      (held_actions.row(mon, "robin")["expired_at"] is not None, held_actions.row(tue, "robin")["expired_at"]), (True, None))
check("event_id: only that event", (ch.same_event("id:E1", "id:E1"), ch.same_event("id:E1", "id:E2"),
      ch.same_event(ch.match_key("update_calendar_event", {"title": "Dentist", "on": "2026-10-20", "event_id": "E1"}),
                    ch.match_key("create_calendar_event", {"title": "Dentist", "start": "2026-10-20T10:00:00"}))),
      (True, False, False))
check("a time on both must agree", ch.same_event(
    ch.match_key("delete_calendar_event", {"title": "Dentist", "on": "2026-10-20T09:00:00"}),
    ch.match_key("create_calendar_event", {"title": "Dentist", "start": "2026-10-20T10:00:00"})), False)
ch.drain(live=lambda o: True, send=lambda r: ("done", ""))   # clear the rest before the next section

print("── 2. out of date: expires with a card, never fires late ──")
CARDS.clear()
CLOCK["now"] = T0
as_person("robin", device=device(ASLEEP), said="add Leo pickup today at 3:10")
lib._device_request("create_calendar_event", {"title": "Leo pickup", "start": "2026-10-15T15:10:00"})
h4 = pending("robin")[-1]["id"]
CLOCK["now"] = la(2026, 10, 15, 15, 11)
sent.clear()
ch.drain(live=lambda o: True, send=lambda r: sent.append(r["id"]) or ("done", ""))
check("its time passed: never sent", h4 in sent, False)
exp = [c for c in cards_for("robin") if c.get("key") == f"held-{h4}"][-1]
check("an out-of-date card to Robin, saying when", (exp["title"], "3:10 PM" in exp["detail"], "late" in exp["detail"]),
      ("Didn't happen: out of date", True, True))
as_person("robin", device=device(OK_ADD))
check("a Do it after its time is refused", lib._run_held_action(h4).startswith("That held action"), True)

CLOCK["now"] = T0
as_person("robin", device=device(ASLEEP), said="add Ava recital next week at 6")
lib._device_request("create_calendar_event", {"title": "Ava recital", "start": "2026-10-22T18:00:00"})
h5 = pending("robin")[-1]["id"]
CLOCK["now"] = T0 + ch.WAIT_MAX_S + 60
check("waited past the bound: expired", h5 in ch.expire_stale(now=CLOCK["now"]), True)
check("its card says it waited too long", "more than a day" in [c for c in CARDS if c.get("key") == f"held-{h5}"][-1]["detail"],
      True)

# a held confirm (from someone else's text) goes out of date too: take() refuses it after its time
CLOCK["now"] = T0
hid6 = held_actions.put("robin", "create_calendar_event", {"title": "Leo pickup", "start": "2026-10-15T14:50:00"},
                        now=T0, stale_at=ch.stale_at("create_calendar_event", {"start": "2026-10-15T14:50:00"}),
                        match=ch.match_key("create_calendar_event", {"title": "Leo pickup", "start": "2026-10-15T14:50:00"}))
check("a confirm hold past its time: take refuses",
      isinstance(held_actions.take(hid6, "robin", now=la(2026, 10, 15, 15, 0)), str), True)

print("── a newer change supersedes the older hold (the 2:50 / 3:10 pickup) ──")
CLOCK["now"] = T0
hid7 = held_actions.put("robin", "create_calendar_event", {"title": "Leo pickup", "start": "2026-10-16T14:50:00"},
                        now=T0, stale_at=la(2026, 10, 16, 14, 50),
                        match=ch.match_key("create_calendar_event", {"title": "Leo pickup", "start": "2026-10-16T14:50:00"}))
as_person("robin", device=device({"result": "Moved “Leo pickup” to 3:10 PM."}), said="move Leo pickup to 3:10")
lib._device_request("update_calendar_event", {"title": "Leo pickup", "on": "2026-10-16", "start": "2026-10-16T15:10:00"})
check("the old 2:50 hold ended", held_actions.row(hid7, "robin")["expired_at"] is not None, True)
check("approving it later does nothing", lib._run_held_action(hid7).startswith("That held action was called off"), True)
LEO = ch.match_key("create_calendar_event", {"title": "Leo pickup", "start": "2026-10-16T14:50:00"})
hid8 = held_actions.put("alex", "create_calendar_event", {"title": "Leo pickup", "start": "2026-10-16T14:50:00"},
                        now=T0, match=LEO)
held_actions.supersede("robin", lambda m: ch.same_event(LEO, m), now=T0)
check("Robin's change never touches Alex's hold", held_actions.row(hid8, "alex")["expired_at"], None)

print("── someone not going is not a cancelled event ──")
as_person("robin", device=device({"result": "Deleted."}), said="Ava isn't going to ballet today")
out = lib._device_request("delete_calendar_event", {"title": "Ballet + Modern", "on": "2026-10-15"})
check("refused, asks instead", (out.startswith("Not deleted"), "cancelled for everyone" in out), (True, True))
check("the device was never asked", lib._TURN_IO.device.calls, [])
as_person("robin", device=device({"result": "Deleted."}), said="Ava isn't going, cancel ballet today")
out = lib._device_request("delete_calendar_event", {"title": "Ballet + Modern", "on": "2026-10-15"})
check("an explicit cancel still goes", out, "Deleted.")
for said, refused in (("Ava isn't going to ballet, but don't cancel it", True),
                      ("Ava isn't going to ballet; do not delete it", True),
                      ("Ava is skipping ballet, no need to cancel", True),
                      ("Ava isn't going to ballet and it's not cancelled", True),
                      ("Ava isn't going to ballet, never remove it", True),
                      ("Ava isn't going so cancel ballet", False),
                      ("Ava won't make it to ballet. Delete it.", False),
                      ("Ava isn't going, ballet is cancelled", False)):
    as_person("robin", device=device({"result": "Deleted."}), said=said)
    out = lib._device_request("delete_calendar_event", {"title": "Ballet + Modern", "on": "2026-10-15"})
    check(f"{said!r}: {'refused' if refused else 'goes'}", out.startswith("Not deleted"), refused)
as_person("robin", device=device({"result": "Deleted."}), said="delete the dentist on Monday")
lib._TURN_IO.said = "Leo is skipping soccer\ndelete the dentist on Monday"
check("an absence earlier in the chat doesn't block a later delete",
      lib._device_request("delete_calendar_event", {"title": "Dentist", "on": "2026-10-19"}), "Deleted.")

print("── saying the answer was wrong is not a change (#1610) ──")
for said, refused in (("No, that's wrong, it's at 9.", True),
                      ("That's not right, it's at 9", True),
                      ("you're wrong, the cleaning is at 9", True),
                      ("No, that's wrong, don't move it, just check again", True),
                      ("No, move it to 9", False),
                      ("That's wrong, change it to 9 AM", False),
                      ("it's wrong, please fix it: 9 AM", False),
                      ("move the cleaning to 9", False)):
    as_person("robin", device=device({"result": "Moved."}), said=said)
    out = lib._device_request("update_calendar_event", {"title": "Cleaning", "start": "2026-10-19T09:00:00"})
    check(f"{said!r}: {'refused' if refused else 'goes'}", out.startswith("Not changed"), refused)
as_person("robin", device=device({"result": "Created."}), said="No, that's wrong, it's at 9.")
out = lib._device_request("create_calendar_event", {"title": "Cleaning", "start": "2026-10-19T09:00:00"})
check("…a second event at the disputed time isn't made either, and the device is never asked",
      (out.startswith("Not changed"), "list_calendar_events" in out, lib._TURN_IO.device.calls), (True, True, []))
as_person("robin", device=device({"result": "Moved."}), said="No, that's wrong, it's at 9.")
lib._TURN_IO.confirmed_call = ("update_calendar_event", json.dumps({"title": "Cleaning"}, sort_keys=True))
check("the person's own Yes on the held change still goes",
      lib._device_request("update_calendar_event", {"title": "Cleaning"}), "Moved.")
as_person("robin", device=device({"result": "Moved."}), said=None)
check("not a chat turn (a scheduled action): untouched",
      lib._device_request("update_calendar_event", {"title": "Cleaning"}), "Moved.")

print("── 3. a partial or failed sync never deletes ──")


class Runtime:
    def __init__(self):
        self.deleted = []

    def __call__(self, service, path, method, payload=None):
        if method == "DELETE":
            self.deleted.append(path)
        return {"id": "n-1", "event_id": "e-1"}


rt = Runtime()
LEDGER = _TMP / "ledger.db"
evs = [{"source_id": f"UID-{i}@2026-10-{15 + i:02d}T17:00:00Z", "observed_at": "2026-10-14", "kind": "appointment",
        "title": f"Robin event {i}", "text": "x", "fields": {"start": f"2026-10-{15 + i:02d}T17:00:00Z"}} for i in range(3)]


def ingest(items, sync=None, owner="robin", dev="robin-mac-new"):
    return cx.ingest("calendar", items, runtime=rt, ledger=LEDGER, owner=owner, sync=sync, device_id=dev)


def live_ids(owner="robin"):
    return sorted(cx.live_upstream_ids(owner, "calendar", "UID-", ledger=LEDGER))


ingest(evs)
all3 = sorted(e["source_id"] for e in evs)
check("three events on titan", live_ids(), all3)
# the new Mac (jellybean#1286) sends a sync object: strict from then on. Its first batch here is a partial read.
out = ingest([], {"complete": False})
check("a batch with a sync object: no legacy path", out.get("legacy_calendar_deletes"), 0)
gone = [{"source_id": e["source_id"], "deleted": True} for e in evs[:2]]
out = ingest(gone)
check("a bare absence (no proof): nothing removed", (out["removed"], out["kept_absent"], live_ids(), rt.deleted),
      (0, 2, all3, []))
check("the kept ids come back so the device can prove them later", sorted(out["kept_absent_ids"]),
      sorted(e["source_id"] for e in evs[:2]))
W = {"window_start": "2026-10-14T00:00:00Z", "window_end": "2026-11-14T00:00:00Z"}
for label, sync in (("a partial sync", {**W, "complete": False}), ("a failed sync", {**W, "complete": True, "error": "denied"}),
                    ("a sync that says ok: false", {**W, "complete": True, "ok": False}),
                    ("partial: true", {**W, "complete": True, "partial": True}),
                    ("no window", {"complete": True}),
                    ("a window that doesn't cover them", {"complete": True, "window_start": "2026-11-01T00:00:00Z",
                                                          "window_end": "2026-12-01T00:00:00Z"}),
                    ("complete as a string", {**W, "complete": "true"})):
    out = ingest(gone, sync)
    check(f"{label}: nothing removed", (out["removed"], live_ids()), (0, all3))
check("no KB or node deletes so far", rt.deleted, [])
out = ingest([{"source_id": evs[0]["source_id"], "deleted": True}], {**W, "complete": True})
check("a complete window covering it: removed", (out["removed"], evs[0]["source_id"] in live_ids()), (1, False))
out = ingest([{"source_id": evs[1]["source_id"], "deleted": True, "deleted_reason": "deleted"}])
check("a delete the device saw: removed", (out["removed"], evs[1]["source_id"] in live_ids()), (1, False))
out = ingest([{"source_id": evs[2]["source_id"], "deleted": True, "deleted_reason": "missing"}])
check("an unknown reason is not a delete signal", (out["removed"], evs[2]["source_id"] in live_ids()), (0, True))
ingest([{**evs[2], "source_id": evs[2]["source_id"]}], owner="alex")
ingest([{"source_id": evs[2]["source_id"], "deleted": True, "deleted_reason": "deleted"}], owner="alex")
check("Alex's delete never removes Robin's copy", evs[2]["source_id"] in live_ids("robin"), True)
print("── transition: today's Jellybean (no proof fields) deletes as before until it sends a sync object ──")
LEG = _TMP / "ledger-legacy.db"
leg = [{**e, "source_id": "L" + e["source_id"]} for e in evs]


def ingest_l(items, sync=None, dev="robin-mac-old", owner="robin"):
    return cx.ingest("calendar", items, runtime=rt, ledger=LEG, owner=owner, sync=sync, device_id=dev)


def live_l(owner="robin"):
    return sorted(cx.live_upstream_ids(owner, "calendar", "LUID-", ledger=LEG))


ingest_l(leg)
out = ingest_l([{"source_id": leg[0]["source_id"], "deleted": True}])
check("a legacy batch still deletes as today, counted", (out["removed"], out["kept_absent"], out["legacy_calendar_deletes"],
      leg[0]["source_id"] in live_l()), (1, 0, 1, False))
st = cx.state("robin", ledger=LEG)
check("counted in the sync log (connectors state), Robin's own", ([x["count"] for x in st["legacy_calendar_deletes"]],
      cx.state("alex", ledger=LEG)["legacy_calendar_deletes"]), ([1], []))
out = ingest_l([{"source_id": leg[1]["source_id"], "deleted": True, "deleted_reason": "missing"}])
check("a batch with any proof field follows the strict rules", (out["removed"], out["kept_absent"],
      out["legacy_calendar_deletes"]), (0, 1, 0))
out = ingest_l([{"source_id": leg[1]["source_id"], "deleted": True}], {"complete": True, "error": "denied"})
check("the old Mac's first sync object (a failed read): nothing removed", (out["removed"], leg[1]["source_id"] in live_l()),
      (0, True))
out = ingest_l([{"source_id": leg[1]["source_id"], "deleted": True}])
check("a device that sent proof once stays strict (a downgrade can't relax it)",
      (out["removed"], out["kept_absent"], out["legacy_calendar_deletes"], leg[1]["source_id"] in live_l()), (0, 1, 0, True))
out = ingest_l([{"source_id": leg[2]["source_id"], "deleted": True}], dev="robin-ipad")
check("another device of Robin's that never sent proof is still legacy", (out["removed"], out["legacy_calendar_deletes"]),
      (1, 1))
ingest_l([{**leg[1]}], owner="alex", dev="robin-mac-old")
out = ingest_l([{"source_id": leg[1]["source_id"], "deleted": True}], owner="alex", dev="robin-mac-old")
check("strict is per person and device: Alex's own device key is his", out["legacy_calendar_deletes"], 1)

mo = cx.ingest("fixture", [{"source_id": "F-1", "title": "f"}], runtime=rt, ledger=LEDGER, owner="robin")
mo = cx.ingest("fixture", [{"source_id": "F-1", "deleted": True}], runtime=rt, ledger=LEDGER, owner="robin")
check("other sources unchanged: a bare delete still removes", (mo["removed"], "kept_absent" in mo), (1, False))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
