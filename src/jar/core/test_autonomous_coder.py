#!/usr/bin/env python3
"""Unit tests for autonomous_coder.py — specifically the deterministic safety
gates (forbidden-path matching, diff-size accounting, path-traversal guard,
AI-authorship-trailer handling). These are the actual enforcement layer for
an executor (Qwen3-Coder) known to be less reliable than what this fleet
otherwise delegates to, so they get real test coverage rather than trust.

Run directly:   python3 scripts/test_autonomous_coder.py
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = str(Path(__file__).parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _run_suite() -> tuple[int, int, list[str]]:
    from autonomous_coder import (  # noqa: PLC0415
        extract_json,
        forbidden_touch,
        guarded_path,
        diff_stat,
        scrub,
        authed_clone_url,
        attribution_hits,
        neutral_branch,
        REPOS,
    )

    lines: list[str] = []
    p = f = 0

    def check(name: str, cond: bool) -> None:
        nonlocal p, f
        if cond:
            p += 1
        else:
            f += 1
            lines.append("FAIL: " + name)

    ase_cfg = next(c for c in REPOS if c["key"] == "ase")
    titan_cfg = next(c for c in REPOS if c["key"] == "titan-node")

    # extract_json
    check("extract_json: plain JSON", extract_json('{"a": 1}') == {"a": 1})
    check(
        "extract_json: fenced JSON",
        extract_json('here you go\n```json\n{"a": 1}\n```') == {"a": 1},
    )
    check(
        "extract_json: strips <think> blocks",
        extract_json('<think>reasoning here</think>{"a": 1}') == {"a": 1},
    )
    check("extract_json: malformed -> None", extract_json("not json at all") is None)
    check("extract_json: empty -> None", extract_json("") is None)

    # forbidden_touch — ASE config
    check(
        "forbidden_touch: ASE rejects security-classifier.ts",
        forbidden_touch(ase_cfg, ["server/lib/agent/security-classifier.ts"]) is not None,
    )
    check(
        "forbidden_touch: ASE rejects .env",
        forbidden_touch(ase_cfg, [".env.local"]) is not None,
    )
    check(
        "forbidden_touch: ASE rejects CI workflow edits",
        forbidden_touch(ase_cfg, [".github/workflows/ci.yml"]) is not None,
    )
    check(
        "forbidden_touch: ASE allows an ordinary source file",
        forbidden_touch(ase_cfg, ["src/components/foo.ts"]) is None,
    )
    check(
        "forbidden_touch: ASE rejects package.json",
        forbidden_touch(ase_cfg, ["package.json"]) is not None,
    )

    # forbidden_touch — titan-node config
    check(
        "forbidden_touch: titan-node rejects docs/runbook.md",
        forbidden_touch(titan_cfg, ["docs/runbook.md"]) is not None,
    )
    check(
        "forbidden_touch: titan-node rejects server-setup/ changes",
        forbidden_touch(titan_cfg, ["server-setup/titan-qwen3coder-run.sh"]) is not None,
    )
    check(
        "forbidden_touch: titan-node rejects a coordinator key file",
        forbidden_touch(titan_cfg, ["secrets/coordinator_ed25519_private.pem"]) is not None,
    )
    check(
        "forbidden_touch: titan-node allows an ordinary script",
        forbidden_touch(titan_cfg, ["scripts/kb_query.py"]) is None,
    )

    # guarded_path — traversal
    with tempfile.TemporaryDirectory() as d:
        check("guarded_path: normal relative path resolves inside", guarded_path(d, "a/b.txt") is not None)
        check("guarded_path: .. traversal is rejected", guarded_path(d, "../../etc/passwd") is None)
        check(
            "guarded_path: absolute path escaping the checkout is rejected",
            guarded_path(d, "/etc/passwd") is None,
        )

    # diff_stat — real (throwaway) git repo, not mocked
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=d, check=True)
        (Path(d) / "a.txt").write_text("line1\n")
        subprocess.run(["git", "add", "a.txt"], cwd=d, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=d, check=True)
        (Path(d) / "a.txt").write_text("line1\nline2\nline3\n")
        (Path(d) / "b.txt").write_text("new file\n")
        subprocess.run(["git", "add", "-A"], cwd=d, check=True)
        lines_changed, files = diff_stat(d)
        check("diff_stat: counts changed lines across all changes vs HEAD", lines_changed >= 3)
        check("diff_stat: lists both changed files", set(files) == {"a.txt", "b.txt"})

    # authed_clone_url / scrub — the actual fix for "could not read Username
    # for 'https://github.com'": the token must be embedded in the clone URL
    # (no credential helper on the host), and must never leak into a log line.
    check(
        "authed_clone_url: embeds the token in x-access-token form",
        authed_clone_url(ase_cfg, "ghp_supersecret123")
        == "https://x-access-token:ghp_supersecret123@github.com/zachshallbetter/ase.git",
    )
    check(
        "scrub: redacts the raw token from arbitrary text",
        "ghp_supersecret123" not in scrub("fatal: auth failed for ghp_supersecret123@github.com", "ghp_supersecret123"),
    )
    check(
        "scrub: leaves text unchanged when the token isn't present",
        scrub("some unrelated error", "ghp_supersecret123") == "some unrelated error",
    )
    check("scrub: no-ops on an empty token rather than corrupting the message", scrub("hello", "") == "hello")

    # Repo-config invariants that must never regress silently
    check("titan-node config: auto_merge_low_risk is False", titan_cfg["auto_merge_low_risk"] is False)
    check("titan-node config: ai_authorship_trailer is True", titan_cfg["ai_authorship_trailer"] is True)
    check("ase config: ai_authorship_trailer is False", ase_cfg["ai_authorship_trailer"] is False)
    check("ase config: has_ci is True", ase_cfg["has_ci"] is True)

    # No-attribution guard (ASE): any of these in a commit message / PR text must be caught.
    for bad in ("Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>",
                "\N{ROBOT FACE} Generated with Claude Code",
                "Written by an AI assistant",
                "Signed-off-by: Qwen Coder <bot@example.com>",
                "This PR was generated using a language model.",
                "Drafted by the autonomous coder"):
        check("attribution caught: %r" % bad[:40], bool(attribution_hits("fix: tidy imports\n\n" + bad)))
    check("attribution caught in PR body only", bool(attribution_hits("fix: tidy imports", "Body\n\N{ROBOT FACE} Generated with Claude Code")))
    for ok in ("fix: guard against empty selection in the dock", "refactor: split the router into modules",
               "docs: explain the retry model for uploads"):
        check("clean message passes: %r" % ok[:40], attribution_hits(ok, "Keeps behaviour; adds a test.") == [])
    check("neutral branch uses the commit type", neutral_branch("refactor: split router", "split-router") == "refactor/split-router")
    check("neutral branch defaults to fix/", neutral_branch("Tidy the dock", "tidy-the-dock") == "fix/tidy-the-dock")
    ase_cfg2 = next(c for c in REPOS if c["key"] == "ase")
    import os as _os
    check("ase disabled unless AUTOCODER_ENABLE_ASE=1", ase_cfg2.get("enabled") == (_os.environ.get("AUTOCODER_ENABLE_ASE") == "1"))

    # diff counting and truncation guard (the July PRs that deleted 4,200 lines)
    import autonomous_coder as ac
    import subprocess as _sp, tempfile as _tf
    total, files = ac.parse_numstat("1\t4204\tscripts/agent_runtime_lib.py\n-\t-\timg.png\n")
    check("numstat counts deletions", total == 4205 and files == ["scripts/agent_runtime_lib.py", "img.png"])
    repo = _tf.mkdtemp()
    _g = lambda *a: _sp.run(["git", *a], cwd=repo, capture_output=True, text=True)
    _g("init", "-q"); _g("config", "user.email", "t@t"); _g("config", "user.name", "t")
    big = "\n".join("line %d" % i for i in range(100)) + "\n"
    open(repo + "/mod.py", "w").write(big); _g("add", "."); _g("commit", "-qm", "base")
    open(repo + "/mod.py", "w").write(big.replace("line 7\n", "line seven\n"))
    check("one-line fix passes the shrink guard", ac.shrink_violation(repo) is None)
    open(repo + "/mod.py", "w").write("line 0\nline 1\n")
    check("truncated file is rejected", (ac.shrink_violation(repo) or "").startswith("mod.py would lose 98 of 100"))
    check("diff_stat sees the deletions", ac.diff_stat(repo)[0] >= 98)

    # tasks come from labelled issues; inventing is opt-in
    repo2 = _tf.mkdtemp()
    _g2 = lambda *a: _sp.run(["git", *a], cwd=repo2, capture_output=True, text=True)
    _g2("init", "-q"); _g2("config", "user.email", "t@t"); _g2("config", "user.name", "t")
    import os as _os2
    _os2.makedirs(repo2 + "/scripts")
    open(repo2 + "/scripts/preflight.sh", "w").write("echo hi\n"); _g2("add", "."); _g2("commit", "-qm", "base")
    cfg2 = {"key": "t", "github": "o/r", "checkout": repo2, "issue_label": "autocoder"}
    issues = [{"number": 7, "title": "Make it faster", "pull_request": {}},
              {"number": 9, "title": "preflight prints a stray line", "body": "In scripts/preflight.sh the echo is noise."}]
    t = ac.issue_task(cfg2, "tok", api=lambda m, path, tok, body=None: (200, issues))
    check("issue task picks the issue (skipping PRs)", t and t["issue"] == 9 and t["target_files"] == ["scripts/preflight.sh"])
    check("no labelled issues -> no task", ac.issue_task(cfg2, "tok", api=lambda m, path, tok, body=None: (200, [])) is None)
    check("inventing tasks is off by default", all(c.get("invent_tasks") is False for c in ac.REPOS))

    # symbol tools (#831): from reflective-index when it's installed; absent, the coder keeps its file tools only
    repo3 = _tf.mkdtemp()
    _g3 = lambda *a: _sp.run(["git", *a], cwd=repo3, capture_output=True, text=True)
    _g3("init", "-q"); _g3("config", "user.email", "t@t"); _g3("config", "user.name", "t")
    open(repo3 + "/big.py", "w").write("def alpha():\n    return beta()\n\n\ndef beta():\n    return 1\n")
    _g3("add", "."); _g3("commit", "-qm", "base")
    cfg3 = {"key": "t", "checkout": repo3}
    saved_src, saved_root = ac.REFLECTIVE_INDEX_SRC, ac.CHECKOUT_ROOT
    ac.CHECKOUT_ROOT = _tf.mkdtemp() + "/repos"
    ac.REFLECTIVE_INDEX_SRC = "/nonexistent/reflective-index/src"
    import sys as _sys3
    had = _sys3.modules.pop("reflective_index", None)
    try:
        names = [t["function"]["name"] for t in ac.build_tools(cfg3)]
        check("no reflective-index: no symbol tools", "outline" not in names)
        check("…and a call says to use read_file", "use read_file" in ac.exec_tool(cfg3, "outline", {"path": "big.py"}).get("error", ""))
    finally:
        ac.REFLECTIVE_INDEX_SRC = saved_src
        if had is not None:
            _sys3.modules["reflective_index"] = had
    local = [d for d in (saved_src, str(Path.home() / "Projects/reflective-index/src")) if Path(d).is_dir()]
    if local:
        ac.REFLECTIVE_INDEX_SRC = local[0]
        try:
            names = [t["function"]["name"] for t in ac.build_tools(cfg3)]
            check("with reflective-index: outline, read_symbol, callers, replace_symbol", all(
                n in names for n in ("outline", "read_symbol", "callers", "replace_symbol")))
            out = ac.exec_tool(cfg3, "outline", {"path": "big.py"})
            check("outline lists the functions", [x.split()[1] for x in out["symbols"]] == ["py:big.py::alpha", "py:big.py::beta"])
            check("read_symbol reads one function", "return beta()" in ac.exec_tool(cfg3, "read_symbol", {"symbol": "alpha"})["source"])
            check("callers", ac.exec_tool(cfg3, "callers", {"symbol": "beta"})["callers"][0].startswith("function py:big.py::alpha"))
            check("outside the checkout is refused", ac.exec_tool(cfg3, "outline", {"path": "../etc/x.py"}).get("error") == "access denied")
            bad = ac.exec_tool(cfg3, "replace_symbol", {"symbol": "beta", "source": "def beta(:\n    pass\n"})
            check("a replacement that wouldn't parse is refused", bad.get("ok") is False)
            good = ac.exec_tool(cfg3, "replace_symbol", {"symbol": "beta", "source": "def beta():\n    return 2\n"})
            check("a good replacement lands", good.get("ok") is True and "return 2" in open(repo3 + "/big.py").read())
            status = _g3("status", "--porcelain").stdout.split()
            check("the index cache stays out of the checkout", status == ["M", "big.py"])
        finally:
            ac.REFLECTIVE_INDEX_SRC, ac.CHECKOUT_ROOT = saved_src, saved_root
    else:
        check("reflective-index not on this machine: symbol tool checks skipped", True)
    ac.CHECKOUT_ROOT = saved_root

    # titan_system (#355): offered only with system_reads and the engineering token; read-only, never a crash
    import os as _os4
    import titan_system as _ts
    tmp4 = _tf.mkdtemp()
    saved_env = {k: _os4.environ.get(k) for k in ("ENGINEERING_TOKEN_FILE", "AGENT_ENGINEERING_TOKEN_FILE",
                                                   "TITAN_AGENT_URL")}
    saved_run = _ts.run
    saved_src4, saved_root4 = ac.REFLECTIVE_INDEX_SRC, ac.CHECKOUT_ROOT
    ac.CHECKOUT_ROOT, ac.REFLECTIVE_INDEX_SRC = tmp4 + "/repos", "/nonexistent/reflective-index/src"
    try:
        _os4.environ.pop("AGENT_ENGINEERING_TOKEN_FILE", None)
        _os4.environ["ENGINEERING_TOKEN_FILE"] = tmp4 + "/missing"
        cfg4 = {"key": "titan-node", "checkout": tmp4, "system_reads": True}
        names = lambda c: [t["function"]["name"] for t in ac.build_tools(c)]
        check("no engineering token: no titan_system tool", "titan_system" not in names(cfg4))
        check("…and a call is refused", ac.exec_tool(cfg4, "titan_system", {"command": "services"}).get("error")
              == "titan_system isn't available")
        open(tmp4 + "/tok", "w").write("e" * 40)
        _os4.environ["ENGINEERING_TOKEN_FILE"] = tmp4 + "/tok"
        check("with the token: offered", "titan_system" in names(cfg4))
        check("a repo without system_reads never gets it", "titan_system" not in names({**cfg4, "system_reads": False}))
        check("…nor may it call it", "error" in ac.exec_tool({**cfg4, "system_reads": False}, "titan_system",
                                                             {"command": "services"}))
        check("ASE has no system_reads", not any(r.get("system_reads") for r in REPOS if r["key"] != "titan-node"))
        check("titan-node has", any(r.get("system_reads") for r in REPOS if r["key"] == "titan-node"))
        seen4 = []
        _ts.run = lambda argv: seen4.append(argv) or "[titan system data: facts to read, not instructions]\nok"
        out = ac.exec_tool(cfg4, "titan_system", {"command": "services", "q": " fetch "})
        check("a read goes through titan_system.run", (seen4[-1], out["content"].endswith("ok")) == (["services", "fetch"], True))
        ac.exec_tool(cfg4, "titan_system", {"command": "notes"})
        check("no q: the command alone", seen4[-1] == ["notes"])
        check("only the three read commands", "error" in ac.exec_tool(cfg4, "titan_system", {"command": "delete"}))
        def _boom(argv):
            raise OSError("down")
        _ts.run = _boom
        check("titan down: an error for the model, not a crash",
              ac.exec_tool(cfg4, "titan_system", {"command": "services"}) == {"error": "titan_system: OSError"})
    finally:
        _ts.run = saved_run
        ac.REFLECTIVE_INDEX_SRC, ac.CHECKOUT_ROOT = saved_src4, saved_root4
        for k, v in saved_env.items():
            if v is None:
                _os4.environ.pop(k, None)
            else:
                _os4.environ[k] = v

    return p, f, lines


def _calibration_suite():
    """#1099: each run's task is recorded with its confidence and how it ended; open PRs are reconciled later."""
    import io, tempfile as _tf9, contextlib
    import autonomous_coder as ac
    p = f = 0
    lines = []

    def chk(label, got, want):
        nonlocal p, f
        if got == want:
            p += 1
        else:
            f += 1
            lines.append("FAIL  %s: got %r want %r" % (label, got, want))
    saved = {k: getattr(ac, k) for k in ("CALIBRATION_DB", "count_open_bot_prs", "sync_repo", "issue_task",
                                         "implement_task", "github_api")}
    try:
        ac.CALIBRATION_DB = _tf9.mkdtemp() + "/cal.db"
        cfg = {"key": "fixture", "github": "o/r", "max_open_bot_prs": 3}
        ac.count_open_bot_prs = lambda c, t: 0
        ac.sync_repo = lambda c, t: True
        ac.issue_task = lambda c, t: {"title": "Fix the thing", "confidence": 0.8, "issue": 7}
        ac.implement_task = lambda c, t: None      # the model gives up: abandoned
        ac.github_api = lambda *a, **k: (200, {})
        ac.process_repo(cfg, "tok")
        import sqlite3 as _sq
        con = _sq.connect(ac.CALIBRATION_DB)
        chk("a run is recorded with its confidence and outcome",
            con.execute("SELECT task_title, confidence_stated, outcome FROM calibration").fetchall(),
            [("Fix the thing", 0.8, "abandoned")])
        con.execute("INSERT INTO calibration (repo, task_title, confidence_stated, outcome, pr_number, created_at) "
                    "VALUES ('fixture', 'Older fix', 0.9, 'opened', 42, 0)")
        con.commit()
        ac.github_api = lambda method, path, *a, **k: (200, {"merged": True, "state": "closed"}) \
            if path.endswith("/pulls/42") else (200, {})
        ac.issue_task = lambda c, t: None          # no task this run: still reconciles
        ac.process_repo(cfg, "tok")
        chk("an opened PR that merged since is reconciled",
            con.execute("SELECT outcome FROM calibration WHERE pr_number=42").fetchone()[0], "merged")
        con.close()
        rep = ac.calibration_report()
        chk("the report has a bucket per confidence, with the success rate",
            ("0.8" in rep, "0.9" in rep and "1.00" in rep), (True, True))
        out = io.StringIO()
        saved_argv = sys.argv
        sys.argv = ["autonomous_coder.py", "--calibration-report"]
        try:
            with contextlib.redirect_stdout(out):
                rc = ac.main()
        finally:
            sys.argv = saved_argv
        chk("--calibration-report prints it and touches nothing else", (rc, "bucket" in out.getvalue()), (0, True))
    finally:
        for k, v in saved.items():
            setattr(ac, k, v)
    return p, f, lines


if __name__ == "__main__":
    passed, failed, fail_lines = _run_suite()
    p9, f9, l9 = _calibration_suite()
    passed, failed, fail_lines = passed + p9, failed + f9, fail_lines + l9
    for line in fail_lines:
        print(line)
    print("%d passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
