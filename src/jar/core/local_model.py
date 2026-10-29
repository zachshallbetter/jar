#!/usr/bin/env python3
"""The local model as a manifest (#408 step 1): which llama.cpp build, which model file (and its
sha256), which flags and which GPUs titan-local-agent runs, written down and switchable.

A manifest is a small shell-sourceable file, /ai/config/local-model.d/<name>.env (values single-
quoted, runbook rule 3). `current` is a symlink to the live one; server-setup/titan-local-agent-run.sh
runs it when it exists and falls back to agent.env's AGENT_RUNTIME_CMD when it doesn't, so creating
the first manifest changes nothing. Every switch is a line in the ledger
(/ai/datasets/connectors/models.jsonl).

  python3 scripts/local_model.py init [--apply]   # today's AGENT_RUNTIME_CMD as the first manifest
  python3 scripts/local_model.py list | show [name] | cmd [name]
  python3 scripts/local_model.py check <name>     # static checks: files, sha256, a CUDA 12 build
  python3 scripts/local_model.py switch <name> --by <who> [--apply]   # flips current; no restart

Nothing here restarts the service or loads a model: switching takes effect on the next restart
(the owner's, or the eval-gated promotion in a later step of #408).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

MODEL_DIR = Path(os.environ.get("LOCAL_MODEL_DIR", "/ai/config/local-model.d"))
AGENT_ENV = Path(os.environ.get("LOCAL_AGENT_ENV", "/ai/projects/titan-local-agent/agent.env"))
LEDGER = Path(os.environ.get("LOCAL_MODEL_LEDGER", "/ai/datasets/connectors/models.jsonl"))
FIELDS = ("NAME", "LLAMA_SERVER", "MODEL_FILE", "MODEL_SHA256", "ARGS", "GPUS", "NOTE", "CREATED")


# ---------------------------------------------------------------------------
# Manifests


def parse_runtime_cmd(cmd: str) -> dict[str, str]:
    """AGENT_RUNTIME_CMD ("<llama-server> … --model <file> …") as LLAMA_SERVER, MODEL_FILE and ARGS
    (every other flag, in order). Raises ValueError when it has no binary or no --model."""
    words = shlex.split(cmd)
    if not words:
        raise ValueError("empty command")
    server, rest = words[0], words[1:]
    model, args, i = None, [], 0
    while i < len(rest):
        if rest[i] in ("--model", "-m") and i + 1 < len(rest):
            model = rest[i + 1]
            i += 2
            continue
        args.append(rest[i])
        i += 1
    if not model:
        raise ValueError("no --model in the command")
    return {"LLAMA_SERVER": server, "MODEL_FILE": model, "ARGS": shlex.join(args)}


def command(m: dict[str, str]) -> str:
    """The command line a manifest runs: the build's llama-server, --model first, then its flags."""
    return shlex.join([m["LLAMA_SERVER"], "--model", m["MODEL_FILE"]] + shlex.split(m.get("ARGS", "")))


def same_command(a: str, b: str) -> bool:
    """The same program, model and flags (the --model flag may sit anywhere)."""
    pa, pb = parse_runtime_cmd(a), parse_runtime_cmd(b)
    return pa["LLAMA_SERVER"] == pb["LLAMA_SERVER"] and pa["MODEL_FILE"] == pb["MODEL_FILE"] \
        and shlex.split(pa["ARGS"]) == shlex.split(pb["ARGS"])


def render(m: dict[str, str]) -> str:
    """A manifest file: one KEY='value' line per field (single-quoted; a quote inside is escaped)."""
    lines = ["# titan-local-agent model manifest (scripts/local_model.py, #408). Sourced by",
             "# server-setup/titan-local-agent-run.sh when local-model.d/current points here."]
    for k in FIELDS:
        v = str(m.get(k, "")).replace("'", "'\\''")
        lines.append(f"{k}='{v}'")
    return "\n".join(lines) + "\n"


