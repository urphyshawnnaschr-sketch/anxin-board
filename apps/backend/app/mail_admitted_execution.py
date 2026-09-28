"""Internal R4-admitted fake-execution bridge with no public route or credential access.

This module consumes one independently accepted durable R4 admission before delegating
lifecycle execution to the existing MailWorkflow owner. Until the separate Human/P5
transport gate opens, execution is mechanically restricted to the exact concrete
FakeMailGateway type. The module never reads SecretStore bytes and exposes no HTTP route.
"""

from __future__ import annotations

import hashlib

from app.fake_mail_gateway import FakeMailGateway
from app.mail_admission_candidate import MailAdmissionCandidateError, validate_mail_admission_candidate
from app.mail_durable_admission import MailDurableAdmissionError, get_durable_mail_admission
from app.mail_send_attempts import MailSendAttemptError, SendAttempt, get_send_attempt
from app.mail_transport_profile import MailTransportProfileError, get_mail_transport_profile_history
from app.mail_workflow import MailWorkflowError, execute_send_attempt_once
from app.smtp_mail_gateway import SmtpGatewayConfig


class MailAdmittedExecutionError(RuntimeError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> MailAdmittedExecutionError:
    return MailAdmittedExecutionError(code)


def _load_admission(send_attempt_id: str):
    try:
        admission = get_durable_mail_admission(send_attempt_id)
        validate_mail_admission_candidate(admission.candidate)
        return admission
    except (MailDurableAdmissionError, MailAdmissionCandidateError) as exc:
        raise _fail("MAIL_EXECUTION_ADMISSION_INVALID") from exc


def _load_attempt(send_attempt_id: str) -> SendAttempt:
    try:
        return get_send_attempt(send_attempt_id)
    except MailSendAttemptError as exc:
        raise _fail("MAIL_EXECUTION_SEND_ATTEMPT_INVALID") from exc


def _assert_attempt_identity(admission, attempt: SendAttempt) -> None:
    candidate = admission.candidate
    if (
        admission.send_attempt_id != attempt.send_attempt_id
        or candidate.project_id != attempt.project_id
        or candidate.approval_snapshot_id != attempt.approval_snapshot_id
        or candidate.approval_snapshot_hash != attempt.approval_snapshot_hash
        or candidate.report_version_id != attempt.report_version_id
        or candidate.report_content_hash != attempt.report_content_hash
        or candidate.render_identity != attempt.render_identity
        or candidate.render_hash != attempt.render_hash
        or candidate.html_sha256 != attempt.html_sha256
        or candidate.message_id != attempt.message_id
        or candidate.to_recipients != attempt.to_recipients
        or candidate.recipients_hash != attempt.recipients_hash
        or candidate.subject != attempt.subject
        or candidate.from_identity != attempt.from_identity
        or admission.predecessor_send_attempt_id != attempt.predecessor_send_attempt_id
    ):
        raise _fail("MAIL_EXECUTION_ADMISSION_ATTEMPT_MISMATCH")


def get_admitted_smtp_gateway_config(send_attempt_id: str) -> SmtpGatewayConfig:
    """Resolve exact admitted historical SMTP configuration without reading secret bytes.

    The returned object is configuration identity only. This module intentionally does
    not construct StdlibSmtpMailGateway or accept a SecretStore.
    """
    admission = _load_admission(send_attempt_id)
    candidate = admission.candidate
    try:
        history = get_mail_transport_profile_history()
    except MailTransportProfileError as exc:
        raise _fail("MAIL_EXECUTION_TRANSPORT_PROFILE_INVALID") from exc

    selected = next(
        (
            profile
            for profile in history
            if profile.id == candidate.transport_profile_id
            and profile.version_no == candidate.transport_profile_version_no
        ),
        None,
    )
    if selected is None or (
        selected.profile_hash != candidate.transport_profile_hash
        or selected.secret_ref != candidate.secret_ref
        or selected.from_identity != candidate.from_identity
    ):
        raise _fail("MAIL_EXECUTION_TRANSPORT_PROFILE_MISMATCH")
    try:
        return SmtpGatewayConfig(
            host=selected.host,
            port=selected.port,
            security=selected.security,
            username=selected.username,
            secret_ref=selected.secret_ref,
            timeout_seconds=selected.timeout_seconds,
        )
    except (TypeError, ValueError) as exc:
        raise _fail("MAIL_EXECUTION_TRANSPORT_PROFILE_INVALID") from exc


def execute_admitted_send_attempt_once_for_validation(
    *,
    send_attempt_id: str,
    exact_html: str,
    gateway: FakeMailGateway,
) -> SendAttempt:
    """Run one R4-admitted lifecycle only through the exact in-memory no-I/O fake."""
    # `isinstance()` is deliberately insufficient here: FakeMailGateway is subclassable,
    # and an overriding subclass could perform arbitrary I/O while still passing it.
    # Exact type identity is the pre-P5 mechanical boundary.
    if type(gateway) is not FakeMailGateway:
        raise _fail("MAIL_EXECUTION_REAL_GATEWAY_NOT_AUTHORIZED")

    admission = _load_admission(send_attempt_id)
    attempt = _load_attempt(send_attempt_id)
    _assert_attempt_identity(admission, attempt)

    if type(exact_html) is not str:
        raise _fail("MAIL_EXECUTION_HTML_INVALID")
    try:
        html_hash = hashlib.sha256(exact_html.encode("utf-8", errors="strict")).hexdigest()
    except UnicodeEncodeError as exc:
        raise _fail("MAIL_EXECUTION_HTML_INVALID") from exc
    if html_hash != admission.candidate.html_sha256 or html_hash != attempt.html_sha256:
        raise _fail("MAIL_EXECUTION_HTML_IDENTITY_MISMATCH")

    try:
        terminal = execute_send_attempt_once(
            send_attempt_id=attempt.send_attempt_id,
            exact_html=exact_html,
            gateway=gateway,
        )
    except MailWorkflowError as exc:
        raise _fail(exc.code) from exc

    _assert_attempt_identity(admission, terminal)
    if not terminal.is_terminal:
        raise _fail("MAIL_EXECUTION_TERMINAL_STATE_NOT_PROVEN")
    return terminal
