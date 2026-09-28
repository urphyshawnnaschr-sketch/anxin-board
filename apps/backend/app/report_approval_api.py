"""HTTP adapter for immutable Page07 final report approval."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from app.local_session_api import require_local_write_request
from app.report_approval import (
    create_report_approval_snapshot,
    get_report_approval_snapshot,
)


router = APIRouter()


class ProgressCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    module_id: StrictStr
    stage: StrictStr
    reason: StrictStr
    evidence_ids: list[StrictStr] = Field(min_length=1, max_length=100)


class CreateReportApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_report_state_version: StrictInt
    confirmed_by: StrictStr
    confirmed_timezone: StrictStr
    confirmed_utc_offset_minutes: StrictInt
    human_confirmed: StrictBool
    progress_corrections: list[ProgressCorrectionRequest] = Field(default_factory=list, max_length=100)


@router.get(
    "/api/projects/{project_id}/reports/{report_version_id}/approval",
    status_code=status.HTTP_200_OK,
)
def get_report_approval_http(project_id: int, report_version_id: int) -> dict[str, object]:
    snapshot = get_report_approval_snapshot(
        project_id=project_id,
        report_version_id=report_version_id,
    )
    return {"approval_snapshot": snapshot}


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/approval",
    status_code=status.HTTP_200_OK,
)
def create_report_approval_http(
    project_id: int,
    report_version_id: int,
    payload: CreateReportApprovalRequest,
    request: Request,
) -> dict[str, object]:
    require_local_write_request(request, require_idempotency_key=True)
    return create_report_approval_snapshot(
        project_id=project_id,
        report_version_id=report_version_id,
        expected_report_state_version=payload.expected_report_state_version,
        confirmed_by=payload.confirmed_by,
        confirmed_timezone=payload.confirmed_timezone,
        confirmed_utc_offset_minutes=payload.confirmed_utc_offset_minutes,
        human_confirmed=payload.human_confirmed,
        idempotency_key=request.headers.get("local-idempotency-key") or "",
        progress_corrections=[value.model_dump() for value in payload.progress_corrections],
    )
