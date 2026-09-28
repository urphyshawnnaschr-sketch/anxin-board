"""DeepSeek V4 Flash provider-specific transport.

This module owns only the fixed HTTP adapter and normalized receipt boundary. It does
not decide current model authority, Human send authorization, Gateway readiness, or
retry policy.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import HTTPException

from app.deepseek_credential import ReportCredentialError, read_report_credential
from app.deepseek_model_catalog import (
    REPORT_MAX_OUTPUT_TOKENS,
    REPORT_MODEL_ID,
    REPORT_MODEL_VERSION,
)


CAPABILITY_SCHEMA_VERSION = "deepseek_transport_capability_v1"
PROVIDER = "deepseek"
MODEL_ID = REPORT_MODEL_ID
MODEL_VERSION = REPORT_MODEL_VERSION
ENDPOINT_ORIGIN = "https://api.deepseek.com"
ENDPOINT_PATH = "/chat/completions"
ENDPOINT_URL = f"{ENDPOINT_ORIGIN}{ENDPOINT_PATH}"
TASK_TYPE = "daily_report_generate"
OUTPUT_SCHEMA_VERSION = "daily-report/1.0"
_REGENERATE_TASK_TYPE = "daily_report_regenerate"
_REGENERATE_OUTPUT_SCHEMA_VERSION = "daily-report-regenerate/1.0"
_CAPABILITY_IDENTITIES = (
    (TASK_TYPE, OUTPUT_SCHEMA_VERSION),
    (_REGENERATE_TASK_TYPE, _REGENERATE_OUTPUT_SCHEMA_VERSION),
)
_CAPABILITY_ARGUMENT_OMITTED = object()
MAX_OUTPUT_TOKENS = REPORT_MAX_OUTPUT_TOKENS
_RESPONSE_BYTE_CAP = 16 * 1024 * 1024


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _input_invalid(message: str = "DeepSeek transport 输入无效。") -> HTTPException:
    return _error(400, "DEEPSEEK_TRANSPORT_INPUT_INVALID", message)


def _credential_unavailable(reason: str | None = None) -> HTTPException:
    error = _error(
        503,
        "DEEPSEEK_TRANSPORT_CREDENTIAL_UNAVAILABLE",
        "DeepSeek 凭据不可用，请检查设置中已保存的凭据及当前 Windows 用户权限。",
    )
    if reason is not None:
        error.detail["credential_error_code"] = reason
    return error


def _network_error() -> HTTPException:
    return _error(502, "DEEPSEEK_TRANSPORT_NETWORK_ERROR", "DeepSeek transport network request 未完成。")


def _http_error() -> HTTPException:
    return _error(502, "DEEPSEEK_TRANSPORT_HTTP_ERROR", "DeepSeek transport 收到非成功 HTTP response。")


def _response_too_large() -> HTTPException:
    return _error(502, "DEEPSEEK_TRANSPORT_RESPONSE_TOO_LARGE", "DeepSeek response 超过冻结 byte cap。")


def _response_invalid(message: str = "DeepSeek response 无法完成结构化自校验。") -> HTTPException:
    return _error(502, "DEEPSEEK_TRANSPORT_RESPONSE_INVALID", message)


def _completion_incomplete() -> HTTPException:
    return _error(502, "DEEPSEEK_TRANSPORT_COMPLETION_INCOMPLETE", "DeepSeek completion 未以唯一 stop 结果闭合。")


def _build_capability(task_type: str, output_schema_version: str) -> dict[str, object]:
    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "provider": PROVIDER,
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "endpoint_origin": ENDPOINT_ORIGIN,
        "endpoint_path": ENDPOINT_PATH,
        "task_type": task_type,
        "output_schema_version": output_schema_version,
        "response_format": "json_object",
        "stream": False,
        "thinking": "enabled",
        "reasoning_effort": "high",
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }


def get_deepseek_transport_capabilities() -> tuple[dict[str, object], ...]:
    """Return the exact closed-world adapter capability set; performs zero I/O."""
    return tuple(
        _build_capability(task_type, output_schema_version)
        for task_type, output_schema_version in _CAPABILITY_IDENTITIES
    )


def get_deepseek_transport_capability(
    *,
    task_type: object = _CAPABILITY_ARGUMENT_OMITTED,
    output_schema_version: object = _CAPABILITY_ARGUMENT_OMITTED,
) -> dict[str, object]:
    """Return one exact adapter capability identity; performs zero I/O.

    The no-argument form remains the original daily generation capability. Both identity
    fields must otherwise be supplied explicitly; partial, malformed, unsupported, or
    cross-paired identities fail closed without credential or HTTP access.
    """
    task_type_omitted = task_type is _CAPABILITY_ARGUMENT_OMITTED
    output_schema_version_omitted = output_schema_version is _CAPABILITY_ARGUMENT_OMITTED
    if task_type_omitted and output_schema_version_omitted:
        task_type = TASK_TYPE
        output_schema_version = OUTPUT_SCHEMA_VERSION
    elif task_type_omitted or output_schema_version_omitted:
        raise _input_invalid("DeepSeek transport capability identity 必须完整提供 task_type 与 output_schema_version。")
    if type(task_type) is not str or not task_type.strip():
        raise _input_invalid("DeepSeek transport task_type capability identity 无效。")
    if type(output_schema_version) is not str or not output_schema_version.strip():
        raise _input_invalid("DeepSeek transport output_schema_version capability identity 无效。")
    identity = (task_type, output_schema_version)
    if identity not in _CAPABILITY_IDENTITIES:
        raise _input_invalid("DeepSeek transport capability identity 不受支持。")
    return _build_capability(task_type, output_schema_version)


def _validate_messages(messages: object) -> list[dict[str, Any]]:
    if type(messages) is not list or any(type(item) is not dict for item in messages):
        raise _input_invalid("messages 必须是 JSON-serializable object list。")
    encoding_failed = False
    try:
        json.dumps(messages, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeEncodeError):
        encoding_failed = True
    if encoding_failed:
        raise _input_invalid("messages 必须可安全编码为 JSON。")
    return messages


def _validate_max_tokens(max_tokens: object) -> int:
    if type(max_tokens) is not int or max_tokens <= 0 or max_tokens > MAX_OUTPUT_TOKENS:
        raise _input_invalid("max_tokens 必须是 1..384000 的严格整数。")
    return max_tokens


def _new_client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=False,
        trust_env=False,
        verify=True,
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
    )


def _read_bounded_response(response: httpx.Response) -> bytes:
    if response.is_redirect or 300 <= response.status_code < 400:
        raise _http_error()
    if response.status_code != 200:
        raise _http_error()

    content_length = response.headers.get("content-length")
    if content_length is not None:
        declared_invalid = False
        try:
            declared = int(content_length)
        except (TypeError, ValueError):
            declared_invalid = True
            declared = 0
        if declared_invalid:
            raise _response_invalid("DeepSeek response Content-Length 无效。")
        if declared < 0:
            raise _response_invalid("DeepSeek response Content-Length 无效。")
        if declared > _RESPONSE_BYTE_CAP:
            raise _response_too_large()

    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > _RESPONSE_BYTE_CAP:
            raise _response_too_large()
    if not body:
        raise _response_invalid("DeepSeek response body 为空。")
    return bytes(body)


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _strict_json_loads(value: str) -> object:
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_reject_duplicate_json_keys,
    )


def _non_empty_string(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _usage_value(usage: dict[str, object], field: str) -> int:
    value = usage.get(field)
    if type(value) is not int or value < 0:
        raise _response_invalid("DeepSeek usage token 字段无效。")
    return value


def _normalize_success_response(raw: bytes) -> dict[str, object]:
    decode_failed = False
    text = ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        decode_failed = True
    if decode_failed:
        raise _response_invalid("DeepSeek response 必须是 UTF-8 JSON object。")

    parse_failed = False
    payload: object = None
    try:
        payload = _strict_json_loads(text)
    except (json.JSONDecodeError, ValueError):
        parse_failed = True
    if parse_failed:
        raise _response_invalid("DeepSeek response 必须是严格 JSON object。")
    if type(payload) is not dict:
        raise _response_invalid("DeepSeek response 顶层必须是 object。")

    response_id = payload.get("id")
    actual_model = payload.get("model")
    runtime_fingerprint = payload.get("system_fingerprint")
    if not all(_non_empty_string(value) for value in (response_id, actual_model, runtime_fingerprint)):
        raise _response_invalid("DeepSeek response identity/runtime fingerprint 不完整。")

    choices = payload.get("choices")
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise _response_invalid("DeepSeek response 必须只有一个 choice。")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise _completion_incomplete()

    message = choice.get("message")
    if type(message) is not dict:
        raise _response_invalid("DeepSeek choice.message 无效。")
    if message.get("tool_calls") not in (None, []):
        raise _completion_incomplete()
    content = message.get("content")
    if not _non_empty_string(content):
        raise _response_invalid("DeepSeek message.content 必须是非空 JSON string。")

    content_parse_failed = False
    result: object = None
    try:
        result = _strict_json_loads(content)
    except (json.JSONDecodeError, ValueError):
        content_parse_failed = True
    if content_parse_failed:
        raise _response_invalid("DeepSeek message.content 不是严格 JSON。")
    if type(result) is not dict:
        raise _response_invalid("DeepSeek message.content 必须解析为 JSON object。")

    usage = payload.get("usage")
    if type(usage) is not dict:
        raise _response_invalid("DeepSeek usage 无效。")
    prompt_tokens = _usage_value(usage, "prompt_tokens")
    completion_tokens = _usage_value(usage, "completion_tokens")
    total_tokens = _usage_value(usage, "total_tokens")
    if total_tokens != prompt_tokens + completion_tokens:
        raise _response_invalid("DeepSeek usage total_tokens 无法闭合。")

    return {
        "provider": PROVIDER,
        "provider_response_id": response_id,
        "actual_model": actual_model,
        "provider_runtime_fingerprint": runtime_fingerprint,
        "finish_reason": "stop",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "result": result,
    }


def _send_with_key(
    *,
    messages: list[dict[str, Any]],
    max_tokens: int,
    api_key: str,
) -> dict[str, object]:
    request_body = {
        "model": MODEL_ID,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "stream": False,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    network_failed = False
    try:
        with _new_client() as client:
            with client.stream(
                "POST",
                ENDPOINT_URL,
                headers=headers,
                json=request_body,
            ) as response:
                raw = _read_bounded_response(response)
    except HTTPException:
        raise
    except (httpx.TimeoutException, httpx.RequestError):
        network_failed = True
    if network_failed:
        raise _network_error()

    return _normalize_success_response(raw)


def send_deepseek_v4_flash(*, messages: list[dict[str, Any]], max_tokens: int) -> dict[str, object]:
    """Send exactly one fixed DeepSeek request; never retries or grants authority."""
    validated_messages = _validate_messages(messages)
    validated_max_tokens = _validate_max_tokens(max_tokens)

    try:
        api_key = read_report_credential()
    except ReportCredentialError as exc:
        raise _credential_unavailable(exc.code) from None

    return _send_with_key(
        messages=validated_messages,
        max_tokens=validated_max_tokens,
        api_key=api_key,
    )


def send_deepseek_v4_flash_with_api_key(
    *,
    messages: list[dict[str, Any]],
    max_tokens: int,
    api_key: str,
) -> dict[str, object]:
    """Same single-request adapter using an explicitly supplied in-memory credential.

    This entry exists for the Windows Credential Manager product path. The secret is
    never copied into process environment state and is never returned in receipts or
    error details.
    """
    validated_messages = _validate_messages(messages)
    validated_max_tokens = _validate_max_tokens(max_tokens)
    if type(api_key) is not str or not api_key.strip():
        raise _credential_unavailable()
    return _send_with_key(
        messages=validated_messages,
        max_tokens=validated_max_tokens,
        api_key=api_key,
    )
