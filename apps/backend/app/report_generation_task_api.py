"""HTTP admission/observation adapter for the durable Report Generation Task Core."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, StrictInt

from app.report_generation_tasks import (
    create_report_generation_task,
    get_report_generation_task,
    find_active_report_generation_task,
)

router = APIRouter()


class CreateReportGenerationTaskRequest(BaseModel):
    """Transport-only request for durable task admission."""

    model_config = ConfigDict(extra="forbid")

    local_task_id: str
    evidence_snapshot_id: StrictInt
    create_key: str


_TASK_CORE_HTTP_STATUS_BY_CODE = {
    "REPORT_GENERATION_TASK_INPUT_INVALID": status.HTTP_400_BAD_REQUEST,
    "REPORT_GENERATION_TASK_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "REPORT_GENERATION_TASK_CREATE_CONFLICT": status.HTTP_409_CONFLICT,
    "REPORT_GENERATION_TASK_LOGICAL_TASK_CONFLICT": status.HTTP_409_CONFLICT,
    "REPORT_GENERATION_TASK_LOGICAL_IDENTITY_CONFLICT": status.HTTP_409_CONFLICT,
    "REPORT_GENERATION_TASK_CHECKPOINT_ACTIVE_CONFLICT": status.HTTP_409_CONFLICT,
    "REPORT_GENERATION_TASK_STORED_INVALID": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "REPORT_GENERATION_TASK_TRANSITION_INVALID": status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def _mapped_task_core_exception(exc: HTTPException) -> HTTPException:
    """Map only Task Core frozen codes; preserve all other upstream HTTP truth."""

    detail = exc.detail
    code = detail.get("code") if isinstance(detail, dict) else None
    mapped_status = _TASK_CORE_HTTP_STATUS_BY_CODE.get(code)
    if mapped_status is None or mapped_status == exc.status_code:
        return exc
    return HTTPException(status_code=mapped_status, detail=detail)


@router.post(
    "/api/projects/{project_id}/report-generation-tasks",
    status_code=status.HTTP_201_CREATED,
)
def create_report_generation_task_http(
    project_id: int,
    payload: CreateReportGenerationTaskRequest,
) -> dict[str, object]:
    """Create/replay one durable queued logical task; never execute generation."""

    try:
        return create_report_generation_task(
            project_id=project_id,
            local_task_id=payload.local_task_id,
            evidence_snapshot_id=payload.evidence_snapshot_id,
            create_key=payload.create_key,
        )
    except HTTPException as exc:
        mapped = _mapped_task_core_exception(exc)
        if mapped is exc:
            raise
        raise mapped from exc


@router.get(
    "/api/projects/{project_id}/report-generation-tasks/active",
    status_code=status.HTTP_200_OK,
)
def find_active_report_generation_task_http(
    project_id: int, evidence_snapshot_id: int | None = None,
) -> dict[str, object]:
    """Read the unique active chain, optionally scoped to its frozen snapshot."""
    try:
        return {"task": find_active_report_generation_task(
            project_id=project_id, evidence_snapshot_id=evidence_snapshot_id,
        )}
    except HTTPException as exc:
        mapped = _mapped_task_core_exception(exc)
        if mapped is exc:
            raise
        raise mapped from exc


@router.get(
    "/api/projects/{project_id}/report-generation-tasks/{local_task_id:path}",
    status_code=status.HTTP_200_OK,
)
def get_report_generation_task_http(
    project_id: int,
    local_task_id: str,
) -> dict[str, object]:
    """Observe one exact durable logical task without transition or fallback."""

    try:
        return get_report_generation_task(
            project_id=project_id,
            local_task_id=local_task_id,
        )
    except HTTPException as exc:
        mapped = _mapped_task_core_exception(exc)
        if mapped is exc:
            raise
        raise mapped from exc
