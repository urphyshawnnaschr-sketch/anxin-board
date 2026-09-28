"""Durable repository-only Mail SendAttempt state and recipient result ledger.

This module owns immutable local send identity, append-only recipient outcomes, and
bounded lifecycle persistence. A separately confirmed document revision delegates
source closure to its annotation owner. It does not configure recipients, access
credentials, perform network I/O, or advance report checkpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
from typing import Iterable

from app.db import get_connection
from app.mail_gateway import (
    MailContractError,
    MailSendResult,
    RecipientOutcome,
    _dedupe_recipients,
    _validate_address,
    _validate_message_id,
    _validate_subject,
)


SEND_ATTEMPT_SCHEMA_VERSION = "mail_send_attempt_v1"
_PREPARED = "prepared"
_SENDING = "sending"
_TERMINAL_STATES = frozenset({"sent", "partial", "failed", "unknown", "voided"})
_ALLOWED_STATES = frozenset({_PREPARED, _SENDING, *_TERMINAL_STATES})
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MAX_ID_CHARS = 256
_MAX_SUMMARY_CHARS = 256
_SQLITE_SIGNED_INTEGER_MAX = 2**63 - 1

_ATTEMPT_COLUMNS = (
    "id, schema_version, send_attempt_id, project_id, approval_snapshot_id, "
    "approval_snapshot_hash, report_version_id, report_content_hash, "
    "render_identity, render_hash, html_sha256, message_id, recipients_json, "
    "recipients_hash, subject, from_identity, predecessor_send_attempt_id, "
    "state, identity_hash, terminal_code, terminal_summary, created_at, updated_at"
)
_RESULT_COLUMNS = (
    "id, send_attempt_id, ordinal, recipient, recipient_hash, outcome, error_code, "
    "summary, recorded_at"
)


class MailSendAttemptError(RuntimeError):
    """Stable local mail-state error that never carries message bodies or secrets."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SendAttemptBinding:
    """Foreign-owner identities frozen for one repository-only SendAttempt."""

    send_attempt_id: str
    project_id: int
    approval_snapshot_id: int
    approval_snapshot_hash: str
    report_version_id: int
    report_content_hash: str
    render_identity: str
    render_hash: str
    html_sha256: str
    message_id: str
    to_recipients: tuple[str, ...]
    subject: str
    from_identity: str
    predecessor_send_attempt_id: str | None = None


@dataclass(frozen=True, slots=True)
class StoredRecipientResult:
    send_attempt_id: str
    ordinal: int
    recipient: str
    recipient_hash: str
    outcome: RecipientOutcome
    error_code: str
    summary: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class SendAttempt:
    id: int
    schema_version: str
    send_attempt_id: str
    project_id: int
    approval_snapshot_id: int
    approval_snapshot_hash: str
    report_version_id: int
    report_content_hash: str
    render_identity: str
    render_hash: str
    html_sha256: str
    message_id: str
    to_recipients: tuple[str, ...]
    recipients_hash: str
    subject: str
    from_identity: str
    predecessor_send_attempt_id: str | None
    state: str
    identity_hash: str
    terminal_code: str | None
    terminal_summary: str | None
    created_at: str
    updated_at: str
    recipient_results: tuple[StoredRecipientResult, ...]

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fail(code: str) -> MailSendAttemptError:
    return MailSendAttemptError(code)


def _positive_int(value: object, *, code: str) -> int:
    if type(value) is not int or not 0 < value <= _SQLITE_SIGNED_INTEGER_MAX:
        raise _fail(code)
    return value


def _identity_text(value: object, *, code: str, max_chars: int = _MAX_ID_CHARS) -> str:
    if type(value) is not str or not value or len(value) > max_chars:
        raise _fail(code)
    if value != value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise _fail(code)
    return value


