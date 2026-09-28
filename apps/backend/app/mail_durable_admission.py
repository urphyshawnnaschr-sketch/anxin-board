"""Durable R4 mail admission authority with no transport side effects.

One append-only row proves which exact accepted authorities admitted one existing-owner
SendAttempt identity. R4 never owns mail lifecycle state, credential bytes, transport
execution, recipient outcomes, or retry policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

from fastapi import HTTPException

from app import anxin_board_report_store as report_store
from app import mail_transport_profile as transport_owner
from app import recipient_config as recipient_owner
from app import report_approval as approval_owner
from app import report_approval_recipient_binding as binding_owner
from app.db import get_connection
from app.mail_admission_candidate import (
    MailAdmissionCandidate,
    MailAdmissionCandidateError,
    build_mail_admission_candidate,
    validate_mail_admission_candidate,
)
from app.mail_readiness import MailReadinessError, get_mail_readiness
from app.mail_report_renderer import MailReportRenderError, render_approved_report_for_mail
from app.approved_report_narrative import ApprovedReportNarrativeError, load_approved_report_narrative
from app.approved_module_narrative import ApprovedModuleNarrativeError, get_approved_module_narrative
from app.approved_report_git_metrics import ApprovedReportGitMetricsError, load_approved_report_git_metrics
from app.mail_send_attempts import (
    MailSendAttemptError,
    SendAttempt,
    SendAttemptBinding,
    create_send_attempt,
    get_send_attempt,
)
from app.project_profiles import ProjectProfileAuthorityError, read_bound_project_profile_for_report


SCHEMA_VERSION = "mail_durable_admission_v1"
_TABLE = "mail_durable_admissions"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_SQLITE_MAX = 2**63 - 1
_COLUMNS = (
    "id, schema_version, send_attempt_id, candidate_admission_hash, project_id, "
    "report_version_id, report_content_hash, approval_snapshot_id, approval_snapshot_hash, "
    "approval_recipient_binding_hash, recipient_config_version_id, recipient_config_version_no, "
    "recipients_hash, recipients_json, transport_profile_id, transport_profile_version_no, "
    "transport_profile_hash, secret_ref, from_identity, formal_report_hash, render_identity, "
    "render_hash, html_sha256, subject, message_id, predecessor_send_attempt_id, created_at, record_hash"
)


class MailDurableAdmissionError(RuntimeError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MailDurableAdmission:
    id: int
    schema_version: str
    send_attempt_id: str
    candidate: MailAdmissionCandidate
    predecessor_send_attempt_id: str | None
    created_at: str
    record_hash: str


def _fail(code: str) -> MailDurableAdmissionError:
    return MailDurableAdmissionError(code)


def _positive(value: object, *, code: str = "MAIL_ADMISSION_STORED_INVALID") -> int:
    if type(value) is not int or not 0 < value <= _SQLITE_MAX:
        raise _fail(code)
    return value


def _hash(value: object, *, code: str = "MAIL_ADMISSION_STORED_INVALID") -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise _fail(code)
    return value


def _identity(value: object, *, code: str = "MAIL_ADMISSION_STORED_INVALID") -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        raise _fail(code)
    return value


def _timestamp(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _fail("MAIL_ADMISSION_STORED_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _fail("MAIL_ADMISSION_STORED_INVALID") from exc
    if parsed.tzinfo is None:
        raise _fail("MAIL_ADMISSION_STORED_INVALID")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _fail("MAIL_ADMISSION_STORED_INVALID") from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _send_attempt_id(candidate_hash: str, predecessor: str | None) -> str:
    lineage = predecessor if predecessor is not None else "ROOT"
    return f"mail-admission-{_sha256(f'{candidate_hash}|{lineage}')[:48]}"


def _ensure_schema() -> None:
    try:
        with get_connection() as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_version TEXT NOT NULL CHECK (schema_version = '{SCHEMA_VERSION}'),
                    send_attempt_id TEXT NOT NULL UNIQUE,
                    candidate_admission_hash TEXT NOT NULL,
                    project_id INTEGER NOT NULL CHECK (project_id > 0),
                    report_version_id INTEGER NOT NULL CHECK (report_version_id > 0),
                    report_content_hash TEXT NOT NULL,
                    approval_snapshot_id INTEGER NOT NULL CHECK (approval_snapshot_id > 0),
                    approval_snapshot_hash TEXT NOT NULL,
                    approval_recipient_binding_hash TEXT NOT NULL,
                    recipient_config_version_id INTEGER NOT NULL CHECK (recipient_config_version_id > 0),
                    recipient_config_version_no INTEGER NOT NULL CHECK (recipient_config_version_no > 0),
                    recipients_hash TEXT NOT NULL,
                    recipients_json TEXT NOT NULL,
                    transport_profile_id INTEGER NOT NULL CHECK (transport_profile_id > 0),
                    transport_profile_version_no INTEGER NOT NULL CHECK (transport_profile_version_no > 0),
                    transport_profile_hash TEXT NOT NULL,
                    secret_ref TEXT NOT NULL,
                    from_identity TEXT NOT NULL,
                    formal_report_hash TEXT NOT NULL,
                    render_identity TEXT NOT NULL,
                    render_hash TEXT NOT NULL,
                    html_sha256 TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    predecessor_send_attempt_id TEXT NULL,
                    created_at TEXT NOT NULL,
                    record_hash TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS ix_{_TABLE}_project_report
                ON {_TABLE}(project_id, report_version_id, id DESC);
                CREATE TRIGGER IF NOT EXISTS trg_{_TABLE}_no_update
                BEFORE UPDATE ON {_TABLE}
                BEGIN SELECT RAISE(ABORT, 'mail durable admission is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS trg_{_TABLE}_no_delete
                BEFORE DELETE ON {_TABLE}
                BEGIN SELECT RAISE(ABORT, 'mail durable admission is append-only'); END;
                """
            )
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({_TABLE})").fetchall()}
            required = {
                "id", "schema_version", "send_attempt_id", "candidate_admission_hash", "project_id",
                "report_version_id", "report_content_hash", "approval_snapshot_id", "approval_snapshot_hash",
                "approval_recipient_binding_hash", "recipient_config_version_id", "recipient_config_version_no",
                "recipients_hash", "recipients_json", "transport_profile_id", "transport_profile_version_no",
                "transport_profile_hash", "secret_ref", "from_identity", "formal_report_hash", "render_identity",
                "render_hash", "html_sha256", "subject", "message_id", "predecessor_send_attempt_id",
                "created_at", "record_hash",
            }
            if columns != required:
                raise sqlite3.IntegrityError("mail durable admission schema is obsolete")
            conn.commit()
    except sqlite3.Error as exc:
        raise _fail("MAIL_ADMISSION_STORED_INVALID") from exc


