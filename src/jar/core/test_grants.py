#!/usr/bin/env python3
"""#395/#382: grants, with the owner's amendments (10-01). Hermetic: temp stores."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
tmp = Path(tempfile.mkdtemp())
os.environ["GRANTS_DB"] = str(tmp / "grants.db")
os.environ["GRANTS_AUDIT"] = str(tmp / "audit.jsonl")
import grants as g  # noqa: E402
import provenance as pv  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


def refused(fn, *a, **k) -> str:
    try:
        fn(*a, **k)
        return ""
    except ValueError as exc:
        return str(exc)


NOW = 1_790_900_000.0
H = 3600
MAC = dict(issuer_role="admin", issuer_person="zach")
TEXT = ("send_message", {"to": "Ava", "text": "Dinner is at 6"})

# The contract's vectors (the apps reproduce these byte for byte).
for (case, want) in pv.ARGS_DIGEST_VECTORS:
    check(f"vector {case['tool']}", pv.args_digest(case["tool"], case["args"]) == want)
check("50.0 and 50 hash the same", pv.args_digest("home_control", {"v": 50.0}) == pv.args_digest("home_control", {"v": 50}))
check("slashes and non-ASCII are raw in the canonical form", pv.canonical_json({"p": "~/Desktop/a.txt", "t": "café"})
      == '{"p":"~/Desktop/a.txt","t":"café"}')

# Classes.
check("reads are E0, writes classed, unknown tools E4",
      g.effect_class("list_calendar_events", read_tools={"list_calendar_events"}) == "E0"
      and g.effect_class("create_calendar_event") == "E2" and g.effect_class("send_message") == "E3"
      and g.effect_class("delete_reminder") == "E4" and g.effect_class("brand_new", read_tools=set()) == "E4")

# Amendment 1: E4 never ahead.
check("E4 is never granted ahead", "never granted" in refused(g.issue, "zach", "mac:d1", ("scheduled", "sa-1"), "E4",
                                                             ["delete_reminder"], expires_at=NOW + H, now=NOW, **MAC))
# Amendment 2: E3 exact, single use, ≤ 2 h.
check("E3 without exact args is refused", "exact" in refused(g.issue, "zach", "mac:d1", ("scheduled", "sa-1"), "E3",
                                                             ["send_message"], expires_at=NOW + H, now=NOW, **MAC))
check("E3 for 3 hours is refused", "2 hours" in refused(g.issue, "zach", "mac:d1", ("scheduled", "sa-1"), "E3",
                                                        ["send_message"], args=TEXT, not_before=NOW, expires_at=NOW + 3 * H, now=NOW, **MAC))
check("E3 with two uses is refused", "one use" in refused(g.issue, "zach", "mac:d1", ("scheduled", "sa-1"), "E3",
                                                          ["send_message"], args=TEXT, expires_at=NOW + H, uses=2, now=NOW, **MAC))
e3 = g.issue("zach", "mac:d1", ("scheduled", "sa-1"), "E3", ["send_message"], args=TEXT, not_before=NOW + 60,
             expires_at=NOW + H, now=NOW, **MAC)
check("E3 exact, one use, inside 2 h: granted", e3["args_digest"] == pv.args_digest(*TEXT) and e3["uses_left"] == 1)
# Amendment 4: owner == the device's person; no shared devices; titan only E0/E1.
check("a device can't approve for another person", "own person" in refused(
    g.issue, "zach", "mac:d1", ("scheduled", "sa-2"), "E2", ["set_alarm"], args=("set_alarm", {}), expires_at=NOW + H,
    now=NOW, issuer_role="adult", issuer_person="p_robin"))
check("a shared or household device can't approve ahead", "shared" in refused(
    g.issue, "zach", "iphone:kitchen", ("scheduled", "sa-2"), "E2", ["set_alarm"], args=("set_alarm", {}),
    expires_at=NOW + H, now=NOW, issuer_role="household", issuer_person="zach"))
# The owner's review of #448: a scheduled action's grant binds its exact call, once, until it's due.
check("a scheduled action's grant without the exact call is refused", "exact call" in refused(
    g.issue, "zach", "mac:d1", ("scheduled", "sa-2"), "E2", ["home_control"], expires_at=NOW + H, now=NOW, **MAC))
check("nor for two uses", "exact call" in refused(g.issue, "zach", "mac:d1", ("scheduled", "sa-2"), "E2", ["set_alarm"],
                                                  args=("set_alarm", {}), uses=2, expires_at=NOW + H, now=NOW, **MAC))
check("nor past when the action is due", "due" in refused(g.issue, "zach", "mac:d1", ("scheduled", "sa-2"), "E2", ["set_alarm"],
                                                          args=("set_alarm", {}), expires_at=NOW + 3 * H, subject_deadline=NOW + H,
                                                          now=NOW, **MAC))
check("a tool-level E2 grant is a task step's only", g.issue("zach", "mac:d1", ("task_step", "task:t9"), "E2", ["home_control"],
                                                            expires_at=NOW + H, now=NOW, **MAC)["args_digest"] is None)
check("a file write is approved ahead only for the exact call, once", "exact call" in refused(
    g.issue, "zach", "mac:d1", ("task_step", "task:t9"), "E2", ["write_file"], expires_at=NOW + H, now=NOW, **MAC))
check("an unknown subject kind is refused", "subject" in refused(g.issue, "zach", "titan", ("whatever", "x"), "E1",
                                                                 ["add_todo"], expires_at=NOW + H, now=NOW))
check("titan itself grants E0/E1 only", "E0/E1" in refused(g.issue, "zach", "titan", ("task_step", "task:t1"), "E2",
                                                           ["home_control"], expires_at=NOW + H, now=NOW))
t1 = g.issue("zach", "titan", ("task_step", "task:t1"), "E1", ["add_todo", "kb_retrieve"], expires_at=NOW + 7 * 86400,
             uses=5, now=NOW, classify=lambda t: "E0" if t == "kb_retrieve" else g.effect_class(t))
check("titan grants a task E0/E1 tools", t1["effect_class"] == "E1" and t1["uses_left"] == 5)
check("tools above the class are refused", "above" in refused(g.issue, "zach", "titan", ("task_step", "task:t1"), "E1",
                                                               ["home_control"], expires_at=NOW + H, now=NOW))
# Amendment 5: ≤ 30 days.
check("longer than 30 days is refused", "30 days" in refused(g.issue, "zach", "mac:d1", ("scheduled", "sa-3"), "E2",
                                                             ["set_alarm"], expires_at=NOW + 31 * 86400, now=NOW, **MAC))

# Checking and using.
C = dict(now=NOW + 120)
check("the exact approved message passes", g.check(e3["id"], "zach", *TEXT, **C) is None)
check("a changed text doesn't", "exactly" in (g.check(e3["id"], "zach", "send_message", {"to": "Ava", "text": "Dinner at 7"}, **C) or ""))
check("someone else can't use it", "someone else" in (g.check(e3["id"], "p_robin", *TEXT, **C) or ""))
check("before its window it doesn't apply", "window" in (g.check(e3["id"], "zach", *TEXT, now=NOW) or ""))
check("a use passes once", g.use(e3["id"], "zach", *TEXT, **C) is None)
check("then it's used up", "used up" in (g.use(e3["id"], "zach", *TEXT, **C) or ""))
lines = [json.loads(x) for x in (tmp / "audit.jsonl").read_text().splitlines()]
check("every use is audited, content-free", [x["ok"] for x in lines] == [True, False]
      and all(set(x) == {"grant", "tool", "ok", "ts"} for x in lines), str(lines))

# Amendment 5: revoking cancels the holder.
held = []
e2 = g.issue("zach", "mac:d1", ("scheduled", "sa-9"), "E2", ["set_alarm"], args=("set_alarm", {}), expires_at=NOW + H,
             now=NOW, **MAC)
check("only the owner (or the admin) revokes", not g.revoke(e2["id"], "p_robin"))
check("revoking cancels what holds it", g.revoke(e2["id"], "zach", cancel_holder=held.append, now=NOW + 1)
      and held and held[0]["subject_ref"] == "sa-9")
check("a revoked grant no longer applies", "revoked" in (g.check(e2["id"], "zach", "set_alarm", {}, now=NOW + 2) or ""))
check("the owner's list shows active grants only", all(x["id"] != e2["id"] for x in g.list_for("zach", now=NOW + 2)))

check("the grants store is created 640", (os.stat(os.environ["GRANTS_DB"]).st_mode & 0o777) == 0o640)
api_src = (HERE / "agent_api_server.py").read_text()
check("registering checks the subject is the caller's own pending action, else 404",
      "no such pending action of yours" in api_src and "status_code=404" in api_src.split("no such pending action of yours")[0][-400:])

# Wired: the routes, the class table, and a scheduled device action carrying its grant.
import household_identity as hh  # noqa: E402
check("people may list, register and revoke their own grants", all(hh.route_allowed("adult", m, "/v1/grants" + p)
      for m, p in (("GET", ""), ("POST", ""), ("DELETE", "/gr-1"))) and not hh.route_allowed("household", "POST", "/v1/grants"))
import agent_runtime_lib as lib  # noqa: E402
import scheduled  # noqa: E402
check("the agent's class table: reads E0, deletes E4", lib.effect_class("list_calendar_events") == "E0"
      and lib.effect_class("delete_calendar_event") == "E4" and lib.effect_class("add_todo") == "E1")
row = {"id": "sa-x", "owner": "zach", "tool": "set_alarm", "args": {"time": "07:00"}}
import time as _t  # noqa: E402
gx = g.issue("zach", "mac:d1", ("scheduled", "sa-x"), "E2", ["set_alarm"], args=("set_alarm", {"time": "07:00"}),
             expires_at=_t.time() + H, **MAC)
check("a scheduled device action finds and spends its grant", scheduled._grant_for(row) == gx["id"])
check("and not twice", scheduled._grant_for(row) is None)
check("a titan action carries no grant", scheduled._grant_for({**row, "tool": "get_weather"}) is None)

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all grant checks passed'}")
sys.exit(1 if failures else 0)
