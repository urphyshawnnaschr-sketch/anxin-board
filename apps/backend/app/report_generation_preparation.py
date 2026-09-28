"""Report-generation provider preparation bridge V1.

This module closes only the local, pre-send chain:
queued ReportGenerationTask -> formal ModelCall -> deterministic BudgetProfile ->
Final Context Manifest / local redaction / sensitive-path admission / budget-fit.

It never resolves Current Authority, reads credentials, materializes provider request
bytes, sends network traffic, transitions the report task, or creates a ReportVersion.
Provider capability comes only through the reviewed provider registry.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re

from fastapi import HTTPException

from app import (
    context_manifest,
    context_redaction,
    context_redaction_runtime,
    context_token_framing,
    final_context_manifest,
    model_budget_profiles,
    model_call_ledger,
    model_provider_runtime,
)
from app.context_candidate_runtime import candidate_materialization_scope
from app.model_provider_contract import ProviderCapability
from app.report_generation_tasks import get_report_generation_task


SCHEMA_VERSION = "report_generation_preparation_v1"
_PREPARATION_RULE_VERSION = "rules/1.0"
_PREPARATION_SAMPLE_PACK_VERSION = "samples/1.0"
_TASK_TYPE = "daily_report_generate"
_OUTPUT_SCHEMA_VERSION = "daily-report/1.0"
_RESERVED_OUTPUT_TOKENS = 64_000
_SAFETY_MARGIN_TOKENS = 16_384
_TOKENIZER_FAMILY = "conservative-utf8-byte-upper-bound"
_TOKENIZER_VERSION = "v1"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_READY = "ready_for_gateway_evaluation"
_BLOCKED_CONTEXT = "blocked_context_denied"
_BLOCKED_BUDGET = "blocked_context_budget"
_READINESS_STATES = frozenset({_READY, _BLOCKED_CONTEXT, _BLOCKED_BUDGET})


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _authorization_required() -> HTTPException:
    return _error(
        409,
        "REPORT_GENERATION_PREPARATION_AUTHORIZATION_REQUIRED",
        "准备正式模型调用前，需要项目经理明确同意本次冻结研发证据可用于后续报告生成。当前步骤不会发送数据。",
    )


def _task_not_queued() -> HTTPException:
    return _error(
        409,
        "REPORT_GENERATION_PREPARATION_TASK_NOT_QUEUED",
        "只有 queued 的日报生成任务可以进入模型调用准备阶段。",
    )


def _binding_invalid(
    message: str = "报告任务、Model Call、Budget Profile 与 Final Context Manifest 身份无法闭合。",
) -> HTTPException:
    return _error(409, "REPORT_GENERATION_PREPARATION_BINDING_INVALID", message)


def _require_explicit_authorization(value: object) -> None:
    if value is not True:
        raise _authorization_required()


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _validate_capability(capability: ProviderCapability) -> ProviderCapability:
    if (
        not isinstance(capability, ProviderCapability)
        or type(capability.provider) is not str
        or not capability.provider.strip()
        or type(capability.model_id) is not str
        or not capability.model_id.strip()
        or type(capability.model_version) is not str
        or not capability.model_version.strip()
        or capability.task_type != _TASK_TYPE
        or capability.output_schema_version != _OUTPUT_SCHEMA_VERSION
        or type(capability.context_window_tokens) is not int
        or capability.context_window_tokens <= 0
        or type(capability.max_output_tokens) is not int
        or capability.max_output_tokens <= 0
    ):
        raise _binding_invalid("Provider capability 无法形成日报 preparation identity/constraint closure。")
    return capability


def _preparation_assertions(
    capability: ProviderCapability,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return preparation assertions only; these never grant send-time authority."""
    qualification = {
        "provider": capability.provider,
        "model_id": capability.model_id,
        "model_version": capability.model_version,
        "rule_version": _PREPARATION_RULE_VERSION,
        "output_schema_version": capability.output_schema_version,
        "benchmark_sample_pack_version": _PREPARATION_SAMPLE_PACK_VERSION,
        "qualification_status": "qualified",
    }
    authorization = {
        "provider": capability.provider,
        "authorized": True,
        "valid": True,
    }
    return qualification, authorization


