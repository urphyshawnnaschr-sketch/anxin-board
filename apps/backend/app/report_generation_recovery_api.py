"""Explicit, Human-triggered recovery for ambiguous report-generation task states."""
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from app.local_session_api import require_local_write_request
from app.report_generation_execution import finalize_report_generation_from_existing_task_result
from app.report_generation_tasks import (
    TASK_TYPE,
    get_report_generation_task,
    transition_report_generation_task,
)

router = APIRouter()


class VoidUnknownReportGenerationTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    human_confirmed: bool


@router.post(
    "/api/projects/{project_id}/report-generation-recovery/finalize-existing/{local_task_id:path}",
    status_code=status.HTTP_200_OK,
)
def finalize_existing_report_generation_http(
    project_id: int, local_task_id: str, request: Request
) -> dict[str, object]:
    """Finish ReportVersion from a verified local result; never calls provider transport."""
    require_local_write_request(request, require_idempotency_key=False)
    return finalize_report_generation_from_existing_task_result(
        project_id=project_id, local_task_id=local_task_id
    )


@router.post(
    "/api/projects/{project_id}/report-generation-recovery/void-unknown/{local_task_id:path}",
    status_code=status.HTTP_200_OK,
)
def void_unknown_report_generation_task_http(
    project_id: int,
    local_task_id: str,
    payload: VoidUnknownReportGenerationTaskRequest,
    request: Request,
) -> dict[str, object]:
    """Release one UNKNOWN chain after one explicit Human action.

    This never retries a provider call and never deletes prior durable evidence/results.
    A later new send remains protected by the normal exact send authorization gate.
    """
    require_local_write_request(request, require_idempotency_key=False)
    if payload.human_confirmed is not True:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "REPORT_GENERATION_TASK_UNKNOWN_VOID_CONFIRMATION_REQUIRED",
                "message": "必须明确选择放弃这条结果不确定的任务。",
            },
        )
    task = get_report_generation_task(
        project_id=project_id,
        local_task_id=local_task_id,
    )
    if task.get("task_type") != TASK_TYPE or task.get("state") != "unknown":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "REPORT_GENERATION_TASK_UNKNOWN_VOID_NOT_ALLOWED",
                "message": "当前任务不是可放弃的结果不确定状态。",
            },
        )
    voided = transition_report_generation_task(
        project_id=project_id,
        local_task_id=local_task_id,
        expected_state="unknown",
        new_state="voided",
    )
    return {
        **voided,
        "abandonment_state": "human_confirmed_unknown_voided",
        "provider_retry_state": "not_retried",
        "original_provider_outcome_state": "unknown",
    }