def _compose_current_candidate_in_transaction(
    conn: sqlite3.Connection, project_id: int
) -> MailAdmissionCandidate:
    """Re-close every source through its existing owner while one SQLite snapshot is held."""
    project_id = _positive(project_id, code="MAIL_ADMISSION_INPUT_INVALID")
    try:
        project = conn.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None:
            raise _fail("MAIL_ADMISSION_PROJECT_NOT_FOUND")

        report_columns = report_store._read_columns(conn)
        report_row = conn.execute(
            f"SELECT {report_columns} FROM anxin_board_reports WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if report_row is None:
            raise _fail("MAIL_ADMISSION_REPORT_REQUIRED")
        _, report = report_store._validate_stored_row(
            report_row,
            conn=conn,
            expected_project_id=project_id,
            expected_project_name=project["name"],
        )
        if report.get("schema_version") != "anxin_board_report_v3":
            raise _fail("MAIL_ADMISSION_REPORT_REQUIRED")
        report_version_id = _positive(report.get("report_version_id"), code="MAIL_ADMISSION_REPORT_INVALID")

        recipient_history = recipient_owner._read_history_in_tx(conn, project_id)
        if not recipient_history:
            raise _fail("MAIL_ADMISSION_RECIPIENT_REQUIRED")
        recipient = recipient_history[-1]

        transport_history = transport_owner._read_history_in_tx(conn)
        if not transport_history:
            raise _fail("MAIL_ADMISSION_TRANSPORT_REQUIRED")
        transport = transport_history[-1]

        binding_row = conn.execute(
            f"SELECT {binding_owner._COLUMNS} FROM {binding_owner._TABLE} "
            "WHERE project_id = ? AND report_version_id = ?",
            (project_id, report_version_id),
        ).fetchone()
        if binding_row is None:
            raise _fail("MAIL_ADMISSION_BINDING_REQUIRED")
        binding = binding_owner._close_row(binding_row)

        approval_row = approval_owner._existing_by_report(conn, project_id, report_version_id)
        if approval_row is None:
            raise _fail("MAIL_ADMISSION_APPROVAL_INVALID")
        approval = approval_owner._close_existing_with_report(conn, approval_row)

        profile_id = _positive(report.get("profile_id"), code="MAIL_ADMISSION_REPORT_INVALID")
        profile = read_bound_project_profile_for_report(profile_id, conn=conn)
        narrative = load_approved_report_narrative(
            project_id=project_id, report=report, approval_snapshot=approval, conn=conn,
        )
        module_narrative = get_approved_module_narrative(
            project_id=project_id, report=report, approval_snapshot=approval, profile=profile, conn=conn,
        )
        git_metrics = load_approved_report_git_metrics(project_id=project_id, report=report, approval_snapshot=approval, conn=conn)
        render = render_approved_report_for_mail(
            report, profile=profile, approval_snapshot=approval, approved_narrative=narrative,
            approved_module_narrative=module_narrative,
            approved_git_metrics=git_metrics,
        )

        return validate_mail_admission_candidate(
            build_mail_admission_candidate(
                approval_recipient_binding=binding,
                approval_snapshot=approval,
                recipient_config=recipient,
                formal_report=report,
                render=render,
                transport_profile=transport,
            )
        )
    except MailDurableAdmissionError:
        raise
    except report_store.AnxinBoardReportProjectNotFoundError as exc:
        raise _fail("MAIL_ADMISSION_PROJECT_NOT_FOUND") from exc
    except report_store.AnxinBoardReportStoredInvalidError as exc:
        raise _fail("MAIL_ADMISSION_REPORT_INVALID") from exc
    except recipient_owner.RecipientConfigError as exc:
        raise _fail("MAIL_ADMISSION_RECIPIENT_INVALID") from exc
    except transport_owner.MailTransportProfileError as exc:
        raise _fail("MAIL_ADMISSION_TRANSPORT_INVALID") from exc
    except binding_owner.ApprovalRecipientBindingError as exc:
        raise _fail("MAIL_ADMISSION_BINDING_INVALID") from exc
    except (HTTPException, ProjectProfileAuthorityError) as exc:
        raise _fail("MAIL_ADMISSION_APPROVAL_INVALID") from exc
    except (ApprovedReportNarrativeError, ApprovedModuleNarrativeError) as exc:
        raise _fail("MAIL_ADMISSION_NARRATIVE_INVALID") from exc
    except (MailReportRenderError, MailAdmissionCandidateError, ApprovedReportGitMetricsError) as exc:
        raise _fail("MAIL_ADMISSION_CANDIDATE_INVALID") from exc
    except sqlite3.Error as exc:
        raise _fail("MAIL_ADMISSION_SOURCE_INVALID") from exc


def _compose_current_candidate(project_id: int) -> MailAdmissionCandidate:
    """Build a current candidate through readiness, then independently re-close it in one DB snapshot."""
    try:
        readiness = get_mail_readiness(project_id)
    except MailReadinessError as exc:
        raise _fail("MAIL_ADMISSION_NOT_READY") from exc
    if readiness.get("candidate_ready") is not True:
        raise _fail("MAIL_ADMISSION_NOT_READY")
    projected = readiness.get("candidate")
    projected_hash = projected.get("admission_hash") if isinstance(projected, dict) else None
    try:
        with get_connection() as conn:
            conn.execute("BEGIN")
            candidate = _compose_current_candidate_in_transaction(conn, project_id)
            conn.commit()
    except MailDurableAdmissionError:
        raise
    except sqlite3.Error as exc:
        raise _fail("MAIL_ADMISSION_SOURCE_INVALID") from exc
    if projected_hash != candidate.admission_hash:
        raise _fail("MAIL_ADMISSION_SOURCE_DRIFT")
    return candidate


def _record_payload(
    candidate: MailAdmissionCandidate,
    *,
    send_attempt_id: str,
    predecessor_send_attempt_id: str | None,
    created_at: str,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "send_attempt_id": send_attempt_id,
        "candidate_admission_hash": candidate.admission_hash,
        "project_id": candidate.project_id,
        "report_version_id": candidate.report_version_id,
        "report_content_hash": candidate.report_content_hash,
        "approval_snapshot_id": candidate.approval_snapshot_id,
        "approval_snapshot_hash": candidate.approval_snapshot_hash,
        "approval_recipient_binding_hash": candidate.approval_recipient_binding_hash,
        "recipient_config_version_id": candidate.recipient_config_version_id,
        "recipient_config_version_no": candidate.recipient_config_version_no,
        "recipients_hash": candidate.recipients_hash,
        "recipients_json": _canonical_json(list(candidate.to_recipients)),
        "transport_profile_id": candidate.transport_profile_id,
        "transport_profile_version_no": candidate.transport_profile_version_no,
        "transport_profile_hash": candidate.transport_profile_hash,
        "secret_ref": candidate.secret_ref,
        "from_identity": candidate.from_identity,
        "formal_report_hash": candidate.formal_report_hash,
        "render_identity": candidate.render_identity,
        "render_hash": candidate.render_hash,
        "html_sha256": candidate.html_sha256,
        "subject": candidate.subject,
        "message_id": candidate.message_id,
        "predecessor_send_attempt_id": predecessor_send_attempt_id,
        "created_at": created_at,
    }


def _candidate_from_row(row: sqlite3.Row) -> MailAdmissionCandidate:
    try:
        raw_recipients = json.loads(row["recipients_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise _fail("MAIL_ADMISSION_STORED_INVALID") from exc
    if type(raw_recipients) is not list:
        raise _fail("MAIL_ADMISSION_STORED_INVALID")
    try:
        candidate = MailAdmissionCandidate(
            schema_version="mail_admission_candidate_v1",
            project_id=_positive(row["project_id"]),
            report_version_id=_positive(row["report_version_id"]),
            report_content_hash=_hash(row["report_content_hash"]),
            approval_snapshot_id=_positive(row["approval_snapshot_id"]),
            approval_snapshot_hash=_hash(row["approval_snapshot_hash"]),
            approval_recipient_binding_hash=_hash(row["approval_recipient_binding_hash"]),
            recipient_config_version_id=_positive(row["recipient_config_version_id"]),
            recipient_config_version_no=_positive(row["recipient_config_version_no"]),
            recipients_hash=_hash(row["recipients_hash"]),
            to_recipients=tuple(raw_recipients),
            transport_profile_id=_positive(row["transport_profile_id"]),
            transport_profile_version_no=_positive(row["transport_profile_version_no"]),
            transport_profile_hash=_hash(row["transport_profile_hash"]),
            secret_ref=row["secret_ref"],
            from_identity=row["from_identity"],
            formal_report_hash=_hash(row["formal_report_hash"]),
            render_identity=row["render_identity"],
            render_hash=_hash(row["render_hash"]),
            html_sha256=_hash(row["html_sha256"]),
            subject=row["subject"],
            message_id=row["message_id"],
            admission_hash=_hash(row["candidate_admission_hash"]),
        )
        return validate_mail_admission_candidate(candidate)
    except MailAdmissionCandidateError as exc:
        raise _fail("MAIL_ADMISSION_STORED_INVALID") from exc


def _close_row(row: sqlite3.Row) -> MailDurableAdmission:
    if row["schema_version"] != SCHEMA_VERSION:
        raise _fail("MAIL_ADMISSION_STORED_INVALID")
    candidate = _candidate_from_row(row)
    send_attempt_id = _identity(row["send_attempt_id"])
    predecessor = row["predecessor_send_attempt_id"]
    if predecessor is not None:
        predecessor = _identity(predecessor)
    created_at = _timestamp(row["created_at"])
    payload = _record_payload(
        candidate,
        send_attempt_id=send_attempt_id,
        predecessor_send_attempt_id=predecessor,
        created_at=created_at,
    )
    record_hash = _hash(row["record_hash"])
    if record_hash != _sha256(_canonical_json(payload)):
        raise _fail("MAIL_ADMISSION_STORED_INVALID")
    return MailDurableAdmission(
        id=_positive(row["id"]),
        schema_version=SCHEMA_VERSION,
        send_attempt_id=send_attempt_id,
        candidate=candidate,
        predecessor_send_attempt_id=predecessor,
        created_at=created_at,
        record_hash=record_hash,
    )


def _send_attempt_binding(
    candidate: MailAdmissionCandidate,
    *,
    send_attempt_id: str,
    predecessor_send_attempt_id: str | None,
) -> SendAttemptBinding:
    return SendAttemptBinding(
        send_attempt_id=send_attempt_id,
        project_id=candidate.project_id,
        approval_snapshot_id=candidate.approval_snapshot_id,
        approval_snapshot_hash=candidate.approval_snapshot_hash,
        report_version_id=candidate.report_version_id,
        report_content_hash=candidate.report_content_hash,
        render_identity=candidate.render_identity,
        render_hash=candidate.render_hash,
        html_sha256=candidate.html_sha256,
        message_id=candidate.message_id,
        to_recipients=candidate.to_recipients,
        subject=candidate.subject,
        from_identity=candidate.from_identity,
        predecessor_send_attempt_id=predecessor_send_attempt_id,
    )


def _assert_attempt_matches_prepared(
    attempt: SendAttempt,
    candidate: MailAdmissionCandidate,
    predecessor_send_attempt_id: str | None,
) -> None:
    if attempt.state != "prepared":
        raise _fail("MAIL_ADMISSION_SEND_ATTEMPT_NOT_PREPARED")
    if (
        attempt.project_id != candidate.project_id
        or attempt.approval_snapshot_id != candidate.approval_snapshot_id
        or attempt.approval_snapshot_hash != candidate.approval_snapshot_hash
        or attempt.report_version_id != candidate.report_version_id
        or attempt.report_content_hash != candidate.report_content_hash
        or attempt.render_identity != candidate.render_identity
        or attempt.render_hash != candidate.render_hash
        or attempt.html_sha256 != candidate.html_sha256
        or attempt.message_id != candidate.message_id
        or attempt.to_recipients != candidate.to_recipients
        or attempt.recipients_hash != candidate.recipients_hash
        or attempt.subject != candidate.subject
        or attempt.from_identity != candidate.from_identity
        or attempt.predecessor_send_attempt_id != predecessor_send_attempt_id
    ):
        raise _fail("MAIL_ADMISSION_SEND_ATTEMPT_MISMATCH")


def _materialize_or_reuse_prepared_attempt(
    candidate: MailAdmissionCandidate,
    *,
    send_attempt_id: str,
    predecessor_send_attempt_id: str | None,
) -> SendAttempt:
    binding = _send_attempt_binding(
        candidate,
        send_attempt_id=send_attempt_id,
        predecessor_send_attempt_id=predecessor_send_attempt_id,
    )
    try:
        attempt = get_send_attempt(send_attempt_id)
    except MailSendAttemptError as exc:
        if exc.code != "MAIL_SEND_ATTEMPT_NOT_FOUND":
            raise _fail("MAIL_ADMISSION_SEND_ATTEMPT_REJECTED") from exc
        try:
            attempt = create_send_attempt(binding)
        except MailSendAttemptError as create_exc:
            raise _fail("MAIL_ADMISSION_SEND_ATTEMPT_REJECTED") from create_exc
    _assert_attempt_matches_prepared(attempt, candidate, predecessor_send_attempt_id)
    return attempt


def get_durable_mail_admission(send_attempt_id: str) -> MailDurableAdmission:
    attempt_id = _identity(send_attempt_id, code="MAIL_ADMISSION_INPUT_INVALID")
    _ensure_schema()
    try:
        with get_connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE send_attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise _fail("MAIL_ADMISSION_NOT_FOUND")
            return _close_row(row)
    except MailDurableAdmissionError:
        raise
    except sqlite3.Error as exc:
        raise _fail("MAIL_ADMISSION_STORED_INVALID") from exc


def prepare_durable_mail_admission(
    *,
    project_id: int,
    expected_admission_hash: str,
    predecessor_send_attempt_id: str | None = None,
) -> tuple[MailDurableAdmission, SendAttempt]:
    """Prepare/reuse an owner-validated prepared attempt, then durably admit it.

    No R4 row is inserted until SendAttempt owner materialization/predecessor validation has
    succeeded. The final source reclosure and prepared-state proof run while BEGIN IMMEDIATE
    excludes any concurrent source/attempt writer from sliding into the admission commit.
    """
    project_id = _positive(project_id, code="MAIL_ADMISSION_INPUT_INVALID")
    expected_hash = _hash(expected_admission_hash, code="MAIL_ADMISSION_INPUT_INVALID")
    predecessor = predecessor_send_attempt_id
    if predecessor is not None:
        predecessor = _identity(predecessor, code="MAIL_ADMISSION_INPUT_INVALID")

    candidate_before = _compose_current_candidate(project_id)
    if candidate_before.admission_hash != expected_hash:
        raise _fail("MAIL_ADMISSION_CANDIDATE_STALE")

    _ensure_schema()
    send_attempt_id = _send_attempt_id(expected_hash, predecessor)
    _materialize_or_reuse_prepared_attempt(
        candidate_before,
        send_attempt_id=send_attempt_id,
        predecessor_send_attempt_id=predecessor,
    )

    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidate = _compose_current_candidate_in_transaction(conn, project_id)
            if candidate.admission_hash != expected_hash or candidate != candidate_before:
                raise _fail("MAIL_ADMISSION_SOURCE_DRIFT")

            # While this writer reservation is held, another connection cannot claim or
            # terminalize the SendAttempt. Re-read through the existing owner and require
            # exact prepared state immediately before admission persistence/replay.
            try:
                attempt = get_send_attempt(send_attempt_id)
            except MailSendAttemptError as exc:
                raise _fail("MAIL_ADMISSION_SEND_ATTEMPT_REJECTED") from exc
            _assert_attempt_matches_prepared(attempt, candidate, predecessor)

            existing = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE send_attempt_id = ?",
                (send_attempt_id,),
            ).fetchone()
            if existing is not None:
                admission = _close_row(existing)
                if (
                    admission.candidate != candidate
                    or admission.predecessor_send_attempt_id != predecessor
                ):
                    raise _fail("MAIL_ADMISSION_CONFLICT")
                conn.rollback()
                return admission, attempt

            created_at = _now()
            payload = _record_payload(
                candidate,
                send_attempt_id=send_attempt_id,
                predecessor_send_attempt_id=predecessor,
                created_at=created_at,
            )
            record_hash = _sha256(_canonical_json(payload))
            cursor = conn.execute(
                f"""
                INSERT INTO {_TABLE} (
                    schema_version, send_attempt_id, candidate_admission_hash, project_id,
                    report_version_id, report_content_hash, approval_snapshot_id, approval_snapshot_hash,
                    approval_recipient_binding_hash, recipient_config_version_id,
                    recipient_config_version_no, recipients_hash, recipients_json,
                    transport_profile_id, transport_profile_version_no, transport_profile_hash,
                    secret_ref, from_identity, formal_report_hash, render_identity, render_hash,
                    html_sha256, subject, message_id, predecessor_send_attempt_id, created_at, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SCHEMA_VERSION, send_attempt_id, candidate.admission_hash, candidate.project_id,
                    candidate.report_version_id, candidate.report_content_hash,
                    candidate.approval_snapshot_id, candidate.approval_snapshot_hash,
                    candidate.approval_recipient_binding_hash, candidate.recipient_config_version_id,
                    candidate.recipient_config_version_no, candidate.recipients_hash,
                    payload["recipients_json"], candidate.transport_profile_id,
                    candidate.transport_profile_version_no, candidate.transport_profile_hash,
                    candidate.secret_ref, candidate.from_identity, candidate.formal_report_hash,
                    candidate.render_identity, candidate.render_hash, candidate.html_sha256,
                    candidate.subject, candidate.message_id, predecessor, created_at, record_hash,
                ),
            )
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            if row is None:
                raise _fail("MAIL_ADMISSION_STORED_INVALID")
            admission = _close_row(row)
            conn.commit()
            return admission, attempt
    except MailDurableAdmissionError:
        raise
    except sqlite3.IntegrityError as exc:
        raise _fail("MAIL_ADMISSION_STORED_INVALID") from exc
    except sqlite3.Error as exc:
        raise _fail("MAIL_ADMISSION_STORED_INVALID") from exc
