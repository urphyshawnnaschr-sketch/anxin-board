"""DeepSeek execution orchestration V1.

Owns only the trusted side-effect sequence:
ready Gateway -> exact transient request -> one Transport call -> one Result Ledger call.

It deliberately does not reimplement Gateway authority, provider HTTP behavior,
AI-contract validation, persistence, credential handling, or retry policy.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from threading import Lock

from fastapi import HTTPException

from app import deepseek_transport, model_execution_results, model_gateway
from app.deepseek_model_catalog import REPORT_MODEL_ID, REPORT_MODEL_VERSION


_PROVIDER = "deepseek"
_MODEL_ID = REPORT_MODEL_ID
_MODEL_VERSION = REPORT_MODEL_VERSION
_TASK_TYPE = "daily_report_generate"
_OUTPUT_SCHEMA_VERSION = "daily-report/1.0"
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


def _require_input(
    *,
    model_call_id: object,
    budget_record: object,
) -> tuple[int, Mapping[str, object]]:
    if type(model_call_id) is not int or model_call_id <= 0:
        raise _error(
            "DEEPSEEK_EXECUTION_INPUT_INVALID",
            "model_call_id 必须是正整数。",
        )
    if not isinstance(budget_record, Mapping):
        raise _error(
            "DEEPSEEK_EXECUTION_INPUT_INVALID",
            "budget_record 必须是只读可映射对象。",
        )
    return model_call_id, budget_record


def _require_ready_preflight(
    value: object,
    *,
    model_call_id: int,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != _PREFLIGHT_KEYS:
        raise _error(
            "DEEPSEEK_EXECUTION_PREFLIGHT_INVALID",
            "Gateway Preflight shape 无法闭合。",
        )

    exact_values = {
        "schema_version": "model_gateway_preflight_v1",
        "model_call_id": model_call_id,
        "provider": _PROVIDER,
        "model_id": _MODEL_ID,
        "model_version": _MODEL_VERSION,
        "task_type": _TASK_TYPE,
        "output_schema_version": _OUTPUT_SCHEMA_VERSION,
        "current_qualification_authority_state": "verified",
        "current_authorization_authority_state": "verified",
        "final_request_fit_state": "fit_by_conservative_upper_bound",
        "provider_compatibility_state": "compatible",
        "local_gateway_state": "ready_for_provider_transport",
        "network_send_state": "not_attempted",
    }
    if any(value.get(field) != expected for field, expected in exact_values.items()):
        raise _error(
            "DEEPSEEK_EXECUTION_GATEWAY_NOT_READY",
            "Gateway 尚未形成可执行的 exact ready closure。",
        )

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
            raise _error(
                "DEEPSEEK_EXECUTION_PREFLIGHT_INVALID",
                "Gateway Preflight identity/hash 无法闭合。",
            )

    for field in (
        "current_qualification_authority_ref",
        "current_authorization_authority_ref",
    ):
        if value.get(field) in (None, "", {}, []):
            raise _error(
                "DEEPSEEK_EXECUTION_PREFLIGHT_INVALID",
                "Gateway Preflight authority ref 不完整。",
            )

    purpose_id = value.get("current_authorization_purpose_id")
    if type(purpose_id) is not str or not purpose_id.strip():
        raise _error(
            "DEEPSEEK_EXECUTION_PREFLIGHT_INVALID",
            "Gateway Preflight authorization purpose 不完整。",
        )

    payload_bytes = value.get("framed_payload_utf8_bytes")
    if type(payload_bytes) is not int or payload_bytes < 0:
        raise _error(
            "DEEPSEEK_EXECUTION_PREFLIGHT_INVALID",
            "Gateway Preflight payload byte identity 无效。",
        )
    return value


def _require_transient(
    value: object,
    *,
    preflight: Mapping[str, object],
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != _TRANSIENT_KEYS:
        raise _error(
            "DEEPSEEK_EXECUTION_TRANSIENT_INVALID",
            "Gateway transient request shape 无法闭合。",
        )
    if value.get("schema_version") != "model_gateway_transient_request_v1":
        raise _error(
            "DEEPSEEK_EXECUTION_TRANSIENT_INVALID",
            "Gateway transient request schema 无效。",
        )
    if any(value.get(field) != preflight.get(field) for field in _REBOUND_FIELDS):
        raise _error(
            "DEEPSEEK_EXECUTION_TRANSIENT_MISMATCH",
            "Gateway transient request 与 ready Preflight identity/hash 不一致。",
        )
    return value


def _claim_send_once(model_call_id: int) -> None:
    """Consume this process's one-send right immediately before Transport."""
    with _SEND_CLAIM_LOCK:
        if model_call_id in _SEND_CLAIMED_MODEL_CALL_IDS:
            raise _error(
                "DEEPSEEK_EXECUTION_REPLAY_BLOCKED",
                "该 model_call_id 已经消费过当前进程的一次性 provider send 权利。",
            )
        _SEND_CLAIMED_MODEL_CALL_IDS.add(model_call_id)


def build_ready_deepseek_execution_preflight(
    *,
    model_call_id: int,
    budget_record: Mapping[str, object],
) -> dict[str, object]:
    """Build and validate exact Gateway readiness without sending the business payload.

    This seam may perform the Current Authority checks already owned by Gateway, but it
    never materializes the transient provider request, claims a send, calls Transport,
    or writes ModelExecutionResult.  Callers can therefore keep durable product state
    in ``queued`` until the exact send-time authority closure is genuinely ready.
    """
    model_call_id, budget_record = _require_input(
        model_call_id=model_call_id,
        budget_record=budget_record,
    )
    return dict(
        _require_ready_preflight(
            model_gateway.build_model_gateway_preflight(
                model_call_id=model_call_id,
                budget_record=budget_record,
            ),
            model_call_id=model_call_id,
        )
    )


def execute_deepseek_model_call(
    *,
    model_call_id: int,
    budget_record: Mapping[str, object],
) -> dict[str, object]:
    """Execute one already-authorized DeepSeek model call without retry/fallback."""
    preflight = build_ready_deepseek_execution_preflight(
        model_call_id=model_call_id,
        budget_record=budget_record,
    )

    transient = _require_transient(
        model_gateway.materialize_model_gateway_request_transient(
            model_call_id=model_call_id,
            budget_record=budget_record,
        ),
        preflight=preflight,
    )

    _claim_send_once(model_call_id)

    receipt = deepseek_transport.send_deepseek_v4_flash(
        messages=transient["messages"],
        max_tokens=transient["max_tokens"],
    )

    return model_execution_results.record_model_execution_result(
        model_call_id=model_call_id,
        receipt=receipt,
    )