def _budget_policy_version(capability: ProviderCapability) -> str:
    return f"daily-report-{capability.model_id}/1.0"


def _budget_record(capability: ProviderCapability) -> dict[str, object]:
    if _RESERVED_OUTPUT_TOKENS > capability.max_output_tokens:
        raise _binding_invalid("本地日报预算超过当前 provider capability。")
    return {
        "provider": capability.provider,
        "model_id": capability.model_id,
        "model_version": capability.model_version,
        "budget_policy_version": _budget_policy_version(capability),
        "context_window_tokens": capability.context_window_tokens,
        "max_output_tokens": capability.max_output_tokens,
        "reserved_output_tokens": _RESERVED_OUTPUT_TOKENS,
        "safety_margin_tokens": _SAFETY_MARGIN_TOKENS,
        "tokenizer_family": _TOKENIZER_FAMILY,
        "tokenizer_version": _TOKENIZER_VERSION,
        "counting_policy_version": context_token_framing.COUNTING_POLICY_VERSION,
    }


def _assert_cross_binding(
    *,
    task: Mapping[str, object],
    call: Mapping[str, object],
    budget: Mapping[str, object],
    manifest: Mapping[str, object],
    capability: ProviderCapability,
) -> None:
    expected_call = {
        "project_id": task.get("project_id"),
        "local_task_id": task.get("local_task_id"),
        "snapshot_id": task.get("evidence_snapshot_id"),
        "task_type": task.get("task_type"),
        "provider": capability.provider,
        "model_id": capability.model_id,
        "model_version": capability.model_version,
        "output_schema_version": capability.output_schema_version,
    }
    if any(call.get(field) != expected for field, expected in expected_call.items()):
        raise _binding_invalid("Model Call 未精确绑定当前 queued 报告任务。")
    if (
        budget.get("model_call_id") != call.get("model_call_id")
        or budget.get("call_identity_hash") != call.get("call_identity_hash")
        or budget.get("task_type") != call.get("task_type")
        or budget.get("provider") != call.get("provider")
        or budget.get("model_id") != call.get("model_id")
        or budget.get("model_version") != call.get("model_version")
        or budget.get("model_send_state") != "not_admitted"
    ):
        raise _binding_invalid("Budget Profile 未精确绑定当前 Model Call。")

    expected_manifest = {
        "model_call_id": call.get("model_call_id"),
        "call_identity_hash": call.get("call_identity_hash"),
        "project_id": task.get("project_id"),
        "snapshot_id": task.get("evidence_snapshot_id"),
        "task_type": call.get("task_type"),
        "provider": call.get("provider"),
        "model_id": call.get("model_id"),
        "model_version": call.get("model_version"),
        "budget_profile_hash": budget.get("budget_profile_hash"),
    }
    if any(manifest.get(field) != expected for field, expected in expected_manifest.items()):
        raise _binding_invalid("Final Context Manifest 未精确绑定当前报告任务/Model Call/Budget。")
    if (
        manifest.get("final_manifest_state") != "finalized_local_metadata"
        or manifest.get("gateway_send_state") != "not_evaluated"
        or manifest.get("final_request_fit_state") != "not_evaluated"
        or manifest.get("local_request_readiness_state") not in _READINESS_STATES
        or not _is_hash(manifest.get("final_context_manifest_hash"))
        or not _is_hash(manifest.get("framed_payload_hash"))
    ):
        raise _binding_invalid("Final Context Manifest 本地 readiness/hash 无法闭合。")

    for field in ("admitted_target_count", "denied_target_count"):
        if type(manifest.get(field)) is not int or manifest[field] < 0:
            raise _binding_invalid("Final Context Manifest target count 无效。")
    unsupported = manifest.get("unsupported_context_sources")
    if not isinstance(unsupported, list) or any(type(item) is not str for item in unsupported):
        raise _binding_invalid("Final Context Manifest unsupported context shape 无效。")


