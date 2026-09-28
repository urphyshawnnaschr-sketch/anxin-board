"""Exact-current authority for Page07 contradiction checks.

Reuses the reviewed DeepSeek authority primitives, but binds an independent exact task,
prompt/rule/sample-pack identity and exact Human data-scope authorization.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException

from app import deepseek_current_authority as base, model_qualification_registry
from app.ai_contracts import AI_CONTRACT_SCHEMA_VERSION
from app.model_call_ledger import get_model_call

TASK_TYPE = "report_contradiction_check"
OUTPUT_SCHEMA_VERSION = "report-contradiction-check/1.0"
PROMPT_VERSION = "page07-contradiction-prompt/1.0"
RULE_VERSION = "page07-contradiction-rules/1.0"
SAMPLE_PACK_VERSION = "page07-contradiction-qualification-pack/1.0"
PURPOSE_ID = "anxin_board_report_contradiction_check_v1"


def _call_invalid(message: str) -> HTTPException:
    return base._call_invalid(message)


def _read_call(model_call_id: int) -> Mapping[str, object]:
    try:
        call = get_model_call(model_call_id)
    except HTTPException as exc:
        raise _call_invalid("Contradiction ModelCall 无法读取。") from exc
    expected = {
        "model_call_id": model_call_id,
        "provider": base.PROVIDER,
        "model_id": base.MODEL_ID,
        "model_version": base.MODEL_VERSION,
        "task_type": TASK_TYPE,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "benchmark_sample_pack_version": SAMPLE_PACK_VERSION,
        "preparation_state": "prepared",
    }
    if any(call.get(field) != value for field, value in expected.items()) or type(call.get("call_identity_hash")) is not str:
        raise _call_invalid("Contradiction ModelCall exact identity 无法闭合。")
    return call


def _lookup_qualification(*, prompt_contract_hash: str, sampling_parameters_hash: str) -> Mapping[str, object]:
    lookup = {
        "provider": base.PROVIDER,
        "model_id": base.MODEL_ID,
        "model_version": base.MODEL_VERSION,
        "task_type": TASK_TYPE,
        "ai_contract_schema_version": AI_CONTRACT_SCHEMA_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "prompt_contract_hash": prompt_contract_hash,
        "rule_version": RULE_VERSION,
        "sample_pack_version": SAMPLE_PACK_VERSION,
        "sampling_parameters_hash": sampling_parameters_hash,
    }
    try:
        record = model_qualification_registry.lookup_qualified_record(lookup)
    except model_qualification_registry.QualificationRegistryError as exc:
        if exc.code in {"QUALIFICATION_NOT_ADMITTED", "QUALIFICATION_IDENTITY_MISMATCH", "QUALIFICATION_LOOKUP_INVALID"}:
            raise base._qualification_not_admitted(str(exc)) from exc
        raise base._qualification_registry_invalid(str(exc)) from exc
    if not isinstance(record, Mapping) or any(record.get(field) != value for field, value in lookup.items()) or record.get("qualification_status") != model_qualification_registry.QUALIFIED_STATUS:
        raise base._qualification_registry_invalid("Contradiction qualification registry record 无法形成 exact closure。")
    return record


def resolve_deepseek_contradiction_current_authority(
    *,
    model_call_id: int,
    final_context_manifest_hash: str,
    framed_payload_hash: str,
    request_envelope_hash: str,
    prompt_contract_hash: str,
    sampling_parameters_hash: str,
) -> dict[str, object]:
    model_call_id, final_context_manifest_hash, framed_payload_hash = base._validate_direct_inputs(model_call_id, final_context_manifest_hash, framed_payload_hash)
    request_envelope_hash, prompt_contract_hash, sampling_parameters_hash = base._validate_regenerate_hash_inputs(
        request_envelope_hash=request_envelope_hash,
        prompt_contract_hash=prompt_contract_hash,
        sampling_parameters_hash=sampling_parameters_hash,
    )
    call = _read_call(model_call_id)
    record = _lookup_qualification(prompt_contract_hash=prompt_contract_hash, sampling_parameters_hash=sampling_parameters_hash)
    qualification = base._registry_qualification_evidence(record)
    data_scope_hash = base._stable_hash({
        "provider": base.PROVIDER,
        "model_id": base.MODEL_ID,
        "model_version": base.MODEL_VERSION,
        "model_call_id": model_call_id,
        "call_identity_hash": call["call_identity_hash"],
        "final_context_manifest_hash": final_context_manifest_hash,
        "framed_payload_hash": framed_payload_hash,
        "request_envelope_hash": request_envelope_hash,
        "prompt_contract_hash": prompt_contract_hash,
        "sampling_parameters_hash": sampling_parameters_hash,
        "task_type": TASK_TYPE,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "purpose_id": PURPOSE_ID,
    })
    authorization = base._authorization_evidence_for_purpose(data_scope_hash, purpose_id=PURPOSE_ID)
    result: dict[str, object] = {
        "schema_version": base.SCHEMA_VERSION,
        "model_call_id": model_call_id,
        "call_identity_hash": call["call_identity_hash"],
        "provider": base.PROVIDER,
        "model_id": base.MODEL_ID,
        "model_version": base.MODEL_VERSION,
        "context_window_tokens": base.CONTEXT_WINDOW_TOKENS,
        "max_output_tokens": base.MAX_OUTPUT_TOKENS,
        "purpose_id": PURPOSE_ID,
        "data_scope_hash": data_scope_hash,
        "qualification": qualification,
        "authorization": authorization,
    }
    result["authority_hash"] = base._stable_hash(result)
    return result
