"""HTTP adapter for Page 07 Report Review backend slices."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr

from app.local_session_api import require_local_write_request
from app.report_review import (
    append_supplement_version,
    create_reanalysis_request,
    get_current_review_bundle,
    get_reanalysis_request,
    get_review_bundle,
)
from app.report_validation import create_validation_result, get_latest_validation_result
from app.model_call_ledger import get_model_call
from app.page07_contradiction_preparation import (
    get_report_contradiction_budget_record,
    prepare_report_contradiction_model_call,
)
from app.page07_model_execution import execute_report_contradiction_model_call
from app.page07_model_preparation import prepare_daily_report_regenerate_model_call
from app.report_reanalysis_execution import (
    execute_prepared_report_reanalysis,
    finalize_report_reanalysis_from_existing_result,
)
from app.page07_send_authorization import (
    authorize_page07_send_scope,
    begin_page07_authorized_execution,
    build_contradiction_send_authorization_preview,
    build_reanalysis_send_authorization_preview,
    finish_page07_authorized_execution,
)


router = APIRouter()


class CreateReportSupplementRequest(BaseModel):
    """Transport-only PM supplement command; AI raw fields are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    content: StrictStr
    source_type: Literal["pm_correction", "pm_external_fact"]
    provided_by: StrictStr
    provided_timezone: StrictStr
    idempotency_key: StrictStr
    expected_latest_supplement_version: StrictInt


class CreateReportValidationRequest(BaseModel):
    """Transport-only validation command; the server derives pass/block truth."""

    model_config = ConfigDict(extra="forbid")

    expected_report_state_version: StrictInt


class PrepareContradictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preparation_authorized: bool


class ExecuteContradictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_call_id: StrictInt
    data_scope_hash: StrictStr | None = None


class Page07SendPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_call_id: StrictInt


class Page07AuthorizeSendRequest(Page07SendPreviewRequest):
    data_scope_hash: StrictStr
    human_confirmed: StrictBool


class CreateReportReanalysisRequest(BaseModel):
    """Human correction command; provider execution is intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    expected_report_state_version: StrictInt
    error_location: StrictStr
    corrected_truth: StrictStr
    correction_basis: StrictStr
    correction_source: StrictStr
    requested_by: StrictStr
    requested_timezone: StrictStr


class PrepareReportReanalysisExecutionRequest(BaseModel):
    """Local preparation consent only; this command cannot send provider traffic."""

    model_config = ConfigDict(extra="forbid")

    preparation_authorized: bool


class CancelReportReanalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_report_state_version: StrictInt
    expected_reanalysis_request_hash: StrictStr
    expected_replacement_task_identity_hash: StrictStr
    cancelled_by: StrictStr
    cancellation_reason: StrictStr
    idempotency_key: StrictStr


class ExecuteReportReanalysisRequest(BaseModel):
    """Consume one prepared call only with the exact Human-confirmed data scope."""

    model_config = ConfigDict(extra="forbid")

    model_call_id: StrictInt
    data_scope_hash: StrictStr | None = None


class FinalizeReportReanalysisRequest(BaseModel):
    """Recovery selector for an already-durable verified result; never grants send rights."""

    model_config = ConfigDict(extra="forbid")

    model_call_id: StrictInt


def _with_latest_validation(bundle: dict[str, object]) -> dict[str, object]:
    """Compose two read-only owners; no validation/state decision lives in the adapter."""
    report = bundle["report_version"]
    from app.report_progress_preview import build_preview
    return {
        **bundle,
        "progress_preview": build_preview(bundle),
        "latest_validation_result": get_latest_validation_result(
            project_id=int(bundle["project_id"]),
            report_version_id=int(report["report_version_id"]),
        ),
    }


@router.get(
    "/api/projects/{project_id}/report-review/current",
    status_code=status.HTTP_200_OK,
)
def get_current_report_review_http(project_id: int) -> dict[str, object]:
    """Read the current immutable review bundle; this GET never materializes a version."""
    return _with_latest_validation(get_current_review_bundle(project_id=project_id))


@router.get(
    "/api/projects/{project_id}/reports/{report_version_id}/review",
    status_code=status.HTTP_200_OK,
)
def get_report_review_http(
    project_id: int,
    report_version_id: int,
) -> dict[str, object]:
    """Read one exact immutable review bundle."""
    return _with_latest_validation(
        get_review_bundle(
            project_id=project_id,
            report_version_id=report_version_id,
        )
    )


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/supplements",
    status_code=status.HTTP_201_CREATED,
)
def create_report_supplement_http(
    project_id: int,
    report_version_id: int,
    payload: CreateReportSupplementRequest,
    request: Request,
) -> dict[str, object]:
    """Append/replay PM supplement provenance from the launcher's live browser session."""
    # Domain idempotency is already carried by payload.idempotency_key and enforced by
    # SupplementVersion. The local guard therefore proves session/origin/request identity
    # without inventing a second unrelated idempotency identity.
    require_local_write_request(request, require_idempotency_key=False)
    return append_supplement_version(
        project_id=project_id,
        report_version_id=report_version_id,
        content=payload.content,
        source_type=payload.source_type,
        provided_by=payload.provided_by,
        provided_timezone=payload.provided_timezone,
        idempotency_key=payload.idempotency_key,
        expected_latest_supplement_version=payload.expected_latest_supplement_version,
    )


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/validations",
    status_code=status.HTTP_201_CREATED,
)
def create_report_validation_http(
    project_id: int,
    report_version_id: int,
    payload: CreateReportValidationRequest,
    request: Request,
) -> dict[str, object]:
    """Create/replay server-derived validation from the launcher's live browser session."""
    # ValidationResult already deterministically replays by exact candidate/current-authority
    # hashes, so the transport guard does not add an unrelated idempotency key.
    require_local_write_request(request, require_idempotency_key=False)
    return create_validation_result(
        project_id=project_id,
        report_version_id=report_version_id,
        expected_report_state_version=payload.expected_report_state_version,
    )


