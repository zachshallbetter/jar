#!/usr/bin/env python3
# gate: hermetic
"""Shell hygiene lint (titan-node#1520, "Lessons into guards"): scans every *.sh under scripts/ for the slips we keep repeating.
  1. `| grep -m1` / `| head -1` inside $(...) under pipefail (SIGPIPE fails the pipe on a match): capture first.
  2. `$(tail|cat FILE)` under set -e + pipefail with no `|| true` / existence check (a missing optional file ended a deploy).
  3. Pattern kills: pkill, killall, `kill $(pgrep -f ...)`, `xargs kill`.
  4. `rm -rf` with an unquoted glob, or on a path that starts with a bare variable (could be empty), and any rm of /ai/tmp globs.
  5. Printing process args/env: ps -ef / ps aux / pgrep -a|-l with -f / /proc/*/cmdline|environ / systemctl show -p Environment.
  6. A variable named `path` in scripts that may run under zsh (it is PATH there).
Justified lines go in scripts/shell_hygiene_allow.txt: `file | rule | text on the line | reason`. A stale entry fails too."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOW = ROOT / "shell_hygiene_allow.txt"

R1 = re.compile(r"(\$\(|`)[^\n]*\|\s*(grep\s+(-\w+\s+)*-\w*m\s*1\b|grep\s+--max-count[= ]1\b|head\s+-(n\s*)?1\b)")
R2 = re.compile(r"\$\(\s*(tail|cat)\s+[^|)]*\)")
R3 = re.compile(r"\b(pkill|killall)\b|\bkill\s+(-\w+\s+)*[^;&|\n]*\$\(\s*pgrep|\bxargs\s+(-\S+\s+)*kill\b")
R4_RM = re.compile(r"\brm\s+(-\w+\s+)*(-\w*[rR]\w*\s+)(-\w+\s+)*(?P<args>[^;&|\n]*)")
R5 = [
    re.compile(r"\bps\s+(-?\w*e\w*f\w*|aux\w*|-ef\w*)\b"),
    re.compile(r"\bpgrep\s+(-\w+\s+)*-\w*[al]\w*f|\bpgrep\s+(-\w+\s+)*-\w*f\w*[al]"),
    re.compile(r"/proc/[^\s/]*/(cmdline|environ)"),
    re.compile(r"systemctl\s+show\b[^\n]*-p\s*Environment"),
]
R6 = re.compile(r"(?<![\w$/.-])(local\s+|for\s+|read\s+(-\w+\s+)*|export\s+)?path=|\bfor\s+path\s+in\b|\blocal\s+path\b|\bread\s+(-\w+\s+)*path\b")


def code(line):
    """The line without a trailing comment (a # that starts a word, outside quotes, roughly)."""
    s = line.strip()
    if s.startswith("#"):
        return ""
    out, q = [], None
    for i, ch in enumerate(line):
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out)


def zsh_prone(text):
    head = text.splitlines()[:15]
    first = head[0] if head else ""
    if not first.startswith("#!"):
        return True
    if "zsh" in first:
        return True
    return any(re.match(r"#.*\b(source[ds]?|sourced by)\b", h) for h in head[1:])


def scan(rel, text):
    hits = []
    lines = text.splitlines()
    pipefail = bool(re.search(r"^\s*set\s+[^\n#]*pipefail", text, re.M))
    errexit = bool(re.search(r"^\s*set\s+(-\w*e\w*|[^\n#]*-e\b)", text, re.M))
    zsh = zsh_prone(text)
    for n, raw in enumerate(lines, 1):
        c = code(raw)
        if not c.strip():
            continue
        def hit(rule):
            hits.append((rel, rule, n, raw.strip()))
        if pipefail and R1.search(c):
            hit("pipefail-early-exit")
        if pipefail and errexit:
            for m in R2.finditer(c):
                if "||" not in c and not re.search(r"\[\[?\s+-[efsr]\s|\btest\s+-[efsr]\b", c):
                    hit("missing-file-substitution")
        if R3.search(c):
            hit("pattern-kill")
        for m in R4_RM.finditer(c):
            args = m.group("args")
            if "/ai/tmp/" in args and "*" in args:
                hit("rm-unsafe")
                continue
            for tok in args.split():
                if tok.startswith("-"):
                    continue
                bare_glob = re.search(r"(^|[^\"'\\])\*", re.sub(r"\"[^\"]*\"|'[^']*'", "", tok))
                if ":?" in tok:
                    continue   # ${VAR:?} aborts on an empty variable
                if bare_glob or re.match(r"^\"?\$\{?\w+\}?", tok):
                    hit("rm-unsafe")
                    break
        if any(p.search(c) for p in R5):
            hit("print-process-args")
        if zsh and R6.search(c):
            hit("zsh-path-variable")
    return hits


