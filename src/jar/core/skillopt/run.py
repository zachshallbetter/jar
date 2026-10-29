#!/usr/bin/env python3
"""Optimize one household skill with SkillOpt, entirely on titan.

  /ai/datasets/envs/skillopt/bin/python scripts/skillopt/run.py --skill calendar [--epochs 2] [--smoke]

- The optimizer is titan's local code model (llama-server :8198, qwen_chat backend, thinking off);
  no role points anywhere else, so nothing leaves titan except what an eval turn already sends to
  its brain (the synthetic task questions).
- Rollouts are eval turns through the live agent API (needs titan-node#162: skill_overrides and the
  eval side-effect guard).
- Output lands in /ai/datasets/skillopt/<skill>/<timestamp>/: best_skill.md, history.json,
  summary.json and per-step records. Adopting a result is a reviewed titan-node PR; nothing here
  changes scripts/skills.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from ops_ports import CODE_MODEL_URL  # noqa: E402

SKILLOPT_SRC = Path(os.environ.get("SKILLOPT_SRC", "/ai/datasets/src/SkillOpt"))
OUT_ROOT = Path(os.environ.get("SKILLOPT_OUT", "/ai/datasets/skillopt"))
OPTIMIZER_URL = os.environ.get("SKILLOPT_OPTIMIZER_URL", CODE_MODEL_URL + "/v1")

CONFIG = """_base_: {base}
out_root: {out}
env:
  name: titan_household
  target_skill: {skill}
  skill_init: {skill_init}
  split_mode: split_dir
  split_dir: {out}/splits
  workers: 2
  analyst_workers: 1
  minibatch_size: 4
  edit_budget: 3
model:
  optimizer_backend: qwen_chat
  target_backend: qwen_chat
  optimizer: qwen3.6-35b-a3b
  target: titan-agent
  reasoning_effort: ""
  optimizer_qwen_chat_base_url: {url}
  target_qwen_chat_base_url: {url}
  optimizer_qwen_chat_thinking_mode: disabled
  optimizer_qwen_chat_max_tokens: 8000
  optimizer_qwen_chat_timeout_seconds: 900
  optimizer_qwen_chat_temperature: 0.2
"""


def stratified_splits(tasks: list[dict], skill: str, skill_text: str, out: Path, repeats: int) -> dict:
    """Score every task once with the starting skill, then deal them into train / val / test so the
    ones it fails are spread across all three (a random split can leave val and test with nothing
    to improve). Val and test tasks run `repeats` times each, so one lucky or unlucky turn can't
    pass or fail the gate alone. Writes <out>/splits/<split>/items.json and <out>/baseline.json."""
    import json
    import harness
    base = [dict(harness.run_task(t, skill, skill_text), task=t) for t in tasks]
    (out / "baseline.json").write_text(json.dumps([{k: v for k, v in b.items() if k != "task"} for b in base], indent=1))
    order = sorted(base, key=lambda b: (b["score"], b["id"]))
    deal = ["val", "test", "train", "train"]
    splits: dict[str, list[dict]] = {"train": [], "val": [], "test": []}
    for i, b in enumerate(order):
        splits[deal[i % len(deal)]].append(b["task"])
    for name in ("val", "test"):
        splits[name] = [dict(t, id=f"{t['id']}#{r + 1}") for t in splits[name] for r in range(max(1, repeats))]
    for name, items in splits.items():
        (out / "splits" / name).mkdir(parents=True, exist_ok=True)
        (out / "splits" / name / "items.json").write_text(json.dumps(items, indent=1))
    failing = [b["id"] for b in order if b["score"] < 1.0]
    print(f"[skillopt] baseline mean {sum(b['score'] for b in base) / len(base):.3f}; failing {failing}", flush=True)
    return {n: len(v) for n, v in splits.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True)
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--smoke", action="store_true", help="one tiny step: 4 tasks, no slow/meta update")
    ap.add_argument("--repeats", type=int, default=2, help="runs per val/test task (default 2)")
    a, extra = ap.parse_known_args()

    skill_init = HERE.parent / "skills" / a.skill / "SKILL.md"
    tasks = HERE / "tasks" / f"{a.skill}.jsonl"
    if not skill_init.exists() or not tasks.exists():
        print(f"no skill or task bank for {a.skill!r}", file=sys.stderr)
        return 2
    # Every role local, and no provider credentials in reach of the run.
    for var in ("OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "ANTHROPIC_API_KEY",
                "OPENAI_COMPATIBLE_BASE_URL", "GEMINI_API_KEY"):
        os.environ.pop(var, None)

    out = OUT_ROOT / a.skill / time.strftime("%Y%m%d-%H%M%SZ", time.gmtime())
    out.mkdir(parents=True, exist_ok=True)
    import json
    bank = [json.loads(l) for l in tasks.read_text().splitlines() if l.strip() and not l.startswith("//")]
    sizes = stratified_splits(bank, a.skill, skill_init.read_text(), out, 1 if a.smoke else a.repeats)
    print(f"[skillopt] splits {sizes}", flush=True)
    cfg = out / "config.yaml"
    cfg.write_text(CONFIG.format(base=SKILLOPT_SRC / "configs/_base_/default.yaml", out=out, skill=a.skill,
                                 skill_init=skill_init, url=OPTIMIZER_URL))

    options = [f"train.num_epochs={a.epochs}"]
    if a.smoke:
        options = ["env.limit=4", "train.num_epochs=1", "train.batch_size=4", "gradient.minibatch_size=2",
                   "gradient.merge_batch_size=2", "optimizer.learning_rate=2", "optimizer.min_learning_rate=1",
                   "optimizer.lr_scheduler=constant", "optimizer.use_slow_update=false",
                   "optimizer.use_meta_skill=false", "evaluation.eval_test=false"]

    import scripts.train as T   # SkillOpt's trainer (installed with the package)
    from titan_env import TitanHouseholdAdapter
    T._register_builtins()
    T._ENV_REGISTRY["titan_household"] = TitanHouseholdAdapter
    sys.argv = ["skillopt-train", "--config", str(cfg), "--cfg-options", *options, *extra]
    print(f"[skillopt] {a.skill}: output in {out}", flush=True)
    return T.main() or 0


if __name__ == "__main__":
    sys.exit(main())
