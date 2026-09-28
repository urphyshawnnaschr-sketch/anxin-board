"""HTTP adapter for exact-scope Human send authorization and one explicit execution attempt."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr

from app.local_session_api import require_local_write_request
from app.report_generation_execution import execute_prepared_report_generation
from app.report_send_authorization import (
    authorize_report_send_scope,
    begin_authorized_report_execution,
    build_report_send_authorization_preview,
    finish_authorized_report_execution,
)


router = APIRouter()


class ReportSendScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_call_id: StrictInt


class AuthorizeReportSendRequest(ReportSendScopeRequest):
    data_scope_hash: StrictStr
    human_confirmed: StrictBool


class ExecuteReportSendRequest(ReportSendScopeRequest):
    data_scope_hash: StrictStr


@router.post(
    "/api/projects/{project_id}/report-generation-tasks/{local_task_id:path}/send-authorization-preview",
    status_code=status.HTTP_200_OK,
)
def preview_report_send_authorization_http(
    project_id: int,
    local_task_id: str,
    payload: ReportSendScopeRequest,
    request: Request,
) -> dict[str, object]:
    require_local_write_request(request, require_idempotency_key=False)
    return build_report_send_authorization_preview(
        project_id=project_id,
        local_task_id=local_task_id,
        expected_model_call_id=payload.model_call_id,
    )


@router.post(
    "/api/projects/{project_id}/report-generation-tasks/{local_task_id:path}/authorize-send",
    status_code=status.HTTP_200_OK,
)
def authorize_report_send_http(
    project_id: int,
    local_task_id: str,
    payload: AuthorizeReportSendRequest,
    request: Request,
) -> dict[str, object]:
    require_local_write_request(request, require_idempotency_key=True)
    return authorize_report_send_scope(
        project_id=project_id,
        local_task_id=local_task_id,
        model_call_id=payload.model_call_id,
        expected_data_scope_hash=payload.data_scope_hash,
        human_confirmed=payload.human_confirmed,
    )


@router.post(
    "/api/projects/{project_id}/report-generation-tasks/{local_task_id:path}/execute-authorized",
    status_code=status.HTTP_200_OK,
)
def execute_authorized_report_generation_http(
    project_id: int,
    local_task_id: str,
    payload: ExecuteReportSendRequest,
    request: Request,
) -> dict[str, object]:
    """One explicit Human-triggered attempt; never retries or falls back automatically."""
    require_local_write_request(request, require_idempotency_key=True)
    preview = begin_authorized_report_execution(
        project_id=project_id,
        local_task_id=local_task_id,
        model_call_id=payload.model_call_id,
        expected_data_scope_hash=payload.data_scope_hash,
    )
    try:
        return execute_prepared_report_generation(
            project_id=project_id,
            local_task_id=local_task_id,
            model_call_id=preview["model_call_id"],
        )
    finally:
        finish_authorized_report_execution(payload.data_scope_hash)
