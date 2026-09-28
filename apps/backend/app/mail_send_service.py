"""Explicit Page08 mail-send service over the existing durable mail authorities.

This module is the product-side bridge that was intentionally absent before the Human
send gate opened. A send is allowed only for the exact latest approved V3 report, the
exact current immutable RecipientConfig, and the exact admitted SMTP transport profile.
The SMTP secret is read only by the gateway at the final execution boundary. UNKNOWN
is terminal because MailWorkflow never invokes a terminal SendAttempt again.
"""

from __future__ import annotations

from collections.abc import Callable
import re

from fastapi import HTTPException

from app.anxin_board_report_store import (
    AnxinBoardReportProjectNotFoundError,
    AnxinBoardReportStoredInvalidError,
    load_latest_anxin_board_report,
)
from app.mail_admitted_execution import (
    MailAdmittedExecutionError,
    get_admitted_smtp_gateway_config,
)
from app.mail_durable_admission import (
    MailDurableAdmissionError,
    prepare_durable_mail_admission,
)
from app.mail_gateway import MailGateway
from app.mail_readiness import MailReadinessError, get_mail_readiness
from app.mail_report_renderer import MailReportRenderError, render_approved_report_for_mail
from app.approved_report_narrative import ApprovedReportNarrativeError, load_approved_report_narrative
from app.approved_module_narrative import ApprovedModuleNarrativeError, get_approved_module_narrative
from app.approved_report_git_metrics import ApprovedReportGitMetricsError, load_approved_report_git_metrics
from app.mail_send_attempts import SendAttempt, MailSendAttemptError
from app.mail_document_revision import current_document_predecessor
from app.mail_workflow import MailWorkflowError, execute_send_attempt_once
from app.project_profiles import ProjectProfileAuthorityError, read_bound_project_profile_for_report
from app.recipient_config import RecipientConfigError, get_current_recipient_config
from app.report_approval import get_report_approval_snapshot
from app.report_approval_recipient_binding import (
    ApprovalRecipientBindingError,
    create_approval_recipient_binding,
    get_approval_recipient_binding,
)
from app.secret_store import SecretStore, SecretStoreError
from app.smtp_mail_gateway import SmtpGatewayConfig, StdlibSmtpMailGateway
from app.windows_credential_store import WindowsCredentialStore


