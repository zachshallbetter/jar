# SkillOpt for titan's household skills

Improves the household skills in `scripts/skills/<name>/SKILL.md` with
[microsoft/SkillOpt](https://github.com/microsoft/SkillOpt): the skill text is treated as the
trainable part of a frozen agent. The agent answers tasks with the current skill, an optimizer
model proposes small edits from the scored trajectories, and an edit is kept only if it strictly
improves a held-out validation score. The output is a better `SKILL.md`, nothing else.

## Pieces

| File | What it is |
|---|---|
| `tasks/<skill>.jsonl` | The skill's task bank: questions plus declarative checks (tools called or not, reply content, device-tool args). Fictional fixtures only. |
| `harness.py` | Runs one task as an **eval turn** through titan's real HTTP API with a candidate skill (`skill_overrides`), answers device tools from a fake Mac, and scores the checks. Also a CLI for baselines. |
| `titan_env.py` | The SkillOpt environment: `rollout()` runs tasks through the harness and writes the trajectories the optimizer reads. |
| `run.py` | Registers the environment, writes a config that keeps every model role local, and starts SkillOpt's trainer. |

## Safety

- **Nothing real happens during a run.** Tasks are eval turns (`source: "eval"`): nothing is remembered,
  logged or carded. Device tools go to the fake Mac. Tools with real effects are simulated on eval
  turns: Home Assistant control, routines, records, contacts, KB writes, tasks, file writes, and paid
  generation (titan-node#162).
- **The optimizer is local.** titan's code model runs on llama-server `:8198`, via the `qwen_chat`
  backend with thinking off. `run.py` clears every cloud credential from its own environment. The only
  thing that leaves titan is what any eval turn already sends to the agent's brain: the synthetic task
  questions.
- **A candidate skill only reaches eval turns.** `skill_overrides` is honoured only for an admin's
  eval turn, and only for a skill that exists.
- **Nothing changes by itself.** A run writes `best_skill.md` under
  `/ai/datasets/skillopt/<skill>/<timestamp>/`. Adopting it means a reviewed titan-node PR that edits
  `scripts/skills/<skill>/SKILL.md`.

## Use

```bash
titan skillopt baseline calendar            # score the deployed skill on its tasks
titan skillopt baseline calendar --candidate /path/to/SKILL.md
titan skillopt run calendar --smoke         # one tiny step, to check the plumbing
titan skillopt run calendar --epochs 2      # a real run (detached on titan; log in /ai/logs)
```

On titan itself, the same commands run in the foreground. SkillOpt is installed from source at
`/ai/datasets/src/SkillOpt` (commit f02c6fc, in its own venv at `/ai/datasets/envs/skillopt`).
The PyPI 0.2.0 release predates the local-endpoint backends this needs.

Rough cost of a real run: with the default ratio split (2:1:1), each step runs its train batch plus
the validation tasks as agent turns, and makes about 5–7 optimizer calls. A 24-task skill takes
minutes per step. The code model serves one request at a time, so avoid overlapping the autonomous
coder's run at 03:43 UTC.

## Adding tasks

One JSON object per line in `tasks/<skill>.jsonl`:

```json
{"id": "cal-move", "question": "Move my piano lesson tomorrow to 5pm.",
 "expect": {"tools_any": ["update_calendar_event"], "tools_none": ["delete_calendar_event"],
            "args": [{"tool": "update_calendar_event", "key": "title", "re": "piano"}]}}
```

- **Checks:**
  - `tools_all`, `tools_any`, `tools_none`: tool names, for Mac and titan tools alike;
  - `reply_all`, `reply_any`, `reply_none`: case-insensitive substrings of the reply;
  - `args`: a device tool called with a matching argument.
- **Relative dates:** `{weekday+N}` and `{date+N}` in questions and expected text resolve to N days from today.
- **Scoring:** a task scores the share of its checks that pass.
- **Validation:** `test_skillopt_harness.py`, which is in the deploy gate, checks that every bank is valid JSON and names only real tools.
- **Good tasks** are ones the current skill gets wrong, on behaviour the skill text controls: which
  tool, when to ask first, and what not to do.

## Dedicated tools first

Titan refuses `run_shell` / `run_applescript` when it duplicates a dedicated tool that the turn
hasn't tried yet (#173). The refusal still shows as the tool's status line, so the harness counts
it as used, and `tools_none: ["run_shell", ...]` scores it as a miss. Skills are judged on picking
the right tool first, not on being redirected: texts → `search_messages`, notes → `search_notes`,
calendar edits → the calendar tools, file-content search → `search_file_contents` (the model calls `files` with
mode `contents`; the turn records the tool it runs as, so the banks keep the old names, #1251 C).

## Baseline (2026-09-30, deployed skills)

| Skill | Tasks | Mean score | Fully passed | What fails |
|---|---|---|---|---|
| calendar | 24 | 0.896 | 21 | Books without asking when; no conflict check; clears a day without confirming |
| media | 10 | 0.908 | 7 | Reads the screen, checks Plex for a podcast, searches the web for the library |
| web-research | 7 | 1.000 | 7 | |
| kb-retrieval | 6 | 1.000 | 5 of 5 run (the "remember" task needs #162) | |
| home-automation | 9 | 0.833 | 7 | Checks status instead of switching a light on; reads the screen for the garage instead of saying it won't |

The smoke run on calendar had the local optimizer find the skill's stale "Never delete events" rule
by itself. Moves and cancels have been real tools since #149/#153.

## Not done yet

- Nightly scheduling, e.g. SkillOpt-Sleep's gated stage-for-review loop using this harness as its
  backend. It's left for later on purpose; for now each run is started on purpose and each result
  reviewed.
- `code-execution` has a read-only bank only (Mac read tools the fake Mac answers: file-content search, git status, ports, log tail, find). Tasks that would run code or write files on titan or the Mac stay out.
