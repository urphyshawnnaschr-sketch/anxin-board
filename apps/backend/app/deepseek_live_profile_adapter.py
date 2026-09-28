"""Owner real-world DeepSeek model discovery and Project Profile transport.

This module is intentionally narrow.  It adds a live /models preflight and a selected-model
Project Profile adapter without changing the frozen formal-report provider/qualification
registry.  API keys are accepted only in memory and are never logged or returned.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Callable

import httpx
from fastapi import HTTPException

from app.deepseek_extended_provider_adapter import build_deepseek_extended_provider_adapter
from app.model_provider_contract import (
    ProviderCapability,
    ProviderCredentialRequest,
    ProviderCurrentAuthority,
    ProviderReceipt,
    ProviderRequest,
)
from app.profile_response_errors import profile_response_error
from app.secret_store import (
    SecretAccessDeniedError,
    SecretNotFoundError,
    SecretStoreOSError,
    UnsupportedSecretStorePlatformError,
)
from app.windows_credential_store import WindowsCredentialStore


PROVIDER = "deepseek"
MODELS_URL = "https://api.deepseek.com/models"
CHAT_URL = "https://api.deepseek.com/chat/completions"
MODEL_SELECTION_REF = "deepseek-model-id"
PROFILE_TASK = "project_profile_build"
PROFILE_SCHEMA = "project-profile-build/1.0"
PROFILE_MAX_OUTPUT_TOKENS = 24_000
_CONTEXT_WINDOW_TOKENS = 1_000_000
_RESPONSE_BYTE_CAP = 16 * 1024 * 1024
_MODEL_ID_RE = re.compile(r"^deepseek-[A-Za-z0-9._-]{1,80}$", re.ASCII)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _client() -> httpx.Client:
    # Preserve the existing product security boundary: do not inherit arbitrary HTTP(S)
    # proxy environment variables that could receive the user's provider credential.
    return httpx.Client(
        follow_redirects=False,
        trust_env=False,
        verify=True,
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
    )


def _bounded_body(response: httpx.Response) -> bytes:
    content = response.content
    if not content or len(content) > _RESPONSE_BYTE_CAP:
        raise _error(502, "MODEL_CONNECTION_RESPONSE_INVALID", "DeepSeek 返回内容为空或超过安全上限。")
    return content


def _strict_json(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _error(502, "MODEL_CONNECTION_RESPONSE_INVALID", "DeepSeek 返回内容不是有效 UTF-8。") from exc

    def reject_constant(_value: str) -> None:
        raise ValueError("non-standard JSON constant")

    def reject_duplicate(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(text, parse_constant=reject_constant, object_pairs_hook=reject_duplicate)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _error(502, "MODEL_CONNECTION_RESPONSE_INVALID", "DeepSeek 返回内容不是严格 JSON。") from exc


def _connection_http_error(status: int) -> HTTPException:
    if status == 401:
        return _error(401, "MODEL_CONNECTION_AUTH_FAILED", "DeepSeek API Key 无效或已失效（401）。")
    if status == 403:
        return _error(403, "MODEL_CONNECTION_FORBIDDEN", "DeepSeek 拒绝当前账号访问（403）。")
    if status == 429:
        return _error(429, "MODEL_CONNECTION_RATE_LIMITED", "DeepSeek 当前限流（429），请稍后再测试。")
    if status >= 500:
        return _error(502, "MODEL_CONNECTION_PROVIDER_UNAVAILABLE", f"DeepSeek 服务暂时不可用（HTTP {status}）。")
    return _error(502, "MODEL_CONNECTION_HTTP_FAILED", f"DeepSeek 连接测试失败（HTTP {status}）。")


def list_deepseek_models_with_api_key(api_key: str) -> tuple[str, ...]:
    """Read current account model IDs through the provider's read-only /models endpoint."""
    if type(api_key) is not str or not api_key.strip():
        raise _error(409, "MODEL_CREDENTIAL_REQUIRED", "请先保存 DeepSeek API Key。")
    try:
        with _client() as client:
            response = client.get(
                MODELS_URL,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise _error(
            502,
            "MODEL_CONNECTION_NETWORK_ERROR",
            "无法完成 DeepSeek HTTPS 连接测试；请检查本机网络、代理或 TLS 环境。",
        ) from exc
    if response.is_redirect or response.status_code != 200:
        raise _connection_http_error(response.status_code)
    payload = _strict_json(_bounded_body(response))
    if type(payload) is not dict or type(payload.get("data")) is not list:
        raise _error(502, "MODEL_CONNECTION_RESPONSE_INVALID", "DeepSeek /models 返回结构无法识别。")
    model_ids: list[str] = []
    for item in payload["data"]:
        if type(item) is not dict:
            continue
        model_id = item.get("id")
        if type(model_id) is str and _MODEL_ID_RE.fullmatch(model_id) and model_id not in model_ids:
            model_ids.append(model_id)
    if not model_ids:
        raise _error(409, "MODEL_CONNECTION_NO_SUPPORTED_MODELS", "当前 DeepSeek 账号没有返回可选择的模型。")
    return tuple(model_ids)


def profile_http_diagnostic(status):
    """Only bounded status and fixed categories; never provider body/headers."""
    if type(status) is not int or not 100 <= status <= 599:
        return {}
    category = {400: 'request', 401: 'auth', 402: 'payment', 403: 'forbidden',
                404: 'model_unavailable', 422: 'request', 429: 'rate'}.get(status,
                'service' if status >= 500 else 'other')
    return {'provider_http_status': status, 'provider_http_class': category}


def _profile_http_error(status: int) -> HTTPException:
    diagnostic = profile_http_diagnostic(status)
    if not diagnostic:
        return _error(502, "PROFILE_GENERATION_PROVIDER_HTTP_FAILED", "DeepSeek 返回无法识别的 HTTP 状态；本次未形成候选。")
    errors = {
        400: (400, "REQUEST_REJECTED", "DeepSeek 拒绝本次请求参数（400）。"),
        401: (401, "AUTH_FAILED", "DeepSeek API Key 已失效（401）；请重新保存并测试模型配置。"),
        402: (402, "PAYMENT_REQUIRED", "DeepSeek 账户余额不足（402）；请检查账户余额。"),
        403: (403, "FORBIDDEN", "DeepSeek 拒绝当前模型调用（403）。"),
        404: (409, "MODEL_UNAVAILABLE", "当前选择的 DeepSeek 模型已不可用（404）。"),
        422: (422, "REQUEST_REJECTED", "DeepSeek 请求参数无效（422）。"),
        429: (429, "RATE_LIMITED", "DeepSeek 当前限流（429）；本次未形成候选，不会自动重试。"),
    }
    http_status, suffix, message = errors.get(status, (502,
        'UNAVAILABLE' if status >= 500 else 'HTTP_FAILED',
        f"DeepSeek 返回 HTTP {status}；本次未形成候选。"))
    error = _error(http_status, 'PROFILE_GENERATION_PROVIDER_' + suffix, message)
    error.detail['diagnostic'] = diagnostic
    return error


def _normalize_profile_receipt(response: httpx.Response, *, requested_model: str) -> ProviderReceipt:
    try:
        payload = _strict_json(_bounded_body(response))
    except (HTTPException, RecursionError):
        raise profile_response_error("ENVELOPE_INVALID") from None
    if type(payload) is not dict:
        raise profile_response_error("ENVELOPE_INVALID")
    response_id = payload.get("id")
    actual_model = payload.get("model")
    if type(response_id) is not str or not response_id.strip():
        raise profile_response_error("ID_INVALID")
    if actual_model != requested_model:
        raise _error(502, "PROFILE_GENERATION_MODEL_IDENTITY_MISMATCH", "DeepSeek 返回模型与本次选择的模型不一致。")
    choices = payload.get("choices")
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise profile_response_error("CHOICES_INVALID")
    choice = choices[0]
    if choice.get("finish_reason") == "length":
        raise profile_response_error("OUTPUT_TRUNCATED")
    if choice.get("finish_reason") != "stop":
        raise profile_response_error("FINISH_INVALID")
    message = choice.get("message")
    if type(message) is not dict or type(message.get("content")) is not str or not message["content"].strip():
        raise profile_response_error("CONTENT_MISSING")
    try:
        result = _strict_json(message["content"].encode("utf-8"))
    except (HTTPException, UnicodeEncodeError, RecursionError):
        raise profile_response_error("CONTENT_JSON_INVALID") from None
    if type(result) is not dict:
        raise profile_response_error("CONTENT_NOT_OBJECT")
    usage = payload.get("usage")
    if type(usage) is not dict:
        raise profile_response_error("USAGE_MISSING")
    values: dict[str, int] = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(field)
        if type(value) is not int or value < 0:
            raise profile_response_error("USAGE_INVALID")
        values[field] = value
    if values["total_tokens"] != values["prompt_tokens"] + values["completion_tokens"]:
        raise profile_response_error("USAGE_MISMATCH")
    fingerprint = payload.get("system_fingerprint")
    if type(fingerprint) is not str or not fingerprint.strip():
        fingerprint = "not-provided-by-provider"
    return ProviderReceipt(
        provider=PROVIDER,
        provider_response_id=response_id,
        actual_model=actual_model,
        provider_runtime_fingerprint=fingerprint,
        finish_reason="stop",
        prompt_tokens=values["prompt_tokens"],
        completion_tokens=values["completion_tokens"],
        total_tokens=values["total_tokens"],
        result=result,
    )


def send_profile_with_api_key(
    *, messages: tuple[Mapping[str, object], ...], max_tokens: int, api_key: str, model_id: str
) -> ProviderReceipt:
    """Delegate the real profile POST to the narrow owner-test transport policy.

    The import is intentionally local: the policy reuses the strict response helpers in
    this module, so importing it only after this module has initialized avoids a circular
    import while making the production adapter path exercise the exact reviewed policy.
    """

    from app.deepseek_profile_transport_policy import send_profile_with_api_key as send_with_policy

    if type(model_id) is not str or _MODEL_ID_RE.fullmatch(model_id) is None:
        raise _error(409, "PROFILE_GENERATION_MODEL_SELECTION_REQUIRED", "请先测试 DeepSeek 连接并选择一个当前可用模型。")
    return send_with_policy(
        messages=messages,
        max_tokens=max_tokens,
        api_key=api_key,
        model_id=model_id,
    )


class LiveDeepSeekProfileAdapter:
    """Dynamic selected-model adapter used only by the pre-formal Project Profile lane."""

    def __init__(self, store_factory: Callable[[], object] = WindowsCredentialStore) -> None:
        self._store_factory = store_factory
        self._base = build_deepseek_extended_provider_adapter()

    @property
    def provider_id(self) -> str:
        return PROVIDER

    def _selected_model(self) -> str:
        try:
            value = self._store_factory().get(MODEL_SELECTION_REF)
        except SecretNotFoundError as exc:
            raise _error(409, "PROFILE_GENERATION_MODEL_SELECTION_REQUIRED", "请先在“模型设置”测试 DeepSeek 连接并选择一个当前可用模型。") from exc
        except (UnsupportedSecretStorePlatformError, SecretAccessDeniedError, SecretStoreOSError) as exc:
            raise _error(409, "PROFILE_GENERATION_MODEL_SELECTION_REQUIRED", "无法读取当前模型选择，请回到模型设置重新测试。") from exc
        if type(value) is not str or _MODEL_ID_RE.fullmatch(value) is None:
            raise _error(409, "PROFILE_GENERATION_MODEL_SELECTION_REQUIRED", "当前模型选择无效，请重新测试 DeepSeek 连接。")
        return value

    def get_capability(self, *, task_type: str, output_schema_version: str) -> ProviderCapability:
        if (task_type, output_schema_version) not in {(PROFILE_TASK, PROFILE_SCHEMA), (PROFILE_TASK, "project-profile-build/2.0")}:
            return self._base.get_capability(task_type=task_type, output_schema_version=output_schema_version)
        model_id = self._selected_model()
        return ProviderCapability(
            provider=PROVIDER,
            model_id=model_id,
            model_version=model_id,
            task_type=PROFILE_TASK,
            output_schema_version=output_schema_version,
            context_window_tokens=_CONTEXT_WINDOW_TOKENS,
            max_output_tokens=PROFILE_MAX_OUTPUT_TOKENS,
        )

    def estimate_request_utf8_bytes(self, *, messages: tuple[Mapping[str, object], ...], max_output_tokens: int) -> int:
        return self._base.estimate_request_utf8_bytes(messages=messages, max_output_tokens=max_output_tokens)

    def resolve_current_authority(
        self, *, model_call_id: int, final_context_manifest_hash: str, framed_payload_hash: str,
        task_type: str, request_envelope_hash: str | None = None,
        prompt_contract_hash: str | None = None, sampling_parameters_hash: str | None = None,
    ) -> ProviderCurrentAuthority:
        return self._base.resolve_current_authority(
            model_call_id=model_call_id,
            final_context_manifest_hash=final_context_manifest_hash,
            framed_payload_hash=framed_payload_hash,
            task_type=task_type,
            request_envelope_hash=request_envelope_hash,
            prompt_contract_hash=prompt_contract_hash,
            sampling_parameters_hash=sampling_parameters_hash,
        )

    def execute(self, request: ProviderRequest) -> ProviderReceipt:
        return self._base.execute(request)

    def execute_with_credential(self, request: ProviderCredentialRequest, credential: str) -> ProviderReceipt:
        if (request.task_type, request.output_schema_version) not in {(PROFILE_TASK, PROFILE_SCHEMA), (PROFILE_TASK, "project-profile-build/2.0")}:
            return self._base.execute_with_credential(request, credential)
        selected = self._selected_model()
        if request.provider != PROVIDER or request.model_id != selected or request.model_version != selected:
            raise _error(409, "PROFILE_GENERATION_MODEL_SELECTION_STALE", "生成前模型选择已变化，请重新发起本次生成。")
        try:
            available = list_deepseek_models_with_api_key(credential)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, Mapping) else {}
            code = str(detail.get("code") or "")
            message = str(detail.get("message") or "DeepSeek 模型预检失败。")
            mapping = {
                "MODEL_CONNECTION_AUTH_FAILED": "PROFILE_GENERATION_MODEL_PREFLIGHT_AUTH_FAILED",
                "MODEL_CONNECTION_FORBIDDEN": "PROFILE_GENERATION_MODEL_PREFLIGHT_FORBIDDEN",
                "MODEL_CONNECTION_RATE_LIMITED": "PROFILE_GENERATION_MODEL_PREFLIGHT_RATE_LIMITED",
                "MODEL_CONNECTION_NETWORK_ERROR": "PROFILE_GENERATION_MODEL_PREFLIGHT_NETWORK_ERROR",
                "MODEL_CONNECTION_PROVIDER_UNAVAILABLE": "PROFILE_GENERATION_MODEL_PREFLIGHT_PROVIDER_UNAVAILABLE",
                "MODEL_CONNECTION_HTTP_FAILED": "PROFILE_GENERATION_MODEL_PREFLIGHT_HTTP_FAILED",
                "MODEL_CONNECTION_RESPONSE_INVALID": "PROFILE_GENERATION_MODEL_PREFLIGHT_RESPONSE_INVALID",
                "MODEL_CONNECTION_NO_SUPPORTED_MODELS": "PROFILE_GENERATION_MODEL_PREFLIGHT_NO_MODELS",
            }
            raise _error(exc.status_code, mapping.get(code, "PROFILE_GENERATION_MODEL_PREFLIGHT_FAILED"), message) from exc
        if selected not in available:
            raise _error(409, "PROFILE_GENERATION_MODEL_SELECTION_STALE", "当前选择的模型已不在 DeepSeek /models 列表中，请重新测试并选择。")
        return send_profile_with_api_key(
            messages=request.messages,
            max_tokens=request.max_output_tokens,
            api_key=credential,
            model_id=selected,
        )


def build_live_deepseek_profile_adapter() -> LiveDeepSeekProfileAdapter:
    return LiveDeepSeekProfileAdapter()