class MailSendServiceError(RuntimeError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> MailSendServiceError:
    return MailSendServiceError(code)


def _positive(value: object, *, code: str = "MAIL_SEND_INPUT_INVALID") -> int:
    if type(value) is not int or value <= 0:
        raise _fail(code)
    return value


def _timezone(value: object) -> str:
    if type(value) is not str or not value or len(value) > 128 or value != value.strip():
        raise _fail("MAIL_SEND_INPUT_INVALID")
    if any(ord(ch) < 32 or 127 <= ord(ch) <= 159 or ch in {"\u2028", "\u2029"} for ch in value):
        raise _fail("MAIL_SEND_INPUT_INVALID")
    return value


def _offset(value: object) -> int:
    if type(value) is not int or value < -840 or value > 840:
        raise _fail("MAIL_SEND_INPUT_INVALID")
    return value


def _idempotency(value: object) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise _fail("MAIL_SEND_INPUT_INVALID")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
    if any(ch not in allowed for ch in value):
        raise _fail("MAIL_SEND_INPUT_INVALID")
    return value


def _latest_v3(project_id: int) -> dict[str, object]:
    try:
        report = load_latest_anxin_board_report(project_id=project_id)
    except AnxinBoardReportProjectNotFoundError as exc:
        raise _fail("MAIL_SEND_PROJECT_NOT_FOUND") from exc
    except AnxinBoardReportStoredInvalidError as exc:
        raise _fail("MAIL_SEND_REPORT_INVALID") from exc
    if not isinstance(report, dict) or report.get("schema_version") != "anxin_board_report_v3":
        raise _fail("MAIL_SEND_FORMAL_REPORT_REQUIRED")
    return report


def _current_recipient(project_id: int) -> dict[str, object]:
    try:
        current = get_current_recipient_config(project_id)
    except RecipientConfigError as exc:
        if exc.code == "RECIPIENT_CONFIG_PROJECT_NOT_FOUND":
            raise _fail("MAIL_SEND_PROJECT_NOT_FOUND") from exc
        raise _fail("MAIL_SEND_RECIPIENT_INVALID") from exc
    if not isinstance(current, dict):
        raise _fail("MAIL_SEND_RECIPIENT_REQUIRED")
    return current


def _ensure_recipient_binding(
    *,
    project_id: int,
    report_version_id: int,
    recipient: dict[str, object],
    confirmed_timezone: str,
    confirmed_utc_offset_minutes: int,
    idempotency_key: str,
) -> dict[str, object]:
    try:
        existing = get_approval_recipient_binding(
            project_id=project_id,
            report_version_id=report_version_id,
        )
    except ApprovalRecipientBindingError as exc:
        raise _fail("MAIL_SEND_RECIPIENT_BINDING_INVALID") from exc

    recipient_id = _positive(recipient.get("id"), code="MAIL_SEND_RECIPIENT_INVALID")
    recipient_no = _positive(recipient.get("version_no"), code="MAIL_SEND_RECIPIENT_INVALID")
    recipient_hash = recipient.get("recipients_hash")

    if existing is not None:
        if (
            existing.get("recipient_config_version_id") != recipient_id
            or existing.get("recipient_config_version_no") != recipient_no
            or existing.get("recipients_hash") != recipient_hash
        ):
            # An approved report may never silently switch to a later recipient list.
            raise _fail("MAIL_SEND_REPORT_BOUND_TO_DIFFERENT_RECIPIENTS")
        return existing

    try:
        return create_approval_recipient_binding(
            project_id=project_id,
            report_version_id=report_version_id,
            expected_recipient_config_version_id=recipient_id,
            expected_recipient_config_version_no=recipient_no,
            confirmed_by="page08-local-user",
            confirmed_timezone=confirmed_timezone,
            confirmed_utc_offset_minutes=confirmed_utc_offset_minutes,
            human_confirmed=True,
            idempotency_key=idempotency_key,
        )
    except ApprovalRecipientBindingError as exc:
        raise _fail(exc.code) from exc


def _render_exact_current(
    *,
    project_id: int,
    expected_report_version_id: int,
    expected_report_content_hash: str,
):
    report = _latest_v3(project_id)
    if (
        report.get("report_version_id") != expected_report_version_id
        or report.get("report_content_hash") != expected_report_content_hash
    ):
        raise _fail("MAIL_SEND_REPORT_DRIFT")
    profile_id = _positive(report.get("profile_id"), code="MAIL_SEND_REPORT_INVALID")
    try:
        profile = read_bound_project_profile_for_report(profile_id)
        approval = get_report_approval_snapshot(
            project_id=project_id,
            report_version_id=expected_report_version_id,
        )
        if not isinstance(approval, dict):
            raise _fail("MAIL_SEND_APPROVAL_INVALID")
        narrative = load_approved_report_narrative(
            project_id=project_id, report=report, approval_snapshot=approval,
        )
        module_narrative = get_approved_module_narrative(
            project_id=project_id, report=report, approval_snapshot=approval, profile=profile,
        )
        git_metrics = load_approved_report_git_metrics(project_id=project_id, report=report, approval_snapshot=approval)
        render = render_approved_report_for_mail(
            report,
            profile=profile,
            approval_snapshot=approval,
            approved_narrative=narrative,
            approved_module_narrative=module_narrative,
            approved_git_metrics=git_metrics,
        )
    except MailSendServiceError:
        raise
    except (ApprovedReportNarrativeError, ApprovedModuleNarrativeError) as exc:
        raise _fail("MAIL_SEND_NARRATIVE_INVALID") from exc
    except (HTTPException, ProjectProfileAuthorityError, MailReportRenderError, ApprovedReportGitMetricsError) as exc:
        raise _fail("MAIL_SEND_RENDER_INVALID") from exc
    return report, render


SecretStoreFactory = Callable[[], SecretStore]
GatewayFactory = Callable[[SmtpGatewayConfig, SecretStore], MailGateway]


def _default_gateway_factory(config: SmtpGatewayConfig, store: SecretStore) -> MailGateway:
    return StdlibSmtpMailGateway(config=config, secret_store=store)


def send_current_approved_report_once(
    *,
    project_id: int,
    expected_report_version_id: int,
    expected_recipient_config_version_no: int,
    confirmed_timezone: str,
    confirmed_utc_offset_minutes: int,
    human_confirmed: bool,
    idempotency_key: str,
    expected_module_narrative_hash: str | None = None,
    secret_store_factory: SecretStoreFactory = WindowsCredentialStore,
    gateway_factory: GatewayFactory = _default_gateway_factory,
) -> SendAttempt:
    """Bind, admit and execute exactly one current approved-report mail attempt.

    No external I/O happens before all immutable identities are closed. Once the
    gateway is invoked, MailWorkflow owns the lifecycle and makes UNKNOWN terminal.
    """
    project_id = _positive(project_id)
    expected_report_version_id = _positive(expected_report_version_id)
    expected_recipient_config_version_no = _positive(expected_recipient_config_version_no)
    timezone = _timezone(confirmed_timezone)
    utc_offset = _offset(confirmed_utc_offset_minutes)
    idem = _idempotency(idempotency_key)
    if expected_module_narrative_hash is not None and (
        type(expected_module_narrative_hash) is not str
        or re.fullmatch(r'[0-9a-f]{64}', expected_module_narrative_hash) is None
    ):
        raise _fail('MAIL_SEND_INPUT_INVALID')
    if human_confirmed is not True:
        raise _fail("MAIL_SEND_HUMAN_CONFIRMATION_REQUIRED")

    report = _latest_v3(project_id)
    if report.get("report_version_id") != expected_report_version_id:
        raise _fail("MAIL_SEND_REPORT_STALE")

    recipient = _current_recipient(project_id)
    if recipient.get("version_no") != expected_recipient_config_version_no:
        raise _fail("MAIL_SEND_RECIPIENT_STALE")

    _ensure_recipient_binding(
        project_id=project_id,
        report_version_id=expected_report_version_id,
        recipient=recipient,
        confirmed_timezone=timezone,
        confirmed_utc_offset_minutes=utc_offset,
        idempotency_key=idem,
    )

    try:
        readiness = get_mail_readiness(project_id)
    except MailReadinessError as exc:
        raise _fail("MAIL_SEND_NOT_READY") from exc
    candidate = readiness.get("candidate") if isinstance(readiness, dict) else None
    if not isinstance(readiness, dict) or readiness.get("candidate_ready") is not True or not isinstance(candidate, dict):
        raise _fail("MAIL_SEND_NOT_READY")
    admission_hash = candidate.get("admission_hash")
    if type(admission_hash) is not str:
        raise _fail("MAIL_SEND_NOT_READY")

    _, proposed_render = _render_exact_current(
        project_id=project_id,
        expected_report_version_id=expected_report_version_id,
        expected_report_content_hash=report['report_content_hash'],
    )
    module_match = re.fullmatch(r'mail_report_render_v11:[0-9a-f]{64}:([0-9a-f]{64}|none)', proposed_render.render_identity)
    displayed_hash = module_match[1] if module_match and module_match[1] != 'none' else None
    if displayed_hash != expected_module_narrative_hash:
        raise _fail('MAIL_SEND_MODULE_NARRATIVE_STALE')
    try:
        predecessor = current_document_predecessor(project_id, expected_report_version_id, proposed_render)
    except MailSendAttemptError as exc:
        raise _fail(exc.code) from exc
    try:
        admission, attempt = prepare_durable_mail_admission(
            project_id=project_id,
            expected_admission_hash=admission_hash,
            predecessor_send_attempt_id=predecessor,
        )
    except MailDurableAdmissionError as exc:
        raise _fail(exc.code) from exc

    if admission.candidate.report_version_id != expected_report_version_id:
        raise _fail("MAIL_SEND_ADMISSION_IDENTITY_MISMATCH")

    _, render = _render_exact_current(
        project_id=project_id,
        expected_report_version_id=expected_report_version_id,
        expected_report_content_hash=admission.candidate.report_content_hash,
    )
    if (
        render.render_identity != admission.candidate.render_identity
        or render.render_hash != admission.candidate.render_hash
        or render.html_sha256 != admission.candidate.html_sha256
        or render.subject != admission.candidate.subject
    ):
        raise _fail("MAIL_SEND_RENDER_IDENTITY_MISMATCH")

    try:
        config = get_admitted_smtp_gateway_config(attempt.send_attempt_id)
    except MailAdmittedExecutionError as exc:
        raise _fail(exc.code) from exc

    try:
        store = secret_store_factory()
        gateway = gateway_factory(config, store)
    except SecretStoreError as exc:
        raise _fail("MAIL_SEND_CREDENTIAL_UNAVAILABLE") from exc
    except (TypeError, ValueError) as exc:
        raise _fail("MAIL_SEND_GATEWAY_UNAVAILABLE") from exc

    try:
        terminal = execute_send_attempt_once(
            send_attempt_id=attempt.send_attempt_id,
            exact_html=render.html_body,
            gateway=gateway,
        )
    except MailWorkflowError as exc:
        raise _fail(exc.code) from exc

    if not terminal.is_terminal:
        raise _fail("MAIL_SEND_TERMINAL_STATE_NOT_PROVEN")
    return terminal


def safe_send_attempt_response(attempt: SendAttempt) -> dict[str, object]:
    """Public response without hashes, credential refs or body material."""
    if not isinstance(attempt, SendAttempt):
        raise TypeError("attempt must be SendAttempt")
    return {
        "schema_version": "mail_send_action_v1",
        "send_attempt_id": attempt.send_attempt_id,
        "project_id": attempt.project_id,
        "report_version_id": attempt.report_version_id,
        "state": attempt.state,
        "terminal": attempt.is_terminal,
        "from_identity": attempt.from_identity,
        "to_recipients": list(attempt.to_recipients),
        "subject": attempt.subject,
        "terminal_code": attempt.terminal_code,
        "terminal_summary": attempt.terminal_summary,
        "recipient_results": [
            {
                "recipient": item.recipient,
                "outcome": item.outcome.value,
                "error_code": item.error_code,
                "summary": item.summary,
            }
            for item in attempt.recipient_results
        ],
    }
