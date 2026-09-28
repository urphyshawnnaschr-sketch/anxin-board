"""HTTP adapter for local report-generation model-call preparation."""

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, StrictBool

from app.report_generation_preparation import prepare_report_generation_model_call


router = APIRouter()


class PrepareReportGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preparation_authorized: StrictBool


@router.post(
    "/api/projects/{project_id}/report-generation-tasks/{local_task_id:path}/prepare-model-call",
    status_code=status.HTTP_200_OK,
)
def prepare_report_generation_model_call_http(
    project_id: int,
    local_task_id: str,
    payload: PrepareReportGenerationRequest,
) -> dict[str, object]:
    """Prepare/replay formal local call identity; never executes provider transport."""
    return prepare_report_generation_model_call(
        project_id=project_id,
        local_task_id=local_task_id,
        preparation_authorized=payload.preparation_authorized,
    )
