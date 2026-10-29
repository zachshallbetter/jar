#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


_SCHEMA_META_KEYS = {
    "default",
    "description",
    "examples",
    "nullable",
    "title",
}

_SCHEMA_MARKER_KEYS = {
    "$defs",
    "$ref",
    "additionalItems",
    "additionalProperties",
    "allOf",
    "anyOf",
    "const",
    "contains",
    "contentEncoding",
    "contentMediaType",
    "contentSchema",
    "definitions",
    "dependentRequired",
    "dependentSchemas",
    "enum",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "if",
    "items",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "minItems",
    "minLength",
    "minProperties",
    "minimum",
    "multipleOf",
    "not",
    "oneOf",
    "pattern",
    "patternProperties",
    "prefixItems",
    "properties",
    "propertyNames",
    "required",
    "then",
    "type",
    "unevaluatedItems",
    "unevaluatedProperties",
}


def _permissive_object_schema(source: dict[str, Any] | None = None) -> dict[str, Any]:
    source = source or {}
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    }
    for key in ("description", "title"):
        value = source.get(key)
        if isinstance(value, str) and value:
            schema[key] = value
    return schema


def _normalize_schema(schema: Any) -> Any:
    if isinstance(schema, bool):
        return schema
    if schema is None:
        return _permissive_object_schema()
    if not isinstance(schema, dict):
        return _permissive_object_schema()

    normalized = dict(schema)

    for key in ("allOf", "anyOf", "oneOf"):
        if key in normalized and isinstance(normalized[key], list):
            normalized[key] = [_normalize_schema(item) for item in normalized[key]]

    if "not" in normalized and isinstance(normalized["not"], dict):
        normalized["not"] = _normalize_schema(normalized["not"])

    if "$ref" in normalized:
        return normalized

    if "type" not in normalized:
        if any(key in normalized for key in ("properties", "required", "additionalProperties", "patternProperties")):
            normalized["type"] = "object"
        elif any(key in normalized for key in ("items", "prefixItems")):
            normalized["type"] = "array"
        elif set(normalized.keys()).issubset(_SCHEMA_META_KEYS):
            return _permissive_object_schema(normalized)
        elif not any(key in normalized for key in _SCHEMA_MARKER_KEYS):
            return _permissive_object_schema(normalized)

    schema_type = normalized.get("type")
    if schema_type == "object":
        properties = normalized.get("properties")
        if isinstance(properties, dict):
            normalized["properties"] = {
                str(name): _normalize_schema(child) for name, child in properties.items()
            }
        else:
            normalized["properties"] = {}
        pattern_properties = normalized.get("patternProperties")
        if isinstance(pattern_properties, dict):
            normalized["patternProperties"] = {
                str(name): _normalize_schema(child) for name, child in pattern_properties.items()
            }
        additional = normalized.get("additionalProperties")
        if isinstance(additional, dict):
            normalized["additionalProperties"] = _normalize_schema(additional)
        elif additional is None:
            normalized["additionalProperties"] = True
        required = normalized.get("required")
        if isinstance(required, list):
            normalized["required"] = [item for item in required if isinstance(item, str)]
    elif schema_type == "array":
        items = normalized.get("items")
        if isinstance(items, (dict, bool)):
            normalized["items"] = _normalize_schema(items)
        prefix_items = normalized.get("prefixItems")
        if isinstance(prefix_items, list):
            normalized["prefixItems"] = [_normalize_schema(item) for item in prefix_items]

    return normalized


def _normalize_tool(tool: Any) -> Any:
    if not isinstance(tool, dict):
        return tool
    normalized = dict(tool)
    if normalized.get("type") != "function":
        return normalized

    if isinstance(normalized.get("function"), dict):
        function = dict(normalized["function"])
        function["parameters"] = _normalize_schema(function.get("parameters"))
        normalized["function"] = function
        return normalized

    if "parameters" in normalized or "input_schema" in normalized:
        normalized["parameters"] = _normalize_schema(
            normalized.get("parameters", normalized.get("input_schema"))
        )
        normalized.pop("input_schema", None)
    return normalized


def _response_text_part_type(role: Any) -> str:
    return "output_text" if role == "assistant" else "input_text"


def _normalize_response_content(content: Any, role: Any) -> Any:
    if isinstance(content, str):
        return [{"type": _response_text_part_type(role), "text": content}]
    if isinstance(content, dict):
        if "type" in content:
            return [content]
        if isinstance(content.get("text"), str):
            return [{"type": _response_text_part_type(role), "text": content["text"]}]
        return [content]
    if isinstance(content, list):
        normalized_parts: list[Any] = []
        for item in content:
            if isinstance(item, str):
                normalized_parts.append({"type": _response_text_part_type(role), "text": item})
            elif isinstance(item, dict) and "type" not in item and isinstance(item.get("text"), str):
                normalized_parts.append({"type": _response_text_part_type(role), "text": item["text"]})
            else:
                normalized_parts.append(item)
        return normalized_parts
    return content