@router.get(
    "/api/projects/{project_id}/reports/{report_version_id}/reanalysis",
    status_code=status.HTTP_200_OK,
)
def get_report_reanalysis_http(
    project_id: int,
    report_version_id: int,
) -> dict[str, object]:
    """Read the durable replacement-chain fact for an already superseded report."""
    return get_reanalysis_request(
        project_id=project_id,
        report_version_id=report_version_id,
    )


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/reanalysis",
    status_code=status.HTTP_201_CREATED,
)
def create_report_reanalysis_http(
    project_id: int,
    report_version_id: int,
    payload: CreateReportReanalysisRequest,
    request: Request,
) -> dict[str, object]:
    """Persist one Human correction and queue its replacement task; never executes AI."""
    require_local_write_request(request, require_idempotency_key=True)
    return create_reanalysis_request(
        project_id=project_id,
        report_version_id=report_version_id,
        error_location=payload.error_location,
        corrected_truth=payload.corrected_truth,
        correction_basis=payload.correction_basis,
        correction_source=payload.correction_source,
        requested_by=payload.requested_by,
        requested_timezone=payload.requested_timezone,
        idempotency_key=request.headers.get("local-idempotency-key") or "",
        expected_report_state_version=payload.expected_report_state_version,
    )


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/reanalysis/cancel",
    status_code=status.HTTP_200_OK,
)
def cancel_report_reanalysis_http(project_id: int, report_version_id: int, payload: CancelReportReanalysisRequest, request: Request):
    require_local_write_request(request, require_idempotency_key=True)
    from app.report_reanalysis_cancellation import cancel_unstarted_reanalysis
    return cancel_unstarted_reanalysis(project_id=project_id, report_version_id=report_version_id, **payload.model_dump())


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/reanalysis/prepare",
    status_code=status.HTTP_201_CREATED,
)
def prepare_report_reanalysis_execution_http(
    project_id: int,
    report_version_id: int,
    payload: PrepareReportReanalysisExecutionRequest,
    request: Request,
) -> dict[str, object]:
    """Prepare exact regenerate ModelCall/manifest only; never consumes send authority."""
    require_local_write_request(request, require_idempotency_key=False)
    bundle = get_reanalysis_request(project_id=project_id, report_version_id=report_version_id)
    replacement = bundle["replacement_task"]
    return prepare_daily_report_regenerate_model_call(
        project_id=project_id,
        local_task_id=str(replacement["local_task_id"]),
        preparation_authorized=payload.preparation_authorized,
    )


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/reanalysis/send-authorization-preview",
    status_code=status.HTTP_200_OK,
)
def preview_report_reanalysis_send_http(
    project_id: int, report_version_id: int, payload: Page07SendPreviewRequest, request: Request
) -> dict[str, object]:
    require_local_write_request(request, require_idempotency_key=False)
    return build_reanalysis_send_authorization_preview(
        project_id=project_id, report_version_id=report_version_id, model_call_id=payload.model_call_id
    )


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/reanalysis/authorize-send",
    status_code=status.HTTP_200_OK,
)
def authorize_report_reanalysis_send_http(
    project_id: int, report_version_id: int, payload: Page07AuthorizeSendRequest, request: Request
) -> dict[str, object]:
    require_local_write_request(request, require_idempotency_key=True)
    preview = build_reanalysis_send_authorization_preview(
        project_id=project_id, report_version_id=report_version_id, model_call_id=payload.model_call_id
    )
    return authorize_page07_send_scope(
        preview=preview, expected_data_scope_hash=payload.data_scope_hash, human_confirmed=payload.human_confirmed
    )


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/reanalysis/execute",
    status_code=status.HTTP_201_CREATED,
)
def execute_report_reanalysis_http(
    project_id: int,
    report_version_id: int,
    payload: ExecuteReportReanalysisRequest,
    request: Request,
) -> dict[str, object]:
    """Consume one exact Page07 permit and always revoke it after the single attempt."""
    require_local_write_request(request, require_idempotency_key=True)
    preview = build_reanalysis_send_authorization_preview(
        project_id=project_id, report_version_id=report_version_id, model_call_id=payload.model_call_id
    )
    begin_page07_authorized_execution(preview=preview, expected_data_scope_hash=payload.data_scope_hash)
    try:
        call = get_model_call(payload.model_call_id)
        return execute_prepared_report_reanalysis(
            project_id=project_id,
            source_report_version_id=report_version_id,
            local_task_id=str(call["local_task_id"]),
            model_call_id=payload.model_call_id,
        )
    finally:
        finish_page07_authorized_execution(expected_data_scope_hash=payload.data_scope_hash)


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/reanalysis/finalize-existing",
    status_code=status.HTTP_200_OK,
)
def finalize_report_reanalysis_existing_http(
    project_id: int,
    report_version_id: int,
    payload: FinalizeReportReanalysisRequest,
    request: Request,
) -> dict[str, object]:
    """Recover only from an already-durable verified result; never sends provider traffic."""
    require_local_write_request(request, require_idempotency_key=True)
    call = get_model_call(payload.model_call_id)
    return finalize_report_reanalysis_from_existing_result(
        project_id=project_id,
        source_report_version_id=report_version_id,
        local_task_id=str(call["local_task_id"]),
        model_call_id=payload.model_call_id,
    )


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/contradiction/prepare",
    status_code=status.HTTP_201_CREATED,
)
def prepare_report_contradiction_http(
    project_id: int,
    report_version_id: int,
    payload: PrepareContradictionRequest,
    request: Request,
) -> dict[str, object]:
    """Prepare the exact report/supplement contradiction call locally; never sends provider traffic."""
    require_local_write_request(request, require_idempotency_key=False)
    return prepare_report_contradiction_model_call(
        project_id=project_id,
        report_version_id=report_version_id,
        preparation_authorized=payload.preparation_authorized,
    )


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/contradiction/send-authorization-preview",
    status_code=status.HTTP_200_OK,
)
def preview_report_contradiction_send_http(
    project_id: int, report_version_id: int, payload: Page07SendPreviewRequest, request: Request
) -> dict[str, object]:
    require_local_write_request(request, require_idempotency_key=False)
    return build_contradiction_send_authorization_preview(
        project_id=project_id, report_version_id=report_version_id, model_call_id=payload.model_call_id
    )


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/contradiction/authorize-send",
    status_code=status.HTTP_200_OK,
)
def authorize_report_contradiction_send_http(
    project_id: int, report_version_id: int, payload: Page07AuthorizeSendRequest, request: Request
) -> dict[str, object]:
    require_local_write_request(request, require_idempotency_key=True)
    preview = build_contradiction_send_authorization_preview(
        project_id=project_id, report_version_id=report_version_id, model_call_id=payload.model_call_id
    )
    return authorize_page07_send_scope(
        preview=preview, expected_data_scope_hash=payload.data_scope_hash, human_confirmed=payload.human_confirmed
    )


