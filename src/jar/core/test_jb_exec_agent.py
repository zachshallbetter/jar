#!/usr/bin/env python3
# gate: hermetic
"""The executor agent for another machine (#1631 slice 4c, jb_exec_agent.py): its config must be its owner's only and
name an executor token; it answers machine_status only, with names that carry no path (no username) and no arguments,
and refuses anything else without running it; it heartbeats and long-polls at most once a second and backs off on
failure. No network: titan and ps are fakes. Run: python3 scripts/test_jb_exec_agent.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jb_exec_agent as A  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


_TMP = Path(tempfile.mkdtemp(prefix="jb-exec-agent-"))
print("── its config ──")
cfgp = _TMP / "agent.json"
cfgp.write_text(json.dumps({"titan": "http://titan:7770", "token": "x." + "a" * 43, "name": "Garage box"}))
os.chmod(cfgp, 0o644)
try:
    A.load_config(cfgp)
    check("readable by others: refused", "loaded", "refused")
except A.ConfigError as exc:
    check("readable by others: refused, saying chmod 600", "chmod 600" in str(exc), True)
os.chmod(cfgp, 0o600)
check("its owner's only: loaded", A.load_config(cfgp), {"titan": "http://titan:7770", "token": "x." + "a" * 43, "name": "Garage box"})
for label, body, why in (("an ordinary device token", {"titan": "http://titan:7770", "token": "a" * 43}, "starts with x."),
                         ("not an address", {"titan": "titan; rm -rf /", "token": "x." + "a" * 43}, "server's address")):
    p = _TMP / f"{label[:5]}.json"
    p.write_text(json.dumps(body))
    os.chmod(p, 0o600)
    try:
        A.load_config(p)
        check(f"{label}: refused", "loaded", "refused")
    except A.ConfigError as exc:
        check(f"{label}: refused, saying why", why in str(exc), True)

print("── what it answers ──")
PS = ("  51.0 /Users/alex/Applications/Harbor.app/Contents/MacOS/Harbor\n"
      "   3.5 /usr/libexec/syslogd\n"
      "  88.2 python3\n"
      "   0.1 kernel_task\n"
      "  12.0 /home/robin/bin/ignore-your-rules;rm\n"
      "   7.0 Google Chrome Helper\n"
      "   bad line\n")
CALLS = []


def fake_run(argv):
    CALLS.append(argv)
    return PS if argv[0] == "/bin/ps" else ""


top = A.busiest(fake_run)
check("the five busiest by CPU, by bare name: no path, so no username", [n for n, _ in top],
      ["python3", "Harbor", "ignore-your-rulesrm", "Google Chrome Helper", "syslogd"])
check("…nothing of /Users, /home or a user's name survives", any(x in " ".join(n for n, _ in top) for x in ("/", "alex", "robin")),
      False)
check("…ps is asked for CPU and the command name only (comm), never its arguments", CALLS[-1], ["/bin/ps", "-Ao", "%cpu=,comm="])
proc = _TMP / "uptime"
proc.write_text("273600.5 1000.0\n")
mem = _TMP / "meminfo"
mem.write_text("MemTotal:       16384000 kB\nMemFree:  100 kB\nMemAvailable:    8192000 kB\n")
st = A.status(fake_run, mounts=("/",), proc=proc, meminfo=mem)
check("the status: host, uptime, load, memory, disk, the busiest", (st.startswith("host "), "up 3 d 4 h" in st, "load " in st,
      "memory 8000 MB free of 16000 MB" in st, "disk /:" in st, st.splitlines()[-1].startswith("busiest: python3 88%")),
      (True, True, True, True, True, True))
check("…bounded: a few lines", len(st.splitlines()) <= 8, True)
n = len(CALLS)
check("anything but machine_status: refused, and nothing run",
      (A.answer({"tool": "run_shell", "args": {"command": "rm -rf /"}}, fake_run), len(CALLS) - n),
      ({"status": "refused", "result": "Not an operation this machine has."}, 0))
check("machine_status: done, with the status", A.answer({"tool": "machine_status"}, fake_run)["status"], "done")
check("a name is cleaned like a process name", (A.clean_name("/Users/alex/x/Tool"), A.clean_name("a<b>c\nd")), ("Tool", "abcd"))

print("── checking one app (#1638) ──")
PS2 = ("  10.0  204800  02-03:04:05 /Applications/Harbor.app/Contents/MacOS/Harbor\n"
       "   2.0  102400     05:10 /Applications/Harbor.app/Contents/MacOS/Harbor\n"
       "   7.0   51200  01:00:00 /Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Helper\n"
       "   1.0   51200     00:30 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n"
       "   bad\n")
CALLS2 = []


def ps2(argv):
    CALLS2.append(argv)
    return PS2


apps = _TMP / "Applications"
(apps / "Harbor.app" / "Contents").mkdir(parents=True)
import plistlib  # noqa: E402
(apps / "Harbor.app" / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "4.2.1"}))
reports = _TMP / "DiagnosticReports"
reports.mkdir()
NOW = 2_000_000_000.0
for fn, age in (("Harbor-2026-10-14-101010.ips", 1), ("Harbor_2026-10-01.crash", 14), ("HarborHelper-2026.ips", 1),
                ("Other-2026.ips", 1)):
    (reports / fn).write_text("never read")
    os.utime(reports / fn, (NOW - age * 86400, NOW - age * 86400))
NODPKG, NOCRASH = _TMP / "no-dpkg-status", _TMP / "no-crash"   # never the host's own files (titan has both)
got = A.app_check("harbor", ps2, apps=apps, reports=reports, now=NOW, dpkg=NODPKG, crash=NOCRASH)
check("Harbor (any case): its two processes, CPU and memory summed, the longest run",
      got.splitlines()[:2], ["harbor: running, 2 processes", "CPU 12%, memory 300 MB, up 2 d 3 h"])
got = A.app_check("Harbor", ps2, apps=apps, reports=reports, now=NOW, dpkg=NODPKG, crash=NOCRASH)
check("…as named: the version, and only this week's crash reports of Harbor itself (not HarborHelper, not older)",
      got.splitlines()[2:], ["version 4.2.1", "1 crash report in the last 7 days"])
check("ps is asked CPU, memory, elapsed time and the bare command, never arguments; the name never on a command line",
      (CALLS2[-1], any("Harbor" in a for a in CALLS2[-1])), (["/bin/ps", "-Ao", "%cpu=,rss=,etime=,comm="], False))
got = A.app_check("Chrome", ps2, apps=apps, reports=reports, now=NOW, dpkg=NODPKG, crash=NOCRASH)
check("no exact name: processes whose name contains it, named", got.splitlines()[0],
      "Chrome: running, 2 processes (Google Chrome, Google Chrome Helper)")
got = A.app_check("Lantern", ps2, apps=apps, reports=reports, now=NOW, dpkg=NODPKG, crash=NOCRASH)
check("not running: said, with its crash count", got.splitlines(), ["Lantern: not running", "0 crash reports in the last 7 days"])
check("a name that's a path or carries odd characters is cleaned first: no path is ever built from it",
      (A.app_check("../../etc/passwd", ps2, apps=apps, reports=reports, now=NOW, dpkg=NODPKG, crash=NOCRASH).splitlines()[0],
       A.app_version("..", apps), A.app_check("", ps2)), ("passwd: not running", None, "Which app? Send its name."))
check("no crash folder here: no crash line", A.app_check("Harbor", ps2, apps=apps, reports=_TMP / "none", now=NOW, dpkg=NODPKG, crash=NOCRASH)
      .splitlines()[-1], "version 4.2.1")
print("── on a Linux machine ──")
dpkg = _TMP / "dpkg-status"
dpkg.write_text("Package: lantern\nStatus: install ok installed\nVersion: 2.7.1-1ubuntu2\n\n"
                "Package: old-tool\nStatus: deinstall ok config-files\nVersion: 1.0\n\n"
                "Package: harbor-site\nStatus: install ok installed\nVersion: 0.9\n")
crash = _TMP / "crash"
crash.mkdir()
for fn, age in (("_usr_bin_lantern.1000.crash", 1), ("_usr_bin_lantern.1000.crash.old", 1), ("_usr_lib_lanternd.0.crash", 1),
                ("_opt_lantern_lantern.1000.crash", 10)):
    (crash / fn).write_text("never read")
    os.utime(crash / fn, (NOW - age * 86400, NOW - age * 86400))
got = A.app_check("Lantern", ps2, apps=apps, reports=_TMP / "none", now=NOW, dpkg=dpkg, crash=crash)
check("Linux: its installed package's version, and this week's crash reports of that program only",
      got.splitlines(), ["Lantern: not running", "version 2.7.1-1ubuntu2", "1 crash report in the last 7 days"])
check("…a removed package has no version; a name with spaces is a package with hyphens",
      (A.package_version("old-tool", dpkg), A.package_version("Harbor site", dpkg)), (None, "0.9"))
check("…a Mac app's own version wins where there is one", A.app_check("Harbor", ps2, apps=apps, reports=reports, now=NOW,
                                                                     dpkg=dpkg, crash=crash).splitlines()[2], "version 4.2.1")
check("…no dpkg or crash folder (a Mac, or another Linux): nothing claimed",
      (A.package_version("lantern", _TMP / "none"), A.linux_crashes("lantern", _TMP / "none")), (None, None))
check("elapsed times parse", [A._etime_s(x) for x in ("05:10", "01:00:00", "02-03:04:05", "x")], [310, 3600, 183845, None])
check("answer: app_check with its args; a missing or odd args is a question back, never an error",
      (A.answer({"tool": "app_check", "args": {"app": "Harbor"}}, ps2)["result"].splitlines()[0],
       A.answer({"tool": "app_check", "args": "Harbor"}, ps2)), ("Harbor: running, 2 processes",
                                                                 {"status": "done", "result": "Which app? Send its name."}))

print("── a listed repository (#1636) ──")
import subprocess  # noqa: E402
repo = _TMP / "work" / "harbor-site"
repo.mkdir(parents=True)
ENV = {"PATH": "/usr/bin:/bin", "HOME": str(_TMP), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Robin Hale",
       "GIT_AUTHOR_EMAIL": "robin@example.com", "GIT_COMMITTER_NAME": "Robin Hale", "GIT_COMMITTER_EMAIL": "robin@example.com"}


def git(*a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, env=ENV)


git("init", "-q", "-b", "release.2")
(repo / "a.txt").write_text("one")
git("add", "a.txt")
git("commit", "-q", "-m", "First page")
(repo / "a.txt").write_text("two")
(repo / "new.txt").write_text("new")
marker = _TMP / "fsmonitor-ran"
git("config", "core.fsmonitor", f"touch {marker}")   # a repo's own config naming a program: never run by a read
REPOS = {"Harbor site": str(repo)}
got = A.repo_status("harbor SITE", REPOS)
check("a listed repository by name (any case): its branch, no upstream, what changed", got.splitlines()[:2],
      ["Harbor site: on release.2, no upstream", "1 changed file, 1 untracked"])
check("…which files changed and by how much (#1636), never their contents", (got.splitlines()[2:4], "two" in got),
      (["changes:", "  a.txt (+1 -1)"], False))
check("…its last commits", (got.splitlines()[4], got.splitlines()[5].endswith(" First page")), ("last commits:", True))
check("…its path never in the answer", str(repo) in got or "work" in got, False)
check("…and a program its own config names (core.fsmonitor) never ran", marker.exists(), False)
marker2 = _TMP / "extdiff-ran"
git("config", "diff.external", f"sh -c 'touch {marker2}'")
(repo / ".gitattributes").write_text("*.txt diff=peek\n")
git("config", "diff.peek.textconv", f"sh -c 'touch {marker2}; cat'")
got2 = A.repo_status("Harbor site", {"Harbor site": str(repo)})
# numstat runs neither even without the flags (mutation-checked 10-16); --no-ext-diff --no-textconv stay as a guard if a
# later git or a changed call starts to
check("…nor the diff programs it names (diff.external, a textconv)", (marker2.exists(), "a.txt (+1 -1)" in got2),
      (False, True))
(repo / ".gitattributes").unlink()
check("no name: the listed names, never their paths", A.repo_status("", REPOS), "Listed: Harbor site")
check("a name not listed (or a path): the names, nothing read", (A.repo_status(str(repo), REPOS), A.repo_status("x", {})),
      ("Not one listed here. Listed: Harbor site",
       "No repositories are listed on this machine (its owner adds them to the agent's config)."))
check("a listed path that isn't a repository: said", A.repo_status("Gone", {"Gone": str(_TMP / "nowhere")}),
      "Gone: couldn't read it (not a git repository here, or git isn't installed).")
check("answer: repo_status with the machine's own listed repos", A.answer({"tool": "repo_status", "args": {"repo": "Harbor site"}},
                                                                          repos=REPOS)["result"].split("\n")[0],
      "Harbor site: on release.2, no upstream")
check("the read wrote nothing: the index untouched by a lock", (repo / ".git" / "index.lock").exists(), False)
for label, raw, ok in (("a name: an absolute path", {"Site": "/srv/site"}, True), ("a relative path", {"Site": "site"}, False),
                       ("not an object", ["/srv/site"], False), ("too many", {f"r{i}": f"/r{i}" for i in range(21)}, False)):
    try:
        A.repos_of(raw)
        check(f"repos in the config, {label}", True, ok)
    except A.ConfigError:
        check(f"repos in the config, {label}", False, ok)

print("── talking to titan ──")
SENT, SLEPT = [], []
CLOCK = {"t": 0.0}
INBOX = [{"requests": [{"id": "r1", "tool": "machine_status"}, {"id": "r2", "tool": "read_file"},
                       {"id": "../../etc", "tool": "machine_status"}]},
         urllib.error.URLError("down"), {"requests": []}]


def fake_request(cfg, method, path, body=None, timeout=35):
    SENT.append((method, path, body["status"] if body and "status" in body else (body or {}).get("name")))
    if path.startswith("/v1/device/inbox?"):
        got = INBOX.pop(0)
        if isinstance(got, Exception):
            raise got
        return got
    return {}


def fake_sleep(s):
    SLEPT.append(round(s, 2))
    CLOCK["t"] += s


A.answer = (lambda real: (lambda item, run=fake_run, **kw: real(item, fake_run, **kw)))(A.answer)
A.run_forever({"titan": "http://titan:7770", "token": "x.t", "name": "Garage box"}, request=fake_request, sleep=fake_sleep,
              clock=lambda: CLOCK["t"], rounds=3)
check("presence once (then every 60 s), then the inbox", SENT[0], ("POST", "/v1/device/presence", "Garage box"))
check("machine_status answered done; another tool refused; an id that isn't one is never used in a path",
      [s for s in SENT if "/result" in s[1]], [("POST", "/v1/device/inbox/r1/result", "done"),
                                               ("POST", "/v1/device/inbox/r2/result", "refused")])
check("never more than one poll a second, and a failure backs off", (SLEPT[0] >= 1.0, 2.0 in SLEPT), (True, True))
check("the backoff grows and stops at a minute", [A.backoff(n) for n in (1, 2, 3, 10)], [2.0, 4.0, 8.0, 60.0])

SENT2, FAILS = [], {"left": 1}


def flaky_request(cfg, method, path, body=None, timeout=35):
    if path.startswith("/v1/device/inbox?"):
        SENT2.append(("GET",))
        return {"requests": [{"id": "a1", "tool": "machine_status"}, {"id": "a2", "tool": "machine_status"}]} \
            if len([x for x in SENT2 if x == ("GET",)]) == 1 else {"requests": []}
    if "/result" in path:
        if path.endswith("/a1/result") and FAILS["left"]:
            FAILS["left"] -= 1
            raise urllib.error.URLError("blip")
        SENT2.append(("POST", path.split("/")[-2]))
    return {}


A.run_forever({"titan": "http://titan:7770", "token": "x.t", "name": "Garage box"}, request=flaky_request, sleep=lambda s: None,
              clock=lambda: 0.0, rounds=3)
posts = [x[1] for x in SENT2 if x[0] == "POST"]
gets_before_a1 = SENT2.index(("POST", "a1")) if ("POST", "a1") in SENT2 else -1
check("a result that didn't go out is sent again, both of the batch, in order (review)", posts[:2], ["a1", "a2"])
check("…before titan is polled again", [x for x in SENT2[:gets_before_a1] if x == ("GET",)], [("GET",)])

print("── pairing this machine ──")
cfg2 = _TMP / "paired.json"
CLAIMS = []
out = A.pair(cfg2, "http://titan:7770", "482 913", "Garage box",
             post=lambda url, body: (CLAIMS.append((url, body)), {"token": "x." + "b" * 43})[1])
check("a code is claimed as platform other, under its name", CLAIMS, [("http://titan:7770/v1/pair/claim",
                                                                       {"code": "482913", "device_name": "Garage box", "platform": "other"})])
check("…and its config is written 0600, loadable", (oct(cfg2.stat().st_mode & 0o777), A.load_config(cfg2)["name"]),
      ("0o600", "Garage box"))
for label, args, why in (("an existing config", (cfg2, "http://titan:7770", "111111"), "already exists"),
                         ("not 6 digits", (_TMP / "n1.json", "http://titan:7770", "12ab"), "6 digits"),
                         ("not an address", (_TMP / "n2.json", "titan; rm", "111111"), "server's address")):
    try:
        A.pair(*args, post=lambda url, body: {"token": "x.t"})
        check(f"pair refuses {label}", "paired", "refused")
    except A.ConfigError as exc:
        check(f"pair refuses {label}, saying why", why in str(exc), True)
try:
    A.pair(_TMP / "n3.json", "http://titan:7770", "222222", post=lambda url, body: {"token": "a" * 43})
    check("an ordinary device's code: refused", "paired", "refused")
except A.ConfigError as exc:
    check("an ordinary device's code (no x. token): refused, nothing written", ("wasn't for a machine" in str(exc),
                                                                              (_TMP / "n3.json").exists()), (True, False))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
