"""HTTP boundary for explicit one-shot Project Profile generation.

The provider/data-scope implementation is owned by ``project_profile_generation``.
This module owns the mounted local-session + durable idempotency boundary and binds the
bootstrap repository-context adapter that reuses the existing controlled Git/safety stack.
It must not duplicate provider transport, credential, prompt, authorization, or candidate logic.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, HTTPException, Request

from app import project_profile_generation as core
from app.local_session_api import require_local_write_request
from app.project_profile_bootstrap_context import read_repo_context as _bootstrap_repo_context
from app.project_profile_generation_attempt import execute_profile_generation_once


# The first Project Profile predates EvidenceSnapshot, so bind the bootstrap-only context adapter
# once at the mounted boundary. The canonical generator, provider transport, authorization and
# candidate persistence remain owned by project_profile_generation.
core._read_repo_context = _bootstrap_repo_context

router = APIRouter()
_UNKNOWN_MESSAGE = (
    "本次模型请求结果不确定，系统不会自动重发。请先检查项目档案是否已经出现候选；"
    "若仍需重新生成，请由项目经理明确发起一次新的调用。"
)


def _attempt_unknown_error() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "PROFILE_GENERATION_RESULT_UNKNOWN",
            "message": _UNKNOWN_MESSAGE,
        },
    )


@router.post("/api/projects/{project_id}/profile-candidates/generate", status_code=201)
def generate_profile_candidate(
    project_id: int,
    payload: core.GenerateProfilePayload,
    request: Request,
) -> dict[str, object]:
    """Require live local Human authority and one durable send identity."""

    require_local_write_request(request, require_idempotency_key=True)
    idempotency_key = request.headers.get("local-idempotency-key") or ""
    return execute_guarded_attempt(project_id=project_id, idempotency_key=idempotency_key, operation=lambda: core.generate_profile_candidate(project_id, payload))


def execute_guarded_attempt(*, project_id, idempotency_key, operation):
    try:
        return execute_profile_generation_once(
            project_id=project_id,
            idempotency_key=idempotency_key,
            operation=operation,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, Mapping) else {}
        code = detail.get("code")
        # Failure to claim/read/finalize the durable attempt cannot prove that provider
        # send authority was not already consumed. Expose only UNKNOWN so the browser
        # keeps the same durable key rather than materializing a second provider call.
        if type(code) is str and code.startswith("PROFILE_GENERATION_ATTEMPT_"):
            raise _attempt_unknown_error() from exc
        raise
    except Exception as exc:
        # Conservative fail-closed boundary for unexpected ledger/storage failures.
        raise _attempt_unknown_error() from exc
