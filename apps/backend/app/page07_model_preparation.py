"""Local-only preparation for Page07 replacement analysis.

Creates/replays an exact daily_report_regenerate ModelCall and Final Context Manifest.
It does not resolve current authority, read credentials, or send provider traffic.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re

from fastapi import HTTPException

from app.context_candidate_runtime import candidate_materialization_scope
from app.page07_qualification_preflight import require_regenerate_qualification

from app import context_token_framing, final_context_manifest, model_budget_profiles, model_call_ledger, model_provider_runtime
from app.model_provider_contract import ProviderCapability
from app.report_generation_tasks import get_report_regeneration_task

SCHEMA_VERSION = "page07_model_preparation_v1"
TASK_TYPE = "daily_report_regenerate"
OUTPUT_SCHEMA_VERSION = "daily-report-regenerate/1.0"
RULE_VERSION = "page07-regenerate-rules/2.0"
SAMPLE_PACK_VERSION = "page07-regenerate-qualification-pack/2.0"
_RESERVED_OUTPUT_TOKENS = 64_000
_SAFETY_MARGIN_TOKENS = 16_384
_TOKENIZER_FAMILY = "conservative-utf8-byte-upper-bound"
_TOKENIZER_VERSION = "v1"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _require_authorized(value: object) -> None:
    if value is not True:
        raise _error("PAGE07_REGENERATE_PREPARATION_AUTHORIZATION_REQUIRED", "准备重分析模型调用前需要明确的本地 preparation 授权；当前步骤不会发送数据。")


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
        raise _error("PAGE07_REGENERATE_PROVIDER_CAPABILITY_INVALID", "当前 provider 不能形成 exact regenerate capability closure。")
    return capability


def _budget_record(capability: ProviderCapability) -> dict[str, object]:
    return {
        "provider": capability.provider,
        "model_id": capability.model_id,
        "model_version": capability.model_version,
        "budget_policy_version": f"page07-regenerate-{capability.model_id}/1.0",
        "context_window_tokens": capability.context_window_tokens,
        "max_output_tokens": capability.max_output_tokens,
        "reserved_output_tokens": _RESERVED_OUTPUT_TOKENS,
        "safety_margin_tokens": _SAFETY_MARGIN_TOKENS,
        "tokenizer_family": _TOKENIZER_FAMILY,
        "tokenizer_version": _TOKENIZER_VERSION,
        "counting_policy_version": context_token_framing.COUNTING_POLICY_VERSION,
    }


def _assert_binding(*, task: Mapping[str, object], call: Mapping[str, object], budget: Mapping[str, object], manifest: Mapping[str, object], capability: ProviderCapability) -> None:
    expected_call = {
        "project_id": task.get("project_id"),
        "local_task_id": task.get("local_task_id"),
        "snapshot_id": task.get("evidence_snapshot_id"),
        "task_type": TASK_TYPE,
        "provider": capability.provider,
        "model_id": capability.model_id,
        "model_version": capability.model_version,
        "rule_version": RULE_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "benchmark_sample_pack_version": SAMPLE_PACK_VERSION,
    }
    if any(call.get(field) != expected for field, expected in expected_call.items()):
        raise _error("PAGE07_REGENERATE_PREPARATION_BINDING_INVALID", "Regenerate ModelCall 未精确绑定 durable replacement task。")
    if budget.get("model_call_id") != call.get("model_call_id") or budget.get("call_identity_hash") != call.get("call_identity_hash") or budget.get("model_send_state") != "not_admitted":
        raise _error("PAGE07_REGENERATE_PREPARATION_BINDING_INVALID", "Regenerate Budget Profile 与 ModelCall 漂移。")
    expected_manifest = {
        "model_call_id": call.get("model_call_id"),
        "call_identity_hash": call.get("call_identity_hash"),
        "project_id": task.get("project_id"),
        "snapshot_id": task.get("evidence_snapshot_id"),
        "task_type": TASK_TYPE,
        "provider": capability.provider,
        "model_id": capability.model_id,
        "model_version": capability.model_version,
        "rule_version": RULE_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "benchmark_sample_pack_version": SAMPLE_PACK_VERSION,
        "budget_profile_hash": budget.get("budget_profile_hash"),
    }
    if any(manifest.get(field) != expected for field, expected in expected_manifest.items()):
        raise _error("PAGE07_REGENERATE_PREPARATION_BINDING_INVALID", "Regenerate Final Context Manifest 与 durable call/task 漂移。")
    if manifest.get("final_manifest_state") != "finalized_local_metadata" or manifest.get("gateway_send_state") != "not_evaluated" or manifest.get("final_request_fit_state") != "not_evaluated":
        raise _error("PAGE07_REGENERATE_PREPARATION_BINDING_INVALID", "Regenerate manifest 越权进入 send-time 状态。")
    for field in ("final_context_manifest_hash", "framed_payload_hash"):
        if type(manifest.get(field)) is not str or _HASH_RE.fullmatch(str(manifest[field])) is None:
            raise _error("PAGE07_REGENERATE_PREPARATION_BINDING_INVALID", "Regenerate manifest hash 无效。")


@candidate_materialization_scope()
def prepare_daily_report_regenerate_model_call(*, project_id: int, local_task_id: str, preparation_authorized: bool) -> dict[str, object]:
    _require_authorized(preparation_authorized)
    task = get_report_regeneration_task(project_id=project_id, local_task_id=local_task_id)
    if task.get("task_type") != TASK_TYPE or task.get("state") != "queued":
        raise _error("PAGE07_REGENERATE_TASK_NOT_QUEUED", "只有 queued replacement task 可以准备 regenerate ModelCall。")
    capability = _resolve_capability()
    require_regenerate_qualification(capability=capability, rule_version=RULE_VERSION, sample_pack_version=SAMPLE_PACK_VERSION)
    qualification_assertion = {
        "provider": capability.provider,
        "model_id": capability.model_id,
        "model_version": capability.model_version,
        "rule_version": RULE_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "benchmark_sample_pack_version": SAMPLE_PACK_VERSION,
        "qualification_status": "qualified",
    }
    authorization_assertion = {"provider": capability.provider, "authorized": True, "valid": True}
    call = model_call_ledger.prepare_model_call(
        local_task_id=task["local_task_id"],
        call_prepare_key=f"report-regenerate:{task['identity_hash']}",
        snapshot_id=task["evidence_snapshot_id"],
        task_type=TASK_TYPE,
        provider=capability.provider,
        model_id=capability.model_id,
        model_version=capability.model_version,
        rule_version=RULE_VERSION,
        output_schema_version=OUTPUT_SCHEMA_VERSION,
        benchmark_sample_pack_version=SAMPLE_PACK_VERSION,
        qualification_record=qualification_assertion,
        data_sending_authorization=authorization_assertion,
    )
    raw_budget = _budget_record(capability)
    budget = model_budget_profiles.build_model_budget_profile(model_call_id=call["model_call_id"], budget_record=raw_budget)
    manifest = final_context_manifest.build_final_context_manifest(model_call_id=call["model_call_id"], budget_record=raw_budget)
    _assert_binding(task=task, call=call, budget=budget, manifest=manifest, capability=capability)
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": task["project_id"],
        "replacement_task_id": task["id"],
        "local_task_id": task["local_task_id"],
        "evidence_snapshot_id": task["evidence_snapshot_id"],
        "task_identity_hash": task["identity_hash"],
        "model_call_id": call["model_call_id"],
        "call_identity_hash": call["call_identity_hash"],
        "budget_profile_hash": budget["budget_profile_hash"],
        "final_context_manifest_hash": manifest["final_context_manifest_hash"],
        "framed_payload_hash": manifest["framed_payload_hash"],
        "local_request_readiness_state": manifest["local_request_readiness_state"],
        "provider": call["provider"],
        "model_id": call["model_id"],
        "model_version": call["model_version"],
        "preparation_state": "prepared",
        "provider_send_state": "not_attempted",
        "next_gate": "exact_human_send_authorization_required" if manifest["local_request_readiness_state"] == "ready_for_gateway_evaluation" else "local_context_blocked",
    }


def get_daily_report_regenerate_budget_record(*, provider: str | None = None) -> dict[str, object]:
    return deepcopy(_budget_record(_resolve_capability(provider=provider)))
