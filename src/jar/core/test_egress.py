#!/usr/bin/env python3
# gate: hermetic   (every external call is stubbed; nothing leaves the machine)
"""The egress ledger (#409): each turn records, content-free, which external services it sent data to.
The turn log carries `egress` and `person`, the API's done event and /v1/prompt carry `egress` (the apps'
"left the house" badge), and the weekly report counts it. Hermetic.

Run: python3 scripts/test_egress.py
"""
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-egress-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["GEMINI_API_KEY"] = "test-key-not-real"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import egress  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


class NoNetwork(Exception):
    pass


def no_network(*a, **k):
    raise NoNetwork("blocked by test")


print("\n── the sink ──")
egress.begin(None)
egress.note("gemini")
check("outside a turn: nothing recorded", egress.current(), None)
outer = []
prev = egress.begin(outer)
egress.note("gemini"); egress.note("gemini"); egress.note("jev")
check("once per kind, in order", outer, ["gemini", "jev"])
inner = []
p2 = egress.begin(inner)
egress.note("weather")
egress.begin(p2)
check("a nested turn records into its own list and gives the outer one back", (inner, egress.current()), (["weather"], ["gemini", "jev"]))
egress.begin(prev)

print("\n── each hook notes its kind (the network is stubbed) ──")
real_urlopen = urllib.request.urlopen
urllib.request.urlopen = no_network
import weather  # noqa: E402
import push  # noqa: E402
import todo_issues  # noqa: E402
import youtube_sync  # noqa: E402


def run_turn(body):
    """A turn whose inner part runs `body`; returns the turn's egress list and its log line."""
    real_inner = lib._run_agent_turn_inner
    lib._run_agent_turn_inner = lambda messages, *a, **k: (body(), "A real answer for the test.")[1]
    lib._TURN_IO.person = {"id": "p_m", "name": "Robin"}
    lib._TURN_IO.role = "adult"
    try:
        lib._run_agent_turn([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}])
    finally:
        lib._run_agent_turn_inner = real_inner
        lib._TURN_IO.person = lib._TURN_IO.role = None
    rec = json.loads(Path(os.environ["AGENT_TURN_LOG"]).read_text().splitlines()[-1])
    return list(lib.last_turn_info().get("egress") or []), rec


def try_(f):
    try:
        f()
    except Exception:  # noqa: BLE001  (the stubbed network fails; the note comes first)
        pass


class FakeJev:
    def resolve_credentials(self):
        return True

    def decide(self, *a, **k):
        return {"answers": {}}

    other = "passes through"


got, rec = run_turn(lambda: None)
check("a fully local turn: egress []", got, [])
check("… and the log says so, with the person", (rec.get("egress"), rec.get("person")), ([], "p_m"))

cases = [
    ("gemini (the chat brain's call)", lambda: try_(lambda: lib._gemini_call_attempts({"contents": []}, "m", 1)), ["gemini"]),
    ("gemini (image, bill, speech helpers)", lambda: try_(lambda: lib._gemini("m", [{"text": "x"}], timeout=1)), ["gemini"]),
    ("ai_gateway (Vercel OpenAI-compatible brain)",
     lambda: try_(lambda: (os.environ.__setitem__("AI_GATEWAY_API_KEY", "test-gw"),
                           lib._ai_gateway_call({"model": "m", "messages": []}))[-1]), ["ai_gateway"]),
    ("web search through Gemini grounding", lambda: try_(lambda: lib._gemini_search("q", timeout=1)), ["gemini", "web_search"]),
    ("web search through Brave", lambda: try_(lambda: lib._brave_search("q", "key")), ["web_search"]),
    ("fetch_url", lambda: try_(lambda: lib._execute_tool("fetch_url", {"url": "https://example.com"})), ["fetch_url"]),
    ("weather (Open-Meteo)", lambda: try_(lambda: weather._get("https://api.open-meteo.com/x")), ["weather"]),
    ("jev (any decide through _jev_client)", lambda: lib._JevEgress(FakeJev()).decide({}, {}), ["jev"]),
    ("github (Claude's work queue)", lambda: try_(lambda: todo_issues._api("t")("GET", "/x")), ["github"]),
    ("youtube (a person's liked videos, #470)",
     lambda: try_(lambda: youtube_sync.urllib_http("GET", youtube_sync.API + "/x", None, {})), ["youtube"]),
]
for label, body, want in cases:
    got, rec = run_turn(body)
    check(label, (got, rec.get("egress")), (want, want))

check("the Jev proxy passes everything else through", lib._JevEgress(FakeJev()).other, "passes through")

