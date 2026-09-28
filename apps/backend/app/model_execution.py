"""Provider-agnostic model execution orchestration.

Owns the generic trusted side-effect sequence only:
ready Provider Gateway -> exact transient request -> one process send claim -> exact
adapter transport -> one Result Ledger call. It never retries or falls back to another
provider.
"""

from __future__ import annotations

from app.context_candidate_runtime import candidate_materialization_scope

from collections.abc import Callable, Mapping
import re
import logging
from threading import Lock

from fastapi import HTTPException

from app import model_execution_results, model_provider_gateway, model_provider_runtime
from app.model_provider_contract import ProviderReceipt, ProviderRequest


_logger = logging.getLogger(__name__)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEND_CLAIM_LOCK = Lock()
_SEND_CLAIMED_MODEL_CALL_IDS: set[int] = set()

_PREFLIGHT_KEYS = {
    "schema_version",
    "model_call_id",
    "call_identity_hash",
    "final_context_manifest_hash",
    "provider",
    "model_id",
    "model_version",
    "task_type",
    "output_schema_version",
    "current_qualification_authority_state",
    "current_qualification_authority_ref",
    "current_qualification_evidence_hash",
    "current_authorization_authority_state",
    "current_authorization_authority_ref",
    "current_authorization_evidence_hash",
    "current_authorization_data_scope_hash",
    "current_authorization_purpose_id",
    "framed_payload_hash",
    "framed_payload_utf8_bytes",
    "request_envelope_hash",
    "final_request_fit_state",
    "provider_compatibility_state",
    "local_gateway_state",
    "network_send_state",
    "gateway_preflight_hash",
}
_TRANSIENT_KEYS = {
    "schema_version",
    "model_call_id",
    "call_identity_hash",
    "final_context_manifest_hash",
    "provider",
    "model_id",
    "model_version",
    "task_type",
    "output_schema_version",
    "framed_payload_hash",
    "framed_payload_utf8_bytes",
    "request_envelope_hash",
    "conservative_local_request_upper_bound",
    "max_tokens",
    "messages",
}
_REBOUND_FIELDS = (
    "model_call_id",
    "call_identity_hash",
    "final_context_manifest_hash",
    "provider",
    "model_id",
    "model_version",
    "task_type",
    "output_schema_version",
    "framed_payload_hash",
    "framed_payload_utf8_bytes",
    "request_envelope_hash",
)


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _non_empty(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _require_input(
    *, model_call_id: object, budget_record: object
) -> tuple[int, Mapping[str, object]]:
    if type(model_call_id) is not int or model_call_id <= 0:
        raise _error("MODEL_EXECUTION_INPUT_INVALID", "model_call_id 必须是正整数。")
    if not isinstance(budget_record, Mapping):
        raise _error("MODEL_EXECUTION_INPUT_INVALID", "budget_record 必须是只读可映射对象。")
    return model_call_id, budget_record


def _require_ready_preflight(
    value: object, *, model_call_id: int
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != _PREFLIGHT_KEYS:
        raise _error("MODEL_EXECUTION_PREFLIGHT_INVALID", "Gateway Preflight shape 无法闭合。")
    exact = {
        "schema_version": "model_gateway_preflight_v1",
        "model_call_id": model_call_id,
        "current_qualification_authority_state": "verified",
        "current_authorization_authority_state": "verified",
        "final_request_fit_state": "fit_by_conservative_upper_bound",
        "provider_compatibility_state": "compatible",
        "local_gateway_state": "ready_for_provider_transport",
        "network_send_state": "not_attempted",
    }
    if any(value.get(field) != expected for field, expected in exact.items()):
        reasons = {
            "blocked_final_manifest": ("context_manifest", "发送资料清单未通过完整性或安全检查；请重新准备并检查资料范围。"),
            "blocked_provider_compatibility": ("provider_compatibility", "所选模型不支持当前任务或输出格式；请检查模型配置。"),
            "blocked_request_budget": ("request_budget", "本次请求超过当前模型预算；请检查分批计划及输出预留。"),
            "blocked_current_authority_unavailable": ("current_authority", "无法读取当前模型资格或授权依据；请检查模型设置和网络。"),
            "blocked_current_qualification": ("qualification", "当前模型资格验证未通过；请检查模型配置。"),
            "blocked_current_authorization": ("authorization", "本次发送授权尚未生效或资料范围已变化；请重新确认发送范围。"),
        }
        state = value.get("local_gateway_state")
        stage, message = reasons.get(
            state if isinstance(state, str) else "",
            ("preflight_identity", "发送前校验的任务身份或状态不一致；已阻止发送，请刷新并反馈此错误码。"),
        )
        error = _error("MODEL_EXECUTION_GATEWAY_NOT_READY", message)
        error.detail.update(stage=stage, cause_code="MODEL_EXECUTION_" + stage.upper())
        _logger.warning("Model pre-send check blocked: code=MODEL_EXECUTION_GATEWAY_NOT_READY stage=%s", stage)
        raise error
    for field in ("provider", "model_id", "model_version", "task_type", "output_schema_version"):
        if not _non_empty(value.get(field)):
            raise _error("MODEL_EXECUTION_PREFLIGHT_INVALID", "Gateway provider/model/task identity 不完整。")
    for field in (
        "call_identity_hash",
        "final_context_manifest_hash",
        "current_qualification_evidence_hash",
        "current_authorization_evidence_hash",
        "current_authorization_data_scope_hash",
        "framed_payload_hash",
        "request_envelope_hash",
        "gateway_preflight_hash",
    ):
        if not _is_sha256(value.get(field)):
            raise _error("MODEL_EXECUTION_PREFLIGHT_INVALID", "Gateway Preflight identity/hash 无法闭合。")
    for field in ("current_qualification_authority_ref", "current_authorization_authority_ref"):
        if value.get(field) in (None, "", {}, []):
            raise _error("MODEL_EXECUTION_PREFLIGHT_INVALID", "Gateway Preflight authority ref 不完整。")
    if not _non_empty(value.get("current_authorization_purpose_id")):
        raise _error("MODEL_EXECUTION_PREFLIGHT_INVALID", "Gateway authorization purpose 不完整。")
    payload_bytes = value.get("framed_payload_utf8_bytes")
    if type(payload_bytes) is not int or payload_bytes < 0:
        raise _error("MODEL_EXECUTION_PREFLIGHT_INVALID", "Gateway payload byte identity 无效。")
    return value


def _require_transient(
    value: object, *, preflight: Mapping[str, object]
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != _TRANSIENT_KEYS:
        raise _error("MODEL_EXECUTION_TRANSIENT_INVALID", "Gateway transient request shape 无法闭合。")
    if value.get("schema_version") != "model_gateway_transient_request_v1":
        raise _error("MODEL_EXECUTION_TRANSIENT_INVALID", "Gateway transient request schema 无效。")
    if any(value.get(field) != preflight.get(field) for field in _REBOUND_FIELDS):
        raise _error("MODEL_EXECUTION_TRANSIENT_MISMATCH", "Gateway transient 与 ready Preflight identity/hash 不一致。")
    max_tokens = value.get("max_tokens")
    messages = value.get("messages")
    if type(max_tokens) is not int or max_tokens <= 0 or type(messages) is not list:
        raise _error("MODEL_EXECUTION_TRANSIENT_INVALID", "Gateway transient provider material 无效。")
    if any(not isinstance(item, Mapping) for item in messages):
        raise _error("MODEL_EXECUTION_TRANSIENT_INVALID", "Gateway transient messages shape 无效。")
    return value


def _claim_send_once(model_call_id: int) -> None:
    with _SEND_CLAIM_LOCK:
        if model_call_id in _SEND_CLAIMED_MODEL_CALL_IDS:
            raise _error(
                "MODEL_EXECUTION_REPLAY_BLOCKED",
                "该 model_call_id 已消费当前进程的一次性 provider send 权利。",
            )
        _SEND_CLAIMED_MODEL_CALL_IDS.add(model_call_id)



def _release_send_claim_before_transport(model_call_id: int) -> None:
    """Release only an in-process claim when provider transport provably never started."""
    with _SEND_CLAIM_LOCK:
        _SEND_CLAIMED_MODEL_CALL_IDS.discard(model_call_id)


def _provider_request(transient: Mapping[str, object]) -> ProviderRequest:
    messages = transient["messages"]
    if type(messages) is not list:
        raise _error("MODEL_EXECUTION_TRANSIENT_INVALID", "Gateway transient messages 无效。")
    return ProviderRequest(
        model_call_id=int(transient["model_call_id"]),
        call_identity_hash=str(transient["call_identity_hash"]),
        provider=str(transient["provider"]),
        model_id=str(transient["model_id"]),
        model_version=str(transient["model_version"]),
        task_type=str(transient["task_type"]),
        output_schema_version=str(transient["output_schema_version"]),
        messages=tuple(dict(item) for item in messages),
        max_output_tokens=int(transient["max_tokens"]),
    )


def _receipt_mapping(receipt: object) -> dict[str, object]:
    if not isinstance(receipt, ProviderReceipt):
        raise _error(
            "MODEL_EXECUTION_RECEIPT_INVALID",
            "Provider adapter 未返回规范化 ProviderReceipt；禁止写入 Result Ledger。",
        )
    return {
        "provider": receipt.provider,
        "provider_response_id": receipt.provider_response_id,
        "actual_model": receipt.actual_model,
        "provider_runtime_fingerprint": receipt.provider_runtime_fingerprint,
        "finish_reason": receipt.finish_reason,
        "prompt_tokens": receipt.prompt_tokens,
        "completion_tokens": receipt.completion_tokens,
        "total_tokens": receipt.total_tokens,
        "result": dict(receipt.result),
    }


@candidate_materialization_scope()
def build_ready_model_execution_preflight(
    *, model_call_id: int, budget_record: Mapping[str, object]
) -> dict[str, object]:
    """Resolve exact generic Gateway readiness without consuming transport material."""
    model_call_id, budget_record = _require_input(
        model_call_id=model_call_id, budget_record=budget_record
    )
    return dict(
        _require_ready_preflight(
            model_provider_gateway.build_model_provider_gateway_preflight(
                model_call_id=model_call_id,
                budget_record=budget_record,
                raise_authority_diagnostics=True,
            ),
            model_call_id=model_call_id,
        )
    )


def execute_model_call(
    *,
    model_call_id: int,
    budget_record: Mapping[str, object],
    before_provider_send: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Execute exactly one ready provider call; never retry or fallback.

    All Gateway/provider-resolution/transient-materialization failures happen before the
    optional durable send-boundary callback. Once that callback succeeds, provider
    transport may have happened; callers must therefore use UNKNOWN only from that point.
    """
    preflight = build_ready_model_execution_preflight(
        model_call_id=model_call_id,
        budget_record=budget_record,
    )
    adapter = model_provider_runtime.resolve_model_provider_adapter(preflight["provider"])
    transient = _require_transient(
        model_provider_gateway.materialize_model_provider_gateway_request_transient(
            model_call_id=model_call_id,
            budget_record=budget_record,
        ),
        preflight=preflight,
    )
    _claim_send_once(model_call_id)
    try:
        if before_provider_send is not None:
            before_provider_send()
    except Exception:
        _release_send_claim_before_transport(model_call_id)
        raise
    receipt = adapter.execute(_provider_request(transient))
    return model_execution_results.record_model_execution_result(
        model_call_id=model_call_id,
        receipt=_receipt_mapping(receipt),
    )
