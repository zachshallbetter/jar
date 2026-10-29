#!/usr/bin/env bash
set -euo pipefail

EPISTEMIC_RUNTIME_URL="${EPISTEMIC_RUNTIME_URL:-http://127.0.0.1:8500}"
LOCAL_AGENT_URL="${LOCAL_AGENT_URL:-http://127.0.0.1:8010}"
RETRIEVAL_RUNTIME_URL="${RETRIEVAL_RUNTIME_URL:-http://127.0.0.1:9100}"
RETRIEVAL_REQUIRED="${RETRIEVAL_REQUIRED:-0}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-30}"
RETRY_COUNT="${RETRY_COUNT:-60}"
RETRY_DELAY_SECONDS="${RETRY_DELAY_SECONDS:-2}"

http_get_json() {
  python3 - "$1" "$TIMEOUT_SECONDS" <<'PY'
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

url = sys.argv[1]
timeout = int(sys.argv[2])

req = urllib.request.Request(url, headers={"Accept": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        print(resp.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", "replace")
    raise SystemExit(body or f"HTTP {exc.code} for {url}")
except Exception as exc:  # pragma: no cover - shell smoke surface
    raise SystemExit(str(exc))
PY
}

http_post_json() {
  python3 - "$1" "$2" "$TIMEOUT_SECONDS" <<'PY'
from __future__ import annotations

import sys
import urllib.error
import urllib.request

url = sys.argv[1]
body = sys.argv[2].encode("utf-8")
timeout = int(sys.argv[3])

req = urllib.request.Request(
    url,
    data=body,
    headers={"Accept": "application/json", "Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        print(resp.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    body_text = exc.read().decode("utf-8", "replace")
    raise SystemExit(body_text or f"HTTP {exc.code} for {url}")
except Exception as exc:  # pragma: no cover - shell smoke surface
    raise SystemExit(str(exc))
PY
}

json_field() {
  python3 - "$1" "$2" <<'PY'
from __future__ import annotations

import json
import sys

payload = json.loads(sys.argv[1])
path = sys.argv[2].split(".")
value = payload
for key in path:
    if isinstance(value, list):
        value = value[int(key)]
    else:
        value = value[key]
print(value)
PY
}

json_length() {
  python3 - "$1" "$2" <<'PY'
from __future__ import annotations

import json
import sys

payload = json.loads(sys.argv[1])
path = sys.argv[2].split(".")
value = payload
for key in path:
    if isinstance(value, list):
        value = value[int(key)]
    else:
        value = value[key]
print(len(value))
PY
}

wait_for_json() {
  local url="$1"
  local label="$2"
  local attempt=1
  local output=""
  while [[ "${attempt}" -le "${RETRY_COUNT}" ]]; do
    if output="$(http_get_json "${url}" 2>/dev/null)"; then
      printf '%s' "${output}"
      return 0
    fi
    sleep "${RETRY_DELAY_SECONDS}"
    attempt=$((attempt + 1))
  done
  echo "timed out waiting for ${label} at ${url}" >&2
  return 1
}

check_health() {
  local url="$1"
  local label="$2"
  local expected_key="$3"
  local expected_value="$4"
  local payload
  payload="$(wait_for_json "${url}" "${label}")"
  local actual
  actual="$(json_field "${payload}" "${expected_key}")"
  if [[ "${actual}" != "${expected_value}" ]]; then
    echo "${label} reported ${expected_key}=${actual}, expected ${expected_value}" >&2
    return 1
  fi
  printf '%s: %s\n' "${label}" "${actual}"
}

echo "checking epistemic runtime"
check_health "${EPISTEMIC_RUNTIME_URL}/health" "epistemic health" "status" "ok"

echo "checking local agent health"
check_health "${LOCAL_AGENT_URL}/health" "local agent health" "ready" "True"

echo "checking local agent model inventory"
local_agent_models="$(wait_for_json "${LOCAL_AGENT_URL}/v1/models" "local agent models")"
local_agent_model_id="$(json_field "${local_agent_models}" "data.0.id")"
local_agent_model_count="$(json_length "${local_agent_models}" "data")"
echo "local agent model: ${local_agent_model_id}"
echo "local agent models discovered: ${local_agent_model_count}"
if [[ "${local_agent_model_count}" -lt 1 ]]; then
  echo "local agent returned an empty model catalog" >&2
  exit 1
fi
if [[ "${local_agent_model_id}" == prism-ml/* && "${local_agent_model_count}" -lt 3 ]]; then
  echo "turboquant catalog did not expose the expected transformer presets" >&2
  exit 1
fi

echo "checking direct local agent chat"
local_agent_chat_payload="$(python3 - <<'PY'
import json
print(json.dumps({
    "model": "titan-local",
    "messages": [{"role": "user", "content": "Reply with exactly: smoke ok."}],
    "max_tokens": 16,
    "temperature": 0.0,
}))
PY
)"
local_agent_chat_response="$(http_post_json "${LOCAL_AGENT_URL}/v1/chat/completions" "${local_agent_chat_payload}")"
local_agent_chat_text="$(json_field "${local_agent_chat_response}" "choices.0.message.content")"
if [[ -z "${local_agent_chat_text}" ]]; then
  echo "local agent returned an empty completion" >&2
  exit 1
fi
echo "local agent chat ok: ${local_agent_chat_text}"

echo "checking epistemic chat without retrieval context"
epistemic_chat_payload="$(python3 - <<'PY'
import json
print(json.dumps({
    "userMsg": "Reply with exactly: smoke ok.",
    "messages": [{"role": "user", "text": "Reply with exactly: smoke ok."}],
    "documentText": "",
    "config": {
        "truthType": "semantic",
        "fields": [],
        "fieldRegexes": {},
        "simDate": "2026-04-02",
        "max_tokens": 32,
        "temperature": 0.0,
    },
}))
PY
)"
epistemic_chat_response="$(http_post_json "${EPISTEMIC_RUNTIME_URL}/chat" "${epistemic_chat_payload}")"
epistemic_chat_text="$(json_field "${epistemic_chat_response}" "text")"
if [[ -z "${epistemic_chat_text}" ]]; then
  echo "epistemic runtime returned an empty chat response" >&2
  exit 1
fi
echo "epistemic chat ok: ${epistemic_chat_text}"

if [[ "${RETRIEVAL_REQUIRED}" == "1" ]]; then
  echo "checking retrieval runtime"
  check_health "${RETRIEVAL_RUNTIME_URL}/health" "retrieval health" "status" "ok"
fi

echo "checking epistemic chat with grounded document context"
epistemic_grounded_payload="$(python3 - <<'PY'
import json
print(json.dumps({
    "userMsg": "Read the document and return the marker phrase.",
    "messages": [{"role": "user", "text": "Read the document and return the marker phrase."}],
    "documentText": "Titan smoke document. Marker phrase: grounded smoke ok.",
    "config": {
        "truthType": "canonical",
        "fields": [],
        "fieldRegexes": {},
        "simDate": "2026-04-02",
        "max_tokens": 32,
        "temperature": 0.0,
    },
}))
PY
)"
epistemic_grounded_response="$(http_post_json "${EPISTEMIC_RUNTIME_URL}/chat" "${epistemic_grounded_payload}")"
epistemic_grounded_text="$(json_field "${epistemic_grounded_response}" "text")"
if [[ -z "${epistemic_grounded_text}" ]]; then
  echo "epistemic grounded chat returned an empty response" >&2
  exit 1
fi
echo "epistemic grounded chat ok: ${epistemic_grounded_text}"

echo "smoke test passed"
