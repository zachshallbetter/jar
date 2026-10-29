#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Three fixes from the 10-02 review of Zach's conversations:
  (a) a business with several places (Maple Health): an event's address must be the branch its purpose or person fits,
      else titan asks, naming them (Zach's dentist given for Robin's pharmacy pickup; Ava's therapy address for
      "it on 2nd Ave");
  (b) "yes" / "all of the above" to titan's own offer: it does it, not ask again (10-01 21:52–21:54);
  (c) people's aliases: "Jo" is Jordan, in the people store and in name matching.
Temp stores and a fake device. Run: python3 scripts/test_convo_fixes.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-convo-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "WORLD_DIR": "world"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "test"
os.environ["AGENT_WORLD_MODEL"] = "1"
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


st = hh.HouseholdStore(_TMP / "people.db")
hh.reset_store(st)
zach = hh.ADMIN_PERSON_ID
rob = st.add_person("Robin", "adult")["id"]
jor = st.add_person("Jordan", "adult")["id"]
ava = st.add_person("Ava", "kid")["id"]
st.update_person(ava, parents=[zach, rob])
FOLKS = st.people()

print("── (c) aliases ──")
st.update_person(jor, aliases=["Jo", "jo", "Jordan", "  "])
check("stored once each, not their own first name", st.person_facts(jor)["aliases"], ["Jo"])
check("the people store finds her by it", (st.person_named("jo") or {}).get("id"), jor)
check("…and by her name", (st.person_named("Jordan") or {}).get("id"), jor)
try:
    st.update_person(rob, aliases=["Jo"])
    clash = None
except hh.PairingError as exc:
    clash = exc.args[-1] if exc.args else str(exc)
check("an alias that already means someone else is refused", "already means Jordan" in str(clash), True)
try:
    st.update_person(rob, aliases=["<script>"])
    bad = None
except hh.PairingError as exc:
    bad = str(exc)
check("…so is one that isn't a name", bad is not None, True)
lib._PEOPLE_CACHE["at"] = 0
check("name matching: “Jo's recital” is Jordan", lib._named_people("When is Jo's recital?"), [(jor, "Jordan")])
check("the household line says it", "Jordan is also called Jo." in lib.household_roster(), True)
check("a world qualifier “for Jo” is Jordan", W.norm_qual({"for": "Jo"}, st.people()), {"for": jor})
lib._TURN_IO.person = {"id": zach, "name": "Zach"}
lib._TURN_IO.role = "admin"
st.update_person(jor, calendars=["Jordan"])
check("an event “for Jo” goes on Jordan's calendar", lib._default_calendar({"title": "x", "for": "Jo"})[0].get("calendar"),
      "Jordan")
src = (Path(lib.__file__).with_name("agent_api_server.py")).read_text()
check("PATCH /v1/people takes aliases", '"other_parent", "aliases") if k in body' in src, True)

print("── (a) the branch an event's purpose or person fits ──")
B = W.get()
B.ensure("maple health-health", "org", "Maple Health", "household")
B.ensure("lilac-counseling", "org", "Lilac Counseling", "household")
FOLKS = st.people()
W.set_fact(B, "Maple Health", "address", "1502 N Birch Rd", zach, "admin", people=FOLKS, qual={"purpose": "pharmacy"})
W.set_fact(B, "Maple Health", "address", "1001 W 2nd Ave", zach, "admin", people=FOLKS, qual={"purpose": "dental"})
W.set_fact(B, "Lilac Counseling", "address", "220 E 2nd Ave", zach, "admin", people=FOLKS, qual={"for": "Ava"})
lib._TURN_IO.said = "put Robin's prescription pickup on the calendar for Friday"
EV = {"title": "Pick up Robin's prescription", "start": "2026-10-09T15:00", "for": "Robin"}
got = lib._branch_check({**EV, "location": "1001 W 2nd Ave"})
check("Robin's pharmacy pickup at the dental branch: refused, the pharmacy named",
      (got or "").startswith("Maple Health has several places; for pharmacy it's Maple Health (pharmacy) at 1502 N Birch Rd"), True)
