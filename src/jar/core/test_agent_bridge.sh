#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/titan_env.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${TITAN_CLI:-}" ]]; then
  if [[ -x "${SCRIPT_DIR}/titan.sh" ]]; then
    TITAN_CLI="${SCRIPT_DIR}/titan.sh"
  else
    TITAN_CLI="$(command -v titan 2>/dev/null || true)"
  fi
  if [[ -z "${TITAN_CLI}" ]]; then
    TITAN_CLI="${SCRIPT_DIR}/titan.sh"
  fi
fi


cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
    kill "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
  if [[ -n "${TMP_DIR:-}" && -d "${TMP_DIR}" ]]; then
    rm -rf "${TMP_DIR}"
  fi
}
trap cleanup EXIT

TMP_DIR="$(mktemp -d)"
PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
SERVER_FILE="${TMP_DIR}/stub_server.py"
cat >"${SERVER_FILE}" <<'PY'
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[1])
router_log = sys.argv[2]


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send(200, {"data": [{"id": "smoke-model"}]})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        if self.path == "/route":
            with open(router_log, "a", encoding="utf-8") as fh:
                fh.write(payload.get("input", "") + "\n")
                fh.write("route_called\n")
            text = str(payload.get("input", "")).lower()
            if "abstain" in text:
                response = {
                    "truth_type": "abstain",
                    "retrieval_required": False,
                    "confidence_score": 0.05,
                    "action": "abstain",
                    "distribution": [],
                }
            elif "retrieve" in text:
                response = {
                    "truth_type": "canonical",
                    "retrieval_required": True,
                    "confidence_score": 0.94,
                    "action": "retrieve",
                    "distribution": [],
                }
            elif "request" in text or "fields" in text:
                response = {
                    "truth_type": "operational",
                    "retrieval_required": True,
                    "confidence_score": 0.91,
                    "action": "request",
                    "required_field_indices": [2, 5],
                    "distribution": [],
                }
            elif "titan" in text or "right now" in text or "current state" in text or "see on titan" in text:
                response = {
                    "truth_type": "operational",
                    "retrieval_required": True,
                    "confidence_score": 0.88,
                    "action": "request",
                    "distribution": [],
                }
            elif "road runner" in text or "average speed" in text:
                response = {
                    "truth_type": "semantic",
                    "retrieval_required": False,
                    "confidence_score": 0.42,
                    "action": "request",
                    "distribution": [],
                }
            else:
                response = {
                    "truth_type": "semantic",
                    "retrieval_required": False,
                    "confidence_score": 0.91,
                    "action": "answer",
                    "distribution": [
                        {"name": "Semantic", "value": 0.91, "color": "#3b82f6"},
                        {"name": "Canonical", "value": 0.03, "color": "#10b981"},
                        {"name": "Operational", "value": 0.03, "color": "#f59e0b"},
                        {"name": "Abstain", "value": 0.03, "color": "#71717a"},
                    ],
                }
            self._send(200, {
                **response,
            })
            return
        if self.path == "/v1/chat/completions":
            self._send(200, {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "bridge smoke ok",
                        }
                    }
                ]
            })
            return
        self._send(404, {"error": "not found"})

    def log_message(self, format, *args):
        return


server = HTTPServer(("127.0.0.1", port), Handler)
server.serve_forever()
PY

python3 "${SERVER_FILE}" "${PORT}" "${TMP_DIR}/router.log" >"${TMP_DIR}/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

if ! curl -fsS "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
  echo "stub server did not become ready" >&2
  cat "${TMP_DIR}/server.log" >&2 || true
  exit 1
fi

prompt_output="$(
  env \
    TITAN_REMOTE_EXEC=1 \
    NO_PROXY=127.0.0.1,localhost \
    no_proxy=127.0.0.1,localhost \
    HTTP_PROXY= \
    HTTPS_PROXY= \
    ALL_PROXY= \
    "${TITAN_CLI}" agent prompt --endpoint "http://127.0.0.1:${PORT}" --router parametric --router-url "http://127.0.0.1:${PORT}" "Say hello in one short sentence."
)"
if [[ "${prompt_output}" != "bridge smoke ok" ]]; then
  echo "unexpected prompt output: ${prompt_output}" >&2
  exit 1
fi

if ! grep -Fq "route_called" "${TMP_DIR}/router.log"; then
  echo "parametric router was not consulted" >&2
  cat "${TMP_DIR}/router.log" >&2 || true
  exit 1
fi

