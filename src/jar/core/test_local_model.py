#!/usr/bin/env python3
# gate: hermetic   (temp dirs and a fake llama-server; no model is loaded, nothing on titan is touched)
"""The local model as a manifest (#408 step 1, scripts/local_model.py): parse, render, init, switch,
static checks, and the run wrapper (server-setup/titan-local-agent-run.sh) giving the same command
with today's agent.env alone and with the manifest `init` writes. Hermetic.

Run: python3 scripts/test_local_model.py
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import local_model as lm  # noqa: E402

ok = fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}: {detail}")


TODAY = ("/ai/tools/llama.cpp-b11259/llama-server --host 127.0.0.1 --port 8010 --model "
         "/ai/models/titan-agent-shortlist/Qwen3-8B-Q4_K_M.gguf --ctx-size 32768 --parallel 1 "
         "--n-gpu-layers 99 --split-mode layer --tensor-split 0.70,0.12,0.18")

m = lm.parse_runtime_cmd(TODAY)
check("parse: the build, the model, the other flags in order",
      m["LLAMA_SERVER"] == "/ai/tools/llama.cpp-b11259/llama-server"
      and m["MODEL_FILE"].endswith("Qwen3-8B-Q4_K_M.gguf") and "--model" not in m["ARGS"]
      and m["ARGS"].startswith("--host 127.0.0.1 --port 8010 --ctx-size 32768"), str(m))
check("command: the same program, model and flags", lm.same_command(lm.command(m), TODAY), lm.command(m))
check("name: model, build, context", lm.default_name(m) == "qwen3-8b-q4_k_m-b11259-32k", lm.default_name(m))
odd = dict(m, NOTE="it's the 'live' one", GPUS="1,2,0", NAME="x")
back = lm.parse(lm.render(odd))
check("render/parse round trip, quotes and commas kept", back == {k: odd.get(k, "") for k in lm.FIELDS}, str(back))
try:
    lm.parse_runtime_cmd("/x/llama-server --port 1")
    check("parse: a command without --model is refused", False, "no error")
except ValueError:
    check("parse: a command without --model is refused", True)

with tempfile.TemporaryDirectory(prefix="titan-localmodel-") as tmp:
    t = Path(tmp)
    build = t / "tools" / "llama.cpp-b11259"
    build.mkdir(parents=True)
    server = build / "llama-server"
    # A fake llama-server: prints what it was given, so the wrapper's command can be compared.
    server.write_text('#!/usr/bin/env bash\necho "GPUS=$CUDA_VISIBLE_DEVICES ARGS=$*"\n')
    server.chmod(server.stat().st_mode | stat.S_IXUSR)
    model = t / "models" / "Qwen3-8B-Q4_K_M.gguf"
    model.parent.mkdir()
    model.write_bytes(b"GGUF fake weights")
    cmd = TODAY.replace("/ai/tools/llama.cpp-b11259/llama-server", str(server)).replace(
        "/ai/models/titan-agent-shortlist/Qwen3-8B-Q4_K_M.gguf", str(model))
    root = t / "agent"
    root.mkdir()
    (root / "agent.env").write_text(f"GEMINI_API_KEY='not-real'\nAGENT_RUNTIME_CMD=\"{cmd}\"\nTITAN_AGENT_GPU=1,2,0\n")
    mdir, ledger = t / "local-model.d", t / "models.jsonl"

    name, man, made = lm.init(root / "agent.env", mdir, apply=False, ledger=ledger)
    check("init: a dry run writes nothing", not mdir.exists() and not ledger.exists() and not made, "")
    check("init: the GPUs and the model's sha256 are recorded", man["GPUS"] == "1,2,0"
          and man["MODEL_SHA256"] == lm.sha256(str(model)), str(man))
    name, man, made = lm.init(root / "agent.env", mdir, apply=True, ledger=ledger)
    path = mdir / f"{name}.env"
    check("init --apply: the manifest (0640), current, a ledger line",
          path.is_file() and oct(path.stat().st_mode & 0o777) == "0o640" and lm.current_name(mdir) == name and made
          and json.loads(ledger.read_text().splitlines()[-1])["decision"] == "init", f"{lm.current_name(mdir)}")
    check("init: the manifest holds no secret from agent.env", "not-real" not in path.read_text(), "")
    name2, _, made2 = lm.init(root / "agent.env", mdir, apply=True, ledger=ledger)
    check("init again: nothing changes (current stays)", name2 == name and not made2
          and len(ledger.read_text().splitlines()) == 1, "")

    # The wrapper: the same command and GPUs from agent.env alone and from the manifest.
    wrapper = HERE.parent / "server-setup" / "titan-local-agent-run.sh"

    def run_wrapper(model_dir: Path) -> str:
        env = {"PATH": os.environ["PATH"], "TITAN_AGENT_ROOT": str(root), "LOCAL_MODEL_DIR": str(model_dir)}
        r = subprocess.run(["bash", str(wrapper)], capture_output=True, text=True, env=env, timeout=20)
        return r.stdout.strip() or r.stderr.strip()

    plain = run_wrapper(t / "no-such-dir")
    via = run_wrapper(mdir)
    check("wrapper: without a manifest, agent.env's command as before",
          plain.startswith("GPUS=1,2,0 ARGS=") and f"--model {model}" in plain, plain)
    check("wrapper: the manifest runs the same model, flags and GPUs",
          sorted(plain.replace("ARGS=", "ARGS= ").split()) == sorted(via.replace("ARGS=", "ARGS= ").split()) and lm.same_command(
              f"{server} " + plain.split("ARGS=", 1)[1], f"{server} " + via.split("ARGS=", 1)[1]), f"{plain}\n{via}")

    # A second manifest, switched to.
    other = dict(man, NAME="qwen3-8b-q4_k_m-b11259-32k-q8kv",
                 ARGS=man["ARGS"] + " --cache-type-k q8_0 --cache-type-v q8_0", NOTE="#469")
    (mdir / f"{other['NAME']}.env").write_text(lm.render(other))
    rec = lm.switch(other["NAME"], by="test", root=mdir, ledger=ledger)
    check("switch: current flips, the ledger says from and to",
          lm.current_name(mdir) == other["NAME"] and rec["from"] == name and rec["to"] == other["NAME"]
          and json.loads(ledger.read_text().splitlines()[-1])["by"] == "test", str(rec))
    check("switch: the wrapper then runs the new flags", "--cache-type-k q8_0" in run_wrapper(mdir), "")
    try:
        lm.switch("nope", by="test", root=mdir, ledger=ledger)
        check("switch: an unknown manifest is refused", False, "")
    except FileNotFoundError:
        check("switch: an unknown manifest is refused (current unchanged)", lm.current_name(mdir) == other["NAME"])

    # Static checks.
    cuda12 = lambda _p: "libcudart.so.12 => /usr/local/cuda/lib64/libcudart.so.12"  # noqa: E731
    check("check: a good manifest passes", lm.check(man, ldd=cuda12) == [], str(lm.check(man, ldd=cuda12)))
    check("check: a CUDA 13 build is refused (no sm_52)",
          any("CUDA 13" in p for p in lm.check(man, ldd=lambda _p: "libcudart.so.13 => x")), "")
    check("check: a build without CUDA is refused (it would run on the CPU)",
          any("CPU" in p for p in lm.check(man, ldd=lambda _p: "libc.so.6 => x")), "")
    check("check: a changed model file is caught (sha256)",
          any("sha256" in p for p in lm.check(dict(man, MODEL_SHA256="0" * 64), ldd=cuda12)), "")
    check("check: a missing model and build are named",
          len(lm.check(dict(man, MODEL_FILE=str(t / "gone.gguf"), LLAMA_SERVER=str(t / "gone")), ldd=cuda12)) == 2, "")

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