check("…the pharmacy branch is fine", lib._branch_check({**EV, "location": "1502 N. Birch Road"}), None)
got = lib._branch_check({"title": "Maple Health", "start": "2026-10-09T15:00", "location": "1502 N Birch Rd"})
check("nothing says which: ask, naming both", ("Ask which one" in (got or ""), "1001 W 2nd Ave" in (got or ""),
      "1502 N Birch Rd" in (got or "")), (True, True, True))
lib._TURN_IO.said = "add it on 2nd Ave for Zach, Thursday at 3"
got = lib._branch_check({"title": "Appointment", "start": "2026-10-08T15:00", "for": "me", "location": "220 E 2nd Ave"})
check("Ava's therapy address for an event that's Zach's: refused, whose it is", (got or "").startswith(
      "“220 E 2nd Ave” is on record as Lilac Counseling for Ava, not for Zach"), True)
check("…for Ava it's fine", lib._branch_check({"title": "Therapy", "for": "Ava", "location": "220 E 2nd Ave"}), None)
lib._TURN_IO.said = "my dentist appointment at 1001 W 2nd Ave"
check("an address the person said themselves isn't second-guessed",
      lib._branch_check({**EV, "location": "1001 W 2nd Ave"}), None)
check("an address not on record: nothing to check", lib._branch_check({**EV, "location": "9 Elm St"}), None)

calls = []
lib._TURN_IO.device = lambda req: calls.append(req) or {"result": "Created 'x' on Fri Oct 9, 3:00 PM on the Zach calendar."}
lib._TURN_IO.client = None
lib._TURN_IO.device_tools = None
lib._TURN_IO.said = "put Robin's prescription pickup on the calendar"
out = lib._execute_tool("create_calendar_event", {**EV, "location": "1001 W 2nd Ave"})
check("through the tool: not created, nothing sent to the device",
      (out.startswith("Not created: Maple Health has several places"), calls), (True, []))
lib._TURN_IO.device = None

print("── (b) a yes to titan's own offer ──")
check("the offer is found", lib._offer_in("I found three. Want me to add all of them to your calendar? Or just one?"),
      "Want me to add all of them to your calendar?")
for yes in ("yes", "All of the above", "yes please, do it", "Sure!", "go ahead", "both"):
    check(f"a plain yes: {yes!r}", bool(lib._YES_TO_OFFER_RX.match(yes)), True)
for no in ("Yes, but only Friday", "no", "what does that mean?", "yes it is Friday right?"):
    check(f"not a plain yes: {no!r}", bool(lib._YES_TO_OFFER_RX.match(no)), False)
seen = {}
real = lib._run_agent_turn_inner
lib._run_agent_turn_inner = lambda messages, *a, **k: seen.update(m=messages) or "ok"
lib._run_agent_turn([{"role": "user", "content": "add the trash days"},
                     {"role": "assistant", "content": "I found three days. Want me to add all of them to your calendar?"},
                     {"role": "user", "content": "All of the above"}])
note = [m["content"] for m in seen["m"] if m.get("role") == "system" and m["content"].startswith("[THEY SAID YES]")]
check("the model is told they said yes to that offer, and to do it",
      (len(note), "“Want me to add all of them to your calendar?”" in note[0], "don't ask again" in note[0]), (1, True, True))
lib._run_agent_turn([{"role": "user", "content": "x"}, {"role": "assistant", "content": "It's on Friday."},
                     {"role": "user", "content": "yes"}])
check("no offer before it: no note", any(m.get("role") == "system" and m["content"].startswith("[THEY SAID YES]")
                                         for m in seen["m"]), False)
lib._run_agent_turn_inner = real
lib._TURN_IO.person = None
lib._TURN_IO.role = None
lib._TURN_IO.said = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