def _next_gate(readiness: str) -> str:
    if readiness == _READY:
        return "exact_human_send_authorization_required"
    if readiness == _BLOCKED_CONTEXT:
        return "local_context_denied"
    if readiness == _BLOCKED_BUDGET:
        return "local_context_budget_exceeded"
    raise _binding_invalid("未知的本地 request readiness state。")


def _resolve_capability(*, provider: str | None = None) -> ProviderCapability:
    adapter = (
        model_provider_runtime.resolve_default_model_provider_adapter()
        if provider is None
        else model_provider_runtime.resolve_model_provider_adapter(provider)
    )
    return _validate_capability(
        adapter.get_capability(
            task_type=_TASK_TYPE,
            output_schema_version=_OUTPUT_SCHEMA_VERSION,
        )
    )


def _safe_context_findings(manifest: Mapping[str, object], budget_record: dict[str, object]) -> list[dict[str, object]]:
    """Display only scanner-owned safe diagnostics; normal clean targets do no extra body scan."""
    findings = []
    locations = {}
    if manifest.get("denied_targets"):
        core = context_manifest.build_context_manifest_core(
            model_call_id=manifest["model_call_id"], budget_record=budget_record)
        if core["manifest_core_hash"] != manifest["manifest_core_hash"]:
            raise _binding_invalid("风险提示与当前冻结范围不一致。")
        locations = {item["evidence_id"]: item for item in core["items"]}
    for item in manifest.get("denied_targets", []):
        location = locations.get(item["target"], {})
        reason = item.get("admission_reason", "excluded")
        findings.append({
            "target": item["target"],
            "path": location.get("path"),
            "rule_id": item["matched_rule_id"] or reason,
            "risk_level": "high" if reason == "credential_boundary_quarantined" else "warning",
            "action": "excluded",
            "line_start": None, "line_end": None,
            "safe_snippet": "[正文已排除，不会发送]",
        })
    for item in manifest.get("admitted_targets", []):
        target = item["target"]
        redaction = context_redaction_runtime.build_context_redaction_result(
            model_call_id=manifest["model_call_id"],
            budget_record=budget_record,
            target=target,
        )
        if (
            redaction.get("redaction_state") != "quarantined"
            and redaction.get("redaction_match_count") == 0
        ):
            continue
        findings.extend(
            context_redaction.build_context_redaction_diagnostics(
                model_call_id=manifest["model_call_id"],
                budget_record=budget_record,
                target=target,
            )
        )
    return findings


def _prepare_current_model_call(*, task, capability, **kwargs):
    """A new local model identity may replace only a provably unsent queued preparation."""
    try:
        return model_call_ledger.prepare_model_call(**kwargs)
    except HTTPException as exc:
        if not isinstance(exc.detail, dict) or exc.detail.get("code") != "MODEL_CALL_IDEMPOTENCY_CONFLICT":
            raise
        from app.db import get_connection
        from app import report_generation_batches
        current = get_report_generation_task(project_id=task["project_id"], local_task_id=task["local_task_id"])
        derived_key = "report-model:" + report_generation_batches._hash(
            [kwargs["call_prepare_key"], capability.provider, capability.model_id, capability.model_version])
        with get_connection() as conn:
            existing = model_call_ledger._read_by_key(conn, project_id=task["project_id"], call_prepare_key=derived_key)
            if existing is not None and current["state"] in {"queued", "running"}:
                kwargs["call_prepare_key"] = derived_key
                return model_call_ledger.prepare_model_call(**kwargs)
            old = model_call_ledger._read_by_key(conn, project_id=task["project_id"], call_prepare_key=kwargs["call_prepare_key"])
            if old is None or tuple(old[f] for f in ("provider", "model_id", "model_version")) == (capability.provider, capability.model_id, capability.model_version):
                raise
        if current["state"] != "queued":
            raise _task_not_queued()
        report_generation_batches.require_unsent_task(current)
        # Old rows/plans stay immutable. Different scope identity requires a new
        # send confirmation; this preparation never grants provider permission.
        kwargs["call_prepare_key"] = derived_key
        return model_call_ledger.prepare_model_call(**kwargs)


