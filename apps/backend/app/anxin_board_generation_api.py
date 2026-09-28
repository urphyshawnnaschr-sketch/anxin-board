"""HTTP compatibility gate for the legacy direct formal Anxin Board generation route.

Frozen R2 requires formal reports to come only from the verified AI-raw/review/approval
chain. Until that successor orchestration exists, the legacy public POST remains mounted
for transport compatibility but fails closed before any generation or persistence side
effect can occur.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictInt

from app.anxin_board_report import DEFAULT_MANAGER_SUPPLEMENT
from app.projects import get_project


FORMAL_GENERATION_NOT_READY = "ANXIN_BOARD_FORMAL_GENERATION_NOT_READY"


class GenerateAnxinBoardRequest(BaseModel):
    """Legacy transport shape retained so malformed/extra payloads keep framework rejection."""

    model_config = ConfigDict(extra="forbid")

    report_date: Any
    git_snapshot_id: StrictInt
    module_stages: Any
    manager_supplement: Any = DEFAULT_MANAGER_SUPPLEMENT


router = APIRouter()


def _api_error(*, status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


@router.post("/api/projects/{project_id}/anxin-board/generate", status_code=409)
def generate_anxin_board_report(
    project_id: int,
    payload: GenerateAnxinBoardRequest,
) -> dict[str, object]:
    """Fail closed until the verified R2 formal-report orchestration is available."""

    # Preserve the established missing-project precedence. Pydantic/FastAPI has already
    # rejected malformed path/body input before this function is entered.
    get_project(project_id)

    # Deliberately do not call the legacy generation pipeline here. The old pipeline can
    # persist a formal object directly from caller-supplied stages/supplement without the
    # frozen AI raw -> PM review -> contradiction/integrity -> final approval closure.
    raise _api_error(
        status_code=409,
        code=FORMAL_GENERATION_NOT_READY,
        message="正式报告主链尚未完成，当前禁止直接生成正式报告。",
    )
