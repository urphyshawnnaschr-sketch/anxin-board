"""HTTP projection and explicit Human-triggered send action for formal report mail."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr

from app.local_session_api import require_local_write_request
from app.mail_readiness import MailReadinessError, get_mail_readiness
from app.mail_send_service import (
    MailSendServiceError,
    safe_send_attempt_response,
    send_current_approved_report_once,
)


router = APIRouter()


class SendApprovedReportMailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_report_version_id: StrictInt
    expected_recipient_config_version_no: StrictInt
    confirmed_timezone: StrictStr
    confirmed_utc_offset_minutes: StrictInt
    human_confirmed: StrictBool
    expected_module_narrative_hash: StrictStr | None = None


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


@router.get("/api/projects/{project_id}/mail-readiness")
def read_mail_readiness(project_id: int) -> dict[str, object]:
    try:
        return get_mail_readiness(project_id)
    except MailReadinessError as exc:
        if exc.code in {"MAIL_READINESS_INPUT_INVALID", "MAIL_READINESS_PROJECT_NOT_FOUND"}:
            raise _error(404, exc.code, "项目不存在或项目 identity 无效。") from exc
        raise _error(409, exc.code, "邮件准备状态无法从当前已保存事实安全闭合。") from exc


@router.post("/api/projects/{project_id}/mail-send")
def send_approved_report_mail(
    project_id: int,
    payload: SendApprovedReportMailRequest,
    request: Request,
) -> dict[str, object]:
    """Send one exact approved report after one explicit local Human action.

    The endpoint never accepts SMTP credentials or arbitrary recipients.  Those are
    resolved from the already-saved immutable authorities.  A terminal UNKNOWN is not
    retried by this route or by MailWorkflow.
    """
    require_local_write_request(request, require_idempotency_key=True)
    idempotency_key = request.headers.get("local-idempotency-key")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise _error(400, "MAIL_SEND_IDEMPOTENCY_REQUIRED", "发送请求缺少一次性请求标识。")

    try:
        terminal = send_current_approved_report_once(
            project_id=project_id,
            expected_report_version_id=payload.expected_report_version_id,
            expected_recipient_config_version_no=payload.expected_recipient_config_version_no,
            confirmed_timezone=payload.confirmed_timezone,
            confirmed_utc_offset_minutes=payload.confirmed_utc_offset_minutes,
            human_confirmed=payload.human_confirmed,
            idempotency_key=idempotency_key,
            expected_module_narrative_hash=payload.expected_module_narrative_hash,
        )
    except MailSendServiceError as exc:
        if exc.code == "MAIL_SEND_PROJECT_NOT_FOUND":
            raise _error(404, exc.code, "项目不存在。") from exc
        if exc.code == 'MAIL_SEND_MODULE_NARRATIVE_STALE':
            raise _error(409, exc.code, '模块说明已更新，邮件尚未发送；请重新打开报告并核对内容。') from exc
        if exc.code in {
            "MAIL_SEND_INPUT_INVALID",
            "MAIL_SEND_HUMAN_CONFIRMATION_REQUIRED",
        }:
            raise _error(400, exc.code, "发送确认参数无效，邮件没有发送。") from exc
        if exc.code in {
            "MAIL_SEND_FORMAL_REPORT_REQUIRED",
            "MAIL_SEND_REPORT_STALE",
            "MAIL_SEND_RECIPIENT_REQUIRED",
            "MAIL_SEND_RECIPIENT_STALE",
            "MAIL_SEND_REPORT_BOUND_TO_DIFFERENT_RECIPIENTS",
            "MAIL_SEND_NOT_READY",
            "MAIL_SEND_REPORT_DRIFT",
            "MAIL_SEND_DOCUMENT_REVISION_NOT_CONFIRMED",
            "MAIL_ADMISSION_SEND_ATTEMPT_NOT_PREPARED",
        }:
            raise _error(409, exc.code, "正式报告或收件人状态已经变化，邮件没有重复发送；请刷新后确认。") from exc
        raise _error(409, exc.code, "邮件发送链无法安全闭合；邮件不会自动重试。") from exc

    return safe_send_attempt_response(terminal)
