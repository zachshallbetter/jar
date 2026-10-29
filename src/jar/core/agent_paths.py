"""One default for each setting several scripts read (10-05 housekeeping): the environment wins, else this.

Before, call sites disagreed: AGENT_MEMORY_DIR was /home/zach/... in one place and $HOME-based in others,
AGENT_SCRIPT_DIR defaulted to a path on the Mac, AGENT_SESSION was "household" in one place and
"titan-local-agent" in another, and AGENT_API_TOKEN_FILE's conventional path was written out three times.
On titan agent.env and the units set what they need; these defaults are what a test, the gate or a CLI run gets.
"""
from __future__ import annotations

import os
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
# Where a client finds the agent API's token. The API server reads AGENT_API_TOKEN_FILE only when it's set
# (auth is opt-in there), so this default applies to clients: evals, the credential scrubber.
API_TOKEN_FILE = "/ai/config/agent_api_token.txt"
SESSION = "titan-local-agent"


def memory_dir() -> Path:
    """Where the memory pyramid lives (titan_memory.db): AGENT_MEMORY_DIR, else ~/.config/jellybean/memory."""
    env = os.environ.get("AGENT_MEMORY_DIR", "").strip()
    return Path(env) if env else Path(os.environ.get("HOME", "~")).expanduser() / ".config" / "jellybean" / "memory"


def memory_db() -> Path:
    return memory_dir() / "titan_memory.db"


def api_token_file() -> str:
    return os.environ.get("AGENT_API_TOKEN_FILE", "").strip() or API_TOKEN_FILE


def script_dir() -> Path:
    """titan-node's scripts folder: AGENT_SCRIPT_DIR, else the one this file is in."""
    env = os.environ.get("AGENT_SCRIPT_DIR", "").strip()
    return Path(env) if env else SCRIPTS


def session() -> str:
    """The agent session's name (its history file, its workflow instances): AGENT_SESSION, else titan-local-agent."""
    return os.environ.get("AGENT_SESSION", "").strip() or SESSION