def load_allow():
    out = []
    if ALLOW.exists():
        for raw in ALLOW.read_text().splitlines():
            if raw.strip() and not raw.lstrip().startswith("#"):
                parts = [p.strip() for p in raw.split("|", 3)]
                out.append(parts)
    return out


def violations():
    allow = load_allow()
    used = set()
    bad = []
    for p in sorted(ROOT.rglob("*.sh")):
        rel = str(p.relative_to(ROOT))
        for h in scan(rel, p.read_text(errors="replace")):
            ok = False
            for i, a in enumerate(allow):
                if len(a) == 4 and a[3] and a[0] == h[0] and a[1] == h[1] and a[2] in h[3]:
                    used.add(i)
                    ok = True
            if not ok:
                bad.append(h)
    stale = [a for i, a in enumerate(allow) if i not in used]
    return bad, stale, allow


passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {name}\n   got  {got!r}\n   want {want!r}")


# The scanner itself: each rule fires on its slip and stays quiet on the safe form.
HDR = "#!/usr/bin/env bash\nset -euo pipefail\n"
def rules(body, hdr=HDR):
    return sorted({h[1] for h in scan("t.sh", hdr + body)})

check("r1 grep -m1 in $()", rules('x=$(foo | grep -m1 bar)\n'), ["pipefail-early-exit"])
check("r1 head -1 in $()", rules('x=$(foo | head -1)\n'), ["pipefail-early-exit"])
check("r1 capture first is fine", rules('o=$(foo); x=$(printf %s "$o" | grep bar | tail -1)\n'), [])
check("r1 needs pipefail", rules('x=$(foo | head -1)\n', "#!/bin/bash\n"), [])
check("r2 tail of a file", rules('x=$(tail -n 3 "$F")\n'), ["missing-file-substitution"])
check("r2 || true is fine", rules('x=$(tail -n 3 "$F" 2>/dev/null || true)\n'), [])
check("r2 existence check is fine", rules('[ -f "$F" ] && x=$(cat "$F")\n'), [])
check("r3 pkill", rules('pkill -f foo\n'), ["pattern-kill"])
check("r3 killall", rules('killall foo\n'), ["pattern-kill"])
check("r3 kill pgrep", rules('kill $(pgrep -f foo)\n'), ["pattern-kill"])
check("r3 kill a pid is fine", rules('kill "$pid"\n'), [])
check("r4 unquoted glob", rules('rm -rf /x/y/*\n'), ["rm-unsafe"])
check("r4 bare variable path", rules('rm -rf "$D"\n'), ["rm-unsafe"])
check("r4 variable prefix", rules('rm -rf "$D/sub"\n'), ["rm-unsafe"])
check("r4 /ai/tmp glob", rules('rm -f /ai/tmp/"x"*.json\n'), [])  # not -r: only -rf shapes are scanned
check("r4 /ai/tmp glob with -rf", rules('rm -rf /ai/tmp/*.x\n'), ["rm-unsafe"])
check("r4 :? guard is fine", rules('rm -rf "${D:?}/sub"\n'), [])
check("r4 literal prefix is fine", rules('rm -rf /ai/tmp/"$n"\n'), [])
check("r5 ps -ef", rules('ps -ef | grep x\n'), ["print-process-args"])
check("r5 ps aux", rules('ps aux\n'), ["print-process-args"])
check("r5 pgrep -af", rules('pgrep -af foo\n'), ["print-process-args"])
check("r5 pgrep -f alone is fine", rules('pgrep -f foo >/dev/null\n'), [])
check("r5 /proc environ", rules('cat /proc/$p/environ\n'), ["print-process-args"])
check("r5 systemctl Environment", rules('systemctl show -p Environment x\n'), ["print-process-args"])
check("r6 path in a no-shebang script", rules('path=/x\n', ""), ["zsh-path-variable"])
check("r6 path in a bash script is fine", rules('path=/x\n'), [])
check("r6 path in a sourced script", rules('path=/x\n', "#!/bin/bash\n# meant to be sourced\n"), ["zsh-path-variable"])
check("r6 other names are fine", rules('file_path=/x\nmypath=1\n', ""), [])
check("comments are ignored", rules('# pkill foo\n'), [])

bad, stale, allow = violations()
check("every *.sh is clean or allowlisted", [f"{r}:{n} [{rule}] {t}" for r, rule, n, t in bad], [])
check("no stale allowlist entries", [" | ".join(a) for a in stale], [])
check("every allowlist line has a reason", [a for a in allow if len(a) != 4 or not a[3]], [])
check("the scan sees the coord and clockshift dirs",
      all(any(str(p.relative_to(ROOT)).startswith(d) for p in ROOT.rglob("*.sh")) for d in ("coord/", "clockshift/")), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
