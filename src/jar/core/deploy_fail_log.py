#!/usr/bin/env python3
"""A deploy gate test's failure, kept for whoever reads the rollback (10-05: "!!! test_place_regions.py failed" said
nothing about which check). deploy_titan_node.sh pipes the failing test's output here.
  kept      its last 60 lines, after the scrubbers: titan's live credentials and private keys
            (agent_runtime_lib.redact_credentials), personal codes and numbers (redact.redact_personal), and any
            NAME=value whose name says token, secret, password, key or auth. Never the environment.
  where     the path the deploy names (--out, under /ai/logs), created 0600; the deploy prints it.
  python3 scripts/deploy_fail_log.py --out /ai/logs/deploy-fail-<stamp>-<test>.log < output
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

KEEP = 60
_SECRETISH = re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|AUTH)[A-Z0-9_]*)\s*[=:]\s*\S+")


def scrub(text: str) -> str:
    import redact
    out = redact.redact_personal(text)
    try:
        import agent_runtime_lib as lib
        out = lib.redact_credentials(out)
    except Exception:  # noqa: BLE001 - the agent library didn't load: the other scrubbers still run
        pass
    return _SECRETISH.sub(lambda m: f"{m.group(1)}=[removed]", out)


def keep(text: str, out: Path) -> Path:
    lines = text.splitlines()[-KEEP:]
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(out), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(scrub("\n".join(lines)) + "\n")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    keep(sys.stdin.read(), Path(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