@router.post(
    "/api/projects/{project_id}/reports/{report_version_id}/contradiction/execute",
    status_code=status.HTTP_201_CREATED,
)
def execute_report_contradiction_http(
    project_id: int,
    report_version_id: int,
    payload: ExecuteContradictionRequest,
    request: Request,
) -> dict[str, object]:
    """Consume one exact Page07 permit and always revoke it after the single attempt."""
    require_local_write_request(request, require_idempotency_key=True)
    preview = build_contradiction_send_authorization_preview(
        project_id=project_id, report_version_id=report_version_id, model_call_id=payload.model_call_id
    )
    begin_page07_authorized_execution(preview=preview, expected_data_scope_hash=payload.data_scope_hash)
    try:
        call = get_model_call(payload.model_call_id)
        if call.get("project_id") != project_id or call.get("task_type") != "report_contradiction_check":
            raise HTTPException(
                status_code=409,
                detail={"code": "REPORT_CONTRADICTION_CALL_MISMATCH", "message": "Contradiction ModelCall 与 route selector 不一致。"},
            )
        return execute_report_contradiction_model_call(
            model_call_id=payload.model_call_id,
            budget_record=get_report_contradiction_budget_record(provider=str(call["provider"])),
            report_version_id=report_version_id,
        )
    finally:
        finish_page07_authorized_execution(expected_data_scope_hash=payload.data_scope_hash)
