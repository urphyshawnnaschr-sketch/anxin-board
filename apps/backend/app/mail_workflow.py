"""Repository-only MailWorkflow sequencing for one durable SendAttempt.

The workflow is intentionally unreachable from a public route in this slice. It
accepts an injected MailGateway, claims one durable attempt, invokes transport at
most once, and preserves UNKNOWN on any post-invocation ambiguity. Cross-process
liveness is authoritative through a durable renewable execution lease; the local
in-process set is only a fast-path guard. There is no automatic repeat, fallback, or
recipient subset resend path here.
"""

from __future__ import annotations

import hashlib
import secrets
import threading

from app.mail_execution_lease import (
    ExecutionLeaseHeartbeat,
    MailExecutionLeaseError,
    acquire_execution_lease,
    execution_lease_heartbeat,
    permit_orphan_recovery,
    release_execution_lease,
)
from app.mail_gateway import MailContractError, MailGateway, MailGatewayCallError, MailMessage
from app.mail_send_attempts import (
    MailSendAttemptError,
    SendAttempt,
    claim_prepared_attempt,
    close_post_call_unknown,
    close_pre_send_failure,
    get_send_attempt,
    record_send_result,
    recover_stale_sending_as_unknown,
)


class MailWorkflowError(RuntimeError):
    """Stable orchestration error with no body, recipient, or credential content."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_ACTIVE_LOCK = threading.Lock()
_ACTIVE_ATTEMPTS: set[str] = set()


def _enter_local_execution(send_attempt_id: str) -> bool:
    with _ACTIVE_LOCK:
        if send_attempt_id in _ACTIVE_ATTEMPTS:
            return False
        _ACTIVE_ATTEMPTS.add(send_attempt_id)
        return True


def _leave_local_execution(send_attempt_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_ATTEMPTS.discard(send_attempt_id)


def _exact_message(attempt: SendAttempt, exact_html: object) -> MailMessage:
    if type(exact_html) is not str:
        raise MailWorkflowError("MAIL_SEND_HTML_INVALID")
    try:
        body_hash = hashlib.sha256(exact_html.encode("utf-8", errors="strict")).hexdigest()
    except UnicodeEncodeError as exc:
        raise MailWorkflowError("MAIL_SEND_HTML_INVALID") from exc
    if body_hash != attempt.html_sha256:
        raise MailWorkflowError("MAIL_SEND_HTML_IDENTITY_MISMATCH")
    try:
        message = MailMessage(
            message_id=attempt.message_id,
            from_identity=attempt.from_identity,
            to_recipients=attempt.to_recipients,
            subject=attempt.subject,
            html_body=exact_html,
            attempt_correlation_id=attempt.send_attempt_id,
        )
    except MailContractError as exc:
        raise MailWorkflowError("MAIL_SEND_MESSAGE_IDENTITY_INVALID") from exc
    if (
        message.message_id != attempt.message_id
        or message.from_identity != attempt.from_identity
        or message.to_recipients != attempt.to_recipients
        or message.subject != attempt.subject
        or message.body_identity_sha256 != attempt.html_sha256
    ):
        raise MailWorkflowError("MAIL_SEND_MESSAGE_IDENTITY_MISMATCH")
    return message


def _terminal_after_conflict(send_attempt_id: str, conflict: MailSendAttemptError) -> SendAttempt:
    """Prefer already-persisted terminal truth after a narrow cross-process race."""
    current = get_send_attempt(send_attempt_id)
    if current.is_terminal:
        return current
    raise MailWorkflowError(conflict.code) from conflict


def _assert_lease_owned(
    heartbeat: ExecutionLeaseHeartbeat,
    send_attempt_id: str,
) -> SendAttempt | None:
    """Prove the durable owner immediately before terminal persistence.

    If another process already fenced and terminalized the attempt, return that
    terminal truth. Otherwise surface a stable fail-closed ownership error.
    """
    try:
        heartbeat.assert_owned()
    except MailExecutionLeaseError as exc:
        current = get_send_attempt(send_attempt_id)
        if current.is_terminal:
            return current
        raise MailWorkflowError(exc.code) from exc
    return None


def _close_definite_failure(
    heartbeat: ExecutionLeaseHeartbeat,
    send_attempt_id: str,
    *,
    code: str,
) -> SendAttempt:
    terminal = _assert_lease_owned(heartbeat, send_attempt_id)
    if terminal is not None:
        return terminal
    try:
        return close_pre_send_failure(send_attempt_id, code=code)
    except MailSendAttemptError as exc:
        return _terminal_after_conflict(send_attempt_id, exc)


def _close_unknown(
    heartbeat: ExecutionLeaseHeartbeat,
    send_attempt_id: str,
    *,
    code: str,
    summary: str,
) -> SendAttempt:
    terminal = _assert_lease_owned(heartbeat, send_attempt_id)
    if terminal is not None:
        return terminal
    try:
        return close_post_call_unknown(send_attempt_id, code=code, summary=summary)
    except MailSendAttemptError as exc:
        return _terminal_after_conflict(send_attempt_id, exc)


def execute_send_attempt_once(
    *,
    send_attempt_id: str,
    exact_html: str,
    gateway: MailGateway,
) -> SendAttempt:
    """Execute one prepared SendAttempt without automatic repeat invocation.

    A concurrent caller in this process is rejected immediately. A persisted
    ``sending`` state is recoverable only when the durable cross-process lease is
    missing or atomically proven expired; a live lease is authoritative in-progress.
    """
    attempt = get_send_attempt(send_attempt_id)
    if attempt.is_terminal:
        return attempt

    if not _enter_local_execution(attempt.send_attempt_id):
        raise MailWorkflowError("MAIL_SEND_ATTEMPT_IN_PROGRESS")

    try:
        attempt = get_send_attempt(attempt.send_attempt_id)
        if attempt.is_terminal:
            return attempt
        if attempt.state == "sending":
            try:
                recoverable = permit_orphan_recovery(attempt.send_attempt_id)
            except MailExecutionLeaseError as exc:
                raise MailWorkflowError(exc.code) from exc
            if not recoverable:
                raise MailWorkflowError("MAIL_SEND_ATTEMPT_IN_PROGRESS")
            try:
                return recover_stale_sending_as_unknown(attempt.send_attempt_id)
            except MailSendAttemptError as exc:
                return _terminal_after_conflict(attempt.send_attempt_id, exc)
        if attempt.state != "prepared":
            raise MailWorkflowError("MAIL_SEND_ATTEMPT_STATE_INVALID")

        # Close all locally provable identity mismatches before any durable execution
        # ownership is taken, so gateway calls stay 0 on a bad frozen body.
        message = _exact_message(attempt, exact_html)
        owner_token = secrets.token_urlsafe(24)
        try:
            acquire_execution_lease(attempt.send_attempt_id, owner_token)
        except MailExecutionLeaseError as exc:
            raise MailWorkflowError(exc.code) from exc

        try:
            try:
                claimed = claim_prepared_attempt(attempt.send_attempt_id)
            except MailSendAttemptError as exc:
                raise MailWorkflowError(exc.code) from exc
            if claimed.state != "sending" or claimed.identity_hash != attempt.identity_hash:
                raise MailWorkflowError("MAIL_SEND_EXECUTION_CLAIM_INVALID")

            with execution_lease_heartbeat(claimed.send_attempt_id, owner_token) as heartbeat:
                try:
                    result = gateway.send(message)
                except MailGatewayCallError as exc:
                    # The frozen gateway contract defines this exception as a definite
                    # call-boundary failure, not an UNKNOWN transport outcome.
                    return _close_definite_failure(
                        heartbeat,
                        claimed.send_attempt_id,
                        code=exc.code,
                    )
                except Exception:
                    # Once send() was entered, an unclassified exception cannot prove
                    # that transport did not happen. Preserve ambiguity and never invoke again.
                    return _close_unknown(
                        heartbeat,
                        claimed.send_attempt_id,
                        code="MAIL_SEND_GATEWAY_EXCEPTION_UNKNOWN",
                        summary="gateway invocation ended without provable durable transport outcome",
                    )

                terminal = _assert_lease_owned(heartbeat, claimed.send_attempt_id)
                if terminal is not None:
                    return terminal
                try:
                    return record_send_result(claimed.send_attempt_id, result)
                except MailSendAttemptError:
                    # A concurrent process may have fenced/terminalized after our last
                    # ownership proof. Never overwrite its terminal truth. If the attempt
                    # is still nonterminal, re-prove ownership before converting an invalid
                    # result into UNKNOWN.
                    current = get_send_attempt(claimed.send_attempt_id)
                    if current.is_terminal:
                        return current
                    return _close_unknown(
                        heartbeat,
                        claimed.send_attempt_id,
                        code="MAIL_SEND_GATEWAY_RESULT_UNKNOWN",
                        summary="gateway result could not be proven against frozen send identity",
                    )
        finally:
            try:
                release_execution_lease(attempt.send_attempt_id, owner_token)
            except MailExecutionLeaseError:
                # Owner-matched release is cleanup only. Lifecycle truth is already in
                # SendAttempt; never overwrite it because coordination cleanup failed.
                pass
    finally:
        _leave_local_execution(attempt.send_attempt_id)