def parse(text: str) -> dict[str, str]:
    """A manifest file back (the KEY='value' lines render writes)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k in FIELDS:
            out[k] = shlex.split(v)[0] if v.strip() else ""
    return out


def load(name: str, root: Path = MODEL_DIR) -> dict[str, str]:
    p = root / (name if name.endswith(".env") else name + ".env")
    if name == "current":
        p = root / "current"
    return parse(p.read_text())


def current_name(root: Path = MODEL_DIR) -> str | None:
    link = root / "current"
    if not link.is_symlink():
        return None
    return Path(os.readlink(link)).name.removesuffix(".env")


def names(root: Path = MODEL_DIR) -> list[str]:
    return sorted(p.stem for p in root.glob("*.env")) if root.is_dir() else []


def default_name(m: dict[str, str]) -> str:
    """qwen3-8b-q4_k_m-b11259-32k: the model file's stem, the build's tag, the context size."""
    stem = Path(m["MODEL_FILE"]).stem.lower()
    build = Path(m["LLAMA_SERVER"]).parent.name.removeprefix("llama.cpp-")
    args = shlex.split(m.get("ARGS", ""))
    ctx = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("--ctx-size", "-c")), "")
    tail = f"-{int(ctx) // 1024}k" if ctx.isdigit() else ""
    return f"{stem}-{build}{tail}"


def sha256(path: str, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def env_value(text: str, key: str) -> str | None:
    """A KEY=… value from agent.env (quoted or not), without sourcing it (it holds secrets)."""
    for line in text.splitlines():
        if line.startswith(key + "="):
            v = line.split("=", 1)[1].strip()
            return shlex.split(v)[0] if v else ""
    return None


# ---------------------------------------------------------------------------
# Checks (static: no model is loaded)


def check(m: dict[str, str], *, hash_file: bool = True, ldd=None) -> list[str]:
    """What's wrong with a manifest, [] when nothing: the build and the model exist, the model is the
    file the manifest names (sha256), and the build links CUDA 12 (titan's TITAN X are sm_52; 13
    dropped them)."""
    problems = []
    server, model = m.get("LLAMA_SERVER", ""), m.get("MODEL_FILE", "")
    if not (server and os.access(server, os.X_OK)):
        problems.append(f"llama-server not found or not executable: {server or '(none)'}")
    if not (model and os.path.isfile(model)):
        problems.append(f"model file not found: {model or '(none)'}")
    elif hash_file:
        if not m.get("MODEL_SHA256"):
            problems.append("no MODEL_SHA256 in the manifest")
        elif sha256(model) != m["MODEL_SHA256"]:
            problems.append("model file's sha256 differs from the manifest")
    if server and os.access(server, os.X_OK):
        out = (ldd or _ldd)(server)
        if "libcudart.so.13" in out or "libcublas.so.13" in out:
            problems.append("the build links CUDA 13, which has no sm_52 (TITAN X): use a CUDA 12 build")
        elif "libcudart.so.12" not in out and "libggml-cuda" not in out:
            problems.append("the build doesn't link CUDA (libcudart.so.12 / libggml-cuda): it would run on the CPU")
    if not m.get("GPUS"):
        problems.append("no GPUS (CUDA_VISIBLE_DEVICES) in the manifest")
    return problems


def _ldd(path: str) -> str:
    try:
        return subprocess.run(["ldd", path], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------------------
# Switching and the ledger


def switch(name: str, by: str, root: Path = MODEL_DIR, ledger: Path = LEDGER, note: str = "",
           decision: str = "switch") -> dict:
    """Point current at <name> (atomically) and write the ledger line. Doesn't restart anything."""
    target = root / f"{name}.env"
    if not target.is_file():
        raise FileNotFoundError(f"no manifest {target}")
    before = current_name(root)
    tmp = root / f".current.{os.getpid()}"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target.name)
    os.replace(tmp, root / "current")
    rec = {"ts": round(time.time(), 3), "from": before, "to": name, "decision": decision, "by": by}
    if note:
        rec["note"] = note
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def init(agent_env: Path = AGENT_ENV, root: Path = MODEL_DIR, apply: bool = False, ledger: Path = LEDGER,
         hash_file: bool = True) -> tuple[str, dict[str, str], bool]:
    """Today's AGENT_RUNTIME_CMD (and TITAN_AGENT_GPU) as a manifest; with `apply`, written and made
    current when there is no current yet. Returns (name, manifest, made current)."""
    text = agent_env.read_text()
    cmd = env_value(text, "AGENT_RUNTIME_CMD")
    if not cmd:
        raise ValueError(f"no AGENT_RUNTIME_CMD in {agent_env}")
    m = parse_runtime_cmd(cmd)
    m["GPUS"] = env_value(text, "TITAN_AGENT_GPU") or "1"
    m["MODEL_SHA256"] = sha256(m["MODEL_FILE"]) if hash_file and os.path.isfile(m["MODEL_FILE"]) else ""
    m["NAME"] = default_name(m)
    m["NOTE"] = "today's AGENT_RUNTIME_CMD from agent.env (local_model.py init)"
    m["CREATED"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    assert same_command(command(m), cmd), "the manifest would run a different command"
    made = False
    if apply:
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{m['NAME']}.env"
        if not path.exists():
            path.write_text(render(m))
            os.chmod(path, 0o640)
        if current_name(root) is None:
            switch(m["NAME"], by="local_model.py init", root=root, ledger=ledger, decision="init",
                   note="the live setup, written down")
            made = True
    return m["NAME"], m, made


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--apply", action="store_true")
    sub.add_parser("list")
    p = sub.add_parser("show"); p.add_argument("name", nargs="?", default="current")
    p = sub.add_parser("cmd"); p.add_argument("name", nargs="?", default="current")
    p = sub.add_parser("check"); p.add_argument("name", nargs="?", default="current")
    p.add_argument("--no-hash", action="store_true", help="skip the sha256 (a 5 GB file takes ~10 s)")
    p = sub.add_parser("switch"); p.add_argument("name"); p.add_argument("--by", required=True)
    p.add_argument("--note", default=""); p.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "init":
        name, m, made = init(apply=a.apply)
        print(render(m), end="")
        print(f"# {'written' if a.apply else 'dry run (--apply writes it)'}: {MODEL_DIR / (name + '.env')}"
              + (" and made current" if made else ""))
        return 0
    if a.cmd == "list":
        cur = current_name()
        for n in names():
            print(("* " if n == cur else "  ") + n)
        return 0
    if a.cmd in ("show", "cmd", "check"):
        m = load(a.name)
        if a.cmd == "show":
            print(render(m), end="")
        elif a.cmd == "cmd":
            print(f"CUDA_VISIBLE_DEVICES={m.get('GPUS', '')} {command(m)}")
        else:
            problems = check(m, hash_file=not a.no_hash)
            for pr in problems:
                print("FAIL", pr)
            print("ok" if not problems else f"{len(problems)} problem(s)")
            return 1 if problems else 0
        return 0
    if a.cmd == "switch":
        if not a.apply:
            print(f"dry run: current {current_name()} → {a.name} (--apply flips it; restart titan-local-agent to run it)")
            return 0
        rec = switch(a.name, by=a.by, note=a.note)
        print(json.dumps(rec))
        print("current flipped; restart titan-local-agent (and warm_local.py) to run it")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
