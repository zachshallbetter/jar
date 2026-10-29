---
name: code-execution
description: Writes and runs code and scripts: computing and transforming data on titan, scripts and automations on the user's Mac, and changes to titan's or the apps' own code. Use when the user asks to write a script, run code, automate something with code, debug an error, or change how titan or Jellybean works.
triggers: script, run code, write code, port, uncommitted, git, todos, readme, logs, log, last few lines, repo, repository, project, files in, listening on, compute, csv
---

## Overview
Three places, each with its own route:
- **Compute on titan:** `python_eval` (one expression) or `run_python` (a script in a locked sandbox:
  no network, no files, 10 s). For maths, parsing and transforming data.
- **The user's Mac:** write a script with `write_file`, run it with `run_shell` (the Mac asks the user
  first), and if they'll want it again, save it as a skill (`create_skill`) and say so.
- **titan's or the apps' own code:** `add_todo` for Claude with a short spec (what, where, how to tell
  it works). It becomes a GitHub issue and then a pull request the user merges. Never edit a deployed
  copy, and never write scripts for the Mac onto titan.

## When to Use
- "Write a script to…", "run this", "automate X", "what does this code do", "fix this error"
- "Change how Jellybean shows…", "add a feature to titan"

## Core Process
1. Pick the route by where the work lives (above).
2. Read before writing: `read_file` / `list_files` on the Mac, `file_read` on titan.
3. Run it, read the output, and fix it if it fails: two tries, then say what failed and what you'd
   need.
4. Say what you ran and what it showed, in plain words.

## Rules
- The Mac asks the user before shell commands and writes, so don't also ask in words.
- Never put a secret in a script, and never read key or credential files.
- A run that could go on without end gets a limit (a count, a timeout).