request_output="$(
  env \
    TITAN_REMOTE_EXEC=1 \
    NO_PROXY=127.0.0.1,localhost \
    no_proxy=127.0.0.1,localhost \
    HTTP_PROXY= \
    HTTPS_PROXY= \
    ALL_PROXY= \
    "${TITAN_CLI}" agent prompt --endpoint "http://127.0.0.1:${PORT}" --router parametric --router-url "http://127.0.0.1:${PORT}" "Please request more fields before answering."
)"
if [[ "${request_output}" != *"Parametric requested more fields before answering."* ]]; then
  echo "unexpected request output: ${request_output}" >&2
  exit 1
fi

semantic_output="$(
  env \
    TITAN_REMOTE_EXEC=1 \
    NO_PROXY=127.0.0.1,localhost \
    no_proxy=127.0.0.1,localhost \
    HTTP_PROXY= \
    HTTPS_PROXY= \
    ALL_PROXY= \
    "${TITAN_CLI}" agent prompt --endpoint "http://127.0.0.1:${PORT}" --router parametric --router-url "http://127.0.0.1:${PORT}" "What is the average speed of a road runner?"
)"
if [[ "${semantic_output}" != "bridge smoke ok" ]]; then
  echo "unexpected semantic output: ${semantic_output}" >&2
  exit 1
fi

titan_output="$(
  env \
    TITAN_REMOTE_EXEC=1 \
    AGENT_RETRIEVAL_CMD="sh -c 'printf retrieved_titan_context >> \"${TMP_DIR}/retrieval.log\"; printf retrieved context from titan smoke'" \
    NO_PROXY=127.0.0.1,localhost \
    no_proxy=127.0.0.1,localhost \
    HTTP_PROXY= \
    HTTPS_PROXY= \
    ALL_PROXY= \
    "${TITAN_CLI}" agent prompt --endpoint "http://127.0.0.1:${PORT}" --router parametric --router-url "http://127.0.0.1:${PORT}" "Tell me what you can see on Titan right now."
)"
if [[ "${titan_output}" != *"bridge smoke ok"* ]]; then
  echo "unexpected titan output: ${titan_output}" >&2
  exit 1
fi
if [[ ! -f "${TMP_DIR}/retrieval.log" ]] || ! grep -Fq "retrieved_titan_context" "${TMP_DIR}/retrieval.log"; then
  echo "titan retrieval branch was not exercised" >&2
  exit 1
fi

abstain_output="$(
  env \
    TITAN_REMOTE_EXEC=1 \
    NO_PROXY=127.0.0.1,localhost \
    no_proxy=127.0.0.1,localhost \
    HTTP_PROXY= \
    HTTPS_PROXY= \
    ALL_PROXY= \
    "${TITAN_CLI}" agent prompt --endpoint "http://127.0.0.1:${PORT}" --router parametric --router-url "http://127.0.0.1:${PORT}" "Please abstain from answering this question."
)"
if [[ "${abstain_output}" != *"Parametric routed this prompt to abstain"* ]]; then
  echo "unexpected abstain output: ${abstain_output}" >&2
  exit 1
fi

retrieve_output="$(
  env \
    TITAN_REMOTE_EXEC=1 \
    AGENT_RETRIEVAL_CMD="sh -c 'printf retrieved_context >> \"${TMP_DIR}/retrieval.log\"; printf retrieved context from bridge smoke'" \
    NO_PROXY=127.0.0.1,localhost \
    no_proxy=127.0.0.1,localhost \
    HTTP_PROXY= \
    HTTPS_PROXY= \
    ALL_PROXY= \
    "${TITAN_CLI}" agent prompt --endpoint "http://127.0.0.1:${PORT}" --router parametric --router-url "http://127.0.0.1:${PORT}" "Please retrieve exact state before answering."
)"
if [[ "${retrieve_output}" != *"bridge smoke ok"* ]]; then
  echo "unexpected retrieve output: ${retrieve_output}" >&2
  exit 1
fi
if [[ ! -f "${TMP_DIR}/retrieval.log" ]] || ! grep -Fq "retrieved_context" "${TMP_DIR}/retrieval.log"; then
  echo "retrieval branch was not exercised" >&2
  exit 1
fi

context_output="$("${TITAN_CLI}" context --json)"
CONTEXT_OUTPUT="${context_output}" python3 - <<'PY'
import json
import os

context = json.loads(os.environ["CONTEXT_OUTPUT"])
if context.get("runtime_access_mode") != "titan_bridge":
    raise SystemExit(f"unexpected runtime_access_mode: {context.get('runtime_access_mode')}")
if context.get("titan_host") not in {"10.0.0.170", "titan-node", "titan-node.ladon-betelgeuse.ts.net"}:
    raise SystemExit(f"unexpected titan_host: {context.get('titan_host')}")
if len(context.get("registry", {}).get("runtimes", [])) < 8:
    raise SystemExit("runtime registry is incomplete")
PY

printf 'agent_prompt=%s\n' "${prompt_output}"
printf 'titan_context=ok\n'