def _normalize_response_input_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    if item.get("type") == "message":
        normalized = dict(item)
        if "content" in normalized:
            normalized["content"] = _normalize_response_content(
                normalized["content"], normalized.get("role")
            )
        return normalized
    if "role" not in item:
        return item
    normalized = dict(item)
    normalized["type"] = "message"
    if "content" in normalized:
        normalized["content"] = _normalize_response_content(
            normalized["content"], normalized.get("role")
        )
    elif isinstance(normalized.get("text"), str):
        normalized["content"] = [
            {"type": _response_text_part_type(normalized.get("role")), "text": normalized["text"]}
        ]
        normalized.pop("text", None)
    return normalized


def _normalize_payload(payload: Any, path: str | None = None) -> Any:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    tools = normalized.get("tools")
    if isinstance(tools, list):
        normalized["tools"] = [_normalize_tool(tool) for tool in tools]
    if path == "/v1/responses" and isinstance(normalized.get("input"), list):
        normalized["input"] = [
            _normalize_response_input_item(item) for item in normalized["input"]
        ]
    return normalized


_CONTEXT_ERROR_RE = re.compile(
    r"request \((?P<requested>\d+) tokens\) exceeds the available context size \((?P<available>\d+) tokens\)",
    re.IGNORECASE,
)


def _approx_tokens(value: Any) -> int:
    try:
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except TypeError:
        text = str(value)
    # Be conservative on retry compaction. Repetitive chat history can tokenize much
    # more densely than a chars/4 estimate on llama.cpp-backed models.
    return max(1, math.ceil(len(text) / 2))


def _truncate_text(text: str, token_budget: int) -> str:
    if token_budget <= 0:
        return ""
    char_budget = max(32, token_budget * 4)
    if len(text) <= char_budget:
        return text
    keep = max(16, char_budget - 64)
    return "[earlier context truncated]\n" + text[-keep:]


def _truncate_content(content: Any, token_budget: int) -> Any:
    if token_budget <= 0:
        return ""
    if isinstance(content, str):
        return _truncate_text(content, token_budget)
    if isinstance(content, list):
        remaining = token_budget
        kept: list[Any] = []
        for item in reversed(content):
            cost = _approx_tokens(item)
            if kept and cost > remaining:
                break
            if isinstance(item, dict):
                next_item = dict(item)
                if "text" in next_item and isinstance(next_item["text"], str):
                    next_item["text"] = _truncate_text(next_item["text"], remaining)
                    cost = _approx_tokens(next_item)
                kept.append(next_item)
            else:
                kept.append(item)
            remaining -= cost
            if remaining <= 0:
                break
        return list(reversed(kept)) or content[-1:]
    if isinstance(content, dict):
        truncated = dict(content)
        if isinstance(truncated.get("text"), str):
            truncated["text"] = _truncate_text(truncated["text"], token_budget)
        elif isinstance(truncated.get("content"), (str, list, dict)):
            truncated["content"] = _truncate_content(truncated["content"], token_budget)
        return truncated
    return content


def _trim_message_like_items(items: list[Any], token_budget: int) -> list[Any]:
    if token_budget <= 0 or not items:
        return items[-1:] if items else []

    prefix: list[Any] = []
    suffix_candidates = items[:]
    while suffix_candidates:
        item = suffix_candidates[0]
        if isinstance(item, dict) and item.get("role") in {"system", "developer"}:
            prefix.append(item)
            suffix_candidates = suffix_candidates[1:]
            continue
        break

    prefix_cost = sum(_approx_tokens(item) for item in prefix)
    remaining = max(0, token_budget - prefix_cost)
    kept_suffix: list[Any] = []
    for item in reversed(suffix_candidates):
        cost = _approx_tokens(item)
        if kept_suffix and cost > remaining:
            continue
        if cost > remaining:
            trimmed = item
            if isinstance(item, dict):
                trimmed = dict(item)
                if "content" in trimmed:
                    trimmed["content"] = _truncate_content(trimmed["content"], remaining)
                elif "text" in trimmed:
                    trimmed["text"] = _truncate_text(str(trimmed["text"]), remaining)
            elif isinstance(item, str):
                trimmed = _truncate_text(item, remaining)
            kept_suffix.append(trimmed)
            remaining = 0
            break
        kept_suffix.append(item)
        remaining -= cost
        if remaining <= 0:
            break

    compacted = prefix + list(reversed(kept_suffix))
    return compacted or items[-1:]


