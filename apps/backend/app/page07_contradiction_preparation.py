"""Local-only preparation for exact Page07 contradiction checking."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re

from fastapi import HTTPException

from app import context_token_framing, final_context_manifest, model_budget_profiles, model_call_ledger, model_provider_runtime
from app.model_provider_contract import ProviderCapability
from app.page07_contradiction_authority import OUTPUT_SCHEMA_VERSION, RULE_VERSION, SAMPLE_PACK_VERSION, TASK_TYPE
from app.report_contradiction import create_or_replay_contradiction_request

SCHEMA_VERSION = "page07_contradiction_preparation_v1"
_RESERVED_OUTPUT_TOKENS = 24_000
_SAFETY_MARGIN_TOKENS = 8_192
_TOKENIZER_FAMILY = "conservative-utf8-byte-upper-bound"
_TOKENIZER_VERSION = "v1"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _resolve_capability(provider: str | None = None) -> ProviderCapability:
    adapter = model_provider_runtime.resolve_default_model_provider_adapter() if provider is None else model_provider_runtime.resolve_model_provider_adapter(provider)
    capability = adapter.get_capability(task_type=TASK_TYPE, output_schema_version=OUTPUT_SCHEMA_VERSION)
    if (
        not isinstance(capability, ProviderCapability)
        or capability.task_type != TASK_TYPE
        or capability.output_schema_version != OUTPUT_SCHEMA_VERSION
        or type(capability.context_window_tokens) is not int
        or capability.context_window_tokens <= 0
        or type(capability.max_output_tokens) is not int
        or capability.max_output_tokens < _RESERVED_OUTPUT_TOKENS
    ):
        raise _error("PAGE07_CONTRADICTION_PROVIDER_CAPABILITY_INVALID", "当前 provider 不能形成 exact contradiction capability closure。")
    return capability


def _budget_record(capability: ProviderCapability) -> dict[str, object]:
    return {
        "provider": capability.provider,
        "model_id": capability.model_id,
        "model_version": capability.model_version,
        "budget_policy_version": f"page07-contradiction-{capability.model_id}/1.0",
        "context_window_tokens": capability.context_window_tokens,
        "max_output_tokens": capability.max_output_tokens,
        "reserved_output_tokens": _RESERVED_OUTPUT_TOKENS,
        "safety_margin_tokens": _SAFETY_MARGIN_TOKENS,
        "tokenizer_family": _TOKENIZER_FAMILY,
        "tokenizer_version": _TOKENIZER_VERSION,
        "counting_policy_version": context_token_framing.COUNTING_POLICY_VERSION,
    }


def prepare_report_contradiction_model_call(*, project_id: int, report_version_id: int, preparation_authorized: bool) -> dict[str, object]:
    if preparation_authorized is not True:
        raise _error("PAGE07_CONTRADICTION_PREPARATION_AUTHORIZATION_REQUIRED", "准备 contradiction ModelCall 前需要明确本地 preparation 授权；当前步骤不会发送数据。")
    request = create_or_replay_contradiction_request(project_id=project_id, report_version_id=report_version_id)
    capability = _resolve_capability()
    qualification_assertion = {
        "provider": capability.provider, "model_id": capability.model_id,
        "model_version": capability.model_version, "rule_version": RULE_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "benchmark_sample_pack_version": SAMPLE_PACK_VERSION, "qualification_status": "qualified",
    }
    authorization_assertion = {"provider": capability.provider, "authorized": True, "valid": True}
    call = model_call_ledger.prepare_model_call(
        local_task_id=request["local_task_id"],
        call_prepare_key=f"report-contradiction:{request['request_hash'][:64]}",
        snapshot_id=request["evidence_snapshot_id"],
        task_type=TASK_TYPE, provider=capability.provider, model_id=capability.model_id,
        model_version=capability.model_version, rule_version=RULE_VERSION,
        output_schema_version=OUTPUT_SCHEMA_VERSION, benchmark_sample_pack_version=SAMPLE_PACK_VERSION,
        qualification_record=qualification_assertion, data_sending_authorization=authorization_assertion,
    )
    raw_budget = _budget_record(capability)
    budget = model_budget_profiles.build_model_budget_profile(model_call_id=call["model_call_id"], budget_record=raw_budget)
    manifest = final_context_manifest.build_final_context_manifest(model_call_id=call["model_call_id"], budget_record=raw_budget)
    if (
        call.get("project_id") != request["project_id"]
        or call.get("local_task_id") != request["local_task_id"]
        or call.get("snapshot_id") != request["evidence_snapshot_id"]
        or call.get("task_type") != TASK_TYPE
        or call.get("output_schema_version") != OUTPUT_SCHEMA_VERSION
        or manifest.get("model_call_id") != call.get("model_call_id")
        or manifest.get("call_identity_hash") != call.get("call_identity_hash")
        or manifest.get("task_type") != TASK_TYPE
        or manifest.get("output_schema_version") != OUTPUT_SCHEMA_VERSION
        or manifest.get("budget_profile_hash") != budget.get("budget_profile_hash")
        or manifest.get("gateway_send_state") != "not_evaluated"
    ):
        raise _error("PAGE07_CONTRADICTION_PREPARATION_BINDING_INVALID", "Contradiction request/ModelCall/manifest exact binding 无法闭合。")
    for field in ("final_context_manifest_hash", "framed_payload_hash"):
        if type(manifest.get(field)) is not str or _HASH_RE.fullmatch(str(manifest[field])) is None:
            raise _error("PAGE07_CONTRADICTION_PREPARATION_BINDING_INVALID", "Contradiction manifest hash 无效。")
    return {
        "schema_version": SCHEMA_VERSION, "project_id": project_id,
        "report_version_id": report_version_id, "supplement_version_id": request["supplement_version_id"],
        "contradiction_request_id": request["contradiction_request_id"],
        "request_hash": request["request_hash"], "local_task_id": request["local_task_id"],
        "model_call_id": call["model_call_id"], "call_identity_hash": call["call_identity_hash"],
        "budget_profile_hash": budget["budget_profile_hash"],
        "final_context_manifest_hash": manifest["final_context_manifest_hash"],
        "framed_payload_hash": manifest["framed_payload_hash"],
        "provider": call["provider"], "model_id": call["model_id"], "model_version": call["model_version"],
        "preparation_state": "prepared", "provider_send_state": "not_attempted",
        "next_gate": "exact_human_send_authorization_required" if manifest.get("local_request_readiness_state") == "ready_for_gateway_evaluation" else "local_context_blocked",
    }


def get_report_contradiction_budget_record(*, provider: str | None = None) -> dict[str, object]:
    return deepcopy(_budget_record(_resolve_capability(provider=provider)))
