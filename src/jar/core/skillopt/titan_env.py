"""SkillOpt environment: titan's household agent, scored on a skill's task bank.

The framework hands `rollout()` the current SKILL.md text; each task runs as one eval turn with
that text as a `skill_overrides` candidate (harness.run_task), so the skill is judged in the real
prompt, with the real Jev skill pick, tools and brain. The trajectory SkillOpt's optimizer reflects
on is written to <out_dir>/predictions/<id>/conversation.json.

Runs in the SkillOpt venv (/ai/datasets/envs/skillopt); see run.py.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

from skillopt.datasets.base import SplitDataLoader
from skillopt.envs.base import EnvAdapter

import harness


class TitanTasksLoader(SplitDataLoader):
    """Items are harness tasks. run.py writes a stratified split (splits/<split>/items.json);
    ratio mode over tasks/<skill>.jsonl works too."""

    def load_raw_items(self, data_path: str) -> list[dict]:
        with open(data_path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip() and not line.startswith("//")]


class TitanHouseholdAdapter(EnvAdapter):
    def __init__(self, target_skill: str = "", data_path: str = "", split_dir: str = "",
                 split_mode: str = "ratio", split_ratio: str = "2:1:1", split_seed: int = 42,
                 split_output_dir: str = "", workers: int = 2, analyst_workers: int = 1,
                 failure_only: bool = False, minibatch_size: int = 4, edit_budget: int = 3,
                 seed: int = 42, limit: int = 0, **_ignored) -> None:
        if not target_skill:
            raise ValueError("titan_household needs env.target_skill (a skill in scripts/skills)")
        self.target_skill = target_skill
        self.workers = int(workers)
        self.analyst_workers = int(analyst_workers)   # the local optimizer serves one request at a time
        self.failure_only = failure_only
        self.minibatch_size = minibatch_size
        self.edit_budget = edit_budget
        self.dataloader = TitanTasksLoader(split_dir=split_dir, data_path=data_path, split_mode=split_mode,
                                           split_ratio=split_ratio, split_seed=split_seed,
                                           split_output_dir=split_output_dir, seed=seed, limit=limit)

    def setup(self, cfg: dict) -> None:
        super().setup(cfg)
        self.dataloader.setup(cfg)

    def get_dataloader(self):
        return self.dataloader

    def build_env_from_batch(self, batch, **kwargs):
        return list(batch.payload or [])

    def build_train_env(self, batch_size: int, seed: int, **kwargs):
        return self.build_env_from_batch(self.dataloader.build_train_batch(batch_size=batch_size, seed=seed, **kwargs))

    def build_eval_env(self, env_num: int, split: str, seed: int, **kwargs):
        return self.build_env_from_batch(self.dataloader.build_eval_batch(env_num=env_num, split=split, seed=seed,
                                                                         **kwargs))

    def get_task_types(self) -> list[str]:
        return [self.target_skill]

    def rollout(self, env_manager, skill_content: str, out_dir: str, **kwargs) -> list[dict]:
        def one(task: dict) -> dict:
            r = harness.run_task(task, self.target_skill, skill_content)
            pred = os.path.join(out_dir, "predictions", str(task["id"]))
            os.makedirs(pred, exist_ok=True)
            conv: list[dict] = [{"role": "user", "content": harness.render(task["question"])}]
            conv += [{"type": "tool_call", "cmd": f"{c['tool']}({json.dumps(c['args'])})",
                      "obs": harness.fake_mac(c["tool"], c["args"])[:600]} for c in r["device_calls"]]
            if r["status"]:
                conv.append({"role": "system", "content": "status lines (titan tools): " + " | ".join(r["status"])})
            if r.get("skill_shown") is False and r.get("skill_read"):
                picked = r.get("skill_picked")
                conv.append({"role": "system", "content": (
                    f"NOTE: the '{self.target_skill}' skill's full text was read via skill_guide after the pick "
                    f"missed (the skill picker chose {', '.join(picked) if picked else 'no skill'}), which cost an "
                    "extra round. Body edits did reach this turn; edit the description line (frontmatter "
                    "`description:`) if the picker should have chosen this skill.")})
            elif r.get("skill_shown") is False:
                picked = r.get("skill_picked")
                conv.append({"role": "system", "content": (
                    f"NOTE: the '{self.target_skill}' skill's full text was NOT shown on this question "
                    f"(the skill picker chose {', '.join(picked) if picked else 'no skill'}). Only the skill's "
                    "description line (frontmatter `description:`) affects this case: edit the description if "
                    "this question should belong to the skill; body edits can't change this turn.")})
            conv.append({"role": "assistant", "content": r["reply"]})
            verdict = "all checks passed" if not r["failed"] else "FAILED: " + "; ".join(r["failed"])
            conv.append({"role": "system", "content": f"CHECKS ({r['score']:.2f}): {verdict}"})
            with open(os.path.join(pred, "conversation.json"), "w", encoding="utf-8") as f:
                json.dump(conv, f, indent=1)
            return {"id": str(task["id"]), "hard": float(r["score"] == 1.0), "soft": r["score"],
                    "task_type": self.target_skill, "task_description": harness.render(task["question"]),
                    "n_turns": len(r["device_calls"]) + 1, "fail_reason": "; ".join(r["failed"])}

        with ThreadPoolExecutor(max(1, self.workers)) as ex:
            return list(ex.map(one, env_manager))
