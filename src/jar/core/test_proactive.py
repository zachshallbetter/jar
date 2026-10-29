#!/usr/bin/env python3
"""
Unit tests for the proactive loop (scripts/proactive.py): confident noise is dropped, uncertain
events are investigated and reported as cards, NO_ACTION makes no card, a triage outage never
drops an event, duplicates and the hourly cap are enforced, cards are acked, and the agent's
notify_user tool raises a card. Hermetic: triage, the agent turn and the timeline are fakes.

Run: python3 scripts/test_proactive.py
"""
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))

import proactive as pa


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def triage(p_no, score=2):
    return lambda ev: {"actionable": {"probabilities": {"no": p_no, "yes": 1 - p_no}},
                       "urgency": {"score": score}}


def run():
    ok = True
    db = Path(tempfile.mkdtemp()) / "proactive.db"
    lines = []
    common = dict(timeline=lines.append, db=db, background=False)

    print("\n── triage and act ──")
    r = pa.submit_trigger({"source": "downloads", "summary": "build output changed"},
                          triage=triage(0.9), agent=lambda p: "should not run", **common)
    ok &= check("confident noise accepted then dropped", pa.recent_triggers(db=db)[0]["outcome"], "dropped")
    ok &= check("no card for noise", pa.list_cards(db=db), [])

    seen = {}
    r = pa.submit_trigger({"source": "downloads", "kind": "download", "summary": "invoice-oct.pdf from pge.com",
                           "detail": "212 KB"},
                          triage=triage(0.57, 2.2), agent=lambda p: seen.setdefault("p", p) and
                          "NOTIFY\nA PG&E bill arrived; it's due Oct 15.", **common)
    ok &= check("uncertain event investigated", "invoice-oct.pdf" in seen.get("p", ""), True)
    cards = pa.list_cards(db=db)
    ok &= check("one card", len(cards), 1)
    ok &= check("card detail is the agent's note", cards[0]["detail"], "A PG&E bill arrived; it's due Oct 15.")
    ok &= check("urgency from triage", cards[0]["urgency"], 2)
    ok &= check("timeline line written", len(lines), 1)
    ok &= check("trigger reported", pa.recent_triggers(db=db)[0]["outcome"], "reported")

    pa.submit_trigger({"source": "mail", "summary": "newsletter"}, triage=triage(0.5),
                      agent=lambda p: "NO_ACTION", **common)
    ok &= check("NO_ACTION makes no card", len(pa.list_cards(db=db)), 1)
    n_lines = len(lines)
    pa.submit_trigger({"source": "git", "summary": "docs typo fixed"}, triage=triage(0.5),
                      agent=lambda p: "PASSIVE\nFixed a typo in the README.", **common)
    ok &= check("PASSIVE: no card", len(pa.list_cards(db=db)), 1)
    ok &= check("PASSIVE: timeline line", (len(lines) - n_lines, lines[-1].endswith("Fixed a typo in the README.")), (1, True))
    ok &= check("PASSIVE outcome", pa.recent_triggers(db=db)[0]["outcome"], "logged")
    pa.submit_trigger({"source": "git", "summary": "refactor"}, triage=triage(0.5),
                      agent=lambda p: "Reviewed it; no user action is needed.", **common)
    ok &= check("unlabelled reply stays passive (no card)", len(pa.list_cards(db=db)), 1)
    ok &= check("tier parse", [pa._tier(x)[0] for x in ("NO_ACTION", "NOTIFY: a", "**PASSIVE**\nb", "plain")],
                ["none", "notify", "passive", "passive"])

    def broken(ev):
        raise RuntimeError("decider down")
    pa.submit_trigger({"source": "calendar", "summary": "meeting moved to 3pm"}, triage=broken,
                      agent=lambda p: "NOTIFY: Your 2pm moved to 3pm.", **common)
    ok &= check("triage outage still investigates", len(pa.list_cards(db=db)), 2)

    print("\n── git changes go to the code reviewer ──")
    got = {}
    pa.submit_trigger({"source": "git", "kind": "commit", "summary": "jellybean abc123: send_to_claude",
                       "detail": "diff --git a/DeviceExecutor.swift ..."}, triage=triage(0.3),
                      reviewer=lambda ev: got.setdefault("ev", ev) and
                      "NOTIFY\nsend_to_claude restores only text on the clipboard.", **common)
    ok &= check("reviewer saw the diff", "diff --git" in got.get("ev", {}).get("detail", ""), True)
    ok &= check("review NOTIFY becomes a card", any("send_to_claude restores" in c["detail"] for c in pa.list_cards(db=db)), True)
    ok &= check("prompt carries decisions + diff",
                all(k in pa._REVIEW_PROMPT for k in ("{decisions}", "{detail}", "NOTIFY")), True)

    print("\n── commits Claude helped write say so ──")
    n_lines = len(lines)
    pa.submit_trigger({"source": "git", "kind": "commit", "summary": "titan-node 1a2b3c4: capabilities tool",
                       "detail": "Zach Hale\ncapabilities tool\nbody\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
                                 "diff --git a/cap.py b/cap.py\n--- a/cap.py\n+++ b/cap.py\n@@ -1 +1 @@\n-old\n+capabilities\n"},
                      triage=triage(0.5),
                      reviewer=lambda ev: "PASSIVE\nAdds the capabilities tool.", **common)
    ok &= check("co-authored → '(with Claude)' after the subject",
                lines[-1].startswith("titan-node 1a2b3c4: capabilities tool (with Claude) — "), True)
    pa.submit_trigger({"source": "git", "kind": "commit", "summary": "titan-node 5d6e7f8: a README fix",
                       "detail": "Zach Hale\na README fix\n\ndiff --git a/README b/README\n--- a/README\n+++ b/README\n"
                                 "@@ -1 +1 @@\n-teh\n+the\n"}, triage=triage(0.5),
                      reviewer=lambda ev: "PASSIVE\nFixes a typo.", **common)
    ok &= check("Zach's own commit isn't", "(with Claude)" in lines[-1], False)
    ok &= check("… two dev lines", len(lines) - n_lines, 2)

    print("\n── same patch on branch and squash-merge: reviewed once ──")
    patch = "diff --git a/x.swift b/x.swift\nindex 1..2\n--- a/x.swift\n+++ b/x.swift\n@@ -1 +1 @@\n-old line\n+new line\n"
    a = pa.submit_trigger({"source": "git", "summary": "jellybean ac2dc59: feature", "detail": "Author\n" + patch,
                           "dedupe_key": "jellybean:ac2dc59"}, triage=triage(0.3), reviewer=lambda ev: "PASSIVE\nok", **common)
    b = pa.submit_trigger({"source": "git", "summary": "jellybean 6cb823a: feature (#17)", "detail": "Other header\n" + patch.replace("index 1..2", "index 9..8"),
                           "dedupe_key": "jellybean:6cb823a"}, triage=triage(0.3), reviewer=lambda ev: "PASSIVE\nok", **common)
    ok &= check("first accepted, squash duplicate skipped", (a["status"], b["status"]), ("accepted", "duplicate"))
    ok &= check("no diff → no fingerprint", pa._patch_fingerprint("just a message"), None)

    print("\n── dedupe and cap ──")
    r = pa.submit_trigger({"source": "calendar", "summary": "meeting moved to 3pm"}, triage=broken,
                          agent=lambda p: "x", **common)
    ok &= check("duplicate within 6h", r["status"], "duplicate")
    old = pa.MAX_TURNS_PER_HOUR
    try:
        pa.MAX_TURNS_PER_HOUR = 3
        r = pa.submit_trigger({"source": "git", "summary": "new commit"}, triage=triage(0.1),
                              agent=lambda p: "x", **common)
        ok &= check("hourly cap", r["status"], "rate_limited")
    finally:
        pa.MAX_TURNS_PER_HOUR = old

    print("\n── validation and acks ──")
    try:
        pa.submit_trigger({"source": "", "summary": "x"}, **common)
        ok &= check("missing source rejected", False, True)
    except ValueError:
        ok &= check("missing source rejected", True, True)
    cid = pa.list_cards(db=db)[0]["id"]
    ok &= check("ack", pa.ack_card(cid, db=db), True)
    ok &= check("acked card hidden", cid in [c["id"] for c in pa.list_cards(db=db)], False)
    ok &= check("acked card listed with include_acked",
                cid in [c["id"] for c in pa.list_cards(include_acked=True, db=db)], True)

    print("\n── urgent cards nudge the phone, content-free ──")
    import titan_alert
    sent, orig_alert = [], titan_alert.alert
    try:
        titan_alert.alert = lambda title, msg, **kw: sent.append((title, msg)) or []
        pa.add_card("PG&E bill overdue", "Account 1234 is overdue by $212.", urgency=3, db=db)
        pa.add_card("FYI", "minor", urgency=1, db=db)
    finally:
        titan_alert.alert = orig_alert
    ok &= check("one nudge for the urgent card only", len(sent), 1)
    ok &= check("nudge carries no card content", "PG&E" in str(sent) or "1234" in str(sent), False)

    print("\n── notify_user tool ──")
    import agent_runtime_lib as lib
    orig_db, orig_src = pa.DB_PATH, os.environ.get("AGENT_TURN_SOURCE")
    try:
        pa.DB_PATH = db
        os.environ["AGENT_TURN_SOURCE"] = "user"
        out = lib.execute_tool("notify_user", {"title": "Filter due", "detail": "Change the furnace filter.", "urgency": 2})
        ok &= check("tool result", out, "Card sent to you: Filter due")
        ok &= check("card from agent", any(c["source"] == "agent" for c in pa.list_cards(db=db)), True)
        os.environ["AGENT_TURN_SOURCE"] = "eval"
        ok &= check("eval turns show no card", lib.execute_tool("notify_user", {"title": "x"}).startswith("Test turn"), True)
        ok &= check("notify_user is not a background-safe tool", "notify_user" in lib._SAFE_STEP_TOOLS, False)
    finally:
        pa.DB_PATH = orig_db
        if orig_src is None:
            os.environ.pop("AGENT_TURN_SOURCE", None)
        else:
            os.environ["AGENT_TURN_SOURCE"] = orig_src

    print("\n── named checks (commands stay on the Mac) ──")
    ok &= check("catalog published", pa.set_catalog([{"name": "jellybean-build", "description": "swift build"},
                                                   {"name": "git-status"}, {"bogus": 1}], db=db), 2)
    r = pa.request_check("rm -rf ~", db=db)
    ok &= check("unknown name refused, lists available", (r.get("error") is not None, r.get("available")),
                (True, ["git-status", "jellybean-build"]))
    r1 = pa.request_check("jellybean-build", db=db)
    ok &= check("queued", r1["status"], "queued")
    ok &= check("repeat within 10 min reuses the job", pa.request_check("jellybean-build", db=db)["id"], r1["id"])
    jobs = pa.claim_jobs(db=db)
    ok &= check("claim returns the job by name only", jobs and jobs[0]["check"] == "jellybean-build" and set(jobs[0]) == {"id", "check", "requested_at"}, True)
    ok &= check("claimed once", pa.claim_jobs(db=db), [])
    ok &= check("result recorded", pa.post_result(r1["id"], False, 1, "x" * 9000 + "error: boom", 4200, db=db), True)
    ok &= check("result only once", pa.post_result(r1["id"], True, 0, "late", 1, db=db), False)
    j = pa.job(r1["id"], db=db)
    ok &= check("output capped to the tail", (len(j["output"]), j["output"].endswith("error: boom"), j["ok"]), (8000, True, 0))
    orig_db = pa.DB_PATH
    try:
        pa.DB_PATH = db
        out = lib.execute_tool("run_check", {"name": "git-status"})
        ok &= check("run_check tool queues", out.startswith("Check queued: job "), True)
        jid = out.split("job ")[1].split(".")[0]
        ok &= check("check_result while waiting", lib.execute_tool("check_result", {"job_id": jid}), "git-status: waiting for Jellybean to pick it up.")
        ok &= check("check_result after failure", lib.execute_tool("check_result", {"job_id": r1["id"]}).startswith("jellybean-build FAILED (exit 1) in 4s"), True)
        ok &= check("run_check allowed in background turns", "run_check" in lib._SAFE_STEP_TOOLS, True)
    finally:
        pa.DB_PATH = orig_db

    print("\n── card actions ──")
    card = pa.add_card("Build failed", "jellybean-build failed", db=db, actions=[
        {"label": "Re-run", "kind": "check", "value": "jellybean-build"}, {"label": "Looks fine", "kind": "reply"},
        {"label": "rm", "kind": "shell", "value": "rm -rf ~"}, {"label": "Dismiss", "kind": "dismiss"},
        {"label": "Extra", "kind": "dismiss"}])
    ok &= check("actions cleaned (unknown kind dropped, max 3)",
                [a["kind"] for a in card["actions"]], ["check", "reply", "dismiss"])
    listed = next(c for c in pa.list_cards(db=db) if c["id"] == card["id"])
    ok &= check("actions listed with the card", [a["label"] for a in listed["actions"]], ["Re-run", "Looks fine", "Dismiss"])
    r = pa.card_action(card["id"], "a0", db=db)
    ok &= check("check button queues the named check", r["job"].get("status") in ("queued", "reused"), True)
    ok &= check("card acked after the action", card["id"] in [c["id"] for c in pa.list_cards(db=db)], False)
    sent = []
    card2 = pa.add_card("Deploy?", "Ready to deploy.", db=db, actions=[{"label": "Yes", "kind": "reply"}])
    pa.card_action(card2["id"], "a0", db=db, trigger=lambda ev: sent.append(ev) or {"status": "accepted"})
    ok &= check("reply button tells the agent through the loop",
                (sent[0]["source"], "answered \"Yes\"" in sent[0]["summary"]), ("card-reply", True))
    ok &= check("unknown action refused", pa.card_action(card2["id"], "a9", db=db)["ok"], False)
    again = pa.card_action(card2["id"], "a0", db=db, trigger=lambda ev: sent.append(ev) or {"status": "accepted"})
    ok &= check("a repeat or late tap on an answered card does nothing more (ok, already)",
                (again["ok"], again.get("already"), len(sent)), (True, True, 1))
    rr = pa.card_action(card["id"], "a0", db=db)
    ok &= check("…and a check button on an answered card queues nothing", (rr.get("already"), "job" in rr), (True, False))
    pa.submit_trigger({"source": "checks", "summary": "jellybean-build failed (41 s)", "detail": "error: x"},
                      triage=triage(0.2, 2), agent=lambda p: "NOTIFY\nThe build broke in AppShell.swift.", **common)
    fail_card = next(c for c in pa.list_cards(db=db) if c["title"].startswith("jellybean-build failed"))
    ok &= check("failed-check card offers Re-run", fail_card["actions"][0], {"id": "a0", "label": "Re-run", "kind": "check", "value": "jellybean-build"})

    print("\n── host summary ──")
    hs = lib.host_summary()
    ok &= check("host summary shape", all(k in hs for k in ("ok", "problems", "disks", "gpus", "failed_units")), True)
    ok &= check("ok matches problems", hs["ok"], not hs["problems"])

    print("\n── merge commits aren't reviewed ──")
    for summ, want in (("jellybean 0e01d84: Merge remote-tracking branch 'origin/main' into notif-stack", "skipped"),
                       ("titan-node 1a2b3c4: Merge pull request #12 from x/y", "skipped"),
                       ("titan-node 1a2b3c4: Merge main (0.10.1) into dictation", "skipped"),
                       ("jellybean 9f9f9f9: Merged-cells support in the sheet reader", "accepted")):
        r = pa.submit_trigger({"source": "git", "kind": "commit", "summary": summ, "detail": "diff --git a b"},
                              triage=lambda ev: {"actionable": {"probabilities": {"no": 0.99}}}, db=db, background=False)
        ok &= check(f"merge filter: {summ[:40]}", r["status"], want)

    print("\n── triggers cut off by a restart ──")
    import sqlite3, time as _time
    rdb = Path(tempfile.mkdtemp()) / "proactive.db"
    old = sqlite3.connect(rdb)   # a store from before the detail column existed
    old.execute("""CREATE TABLE triggers (id TEXT PRIMARY KEY, source TEXT, kind TEXT, summary TEXT, dedupe_key TEXT,
                   received_at REAL, outcome TEXT, reason TEXT, card_id TEXT, finished_at REAL)""")
    old.execute("INSERT INTO triggers VALUES ('tr-legacy','git','commit','old running one','k0',?, 'running','',NULL,NULL)",
                (_time.time() - 60,))
    old.commit(); old.close()
    pa._db(rdb).close()   # migrates: adds the detail column
    con = sqlite3.connect(rdb)
    now = _time.time()
    rows = [("tr-fresh", "calendar", "event", "Dentist moved", "k1", now - 600, "running", "", "detail text"),
            ("tr-queued", "mail", "message", "Clinic replied", "k2", now - 300, "queued", None, "d"),
            ("tr-stale", "calendar", "event", "Ancient", "k3", now - 8 * 3600, "running", "", "d"),
            ("tr-again", "calendar", "event", "Retried once already", "k4", now - 900, "running",
             "retried after an agent restart", "d"),
            ("tr-done", "calendar", "event", "Finished", "k5", now - 900, "logged", "", "d")]
    con.executemany("INSERT INTO triggers (id, source, kind, summary, dedupe_key, received_at, outcome, reason, detail) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit(); con.close()
    seen = []
    r = pa.recover_interrupted(rdb, background=False, triage=lambda ev: {"actionable": {"probabilities": {"no": 0.1}}},
                               agent=lambda prompt: (seen.append(prompt), "PASSIVE: noted")[1],
                               timeline=lambda line: None)
    ok &= check("fresh running + queued retried; stale, already-retried and detail-less closed",
                r, {"retried": 2, "interrupted": 3})
    got = {t["id"]: t for t in pa.recent_triggers(20, db=rdb)}
    ok &= check("retried ones finish", (got["tr-fresh"]["outcome"], got["tr-queued"]["outcome"]), ("logged", "logged"))
    ok &= check("retry is marked", "retried" in (got["tr-fresh"]["reason"] or ""), True)
    ok &= check("closed ones say why", (got["tr-stale"]["outcome"], got["tr-again"]["outcome"], got["tr-legacy"]["outcome"]),
                ("interrupted",) * 3)
    ok &= check("finished work untouched", got["tr-done"]["outcome"], "logged")
    ok &= check("retry re-ran the stored event", any("Dentist moved" in p for p in seen), True)
    ok &= check("nothing left running", [t for t in got.values() if t["outcome"] in ("running", "queued")], [])
    ok &= check("new triggers store their detail", sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM triggers WHERE detail IS NOT NULL").fetchone()[0] > 0, True)

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