@candidate_materialization_scope()
def prepare_report_generation_model_call(
    *,
    project_id: int,
    local_task_id: str,
    preparation_authorized: bool,
) -> dict[str, object]:
    """Prepare/replay one local model call and close context locally, without provider I/O."""
    _require_explicit_authorization(preparation_authorized)

    task = get_report_generation_task(project_id=project_id, local_task_id=local_task_id)
    from app import report_generation_batches
    resumable = report_generation_batches.task_summary(task) if task.get("state") == "running" else None
    if task.get("task_type") != _TASK_TYPE or (task.get("state") != "queued" and not (resumable and resumable["can_resume"])):
        raise _task_not_queued()

    capability = _resolve_capability()
    qualification, authorization = _preparation_assertions(capability)
    call_prepare_key = f"report-generate:{task['identity_hash']}"
    call = _prepare_current_model_call(
        task=task, capability=capability,
        local_task_id=task["local_task_id"],
        call_prepare_key=call_prepare_key,
        snapshot_id=task["evidence_snapshot_id"],
        task_type=task["task_type"],
        provider=capability.provider,
        model_id=capability.model_id,
        model_version=capability.model_version,
        rule_version=_PREPARATION_RULE_VERSION,
        output_schema_version=capability.output_schema_version,
        benchmark_sample_pack_version=_PREPARATION_SAMPLE_PACK_VERSION,
        qualification_record=qualification,
        data_sending_authorization=authorization,
    )

    raw_budget = _budget_record(capability)
    budget = model_budget_profiles.build_model_budget_profile(
        model_call_id=call["model_call_id"],
        budget_record=raw_budget,
    )
    manifest = final_context_manifest.build_final_context_manifest(
        model_call_id=call["model_call_id"],
        budget_record=raw_budget,
    )
    _assert_cross_binding(
        task=task,
        call=call,
        budget=budget,
        manifest=manifest,
        capability=capability,
    )

    readiness = manifest["local_request_readiness_state"]
    unsupported = manifest["unsupported_context_sources"]
    batch_plan = None
    if report_generation_batches.get_plan(call["model_call_id"]) is not None or report_generation_batches.request_budget_exceeded(manifest, raw_budget):
        batch_plan = report_generation_batches.summary(report_generation_batches.prepare_plan(
            parent=call, manifest=manifest, budget_record=raw_budget))
        if batch_plan["can_resume"]:
            readiness = _READY
    response = {
        "schema_version": SCHEMA_VERSION,
        "project_id": task["project_id"],
        "report_generation_task_id": task["id"],
        "local_task_id": task["local_task_id"],
        "evidence_snapshot_id": task["evidence_snapshot_id"],
        "task_identity_hash": task["identity_hash"],
        "task_state": task["state"],
        "model_call_id": call["model_call_id"],
        "call_identity_hash": call["call_identity_hash"],
        "budget_profile_hash": budget["budget_profile_hash"],
        "final_context_manifest_hash": manifest["final_context_manifest_hash"],
        "framed_payload_hash": manifest["framed_payload_hash"],
        "local_request_readiness_state": readiness,
        "context_admission_state": manifest["context_admission_state"],
        "budget_fit_state": manifest["budget_fit_state"],
        "admitted_target_count": manifest["admitted_target_count"],
        "denied_target_count": manifest["denied_target_count"],
        "unsupported_context_source_count": len(unsupported),
        "context_findings": _safe_context_findings(manifest, raw_budget),
        "provider": call["provider"],
        "model_id": call["model_id"],
        "model_version": call["model_version"],
        "preparation_state": "prepared",
        "preparation_authorization_state": "recorded_assertion_only",
        "provider_send_state": "not_attempted",
        "next_gate": _next_gate(readiness),
    }
    if batch_plan is not None:
        response["batch_plan"] = batch_plan
    return response


def get_report_generation_budget_record(*, provider: str | None = None) -> dict[str, object]:
    """Return the deterministic task budget bound to an exact reviewed provider adapter."""
    return deepcopy(_budget_record(_resolve_capability(provider=provider)))
