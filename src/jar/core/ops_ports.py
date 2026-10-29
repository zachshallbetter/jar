"""The ports that many scripts name (#1518). One place, so a move is one edit; scripts/test_ops_constants.py fails when an
endpoint literal is written again elsewhere. No value changed; a script that honoured an environment override still does.

  AGENT_API_PORT   titan-agent-api (scripts/agent_api_server.py, FastAPI :7770, X-Agent-Token)
  CODE_MODEL_PORT  titan-code-model: llama-server running Qwen3.6-35B-A3B on GPU0+GPU2 (docs/runbook.md, GPU table)
Import: from ops_ports import AGENT_API_PORT, local_url. The shell twin is scripts/ops_ports.sh."""
from __future__ import annotations

AGENT_API_PORT = 7770
CODE_MODEL_PORT = 8198


def local_url(port: int) -> str:
    """http://127.0.0.1:<port>: the loopback URL of a service on this host."""
    return f"http://127.0.0.1:{port}"


AGENT_API_URL = local_url(AGENT_API_PORT)
CODE_MODEL_URL = local_url(CODE_MODEL_PORT)
