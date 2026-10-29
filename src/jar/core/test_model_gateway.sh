#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]] && kill -0 "${BACKEND_PID}" 2>/dev/null; then
    kill "${BACKEND_PID}" 2>/dev/null || true
    wait "${BACKEND_PID}" 2>/dev/null || true
  fi
  if [[ -n "${GATEWAY_PID:-}" ]] && kill -0 "${GATEWAY_PID}" 2>/dev/null; then
    kill "${GATEWAY_PID}" 2>/dev/null || true
    wait "${GATEWAY_PID}" 2>/dev/null || true
  fi
  if [[ -n "${TMP_DIR:-}" && -d "${TMP_DIR}" ]]; then
    rm -rf "${TMP_DIR}"
  fi
}
trap cleanup EXIT

TMP_DIR="$(mktemp -d)"

random_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

BACKEND_PORT="$(random_port)"
GATEWAY_PORT="$(random_port)"
BACKEND_LOG="${TMP_DIR}/backend.log"

python3 - <<'PY' "${BACKEND_PORT}" "${BACKEND_LOG}" >"${TMP_DIR}/backend.out" 2>&1 &
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[1])
log_path = sys.argv[2]


def invalid_schema(value):
    return isinstance(value, dict) and "type" not in value and "properties" not in value and "items" not in value


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
            self._send(200, {"data": [{"id": "stub-model"}]})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        payload = json.loads(raw)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(raw + "\n")
        if len(raw) > 5000:
            self._send(400, {"error": {"message": f"request ({len(raw) // 4} tokens) exceeds the available context size (400 tokens), try increasing it"}})
            return
        tools = payload.get("tools") or []
        for tool in tools:
            if tool.get("type") != "function":
                continue
            if "function" in tool:
                schema = tool["function"].get("parameters")
            else:
                schema = tool.get("parameters")
            if invalid_schema(schema):
                self._send(400, {"error": {"message": f"JSON schema conversion failed: Unrecognized schema: {json.dumps(schema, separators=(',', ':'))}"}})
                return
        if self.path == "/v1/chat/completions":
            self._send(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
            return
        if self.path == "/v1/responses":
            if isinstance(payload.get("input"), list):
                for item in payload["input"]:
                    if isinstance(item, dict) and "role" in item and item.get("type") != "message":
                        self._send(400, {"error": {"message": "Cannot determine type of 'item'"}})
                        return
            self._send(200, {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]})
            return
        self._send(404, {"error": "not found"})

    def log_message(self, *_args):
        return


HTTPServer(("127.0.0.1", port), Handler).serve_forever()
PY
BACKEND_PID=$!

for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:${BACKEND_PORT}/v1/models" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

if ! curl -fsS "http://127.0.0.1:${BACKEND_PORT}/v1/models" >/dev/null 2>&1; then
  echo "backend stub did not become ready" >&2
  exit 1
fi

python3 "${SCRIPT_DIR}/model_gateway_server.py" --host 127.0.0.1 --port "${GATEWAY_PORT}" --backend-url "http://127.0.0.1:${BACKEND_PORT}/v1" >"${TMP_DIR}/gateway.out" 2>&1 &
GATEWAY_PID=$!

for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:${GATEWAY_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

if ! curl -fsS "http://127.0.0.1:${GATEWAY_PORT}/health" >/dev/null 2>&1; then
  echo "gateway did not become ready" >&2
  cat "${TMP_DIR}/gateway.out" >&2 || true
  exit 1
fi

direct_status="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${BACKEND_PORT}/v1/chat/completions" -H 'Content-Type: application/json' -d '{"model":"stub-model","tools":[{"type":"function","function":{"name":"canvas_open","description":"Canvas open","parameters":{"description":"Canvas open input matching the canvas input schema"}}}],"messages":[{"role":"user","content":"test"}]}')"
if [[ "${direct_status}" != "400" ]]; then
  echo "expected direct backend to reject invalid schema, got ${direct_status}" >&2
  exit 1
fi

