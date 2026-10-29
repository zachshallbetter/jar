#!/usr/bin/env python3
# gate: hermetic
"""always_ask is enforced (#1628): an always_ask tool never acts in the model's call. kb_forget (and a connected
server's destructive tool) is held, and a card asks the person on their own devices; their Do it runs it once, for
them only; a turn that isn't a person's own has nobody to ask, so nothing is done; a card that can't be sent means
nothing runs. The tools that confirm on the owner's device by themselves (lock_up, mail_move, mail_move_undo,
mail_unsubscribe) only ever
reach their request step from the model's call, never the step that acts. Fictional people (Alex the admin, Robin an
adult); temporary stores; every acting call is a spy. Run: python3 scripts/test_always_ask.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-always-ask-"))
os.environ["SPACES_DB"] = str(_TMP / "spaces.db")
os.environ["CAPABILITY_SWITCHES"] = str(_TMP / "switches.json")
os.environ["HELD_ACTIONS_DB"] = str(_TMP / "held.db")
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
os.environ["MCP_SERVERS"] = str(_TMP / "none.json")
os.environ["AGENT_TURN_SOURCE"] = "user"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import capability_audit as A  # noqa: E402
import governance as G  # noqa: E402
import held_actions  # noqa: E402
import kb_privacy  # noqa: E402
import proactive  # noqa: E402
import tool_descriptors as T  # noqa: E402

held_actions.DB = _TMP / "held.db"
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def as_person(pid, role="adult", source="user"):
    lib._TURN_IO.person = {"id": pid, "name": pid}
    lib._TURN_IO.role = role
    lib._TURN_IO.source = source
    lib._TURN_IO.client = "mac"
    lib._TURN_IO.capability_tiers = None
    lib._TURN_IO.confirmed_call = None


CARDS: list[dict] = []
DELETES: list[str] = []
proactive.add_card = lambda title, detail="", **kw: CARDS.append({"title": title, "detail": detail, **kw}) or {}
lib._runtime_request = lambda svc, path, method="GET", *a, **k: DELETES.append(path) if method == "DELETE" else {}
kb_privacy.access = lambda meta, rid, person, role: "edit"
kb_privacy.source_meta = lambda req, rid: {}
kb_privacy.drop_acl = lambda rid: None


def held_rows(owner):
    con = held_actions._db()
    try:
        return [dict(r) for r in con.execute("SELECT id, tool, used_at FROM held WHERE owner=?", (owner,))]
    finally:
        con.close()


print("── which tools ──")
always = sorted(n for n, d in T.DESCRIPTORS.items() if d.always_ask)
check("the always_ask tools today", always, ["kb_forget", "lock_up", "mail_move", "mail_move_undo", "mail_unsubscribe",
                                            "titan_health_run",
                                            "titan_model_start", "titan_model_stop", "titan_resources_sweep",
                                            "titan_service_restart"])
check("those that confirm on the device are always_ask ones", T.CONFIRMS_ON_DEVICE <= set(always), True)
check("held by the runtime: kb_forget, and titan's own restarts and canary runs (#1647)", [n for n in always if G.needs_hold(n)],
      ["kb_forget", "titan_health_run", "titan_model_start", "titan_model_stop", "titan_resources_sweep",
       "titan_service_restart"])
check("not always_ask: never held", [G.needs_hold(n) for n in ("search_mail", "kb_retrieve", "mystery_tool")], [False] * 3)

print("── kb_forget is held ──")
as_person("p_alex", role="admin")
out = lib.execute_tool("kb_forget", {"record_id": "8aad9465"})
check("the model's call is held, nothing deleted", (out.startswith(lib.ALWAYS_ASK_HELD), DELETES), (True, []))
check("…a card for Alex, with a Do it", (CARDS[-1]["to"], CARDS[-1]["actions"][0]["label"]), (["p_alex"], "Do it"))
check("…the card never shows the record's id", "8aad9465" in CARDS[-1]["title"] + CARDS[-1]["detail"], False)
check("…and the reply doesn't either", "8aad9465" in out, False)
rows = held_rows("p_alex")
check("one held call, for Alex", [(r["tool"], r["used_at"]) for r in rows], [("kb_forget", None)])
hid = rows[0]["id"]
check("the record says held", [(r["tool"], r["outcome"], r["why"]) for r in A.log("p_alex")][:1],
      [("kb_forget", "refused", "held")])
as_person("p_robin")
check("Robin can't run Alex's held call", (lib._run_held_action(hid), DELETES),
      ("There's no held action with that id for you.", []))
as_person("p_alex", role="admin")
said = lib._run_held_action(hid)
check("Alex's Do it runs it, once", (said.startswith("Forgotten"), DELETES), (True, ["/v1/retrieval/sources/8aad9465"]))
check("…and again does nothing", (lib._run_held_action(hid), len(DELETES)), ("That held action was already done.", 1))
check("after the Do it, the next call is held again", lib.execute_tool("kb_forget", {"record_id": "8aad9465"}).startswith(
    lib.ALWAYS_ASK_HELD), True)
G.set_tier("p_alex", "knowledgeBase", "never")
as_person("p_alex", role="admin")
hid2 = held_rows("p_alex")[-1]["id"]
check("turned off after the card went out: the Do it is refused", (lib._run_held_action(hid2).startswith("Not run:"), len(DELETES)),
      (True, 1))
G.set_tier("p_alex", "knowledgeBase", "always")

print("── nobody to ask ──")
n_cards = len(CARDS)
for src in ("eval", "canary", "test"):
    as_person("p_alex", role="admin", source=src)
    out = lib._always_ask_hold("kb_forget", {"record_id": "x"})
    check(f"a {src} turn: nothing done, no card", (out.startswith("Not done:"), len(CARDS), len(DELETES)), (True, n_cards, 1))
as_person("p_alex", role="admin")
real = proactive.add_card
proactive.add_card = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cards db locked"))
out = lib.execute_tool("kb_forget", {"record_id": "y"})
check("the card can't be sent: refused, nothing deleted", (out.startswith("Not done:"), len(DELETES)), (True, 1))
proactive.add_card = real

print("── a connected server's destructive tool ──")
import mcp_host  # noqa: E402
mcp_host.descriptor = lambda name: T.Descriptor("home", "dangerous", (), "none", True) if name == "mcp_home_wipe" else None
check("is held, by its server's words", (G.needs_hold("mcp_home_wipe"), lib._always_ask_hold("mcp_home_wipe", {}) or ""),
      (True, f"{lib.ALWAYS_ASK_HELD} (run wipe on the home server); nothing changes until they tap Do it. Tell them in one "
             "line, without repeating any id, code or link."))

print("── the ones that confirm on the device never act from the model's call ──")
import home_security  # noqa: E402
import mail_moves  # noqa: E402
ACTS: list[str] = []
REQ: list[str] = []
os.environ.update(HA_LOCK_UP="1", MAIL_SERVER="1", MAIL_SERVER_WRITES="1")
home_security._lock_one = lambda *a, **k: ACTS.append("lock.lock")
home_security.answer = lambda *a, **k: ACTS.append("lock answer")
home_security.request = lambda asker, people, **k: (REQ.append("lock request") or (202, {"locks": ["the front door"]}))
mail_moves.answer = lambda *a, **k: ACTS.append("mail answer")
mail_moves._execute = lambda *a, **k: ACTS.append("mail move")
mail_moves.request = lambda plan, person, **k: (REQ.append("mail request") or {"count": 3})
mail_moves.undo = lambda plan, person, **k: (REQ.append("mail undo request") or (True, "Confirm on your iPhone or Mac."))
import mail_unsubscribe  # noqa: E402
mail_unsubscribe.answer = lambda *a, **k: ACTS.append("unsub answer")
mail_unsubscribe._post = lambda *a, **k: ACTS.append("unsub post")
mail_unsubscribe.request = lambda plan, person, **k: (REQ.append("unsub request") or {"asked": True})
as_person("p_alex", role="admin")
outs = [lib.execute_tool("lock_up", {}), lib.execute_tool("mail_move", {"plan_id": "pl-1"}),
        lib.execute_tool("mail_move_undo", {"plan_id": "pl-1"}), lib.execute_tool("mail_unsubscribe", {"plan_id": "pl-2"})]
check("lock up, archive, undo and unsubscribe each only sent their request", (REQ, ACTS),
      (["lock request", "mail request", "mail undo request", "unsub request"], []))
check("…and said a tap is needed", ["iPhone" in o for o in outs], [True, True, True, True])
check("…and none was held a second time", any(o.startswith(lib.ALWAYS_ASK_HELD) for o in outs), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
