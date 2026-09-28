"""One-shot, spike-only DeepSeek Responses transport with an explicit JSON schema.

No credential lookup, retries, model substitution, or product transport changes.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json

import httpx
from fastapi import HTTPException

from app.deepseek_live_profile_adapter import (
    _bounded_body, _client, _error, _profile_http_error, _strict_json,
    build_live_deepseek_profile_adapter,
)
from app.model_provider_contract import ProviderCredentialRequest, ProviderReceipt


RESPONSES_URL = "https://api.deepseek.com/responses"
SUPPORTED_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})
_SAFETY_MARGIN_BYTES = 16_384
SAMPLING_TEMPERATURE = 0


def _invalid(suffix: str = "RESPONSE_INVALID", diagnostic: dict | None = None) -> HTTPException:
    error = _error(502, "PROFILE_GENERATION_RESPONSES_" + suffix, "Responses contract validation failed.")
    if diagnostic is not None:
        error.detail["diagnostic"] = diagnostic
    return error


def build_payload(*, messages, max_output_tokens: int, model_id: str) -> dict[str, object]:
    if type(model_id) is not str or model_id not in SUPPORTED_MODELS:
        raise _error(409, "PROFILE_GENERATION_RESPONSES_MODEL_UNSUPPORTED", "Selected model does not support this spike transport.")
    if type(max_output_tokens) is not int or max_output_tokens <= 0:
        raise _invalid("REQUEST_INVALID")
    if not isinstance(messages, (tuple, list)) or not messages:
        raise _invalid("REQUEST_INVALID")
    copied = []
    for message in messages:
        if not isinstance(message, Mapping) or set(message) != {"role", "content"} or message.get("role") not in ("system", "user") or type(message.get("content")) is not str:
            raise _invalid("REQUEST_INVALID")
        copied.append(dict(message))
    last_user = next((item for item in reversed(copied) if item["role"] == "user"), None)
    if last_user is None:
        raise _invalid("SCHEMA_INVALID")
    try:
        document = _strict_json(last_user["content"].encode("utf-8"))
    except (HTTPException, UnicodeError, RecursionError):
        raise _invalid("SCHEMA_INVALID") from None
    schema = document.get("required_output_schema") if isinstance(document, dict) else None
    if type(schema) is not dict or schema.get("type") != "object":
        raise _invalid("SCHEMA_INVALID")
    return {
        "model": model_id,
        "input": copied,
        "reasoning": {"effort": "none"},
        "temperature": SAMPLING_TEMPERATURE,
        "max_output_tokens": max_output_tokens,
        "stream": False,
        "text": {"format": {"type": "json_schema", "name": "brownfield_atlas", "schema": deepcopy(schema)}},
    }


def _wire_bytes(*, messages, max_output_tokens: int, model_id: str) -> bytes:
    payload = build_payload(messages=messages, max_output_tokens=max_output_tokens, model_id=model_id)
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _invalid("REQUEST_INVALID") from None


def estimate_request_bytes(*, messages, max_output_tokens: int, model_id: str) -> int:
    return len(_wire_bytes(messages=messages, max_output_tokens=max_output_tokens, model_id=model_id))


def _receipt(response: httpx.Response, model_id: str, max_output_tokens: int | None = None) -> ProviderReceipt:
    try:
        payload = _strict_json(_bounded_body(response))
    except (HTTPException, RecursionError):
        raise _invalid("ENVELOPE_INVALID") from None
    if type(payload) is not dict or payload.get("object") != "response":
        raise _invalid("ENVELOPE_INVALID")
    if payload.get("status") != "completed" or payload.get("error") is not None or payload.get("incomplete_details") is not None:
        raise _invalid("NOT_COMPLETED")
    if payload.get("model") != model_id:
        raise _error(502, "PROFILE_GENERATION_MODEL_IDENTITY_MISMATCH", "Response model differs from selected model.")
    identity = payload.get("id")
    if type(identity) is not str or not identity.strip():
        raise _invalid("ID_INVALID")
    output = payload.get("output")
    if type(output) is not list or not output:
        raise _invalid("CONTENT_INVALID")
    chunks = []
    for item in output:
        if type(item) is not dict or item.get("type") != "message" or item.get("role") != "assistant" or item.get("status") != "completed":
            raise _invalid("CONTENT_INVALID")
        content = item.get("content")
        if type(content) is not list or not content:
            raise _invalid("CONTENT_INVALID")
        for part in content:
            if type(part) is not dict or part.get("type") != "output_text" or type(part.get("text")) is not str:
                raise _invalid("CONTENT_INVALID")
            chunks.append(part["text"])
    usage = payload.get("usage")
    values = [usage.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")] if type(usage) is dict else []
    usage_valid = len(values) == 3 and all(type(value) is int and value >= 0 for value in values) and values[0] + values[1] == values[2]
    diagnostic = {}

    def add_integer(name, value):
        if type(value) is int and 0 <= value <= 2**31 - 1:
            diagnostic[name] = value

    add_integer("chunk_count", len(chunks))
    if usage_valid:
        add_integer("output_tokens", values[1])
        if type(max_output_tokens) is int and max_output_tokens > 0:
            diagnostic["output_limit_reached"] = values[1] >= max_output_tokens
    text = "".join(chunks)
    try:
        raw = text.encode("utf-8")
        add_integer("content_utf8_bytes", len(raw))
        result = _strict_json(raw)
    except HTTPException as exc:
        cause = exc.__cause__
        suffix = "CONTENT_JSON_INVALID"
        if isinstance(cause, json.JSONDecodeError):
            suffix = "CONTENT_JSON_SYNTAX_INVALID"
            add_integer("json_error_line", cause.lineno)
            add_integer("json_error_column", cause.colno)
            add_integer("json_error_offset", cause.pos)
        elif type(cause) is ValueError:
            if cause.args == ("duplicate JSON key",):
                suffix = "CONTENT_JSON_DUPLICATE_KEY_INVALID"
            elif cause.args == ("non-standard JSON constant",):
                suffix = "CONTENT_JSON_NONFINITE_INVALID"
        if text.lstrip().startswith("```"):
            suffix = "CONTENT_JSON_FENCE_INVALID"
        raise _invalid(suffix, diagnostic) from None
    except UnicodeError:
        raise _invalid("CONTENT_UTF8_INVALID", diagnostic) from None
    except RecursionError:
        raise _invalid("CONTENT_JSON_RECURSION_INVALID", diagnostic) from None
    if type(result) is not dict:
        raise _invalid("CONTENT_NOT_OBJECT", diagnostic)
    if not usage_valid:
        raise _invalid("USAGE_INVALID")
    return ProviderReceipt("deepseek", identity, model_id, "not-provided-by-provider", "stop", *values, result)


class BrownfieldResponsesAdapter:
    provider_id = "deepseek"
    estimate_request_bytes = staticmethod(estimate_request_bytes)

    def __init__(self):
        self._selected = build_live_deepseek_profile_adapter()

    def get_capability(self, *, task_type: str, output_schema_version: str):
        capability = self._selected.get_capability(task_type=task_type, output_schema_version=output_schema_version)
        if capability.provider != "deepseek" or capability.model_id not in SUPPORTED_MODELS:
            raise _error(409, "PROFILE_GENERATION_RESPONSES_MODEL_UNSUPPORTED", "Selected model does not support this spike transport.")
        return capability

    def execute_with_credential(self, request: ProviderCredentialRequest, credential: str) -> ProviderReceipt:
        capability = self.get_capability(task_type=request.task_type, output_schema_version=request.output_schema_version)
        if (request.provider, request.model_id, request.model_version) != (capability.provider, capability.model_id, capability.model_version):
            raise _error(409, "PROFILE_GENERATION_MODEL_SELECTION_STALE", "Selected model changed before dispatch.")
        wire = _wire_bytes(messages=request.messages, max_output_tokens=request.max_output_tokens, model_id=request.model_id)
        if request.max_output_tokens > capability.max_output_tokens or len(wire) > capability.context_window_tokens - request.max_output_tokens - _SAFETY_MARGIN_BYTES:
            raise _invalid("REQUEST_OVER_BUDGET")
        if type(credential) is not str or not credential.strip():
            raise _error(409, "MODEL_CREDENTIAL_REQUIRED", "Provider credential required.")
        try:
            with _client() as client:
                response = client.post(RESPONSES_URL, content=wire, headers={"Authorization": "Bearer " + credential, "Content-Type": "application/json", "Accept": "application/json"})
        except (httpx.TimeoutException, httpx.RequestError):
            raise _error(502, "PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN", "Provider send outcome is unknown; no automatic retry.") from None
        if response.is_redirect or response.status_code != 200:
            raise _profile_http_error(response.status_code)
        return _receipt(response, request.model_id, request.max_output_tokens)


def build_brownfield_responses_adapter() -> BrownfieldResponsesAdapter:
    return BrownfieldResponsesAdapter()
