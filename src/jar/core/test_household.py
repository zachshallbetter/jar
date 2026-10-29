"""Comprehensive household-agent test harness. Run on the host (reaches all runtimes).

LIVE: it writes to titan's real stores. Two `testhh` bills go straight into the record runtime, a KB
note goes in through ingest_text (a made-up garage code), and extract_bill files a sample bill with
Gemini. So it runs only with --live, on titan, one at a time (it shares the household eval's lock).
The KB and the record store are snapshotted first, then rolled back and verified after, as
test_household_agent.py does (#273). A run killed outright leaves its snapshot at
/ai/tmp/household-test-pending.json, and the next run refuses until a person checks it.

Run (on titan, from scripts/): python3 test_household.py --live
"""
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PENDING = "/ai/tmp/household-test-pending.json"
LOCK = "/tmp/household-eval.lock"   # the household eval's: one live writer at a time
TS = "testhh"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and detail:
        line += "  -- " + str(detail)[:200]
    print(line)


def _run(A) -> None:
    # ---------- record-runtime: CRUD + filter endpoint ----------
    try:
        A._runtime_request("record", "/v1/nodes", "POST", {"node_id": TS+"-b1", "node_type": TS+"bill",
            "data": {"vendor": "AlphaCo", "amount": 50, "due_date": "2026-07-20"}})
        A._runtime_request("record", "/v1/nodes", "POST", {"node_id": TS+"-b2", "node_type": TS+"bill",
            "data": {"vendor": "BetaCo", "amount": 10, "due_date": "2026-08-15"}})
        n = A._runtime_request("record", "/v1/nodes/"+TS+"-b1", "GET")
        check("record create+get stores data", (n.get("data") or {}).get("vendor") == "AlphaCo", n)
        r = A._runtime_request("record", "/v1/nodes?node_type="+TS+"bill", "GET")
        check("record list by type (=2)", r.get("count") == 2, r.get("count"))
        r = A._runtime_request("record", "/v1/nodes?node_type="+TS+"bill&field=due_date&lte=2026-07-31", "GET")
        check("record filter lte (=1)", r.get("count") == 1, r.get("count"))
        r = A._runtime_request("record", "/v1/nodes?node_type="+TS+"bill&field=due_date&gte=2026-08-01", "GET")
        check("record filter gte (=1)", r.get("count") == 1, r.get("count"))
        r = A._runtime_request("record", "/v1/nodes?node_type="+TS+"bill&field=vendor&eq=AlphaCo", "GET")
        check("record filter eq (=1)", r.get("count") == 1, r.get("count"))
        r = A._runtime_request("record", "/v1/nodes?node_type="+TS+"bill&limit=1", "GET")
        check("record limit honored", len(r.get("nodes") or []) == 1, len(r.get("nodes") or []))
        r = A._runtime_request("record", "/v1/nodes?node_type="+TS+"nope", "GET")
        check("record empty result (=0)", r.get("count") == 0, r.get("count"))
    except Exception as e:
        check("record-runtime block", False, e)

    # ---------- find_records tool ----------
    try:
        out = A.execute_tool("find_records", {"type": TS+"bill", "field": "due_date", "before": "2026-07-31"})
        check("find_records before-filter (AlphaCo only)", "AlphaCo" in out and "BetaCo" not in out, out)
        out = A.execute_tool("find_records", {"type": TS+"bill"})
        check("find_records all (both)", "AlphaCo" in out and "BetaCo" in out, out)
        out = A.execute_tool("find_records", {"type": TS+"nope"})
        check("find_records empty", "No matching" in out, out)
    except Exception as e:
        check("find_records block", False, e)

    # ---------- KB: ingest (param is `content`) + retrieve ----------
    try:
        ing = A.execute_tool("ingest_text", {"content": "The "+TS+" garage code is 7731.", "title": TS+"-note"})
        check("ingest_text ok", "error" not in ing.lower(), ing)
        time.sleep(2)
        ret = A.execute_tool("kb_retrieve", {"query": TS+" garage code"})
        check("kb_retrieve finds ingested", "7731" in ret or TS in ret, ret[:160])
    except Exception as e:
        check("kb block", False, e)

    # ---------- compute tools ----------
    try:
        check("count_chars (10 letters)", "10" in A.execute_tool("count_chars", {"text": "hello world"}), None)
        check("python_eval arithmetic", "1024" in A.execute_tool("python_eval", {"expression": "2**10"}), None)
    except Exception as e:
        check("compute tools block", False, e)

    # ---------- workflow-runtime: auth + lifecycle probe (Phase 4 prep) ----------
    try:
        d = A._runtime_request("workflow", "/v1/definitions", "GET")
        check("workflow auth: GET /v1/definitions (200)", isinstance(d, (dict, list)), d)
        print("    definitions -> " + json.dumps(d)[:200])
        i = A._runtime_request("workflow", "/v1/instances", "GET")
        check("workflow auth: GET /v1/instances (200)", isinstance(i, (dict, list)), i)
        print("    instances -> " + json.dumps(i)[:200])
    except Exception as e:
        check("workflow auth block", False, e)

    # ---------- extract_bill (Gemini; tolerate transient 429) ----------
    try:
        out = A.execute_tool("extract_bill", {"path": "/tmp/sample-bill.txt"})
        ok = ("Filed" in out or "ACME" in out) or ("429" in out or "rate" in out.lower())
        check("extract_bill (works or transient-429)", ok, out)
    except Exception as e:
        check("extract_bill block", False, e)


def main(argv: list[str]) -> int:
    if "--live" not in argv:
        # #400: it used to run on import with no guard and no cleanup, so a sweep of every test_*.py on
        # titan left testhh bills and a fake garage-code note in the household's real stores.
        print("Not run: this harness writes test bills, a KB note and a filed bill to titan's live "
              "stores. Pass --live (on titan) to run it; it rolls everything back after.")
        return 0
    if not (socket.gethostname().startswith("titan") or os.environ.get("TITAN_REMOTE_EXEC") == "1"):
        print("Not run: --live runs on titan itself (it reaches the runtimes on localhost).")
        return 1
    import fcntl
    import signal
    from test_household_agent import kb_hygiene   # snapshot / rollback / verify, on this host
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"Not run: a household eval or test is running ({LOCK}).")
        return 1

    def _stop(signum, _frame):
        raise SystemExit(f"stopped by signal {signum}")   # unwinds through the finally: the rollback runs
    for sig in (signal.SIGTERM, signal.SIGALRM, signal.SIGHUP):
        signal.signal(sig, _stop)
    # A fixed path: snapshot refuses while it exists, so a run killed outright blocks the next one.
    print("kb: " + kb_hygiene("snapshot", "--out", PENDING))
    try:
        import agent_runtime_lib as A
        _run(A)
    finally:
        for sig in (signal.SIGTERM, signal.SIGALRM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)   # the rollback itself is never cut short
        print("kb: " + kb_hygiene("rollback", "--snapshot", PENDING))
        # Nothing it wrote may remain; a leftover makes verify exit non-zero, which raises here.
        print("kb: " + kb_hygiene("verify", "--snapshot", PENDING, "--remove"))
    print("\n==== %d passed, %d failed ====" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
