"""Owner-test Project Profile transport policy for current DeepSeek chat completion."""

from __future__ import annotations

from collections.abc import Mapping
import json

import httpx

from app.deepseek_live_profile_adapter import (
    CHAT_URL,
    _error,
    _normalize_profile_receipt,
    _profile_http_error,
)
from app.model_provider_contract import ProviderReceipt


def _client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=False,
        trust_env=False,
        verify=True,
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
    )


def _network_unknown_message(exc: httpx.RequestError) -> str:
    """Return a sanitized transport category while preserving at-most-once semantics."""
    if isinstance(exc, httpx.ConnectTimeout):
        return "连接 DeepSeek API 超时；本次请求结果无法确认，系统不会自动重发以避免重复扣费。"
    if isinstance(exc, httpx.ReadTimeout):
        return "等待 DeepSeek 返回结果超时；本次请求可能已被服务端处理，系统不会自动重发以避免重复扣费。"
    if isinstance(exc, httpx.WriteTimeout):
        return "向 DeepSeek 发送请求数据时超时；服务端是否完整收到请求无法确认，系统不会自动重发以避免重复扣费。"
    if isinstance(exc, httpx.PoolTimeout):
        return "本地 DeepSeek HTTP 连接资源等待超时；本次请求结果无法确认，系统不会自动重发以避免重复扣费。"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "DeepSeek 连接在返回完整响应前异常中断；本次请求结果无法确认，系统不会自动重发以避免重复扣费。"
    if isinstance(exc, httpx.ConnectError):
        return "无法稳定建立 DeepSeek HTTPS 连接；本次请求结果无法确认，系统不会自动重发以避免重复扣费。"
    return "DeepSeek 请求发生网络异常；本次请求结果无法确认，系统不会自动重发以避免重复扣费。"


def _profile_request_body(
    *, messages: tuple[Mapping[str, object], ...], max_tokens: int, model_id: str
) -> dict[str, object]:
    if type(max_tokens) is not int or max_tokens <= 0:
        raise _error(400, "PROFILE_GENERATION_PROVIDER_REQUEST_INVALID", "模型输出预算无效。")
    if type(model_id) is not str or not model_id:
        raise _error(400, "PROFILE_GENERATION_PROVIDER_REQUEST_INVALID", "模型身份无效。")
    materialized = []
    for item in messages:
        if not isinstance(item, Mapping):
            raise _error(400, "PROFILE_GENERATION_PROVIDER_REQUEST_INVALID", "模型消息结构无效。")
        materialized.append(dict(item))
    return {
        "model": model_id,
        "thinking": {"type": "disabled"},
        "messages": materialized,
        "response_format": {"type": "json_object"},
        "stream": False,
        "max_tokens": max_tokens,
    }


def profile_request_body_bytes(
    *, messages: tuple[Mapping[str, object], ...], max_tokens: int, model_id: str
) -> bytes:
    """Canonical body bytes used both for admission and the actual HTTP POST."""
    try:
        return json.dumps(
            _profile_request_body(messages=messages, max_tokens=max_tokens, model_id=model_id),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _error(400, "PROFILE_GENERATION_PROVIDER_REQUEST_INVALID", "模型请求无法安全序列化。") from exc


def estimate_profile_request_utf8_bytes(
    *, messages: tuple[Mapping[str, object], ...], max_tokens: int, model_id: str
) -> int:
    return len(profile_request_body_bytes(messages=messages, max_tokens=max_tokens, model_id=model_id))


def send_profile_with_api_key(
    *,
    messages: tuple[Mapping[str, object], ...],
    max_tokens: int,
    api_key: str,
    model_id: str,
) -> ProviderReceipt:
    """Use deterministic JSON-focused generation and preserve ambiguous-POST safety."""
    body = profile_request_body_bytes(messages=messages, max_tokens=max_tokens, model_id=model_id)
    try:
        with _client() as client:
            response = client.post(
                CHAT_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                content=body,
            )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise _error(
            502,
            "PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN",
            _network_unknown_message(exc),
        ) from exc
    if response.is_redirect or response.status_code != 200:
        raise _profile_http_error(response.status_code)
    return _normalize_profile_receipt(response, requested_model=model_id)
