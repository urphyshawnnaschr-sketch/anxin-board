"""Durable lifecycle coordinator for one prepared daily report execution.

This owner bridges Product task state to the provider-agnostic model execution seam:

queued -> exact Gateway ready check -> running -> provider execution -> verified
ModelExecutionResult -> immutable ReportVersion -> succeeded.

It does not create Human send authorization, access credentials, implement provider HTTP,
validate AI output, or retry an uncertain external side effect.  The durable ``running``
transition is the cross-process send claim: once consumed, another invocation cannot
blindly execute the same logical task again.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException

from app import (
    model_execution,
    model_execution_results,
    report_generation_preparation,
    report_review,
)
from app.model_call_ledger import get_model_call
from app.report_generation_tasks import (
    get_report_generation_task,
    transition_report_generation_task,
)


SCHEMA_VERSION = "report_generation_execution_lifecycle_v1"
_TASK_TYPE = "daily_report_generate"
_RESULT_NOT_FOUND = "MODEL_EXECUTION_RESULT_NOT_FOUND"
_OUTPUT_INVALID = "MODEL_EXECUTION_RESULT_OUTPUT_INVALID"


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _input_invalid(message: str = "报告执行生命周期输入无效。") -> HTTPException:
    return _error(400, "REPORT_GENERATION_EXECUTION_INPUT_INVALID", message)


def _binding_invalid(message: str = "报告任务、Model Call 与执行结果身份无法闭合。") -> HTTPException:
    return _error(409, "REPORT_GENERATION_EXECUTION_BINDING_INVALID", message)


def _state_invalid(message: str = "当前报告任务状态不能进入真实执行生命周期。") -> HTTPException:
    return _error(409, "REPORT_GENERATION_EXECUTION_STATE_INVALID", message)


def _result_unavailable() -> HTTPException:
    return _error(
        409,
        "REPORT_GENERATION_EXECUTION_RESULT_UNAVAILABLE",
        "当前 Model Call 还没有可验证的 ModelExecutionResult；不会因此自动重发模型。",
    )


def _finalization_pending() -> HTTPException:
    return _error(
        409,
        "REPORT_GENERATION_EXECUTION_FINALIZATION_PENDING",
        "模型结果已经可验证，但 ReportVersion 尚未完成物化。任务保持 running，可从已有结果继续收口，不会重发模型。",
    )


def _state_reconciliation_failed() -> HTTPException:
    return _error(
        409,
        "REPORT_GENERATION_EXECUTION_STATE_RECONCILIATION_FAILED",
        "外部执行结果无法安全确认，且 durable task state 未能收口；必须人工检查，禁止自动重试。",
    )


def _code(exc: HTTPException) -> str | None:
    detail = exc.detail
    return detail.get("code") if isinstance(detail, Mapping) else None


def _is_deterministic_post_response_failure(exc: Exception) -> bool:
    return isinstance(exc, HTTPException) and _code(exc) == _OUTPUT_INVALID


def _require_model_call_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise _input_invalid("model_call_id 必须是正整数。")
    return value


def _load_bound_subject(
    *, project_id: int, local_task_id: str, model_call_id: int
) -> tuple[dict[str, object], dict[str, object]]:
    task = get_report_generation_task(project_id=project_id, local_task_id=local_task_id)
    call = get_model_call(model_call_id)
    expected = {
        "project_id": task.get("project_id"),
        "local_task_id": task.get("local_task_id"),
        "snapshot_id": task.get("evidence_snapshot_id"),
        "task_type": task.get("task_type"),
    }
    provider_identity_fields = (
        "provider",
        "model_id",
        "model_version",
        "output_schema_version",
    )
    if (
        task.get("task_type") != _TASK_TYPE
        or any(call.get(field) != value for field, value in expected.items())
        or call.get("preparation_state") != "prepared"
        or any(
            type(call.get(field)) is not str or not str(call[field]).strip()
            for field in provider_identity_fields
        )
    ):
        raise _binding_invalid()
    return task, call


def _read_result_if_available(model_call_id: int) -> dict[str, object] | None:
    try:
        return model_execution_results.get_model_execution_result_for_call(model_call_id)
    except HTTPException as exc:
        if _code(exc) == _RESULT_NOT_FOUND:
            return None
        raise


def _assert_result_binding(
    *,
    task: Mapping[str, object],
    call: Mapping[str, object],
    result: Mapping[str, object],
) -> None:
    expected = {
        "model_call_id": call.get("model_call_id"),
        "project_id": task.get("project_id"),
        "snapshot_id": task.get("evidence_snapshot_id"),
        "local_task_id": task.get("local_task_id"),
        "task_type": task.get("task_type"),
        "call_identity_hash": call.get("call_identity_hash"),
    }
    if any(result.get(field) != value for field, value in expected.items()):
        raise _binding_invalid("ModelExecutionResult 未精确绑定当前 report task / Model Call。")
    model_result_id = result.get("model_result_id")
    if type(model_result_id) is not int or model_result_id <= 0:
        raise _binding_invalid("ModelExecutionResult 缺少合法 model_result_id。")


def _enter_running(task: Mapping[str, object]) -> dict[str, object]:
    if task.get("state") == "running":
        return dict(task)
    if task.get("state") != "queued":
        raise _state_invalid()
    return transition_report_generation_task(
        project_id=task["project_id"],
        local_task_id=task["local_task_id"],
        expected_state="queued",
        new_state="running",
    )


def _finish_from_result(
    *,
    task: Mapping[str, object],
    call: Mapping[str, object],
    result: Mapping[str, object],
    execution_source: str,
) -> dict[str, object]:
    _assert_result_binding(task=task, call=call, result=result)
    try:
        materialized = report_review.materialize_report_version(
            project_id=task["project_id"],
            model_execution_result_id=result["model_result_id"],
        )
    except Exception as exc:
        raise _finalization_pending() from exc

    report = materialized.get("report_version") if isinstance(materialized, Mapping) else None
    report_version_id = report.get("report_version_id") if isinstance(report, Mapping) else None
    if type(report_version_id) is not int or report_version_id <= 0:
        raise _finalization_pending()

    transitioned = transition_report_generation_task(
        project_id=task["project_id"],
        local_task_id=task["local_task_id"],
        expected_state="running",
        new_state="succeeded",
    )
    if not isinstance(transitioned, Mapping) or transitioned.get("state") != "succeeded":
        raise _state_reconciliation_failed()

    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": task["project_id"],
        "local_task_id": task["local_task_id"],
        "model_call_id": call["model_call_id"],
        "model_result_id": result["model_result_id"],
        "report_version_id": report_version_id,
        "task_state": "succeeded",
        "execution_source": execution_source,
        "provider_retry_state": "not_retried",
    }


def finalize_report_generation_from_existing_result(
    *,
    project_id: int,
    local_task_id: str,
    model_call_id: int,
) -> dict[str, object]:
    """Finish from an already-durable verified result; never calls Gateway or provider."""
    model_call_id = _require_model_call_id(model_call_id)
    task, call = _load_bound_subject(
        project_id=project_id,
        local_task_id=local_task_id,
        model_call_id=model_call_id,
    )
    if task.get("state") not in {"queued", "running"}:
        raise _state_invalid("只有 queued/running task 可以从已有 ModelExecutionResult 收口。")

    result = _read_result_if_available(model_call_id)
    if result is None:
        raise _result_unavailable()

    # Recovery evidence must close against the prepared subject before any task-state
    # mutation. A foreign/corrupt result must never consume queued -> running.
    _assert_result_binding(task=task, call=call, result=result)
    running = _enter_running(task)
    return _finish_from_result(
        task=running,
        call=call,
        result=result,
        execution_source="existing_verified_result",
    )



def finalize_report_generation_from_existing_task_result(
    *, project_id: int, local_task_id: str
) -> dict[str, object]:
    """Find the one verified single-call result for this task and finish locally."""
    task = get_report_generation_task(project_id=project_id, local_task_id=local_task_id)
    if task.get("task_type") != _TASK_TYPE or task.get("state") not in {"queued", "running"}:
        raise _state_invalid("当前任务不能从已有 ModelExecutionResult 恢复。")
    result = model_execution_results.get_single_model_execution_result_for_task(
        project_id=project_id, local_task_id=local_task_id, task_type=_TASK_TYPE
    )
    return finalize_report_generation_from_existing_result(
        project_id=project_id,
        local_task_id=local_task_id,
        model_call_id=result["model_call_id"],
    )


def execute_prepared_report_generation(
    *,
    project_id: int,
    local_task_id: str,
    model_call_id: int,
) -> dict[str, object]:
    """Execute at most once after exact ready closure; never retries an ambiguous send.

    This function is intentionally not mounted as a public HTTP endpoint.  A future UI
    send action must first provide the current exact Human authorization owned by Gateway.
    """
    model_call_id = _require_model_call_id(model_call_id)
    task, call = _load_bound_subject(
        project_id=project_id,
        local_task_id=local_task_id,
        model_call_id=model_call_id,
    )
    from app import report_generation_batches
    if report_generation_batches.get_plan(model_call_id) is not None:
        return report_generation_batches.execute_plan(
            task=task, parent=call,
            budget_record=report_generation_preparation.get_report_generation_budget_record(provider=str(call["provider"])))
    if task.get("state") != "queued":
        raise _state_invalid("只有 queued task 可以消费新的 provider send 权利。")

    existing = _read_result_if_available(model_call_id)
    if existing is not None:
        # Existing durable truth is a recovery input, not a reason to mutate task state
        # before its exact binding is proven.
        _assert_result_binding(task=task, call=call, result=existing)
        running = _enter_running(task)
        return _finish_from_result(
            task=running,
            call=call,
            result=existing,
            execution_source="existing_verified_result",
        )

    provider = str(call["provider"])
    budget_record = report_generation_preparation.get_report_generation_budget_record(
        provider=provider
    )
    for field in ("provider", "model_id", "model_version"):
        if budget_record.get(field) != call.get(field):
            raise _binding_invalid(
                "执行预算所绑定的 provider/model identity 与 durable Model Call 漂移。"
            )

    # All ordinary pre-send checks happen inside model_execution before this callback.
    # The durable running claim is therefore the exact cross-process provider-send boundary.
    running: dict[str, object] | None = None

    def claim_running_before_provider_send() -> None:
        nonlocal running
        running = _enter_running(task)

    try:
        result = model_execution.execute_model_call(
            model_call_id=model_call_id,
            budget_record=budget_record,
            before_provider_send=claim_running_before_provider_send,
        )
        if running is None:
            raise _state_reconciliation_failed()
        _assert_result_binding(task=running, call=call, result=result)
    except Exception as exc:
        # If the send-boundary callback never ran, provider transport provably did not start.
        # Keep the durable task queued and surface the concrete pre-send error for safe retry.
        if running is None:
            raise
        try:
            durable_result = _read_result_if_available(model_call_id)
        except Exception:
            durable_result = None
        if durable_result is not None:
            try:
                _assert_result_binding(task=running, call=call, result=durable_result)
            except Exception:
                durable_result = None
        if durable_result is not None:
            return _finish_from_result(
                task=running,
                call=call,
                result=durable_result,
                execution_source="recovered_verified_result",
            )
        terminal_state = (
            "failed" if _is_deterministic_post_response_failure(exc) else "unknown"
        )
        try:
            transition_report_generation_task(
                project_id=running["project_id"],
                local_task_id=running["local_task_id"],
                expected_state="running",
                new_state=terminal_state,
            )
        except Exception as state_exc:
            raise _state_reconciliation_failed() from state_exc
        raise

    return _finish_from_result(
        task=running,
        call=call,
        result=result,
        execution_source="new_provider_execution",
    )