started = []
real = (push.should_push, push.configured, push.threading.Thread)
push.should_push, push.configured = (lambda c: True), (lambda: True)
push.threading.Thread = lambda **k: type("T", (), {"start": lambda self: started.append(k.get("name"))})()
try:
    got, _ = run_turn(lambda: push.push_card_async({"id": "c1"}))
finally:
    push.should_push, push.configured, push.threading.Thread = real
check("push: noted on the turn's thread, sent on another", (got, started), (["push"], ["apns-push"]))
push.should_push = lambda c: False
try:
    got, _ = run_turn(lambda: push.push_card_async({"id": "c2"}))
finally:
    push.should_push = real[0]
check("a card that isn't pushed isn't egress", got, [])

lib._TURN_IO.device = lambda req: {"result": "sent"}
lib._TURN_IO.device_tools = lib.device_tools_for_client(None, None)
try:
    got, _ = run_turn(lambda: lib._execute_tool("send_to_claude", {"text": "hello"}))
finally:
    lib._TURN_IO.device = lib._TURN_IO.device_tools = None
check("send_to_claude: claude", got, ["claude"])
import mail_server  # noqa: E402
got, _ = run_turn(mail_server._note_egress)   # what opening a mail account notes (#955; search_mail runs in a turn)
check("the person's mail server: mail", got, ["mail"])
check("every kind in the module's list was exercised",
      sorted(egress.KINDS), sorted({"gemini", "ai_gateway", "jev", "web_search", "fetch_url", "weather", "push", "github", "claude", "mail", "youtube"}))
urllib.request.urlopen = real_urlopen

print("\n── the API carries it to the apps ──")
try:
    import agent_api_server as api
    import household_identity as hh
except Exception as exc:  # noqa: BLE001
    print(f"  [SKIP] agent_api_server not importable here: {exc}")
else:
    real_turn = api._lib._run_agent_turn

    def fake(messages, *a, **k):
        lib._TURN_INFO.info = {"egress": ["gemini", "jev"], "brain": "gemini"}
        return "ok"
    api._lib._run_agent_turn = fake
    try:
        api._init_turn("t-egress")
        api._turns["t-egress"].update({"identity": hh.owner_identity(), "source": "eval"})
        api._run_turn("t-egress", "q", [], True, None)
        check("the turn state holds the kinds (the done event and /v1/prompt read it)",
              api._turns["t-egress"].get("egress"), ["gemini", "jev"])
    finally:
        api._lib._run_agent_turn = real_turn
        api._turns.pop("t-egress", None)

    # The skill pick asks Jev while the prompt is built, before the turn's own sink opens (live 10-02: a
    # calendar turn asked Jev and reported egress []). It must still count, once.
    real_match, real_inner = api._skill_match, lib._run_agent_turn_inner
    api._skill_match = lambda q, sk: (egress.note("jev"), (True, {"calendar"}))[1]
    lib._run_agent_turn_inner = lambda messages, *a, **k: (egress.note("gemini"), "A real answer.")[1]
    try:
        api._init_turn("t-pick")
        api._turns["t-pick"].update({"identity": hh.owner_identity(), "source": "eval"})
        api._run_turn("t-pick", "What's on my calendar tomorrow?", [], True, None)
        check("a Jev skill pick while the prompt is built is in the turn's egress, with the turn's own",
              api._turns["t-pick"].get("egress"), ["jev", "gemini"])
        rec = json.loads(Path(os.environ["AGENT_TURN_LOG"]).read_text().splitlines()[-1])
        check("… and in the turn log", rec.get("egress"), ["jev", "gemini"])
        check("… taken once: nothing carried to the next turn", getattr(lib._TURN_IO, "pre_egress", None), None)
        check("no sink left open after the turn", egress.current(), None)
    finally:
        api._skill_match, lib._run_agent_turn_inner = real_match, real_inner
        api._turns.pop("t-pick", None)
    src = Path(api.__file__).read_text()
    check("the done event and /v1/prompt include egress",
          ('"egress": egress_kinds' in src, '"egress": list(turn.get("egress") or [])' in src), (True, True))

print("\n── the weekly report ──")
import agent_turn_report as report  # noqa: E402
rows = [{"egress": ["gemini", "jev"], "person": "zach"}, {"egress": ["gemini"], "person": "zach"},
        {"egress": [], "person": "zach"}, {"egress": [], "person": "p_m"}, {"brain": "gemini"}]
check("left the house, by kind and by person", report.egress_lines(rows),
      ["left the house: 2 of 4 turns (gemini 2, jev 1); fully local: 2", "by person: zach 67% of 3, p_m 0% of 1"])
check("no turn logged egress yet: no lines", report.egress_lines([{"brain": "gemini"}]), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