def _compact_payload(payload: Any, available_tokens: int, requested_tokens: int | None = None) -> Any:
    if not isinstance(payload, dict):
        return payload

    normalized = dict(payload)
    effective_available = available_tokens
    if requested_tokens and requested_tokens > available_tokens:
        ratio = max(0.2, min(1.0, available_tokens / requested_tokens))
        effective_available = max(1024, int(available_tokens * ratio))
    output_tokens = normalized.get("max_output_tokens", normalized.get("max_tokens", 1024))
    try:
        output_tokens_int = int(output_tokens)
    except (TypeError, ValueError):
        output_tokens_int = 1024
    reserve = min(max(512, output_tokens_int + 512), max(1024, effective_available // 3))
    input_budget = max(1024, effective_available - reserve)

    if isinstance(normalized.get("messages"), list):
        normalized["messages"] = _trim_message_like_items(normalized["messages"], input_budget)
    elif isinstance(normalized.get("input"), list):
        normalized["input"] = _trim_message_like_items(normalized["input"], input_budget)
    elif isinstance(normalized.get("input"), str):
        normalized["input"] = _truncate_text(normalized["input"], input_budget)
    elif isinstance(normalized.get("input"), dict):
        compacted_input = dict(normalized["input"])
        if "content" in compacted_input:
            compacted_input["content"] = _truncate_content(compacted_input["content"], input_budget)
        normalized["input"] = compacted_input

    return normalized


def _extract_context_error(body: bytes) -> tuple[int, int] | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message", "")
    else:
        message = payload.get("message", "")
    if not isinstance(message, str):
        return None
    match = _CONTEXT_ERROR_RE.search(message)
    if not match:
        return None
    return int(match.group("requested")), int(match.group("available"))


class _BackendPool:
    def __init__(self, backend_urls: list[str]) -> None:
        if not backend_urls:
            raise ValueError("at least one backend URL is required")
        self._backend_urls = [url.rstrip("/") for url in backend_urls]
        self._counter = itertools.count()
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:
            idx = next(self._counter) % len(self._backend_urls)
            return self._backend_urls[idx]


def _default_backend_urls() -> list[str]:
    return [
        "http://127.0.0.1:18010/v1",
        "http://127.0.0.1:8010/v1",
    ]


class _GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    backend_pool: _BackendPool

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization,Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _proxy(self) -> None:
        if self.path == "/health":
            body = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        backend_base = self.backend_pool.next()
        upstream_url = self._join_backend_url(backend_base, self.path)
        body = self._read_body()
        headers = self._forward_headers(body)
        if body is not None and headers.get("Content-Type", "").startswith("application/json"):
            body = self._normalize_request_body(body)

        try:
            status, response_headers, response_body = self._perform_request(
                upstream_url, headers, body
            )
            self._relay_response(status, response_headers, response_body)
        except urllib.error.URLError as exc:
            self._relay_json_error(502, f"gateway backend unreachable: {exc.reason}")

    def _perform_request(
        self, upstream_url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, Any, bytes]:
        current_body = body
        for _ in range(3):
            request = urllib.request.Request(
                upstream_url,
                data=current_body,
                method=self.command,
                headers=headers,
            )
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    return response.status, response.headers, response.read()
            except urllib.error.HTTPError as exc:
                response_body = exc.read()
                context_error = _extract_context_error(response_body)
                if (
                    exc.code != 400
                    or current_body is None
                    or not headers.get("Content-Type", "").startswith("application/json")
                    or context_error is None
                ):
                    return exc.code, exc.headers, response_body

                requested_tokens, available_tokens = context_error
                compacted = _compact_payload(
                    json.loads(current_body.decode("utf-8")),
                    available_tokens,
                    requested_tokens,
                )
                compacted_body = json.dumps(compacted, separators=(",", ":")).encode("utf-8")
                if compacted_body == current_body:
                    return exc.code, exc.headers, response_body
                current_body = compacted_body

        raise RuntimeError("gateway request retry loop exhausted unexpectedly")

    def _read_body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return None
        return self.rfile.read(length)

    def _forward_headers(self, body: bytes | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            key_lower = key.lower()
            if key_lower in {"host", "content-length", "connection"}:
                continue
            headers[key] = value
        if body is not None and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        return headers

    def _normalize_request_body(self, body: bytes) -> bytes:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body
        normalized = _normalize_payload(payload, self.path)
        return json.dumps(normalized, separators=(",", ":")).encode("utf-8")

    def _join_backend_url(self, backend_base: str, path: str) -> str:
        base = backend_base.rstrip("/")
        if base.endswith("/v1") and path == "/v1":
            return base
        if base.endswith("/v1") and path.startswith("/v1/"):
            return base + path[len("/v1"):]
        return base + path

    def _relay_response(self, status: int, response_headers: Any, body: bytes) -> None:
        self.send_response(status)
        for key, value in response_headers.items():
            key_lower = key.lower()
            if key_lower in {"connection", "content-length", "transfer-encoding"}:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _relay_json_error(self, status: int, message: str) -> None:
        body = json.dumps(
            {"error": {"code": status, "message": message, "type": "gateway_error"}}
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Titan OpenAI-compatible model gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18011)
    parser.add_argument("--backend-url", action="append", dest="backend_urls")
    args = parser.parse_args()

    backend_urls = args.backend_urls or _default_backend_urls()
    parsed = [urllib.parse.urlparse(url) for url in backend_urls]
    for parsed_url in parsed:
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise SystemExit(f"invalid backend URL: {parsed_url.geturl()}")

    handler = type("GatewayHandler", (_GatewayHandler,), {})
    handler.backend_pool = _BackendPool(backend_urls)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(
        json.dumps(
            {
                "host": args.host,
                "port": args.port,
                "backend_urls": backend_urls,
            }
        ),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