gateway_chat="$(curl -sS "http://127.0.0.1:${GATEWAY_PORT}/v1/chat/completions" -H 'Content-Type: application/json' -d '{"model":"stub-model","tools":[{"type":"function","function":{"name":"canvas_open","description":"Canvas open","parameters":{"description":"Canvas open input matching the canvas input schema"}}}],"messages":[{"role":"user","content":"test"}]}')"
if [[ "${gateway_chat}" != *'"content": "ok"'* && "${gateway_chat}" != *'"content":"ok"'* ]]; then
  echo "unexpected gateway chat response: ${gateway_chat}" >&2
  exit 1
fi

gateway_resp="$(curl -sS "http://127.0.0.1:${GATEWAY_PORT}/v1/responses" -H 'Content-Type: application/json' -d '{"model":"stub-model","tools":[{"type":"function","name":"canvas_open","description":"Canvas open","parameters":{"description":"Canvas open input matching the canvas input schema"}}],"input":"test"}')"
if [[ "${gateway_resp}" != *'"status": "completed"'* && "${gateway_resp}" != *'"status":"completed"'* ]]; then
  echo "unexpected gateway responses response: ${gateway_resp}" >&2
  exit 1
fi

if ! grep -Fq '"type":"object"' "${BACKEND_LOG}"; then
  echo "gateway did not normalize tool parameters before forwarding" >&2
  tail -n 5 "${BACKEND_LOG}" >&2 || true
  exit 1
fi

responses_multi_turn_status="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${BACKEND_PORT}/v1/responses" -H 'Content-Type: application/json' -d '{"model":"stub-model","input":[{"role":"user","content":"hello"},{"role":"assistant","content":"hi"},{"role":"user","content":"continue"}],"max_output_tokens":16}')"
if [[ "${responses_multi_turn_status}" != "400" ]]; then
  echo "expected direct backend to reject plain multi-turn responses input, got ${responses_multi_turn_status}" >&2
  exit 1
fi

gateway_multi_turn_resp="$(curl -sS "http://127.0.0.1:${GATEWAY_PORT}/v1/responses" -H 'Content-Type: application/json' -d '{"model":"stub-model","input":[{"role":"user","content":"hello"},{"role":"assistant","content":"hi"},{"role":"user","content":"continue"}],"max_output_tokens":16}')"
if [[ "${gateway_multi_turn_resp}" != *'"status": "completed"'* && "${gateway_multi_turn_resp}" != *'"status":"completed"'* ]]; then
  echo "unexpected gateway multi-turn responses output: ${gateway_multi_turn_resp}" >&2
  exit 1
fi

if ! grep -Fq '"type":"message"' "${BACKEND_LOG}"; then
  echo "gateway did not normalize multi-turn responses input before forwarding" >&2
  tail -n 5 "${BACKEND_LOG}" >&2 || true
  exit 1
fi

long_resp="$(python3 - <<'PY' "${GATEWAY_PORT}"
import json
import sys
import urllib.request

port = int(sys.argv[1])
payload = {
    "model": "stub-model",
    "input": [
        {"role": "system", "content": "keep me"},
        {"role": "user", "content": "x " * 16000},
        {"role": "assistant", "content": "y " * 16000},
        {"role": "user", "content": "recent turn"},
    ],
    "max_output_tokens": 16,
}
req = urllib.request.Request(
    f"http://127.0.0.1:{port}/v1/responses",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=30) as resp:
    print(resp.status)
    print(resp.read().decode())
PY
)"
if [[ "${long_resp}" != *$'200\n'* || "${long_resp}" != *'"status": "completed"'* && "${long_resp}" != *'"status":"completed"'* ]]; then
  echo "unexpected long responses retry output: ${long_resp}" >&2
  exit 1
fi

if ! tail -n 1 "${BACKEND_LOG}" | grep -Fq 'recent turn'; then
  echo "gateway did not preserve newest turn during context compaction" >&2
  tail -n 5 "${BACKEND_LOG}" >&2 || true
  exit 1
fi

echo "model gateway smoke ok"
