# scripts/tools

One-off evaluations and benchmarks, run by hand on titan. No unit, timer, deploy step or other script calls them, and the deploy gate doesn't run them. Each file's docstring says how to run it. The scripts they import (agent_runtime_lib, kb_hygiene, …) stay in `scripts/`.
