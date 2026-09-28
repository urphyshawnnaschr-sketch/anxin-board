"""Provider-agnostic execution seam for Page07 model tasks.

Regenerate uses the already frozen ModelGateway preflight/authorization/qualification
closure, then the same adapter and Result Ledger used by normal report generation.
There is no retry or provider fallback.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException

from app import model_execution, model_execution_results, model_gateway, model_provider_runtime, report_contradiction
from app.model_call_ledger import get_model_call
from app.context_candidate_runtime import candidate_materialization_scope


@candidate_materialization_scope()
def build_ready_daily_report_regenerate_preflight(
    *, model_call_id: int, budget_record: Mapping[str, object], report_version_id: int
) -> dict[str, object]:
    model_call_id, budget_record = model_execution._require_input(
        model_call_id=model_call_id, budget_record=budget_record
    )
    return dict(
        model_execution._require_ready_preflight(
            model_gateway.build_daily_report_regenerate_gateway_preflight(
                model_call_id=model_call_id,
                budget_record=budget_record,
                report_version_id=report_version_id,
            ),
            model_call_id=model_call_id,
        )
    )


def execute_daily_report_regenerate_model_call(
    *, model_call_id: int, budget_record: Mapping[str, object], report_version_id: int
) -> dict[str, object]:
    """Execute one exact ready regenerate call and durably record its verified result."""
    preflight = build_ready_daily_report_regenerate_preflight(
        model_call_id=model_call_id,
        budget_record=budget_record,
        report_version_id=report_version_id,
    )
    adapter = model_provider_runtime.resolve_model_provider_adapter(preflight["provider"])
    transient = model_execution._require_transient(
        model_gateway.materialize_daily_report_regenerate_gateway_request_transient(
            model_call_id=model_call_id,
            budget_record=budget_record,
            report_version_id=report_version_id,
        ),
        preflight=preflight,
    )
    if transient.get("task_type") != "daily_report_regenerate" or transient.get("output_schema_version") != "daily-report-regenerate/1.0":
        raise model_execution._error(
            "MODEL_EXECUTION_TRANSIENT_MISMATCH",
            "Page07 regenerate transient task/schema identity 无法闭合。",
        )
    from app.report_reanalysis_cancellation import claim_reanalysis_send_once
    claim_reanalysis_send_once(model_call_id=model_call_id, report_version_id=report_version_id)
    receipt = adapter.execute(model_execution._provider_request(transient))
    return model_execution_results.record_model_execution_result(
        model_call_id=model_call_id,
        receipt=model_execution._receipt_mapping(receipt),
    )

def build_ready_report_contradiction_preflight(*, model_call_id: int, budget_record: Mapping[str, object], report_version_id: int) -> dict[str, object]:
    model_call_id, budget_record = model_execution._require_input(model_call_id=model_call_id, budget_record=budget_record)
    return dict(model_execution._require_ready_preflight(
        model_gateway.build_report_contradiction_gateway_preflight(
            model_call_id=model_call_id, budget_record=budget_record, report_version_id=report_version_id
        ), model_call_id=model_call_id
    ))


def _read_existing_contradiction_result(model_call_id: int) -> dict[str, object] | None:
    try:
        return model_execution_results.get_model_execution_result_for_call(model_call_id)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, Mapping) else {}
        if detail.get("code") == "MODEL_EXECUTION_RESULT_NOT_FOUND":
            return None
        raise


def _bound_contradiction_call(*, model_call_id: int, report_version_id: int) -> tuple[dict[str, object], dict[str, object]]:
    call = get_model_call(model_call_id)
    request = report_contradiction.get_current_contradiction_request(
        project_id=int(call["project_id"]), report_version_id=report_version_id
    )
    if (
        call.get("task_type") != "report_contradiction_check"
        or call.get("local_task_id") != request.get("local_task_id")
        or call.get("snapshot_id") != request.get("evidence_snapshot_id")
        or request.get("report_version_id") != report_version_id
    ):
        raise model_execution._error(
            "MODEL_EXECUTION_CONTRADICTION_BINDING_INVALID",
            "Contradiction ModelCall 与 durable exact report/supplement request 无法闭合。",
        )
    return dict(call), dict(request)


def execute_report_contradiction_model_call(*, model_call_id: int, budget_record: Mapping[str, object], report_version_id: int) -> dict[str, object]:
    """Execute one durable one-shot contradiction call or replay its verified existing result."""
    call, request = _bound_contradiction_call(model_call_id=model_call_id, report_version_id=report_version_id)
    existing = _read_existing_contradiction_result(model_call_id)
    if existing is not None:
        return existing
    preflight = build_ready_report_contradiction_preflight(model_call_id=model_call_id, budget_record=budget_record, report_version_id=report_version_id)
    adapter = model_provider_runtime.resolve_model_provider_adapter(preflight["provider"])
    transient = model_execution._require_transient(
        model_gateway.materialize_report_contradiction_gateway_request_transient(
            model_call_id=model_call_id, budget_record=budget_record, report_version_id=report_version_id
        ), preflight=preflight
    )
    if transient.get("task_type") != "report_contradiction_check" or transient.get("output_schema_version") != "report-contradiction-check/1.0":
        raise model_execution._error("MODEL_EXECUTION_TRANSIENT_MISMATCH", "Page07 contradiction transient task/schema identity 无法闭合。")
    report_contradiction.claim_contradiction_send_once(request=request, call=call)
    model_execution._claim_send_once(model_call_id)
    receipt = adapter.execute(model_execution._provider_request(transient))
    return model_execution_results.record_model_execution_result(model_call_id=model_call_id, receipt=model_execution._receipt_mapping(receipt))