def _hash(value: object, *, code: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise _fail(code)
    return value


def _bounded_code(value: object, *, code: str) -> str:
    if type(value) is not str or _CODE_RE.fullmatch(value) is None:
        raise _fail(code)
    return value


def _bounded_summary(value: object, *, code: str) -> str:
    if type(value) is not str or not value or len(value) > _MAX_SUMMARY_CHARS:
        raise _fail(code)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise _fail(code)
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_recipients(value: object) -> tuple[tuple[str, ...], str, str]:
    try:
        recipients = _dedupe_recipients(value)
    except MailContractError as exc:
        raise _fail(exc.code) from exc
    recipients_json = _canonical_json(list(recipients))
    return recipients, recipients_json, _sha256_text(recipients_json)


def _validated_binding(binding: SendAttemptBinding) -> tuple[dict[str, object], tuple[str, ...]]:
    if not isinstance(binding, SendAttemptBinding):
        raise TypeError("binding must be SendAttemptBinding")
    send_attempt_id = _identity_text(binding.send_attempt_id, code="MAIL_SEND_ATTEMPT_ID_INVALID")
    project_id = _positive_int(binding.project_id, code="MAIL_SEND_PROJECT_ID_INVALID")
    approval_snapshot_id = _positive_int(
        binding.approval_snapshot_id, code="MAIL_SEND_APPROVAL_ID_INVALID"
    )
    approval_snapshot_hash = _hash(
        binding.approval_snapshot_hash, code="MAIL_SEND_APPROVAL_HASH_INVALID"
    )
    report_version_id = _positive_int(
        binding.report_version_id, code="MAIL_SEND_REPORT_VERSION_ID_INVALID"
    )
    report_content_hash = _hash(
        binding.report_content_hash, code="MAIL_SEND_REPORT_HASH_INVALID"
    )
    render_identity = _identity_text(
        binding.render_identity, code="MAIL_SEND_RENDER_IDENTITY_INVALID"
    )
    render_hash = _hash(binding.render_hash, code="MAIL_SEND_RENDER_HASH_INVALID")
    html_sha256 = _hash(binding.html_sha256, code="MAIL_SEND_HTML_HASH_INVALID")
    try:
        message_id = _validate_message_id(binding.message_id)
        subject = _validate_subject(binding.subject)
        from_identity = _validate_address(
            binding.from_identity, code="MAIL_FROM_IDENTITY_INVALID"
        )
    except MailContractError as exc:
        raise _fail(exc.code) from exc
    recipients, recipients_json, recipients_hash = _canonical_recipients(binding.to_recipients)
    predecessor = binding.predecessor_send_attempt_id
    if predecessor is not None:
        predecessor = _identity_text(predecessor, code="MAIL_SEND_PREDECESSOR_ID_INVALID")
        if predecessor == send_attempt_id:
            raise _fail("MAIL_SEND_PREDECESSOR_SELF_REFERENCE")
    values: dict[str, object] = {
        "schema_version": SEND_ATTEMPT_SCHEMA_VERSION,
        "send_attempt_id": send_attempt_id,
        "project_id": project_id,
        "approval_snapshot_id": approval_snapshot_id,
        "approval_snapshot_hash": approval_snapshot_hash,
        "report_version_id": report_version_id,
        "report_content_hash": report_content_hash,
        "render_identity": render_identity,
        "render_hash": render_hash,
        "html_sha256": html_sha256,
        "message_id": message_id,
        "recipients_json": recipients_json,
        "recipients_hash": recipients_hash,
        "subject": subject,
        "from_identity": from_identity,
        "predecessor_send_attempt_id": predecessor,
    }
    return values, recipients


def _identity_hash(values: dict[str, object], created_at: str) -> str:
    identity = {
        "schema_version": values["schema_version"],
        "send_attempt_id": values["send_attempt_id"],
        "project_id": values["project_id"],
        "approval_snapshot_id": values["approval_snapshot_id"],
        "approval_snapshot_hash": values["approval_snapshot_hash"],
        "report_version_id": values["report_version_id"],
        "report_content_hash": values["report_content_hash"],
        "render_identity": values["render_identity"],
        "render_hash": values["render_hash"],
        "html_sha256": values["html_sha256"],
        "message_id": values["message_id"],
        "recipients_json": values["recipients_json"],
        "recipients_hash": values["recipients_hash"],
        "subject": values["subject"],
        "from_identity": values["from_identity"],
        "predecessor_send_attempt_id": values["predecessor_send_attempt_id"],
        "created_at": created_at,
    }
    return _sha256_text(_canonical_json(identity))


def _rows_for_attempt(conn: sqlite3.Connection, send_attempt_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT {_RESULT_COLUMNS} FROM mail_send_recipient_results "
        "WHERE send_attempt_id = ? ORDER BY ordinal ASC",
        (send_attempt_id,),
    ).fetchall()


def _stored_results(
    rows: Iterable[sqlite3.Row], *, send_attempt_id: str, recipients: tuple[str, ...]
) -> tuple[StoredRecipientResult, ...]:
    results: list[StoredRecipientResult] = []
    for expected_ordinal, row in enumerate(rows, start=1):
        if row["send_attempt_id"] != send_attempt_id or row["ordinal"] != expected_ordinal:
            raise _fail("MAIL_SEND_STORED_RESULT_INVALID")
        if expected_ordinal > len(recipients) or row["recipient"] != recipients[expected_ordinal - 1]:
            raise _fail("MAIL_SEND_STORED_RESULT_INVALID")
        recipient = row["recipient"]
        if row["recipient_hash"] != _sha256_text(recipient):
            raise _fail("MAIL_SEND_STORED_RESULT_INVALID")
        try:
            outcome = RecipientOutcome(row["outcome"])
        except (TypeError, ValueError) as exc:
            raise _fail("MAIL_SEND_STORED_RESULT_INVALID") from exc
        error_code = _bounded_code(row["error_code"], code="MAIL_SEND_STORED_RESULT_INVALID")
        summary = _bounded_summary(row["summary"], code="MAIL_SEND_STORED_RESULT_INVALID")
        recorded_at = _identity_text(
            row["recorded_at"], code="MAIL_SEND_STORED_RESULT_INVALID", max_chars=64
        )
        results.append(
            StoredRecipientResult(
                send_attempt_id=send_attempt_id,
                ordinal=expected_ordinal,
                recipient=recipient,
                recipient_hash=row["recipient_hash"],
                outcome=outcome,
                error_code=error_code,
                summary=summary,
                recorded_at=recorded_at,
            )
        )
    return tuple(results)


def _derive_result_state(results: tuple[StoredRecipientResult, ...]) -> str:
    if not results:
        raise _fail("MAIL_SEND_RESULT_CARDINALITY_INVALID")
    outcomes = tuple(item.outcome for item in results)
    if RecipientOutcome.UNKNOWN in outcomes:
        return "unknown"
    if all(item is RecipientOutcome.ACCEPTED for item in outcomes):
        return "sent"
    if all(item is RecipientOutcome.REJECTED for item in outcomes):
        return "failed"
    return "partial"


def _attempt_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> SendAttempt:
    if row["schema_version"] != SEND_ATTEMPT_SCHEMA_VERSION:
        raise _fail("MAIL_SEND_STORED_ATTEMPT_INVALID")
    state = row["state"]
    if state not in _ALLOWED_STATES:
        raise _fail("MAIL_SEND_STORED_ATTEMPT_INVALID")
    try:
        raw_recipients = json.loads(row["recipients_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise _fail("MAIL_SEND_STORED_ATTEMPT_INVALID") from exc
    recipients, recipients_json, recipients_hash = _canonical_recipients(raw_recipients)
    if row["recipients_json"] != recipients_json or row["recipients_hash"] != recipients_hash:
        raise _fail("MAIL_SEND_STORED_ATTEMPT_INVALID")

    values = {
        "schema_version": row["schema_version"],
        "send_attempt_id": _identity_text(
            row["send_attempt_id"], code="MAIL_SEND_STORED_ATTEMPT_INVALID"
        ),
        "project_id": _positive_int(row["project_id"], code="MAIL_SEND_STORED_ATTEMPT_INVALID"),
        "approval_snapshot_id": _positive_int(
            row["approval_snapshot_id"], code="MAIL_SEND_STORED_ATTEMPT_INVALID"
        ),
        "approval_snapshot_hash": _hash(
            row["approval_snapshot_hash"], code="MAIL_SEND_STORED_ATTEMPT_INVALID"
        ),
        "report_version_id": _positive_int(
            row["report_version_id"], code="MAIL_SEND_STORED_ATTEMPT_INVALID"
        ),
        "report_content_hash": _hash(
            row["report_content_hash"], code="MAIL_SEND_STORED_ATTEMPT_INVALID"
        ),
        "render_identity": _identity_text(
            row["render_identity"], code="MAIL_SEND_STORED_ATTEMPT_INVALID"
        ),
        "render_hash": _hash(row["render_hash"], code="MAIL_SEND_STORED_ATTEMPT_INVALID"),
        "html_sha256": _hash(row["html_sha256"], code="MAIL_SEND_STORED_ATTEMPT_INVALID"),
        "message_id": row["message_id"],
        "recipients_json": recipients_json,
        "recipients_hash": recipients_hash,
        "subject": row["subject"],
        "from_identity": row["from_identity"],
        "predecessor_send_attempt_id": row["predecessor_send_attempt_id"],
    }
    try:
        values["message_id"] = _validate_message_id(values["message_id"])
        values["subject"] = _validate_subject(values["subject"])
        values["from_identity"] = _validate_address(
            values["from_identity"], code="MAIL_FROM_IDENTITY_INVALID"
        )
    except MailContractError as exc:
        raise _fail("MAIL_SEND_STORED_ATTEMPT_INVALID") from exc
    predecessor = values["predecessor_send_attempt_id"]
    if predecessor is not None:
        values["predecessor_send_attempt_id"] = _identity_text(
            predecessor, code="MAIL_SEND_STORED_ATTEMPT_INVALID"
        )
    created_at = _identity_text(
        row["created_at"], code="MAIL_SEND_STORED_ATTEMPT_INVALID", max_chars=64
    )
    updated_at = _identity_text(
        row["updated_at"], code="MAIL_SEND_STORED_ATTEMPT_INVALID", max_chars=64
    )
    identity_hash = _hash(row["identity_hash"], code="MAIL_SEND_STORED_ATTEMPT_INVALID")
    if identity_hash != _identity_hash(values, created_at):
        raise _fail("MAIL_SEND_STORED_ATTEMPT_INVALID")

    terminal_code = row["terminal_code"]
    terminal_summary = row["terminal_summary"]
    if terminal_code is not None:
        terminal_code = _bounded_code(terminal_code, code="MAIL_SEND_STORED_ATTEMPT_INVALID")
    if terminal_summary is not None:
        terminal_summary = _bounded_summary(
            terminal_summary, code="MAIL_SEND_STORED_ATTEMPT_INVALID"
        )
    if (terminal_code is None) != (terminal_summary is None):
        raise _fail("MAIL_SEND_STORED_ATTEMPT_INVALID")

    stored_results = _stored_results(
        _rows_for_attempt(conn, values["send_attempt_id"]),
        send_attempt_id=values["send_attempt_id"],
        recipients=recipients,
    )
    if state in {_PREPARED, _SENDING}:
        if stored_results or terminal_code is not None:
            raise _fail("MAIL_SEND_STORED_ATTEMPT_INVALID")
    elif stored_results:
        if len(stored_results) != len(recipients) or _derive_result_state(stored_results) != state:
            raise _fail("MAIL_SEND_STORED_ATTEMPT_INVALID")
        if terminal_code is not None:
            raise _fail("MAIL_SEND_STORED_ATTEMPT_INVALID")
    elif state in {"failed", "unknown", "voided"}:
        if terminal_code is None:
            raise _fail("MAIL_SEND_STORED_ATTEMPT_INVALID")
    else:
        raise _fail("MAIL_SEND_STORED_ATTEMPT_INVALID")

    return SendAttempt(
        id=_positive_int(row["id"], code="MAIL_SEND_STORED_ATTEMPT_INVALID"),
        schema_version=SEND_ATTEMPT_SCHEMA_VERSION,
        send_attempt_id=values["send_attempt_id"],
        project_id=values["project_id"],
        approval_snapshot_id=values["approval_snapshot_id"],
        approval_snapshot_hash=values["approval_snapshot_hash"],
        report_version_id=values["report_version_id"],
        report_content_hash=values["report_content_hash"],
        render_identity=values["render_identity"],
        render_hash=values["render_hash"],
        html_sha256=values["html_sha256"],
        message_id=values["message_id"],
        to_recipients=recipients,
        recipients_hash=recipients_hash,
        subject=values["subject"],
        from_identity=values["from_identity"],
        predecessor_send_attempt_id=values["predecessor_send_attempt_id"],
        state=state,
        identity_hash=identity_hash,
        terminal_code=terminal_code,
        terminal_summary=terminal_summary,
        created_at=created_at,
        updated_at=updated_at,
        recipient_results=stored_results,
    )


def _get_with_connection(conn: sqlite3.Connection, send_attempt_id: str) -> SendAttempt:
    attempt_id = _identity_text(send_attempt_id, code="MAIL_SEND_ATTEMPT_ID_INVALID")
    row = conn.execute(
        f"SELECT {_ATTEMPT_COLUMNS} FROM mail_send_attempts WHERE send_attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise _fail("MAIL_SEND_ATTEMPT_NOT_FOUND")
    return _attempt_from_row(conn, row)


def _validate_predecessor(
    conn: sqlite3.Connection, values: dict[str, object], predecessor_id: str | None
) -> None:
    if predecessor_id is None:
        return
    predecessor = _get_with_connection(conn, predecessor_id)
    if predecessor.project_id != values["project_id"]:
        raise _fail("MAIL_SEND_PREDECESSOR_PROJECT_MISMATCH")
    if predecessor.state in {_PREPARED, _SENDING}:
        raise _fail("MAIL_SEND_PREDECESSOR_NOT_TERMINAL")

    from app.mail_document_revision import validated_module_revision
    if validated_module_revision(conn, predecessor, values):
        return

    same_approval = (
        predecessor.approval_snapshot_id == values["approval_snapshot_id"]
        and predecessor.approval_snapshot_hash == values["approval_snapshot_hash"]
    )
    if same_approval:
        required_equal = (
            (predecessor.report_version_id, values["report_version_id"]),
            (predecessor.report_content_hash, values["report_content_hash"]),
            (predecessor.render_identity, values["render_identity"]),
            (predecessor.render_hash, values["render_hash"]),
            (predecessor.html_sha256, values["html_sha256"]),
            (predecessor.recipients_hash, values["recipients_hash"]),
        )
        if any(left != right for left, right in required_equal):
            raise _fail("MAIL_SEND_SAME_APPROVAL_IDENTITY_CHANGED")

    same_report = (
        predecessor.report_version_id == values["report_version_id"]
        and predecessor.report_content_hash == values["report_content_hash"]
    )
    if not (same_approval or same_report):
        raise _fail("MAIL_SEND_PREDECESSOR_IDENTITY_MISMATCH")
    if same_report and predecessor.message_id != values["message_id"]:
        raise _fail("MAIL_SEND_SAME_REPORT_MESSAGE_ID_CHANGED")


def create_send_attempt(binding: SendAttemptBinding) -> SendAttempt:
    """Persist one immutable prepared attempt from a foreign-owner frozen binding."""
    values, _ = _validated_binding(binding)
    created_at = _now()
    identity_hash = _identity_hash(values, created_at)
    try:
        with get_connection() as conn:
            _validate_predecessor(
                conn, values, values["predecessor_send_attempt_id"]  # type: ignore[arg-type]
            )
            conn.execute(
                """
                INSERT INTO mail_send_attempts (
                    schema_version, send_attempt_id, project_id,
                    approval_snapshot_id, approval_snapshot_hash,
                    report_version_id, report_content_hash,
                    render_identity, render_hash, html_sha256, message_id,
                    recipients_json, recipients_hash, subject, from_identity,
                    predecessor_send_attempt_id, state, identity_hash,
                    terminal_code, terminal_summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, NULL, NULL, ?, ?)
                """,
                (
                    values["schema_version"],
                    values["send_attempt_id"],
                    values["project_id"],
                    values["approval_snapshot_id"],
                    values["approval_snapshot_hash"],
                    values["report_version_id"],
                    values["report_content_hash"],
                    values["render_identity"],
                    values["render_hash"],
                    values["html_sha256"],
                    values["message_id"],
                    values["recipients_json"],
                    values["recipients_hash"],
                    values["subject"],
                    values["from_identity"],
                    values["predecessor_send_attempt_id"],
                    identity_hash,
                    created_at,
                    created_at,
                ),
            )
            return _get_with_connection(conn, values["send_attempt_id"])  # type: ignore[arg-type]
    except sqlite3.IntegrityError as exc:
        raise _fail("MAIL_SEND_ATTEMPT_CONFLICT") from exc


def get_send_attempt(send_attempt_id: str) -> SendAttempt:
    """Read and self-validate one attempt and its immutable result ledger."""
    with get_connection() as conn:
        return _get_with_connection(conn, send_attempt_id)


def list_send_attempt_history(project_id: int) -> tuple[SendAttempt, ...]:
    """Read append-only local mail history for one project in creation order."""
    project = _positive_int(project_id, code="MAIL_SEND_PROJECT_ID_INVALID")
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT {_ATTEMPT_COLUMNS} FROM mail_send_attempts "
            "WHERE project_id = ? ORDER BY id ASC",
            (project,),
        ).fetchall()
        return tuple(_attempt_from_row(conn, row) for row in rows)


def claim_prepared_attempt(send_attempt_id: str) -> SendAttempt:
    """Atomically claim exactly one prepared attempt for one transport invocation."""
    attempt_id = _identity_text(send_attempt_id, code="MAIL_SEND_ATTEMPT_ID_INVALID")
    with get_connection() as conn:
        current = _get_with_connection(conn, attempt_id)
        if current.state != _PREPARED:
            raise _fail("MAIL_SEND_ATTEMPT_NOT_PREPARED")
        updated_at = _now()
        cursor = conn.execute(
            """
            UPDATE mail_send_attempts
            SET state = 'sending', updated_at = ?
            WHERE send_attempt_id = ? AND state = 'prepared'
            """,
            (updated_at, attempt_id),
        )
        if cursor.rowcount != 1:
            raise _fail("MAIL_SEND_ATTEMPT_NOT_PREPARED")
        return _get_with_connection(conn, attempt_id)


def recover_stale_sending_as_unknown(send_attempt_id: str) -> SendAttempt:
    """Conservatively close a persisted orphan sending claim without re-sending."""
    return _close_without_results(
        send_attempt_id,
        required_state=_SENDING,
        target_state="unknown",
        code="MAIL_SEND_INTERRUPTED_UNKNOWN",
        summary="prior transport outcome cannot be proven after execution interruption",
    )


def close_pre_send_failure(send_attempt_id: str, *, code: str) -> SendAttempt:
    """Close a gateway-declared definite pre-send failure without recipient fiction."""
    failure_code = _bounded_code(code, code="MAIL_SEND_FAILURE_CODE_INVALID")
    return _close_without_results(
        send_attempt_id,
        required_state=_SENDING,
        target_state="failed",
        code=failure_code,
        summary="gateway reported a definite call failure before send completion",
    )


def close_post_call_unknown(
    send_attempt_id: str,
    *,
    code: str = "MAIL_SEND_POST_CALL_UNKNOWN",
    summary: str = "transport was invoked but durable outcome cannot be proven",
) -> SendAttempt:
    """Close post-invocation ambiguity as unknown; never infer safe retry."""
    return _close_without_results(
        send_attempt_id,
        required_state=_SENDING,
        target_state="unknown",
        code=_bounded_code(code, code="MAIL_SEND_FAILURE_CODE_INVALID"),
        summary=_bounded_summary(summary, code="MAIL_SEND_FAILURE_SUMMARY_INVALID"),
    )


def _close_without_results(
    send_attempt_id: str,
    *,
    required_state: str,
    target_state: str,
    code: str,
    summary: str,
) -> SendAttempt:
    attempt_id = _identity_text(send_attempt_id, code="MAIL_SEND_ATTEMPT_ID_INVALID")
    if target_state not in {"failed", "unknown", "voided"}:
        raise _fail("MAIL_SEND_TARGET_STATE_INVALID")
    with get_connection() as conn:
        current = _get_with_connection(conn, attempt_id)
        if current.state != required_state or current.recipient_results:
            raise _fail("MAIL_SEND_ATTEMPT_STATE_CONFLICT")
        updated_at = _now()
        cursor = conn.execute(
            """
            UPDATE mail_send_attempts
            SET state = ?, terminal_code = ?, terminal_summary = ?, updated_at = ?
            WHERE send_attempt_id = ? AND state = ?
            """,
            (target_state, code, summary, updated_at, attempt_id, required_state),
        )
        if cursor.rowcount != 1:
            raise _fail("MAIL_SEND_ATTEMPT_STATE_CONFLICT")
        return _get_with_connection(conn, attempt_id)


def record_send_result(send_attempt_id: str, result: MailSendResult) -> SendAttempt:
    """Append one exact result row per frozen recipient and close the attempt atomically."""
    if not isinstance(result, MailSendResult):
        raise _fail("MAIL_SEND_GATEWAY_RESULT_INVALID")
    attempt_id = _identity_text(send_attempt_id, code="MAIL_SEND_ATTEMPT_ID_INVALID")
    with get_connection() as conn:
        current = _get_with_connection(conn, attempt_id)
        if current.state != _SENDING or current.recipient_results:
            raise _fail("MAIL_SEND_ATTEMPT_STATE_CONFLICT")
        if result.message_id != current.message_id:
            raise _fail("MAIL_SEND_GATEWAY_RESULT_INVALID")
        if result.requested_recipients != current.to_recipients:
            raise _fail("MAIL_SEND_GATEWAY_RESULT_INVALID")
        if tuple(item.recipient for item in result.recipient_results) != current.to_recipients:
            raise _fail("MAIL_SEND_GATEWAY_RESULT_INVALID")
        if len(result.recipient_results) != len(current.to_recipients):
            raise _fail("MAIL_SEND_GATEWAY_RESULT_INVALID")

        recorded_at = _now()
        stored_for_derivation: list[StoredRecipientResult] = []
        try:
            for ordinal, item in enumerate(result.recipient_results, start=1):
                if not isinstance(item.outcome, RecipientOutcome):
                    raise _fail("MAIL_SEND_GATEWAY_RESULT_INVALID")
                code = _bounded_code(item.error_code, code="MAIL_SEND_GATEWAY_RESULT_INVALID")
                summary = _bounded_summary(item.summary, code="MAIL_SEND_GATEWAY_RESULT_INVALID")
                recipient_hash = _sha256_text(item.recipient)
                conn.execute(
                    """
                    INSERT INTO mail_send_recipient_results (
                        send_attempt_id, ordinal, recipient, recipient_hash,
                        outcome, error_code, summary, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        ordinal,
                        item.recipient,
                        recipient_hash,
                        item.outcome.value,
                        code,
                        summary,
                        recorded_at,
                    ),
                )
                stored_for_derivation.append(
                    StoredRecipientResult(
                        send_attempt_id=attempt_id,
                        ordinal=ordinal,
                        recipient=item.recipient,
                        recipient_hash=recipient_hash,
                        outcome=item.outcome,
                        error_code=code,
                        summary=summary,
                        recorded_at=recorded_at,
                    )
                )
            final_state = _derive_result_state(tuple(stored_for_derivation))
            cursor = conn.execute(
                """
                UPDATE mail_send_attempts
                SET state = ?, updated_at = ?
                WHERE send_attempt_id = ? AND state = 'sending'
                """,
                (final_state, recorded_at, attempt_id),
            )
            if cursor.rowcount != 1:
                raise _fail("MAIL_SEND_ATTEMPT_STATE_CONFLICT")
        except sqlite3.IntegrityError as exc:
            raise _fail("MAIL_SEND_RESULT_APPEND_CONFLICT") from exc
        return _get_with_connection(conn, attempt_id)
