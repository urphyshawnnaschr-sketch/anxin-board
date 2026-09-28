"""Immutable Human binding between one approved report and one RecipientConfig version.

R3 owns only the narrow fact that a Human explicitly bound the current immutable
RecipientConfig version to an existing immutable ApprovalSnapshot for future mail use.
It does not own recipient legality/versioning, report approval, SMTP/network, credentials,
SendAttempt execution, UI routing, or checkpoint movement.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

from fastapi import HTTPException

from app.db import get_connection
from app.recipient_config import RecipientConfigError, get_recipient_config_history
from app.report_approval import get_report_approval_snapshot


SCHEMA_VERSION = "approval_recipient_binding_v1"
_TABLE = "report_approval_recipient_bindings"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAX_IDENTITY_CHARS = 200
_SQLITE_SIGNED_INTEGER_MAX = 2**63 - 1

_COLUMNS = (
    "id, schema_version, project_id, report_version_id, approval_snapshot_id, "
    "approval_snapshot_hash, report_content_hash, recipient_config_version_id, "
    "recipient_config_version_no, recipients_hash, confirmed_by, confirmed_at, "
    "confirmed_timezone, confirmed_utc_offset_minutes, human_acknowledged, "
    "idempotency_key, binding_hash, created_at"
)


class ApprovalRecipientBindingError(RuntimeError):
    """Stable non-secret R3 binding failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> ApprovalRecipientBindingError:
    return ApprovalRecipientBindingError(code)


def _positive_int(value: object, *, code: str) -> int:
    if type(value) is not int or not 0 < value <= _SQLITE_SIGNED_INTEGER_MAX:
        raise _fail(code)
    return value


def _hash(value: object, *, code: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise _fail(code)
    return value


def _identity_text(value: object, *, code: str, max_chars: int = _MAX_IDENTITY_CHARS) -> str:
    if type(value) is not str or not value or len(value) > max_chars or value != value.strip():
        raise _fail(code)
    for char in value:
        point = ord(char)
        if point < 32 or 127 <= point <= 159 or point in (0x2028, 0x2029):
            raise _fail(code)
    return value


def _utc_offset(value: object, *, code: str) -> int:
    if type(value) is not int or value < -840 or value > 840:
        raise _fail(code)
    return value


def _idempotency_key(value: object, *, code: str) -> str:
    if type(value) is not str or _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise _fail(code)
    return value


def _timestamp(value: object, *, code: str) -> str:
    text = _identity_text(value, code=code, max_chars=64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _fail(code) from exc
    if parsed.tzinfo is None:
        raise _fail(code)
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _binding_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _ensure_schema() -> None:
    try:
        with get_connection() as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_version TEXT NOT NULL CHECK (schema_version = '{SCHEMA_VERSION}'),
                    project_id INTEGER NOT NULL CHECK (project_id > 0),
                    report_version_id INTEGER NOT NULL CHECK (report_version_id > 0),
                    approval_snapshot_id INTEGER NOT NULL CHECK (approval_snapshot_id > 0),
                    approval_snapshot_hash TEXT NOT NULL,
                    report_content_hash TEXT NOT NULL,
                    recipient_config_version_id INTEGER NOT NULL CHECK (recipient_config_version_id > 0),
                    recipient_config_version_no INTEGER NOT NULL CHECK (recipient_config_version_no > 0),
                    recipients_hash TEXT NOT NULL,
                    confirmed_by TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    confirmed_timezone TEXT NOT NULL,
                    confirmed_utc_offset_minutes INTEGER NOT NULL,
                    human_acknowledged INTEGER NOT NULL CHECK (human_acknowledged = 1),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    binding_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    UNIQUE (project_id, report_version_id),
                    UNIQUE (approval_snapshot_id)
                );
                CREATE TRIGGER IF NOT EXISTS trg_{_TABLE}_no_update
                BEFORE UPDATE ON {_TABLE}
                BEGIN SELECT RAISE(ABORT, 'approval recipient binding is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS trg_{_TABLE}_no_delete
                BEFORE DELETE ON {_TABLE}
                BEGIN SELECT RAISE(ABORT, 'approval recipient binding is append-only'); END;
                """
            )
            columns = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({_TABLE})").fetchall()
            }
            required = {
                "id", "schema_version", "project_id", "report_version_id",
                "approval_snapshot_id", "approval_snapshot_hash", "report_content_hash",
                "recipient_config_version_id", "recipient_config_version_no", "recipients_hash",
                "confirmed_by", "confirmed_at", "confirmed_timezone",
                "confirmed_utc_offset_minutes", "human_acknowledged", "idempotency_key",
                "binding_hash", "created_at",
            }
            if columns != required:
                raise sqlite3.IntegrityError("approval recipient binding schema is obsolete")
            conn.commit()
    except sqlite3.Error as exc:
        raise _fail("REPORT_APPROVAL_RECIPIENT_STORED_INVALID") from exc


def _approval_fact(*, project_id: int, report_version_id: int) -> dict[str, object]:
    try:
        snapshot = get_report_approval_snapshot(
            project_id=project_id,
            report_version_id=report_version_id,
        )
    except HTTPException as exc:
        raise _fail("REPORT_APPROVAL_RECIPIENT_APPROVAL_INVALID") from exc
    if snapshot is None:
        raise _fail("REPORT_APPROVAL_RECIPIENT_APPROVAL_REQUIRED")
    try:
        snapshot_id = _positive_int(
            snapshot.get("approval_snapshot_id"), code="REPORT_APPROVAL_RECIPIENT_APPROVAL_INVALID"
        )
        snapshot_project_id = _positive_int(
            snapshot.get("project_id"), code="REPORT_APPROVAL_RECIPIENT_APPROVAL_INVALID"
        )
        snapshot_report_id = _positive_int(
            snapshot.get("report_version_id"), code="REPORT_APPROVAL_RECIPIENT_APPROVAL_INVALID"
        )
        approval_hash = _hash(
            snapshot.get("approval_snapshot_hash"), code="REPORT_APPROVAL_RECIPIENT_APPROVAL_INVALID"
        )
        report_hash = _hash(
            snapshot.get("report_content_hash"), code="REPORT_APPROVAL_RECIPIENT_APPROVAL_INVALID"
        )
    except AttributeError as exc:
        raise _fail("REPORT_APPROVAL_RECIPIENT_APPROVAL_INVALID") from exc
    if snapshot_project_id != project_id or snapshot_report_id != report_version_id:
        raise _fail("REPORT_APPROVAL_RECIPIENT_APPROVAL_INVALID")
    return {
        "approval_snapshot_id": snapshot_id,
        "approval_snapshot_hash": approval_hash,
        "report_content_hash": report_hash,
    }


def _recipient_history(project_id: int) -> list[dict[str, object]]:
    try:
        history = get_recipient_config_history(project_id)
    except RecipientConfigError as exc:
        if exc.code == "RECIPIENT_CONFIG_PROJECT_NOT_FOUND":
            raise _fail("REPORT_APPROVAL_RECIPIENT_PROJECT_NOT_FOUND") from exc
        raise _fail("REPORT_APPROVAL_RECIPIENT_CONFIG_INVALID") from exc
    return history


def _find_version(
    history: list[dict[str, object]], *, version_id: int, version_no: int
) -> dict[str, object] | None:
    for item in history:
        if item.get("id") == version_id and item.get("version_no") == version_no:
            return item
    return None


def _close_row(row: sqlite3.Row) -> dict[str, object]:
    stored_code = "REPORT_APPROVAL_RECIPIENT_STORED_INVALID"
    if row["schema_version"] != SCHEMA_VERSION:
        raise _fail(stored_code)
    project_id = _positive_int(row["project_id"], code=stored_code)
    report_version_id = _positive_int(row["report_version_id"], code=stored_code)
    approval_id = _positive_int(row["approval_snapshot_id"], code=stored_code)
    approval_hash = _hash(row["approval_snapshot_hash"], code=stored_code)
    report_hash = _hash(row["report_content_hash"], code=stored_code)
    config_id = _positive_int(row["recipient_config_version_id"], code=stored_code)
    config_no = _positive_int(row["recipient_config_version_no"], code=stored_code)
    recipients_hash = _hash(row["recipients_hash"], code=stored_code)
    confirmed_by = _identity_text(row["confirmed_by"], code=stored_code)
    confirmed_at = _timestamp(row["confirmed_at"], code=stored_code)
    confirmed_timezone = _identity_text(
        row["confirmed_timezone"], code=stored_code, max_chars=128
    )
    offset = _utc_offset(row["confirmed_utc_offset_minutes"], code=stored_code)
    if row["human_acknowledged"] != 1:
        raise _fail(stored_code)
    idem = _idempotency_key(row["idempotency_key"], code=stored_code)
    created_at = _timestamp(row["created_at"], code=stored_code)

    try:
        approval = _approval_fact(project_id=project_id, report_version_id=report_version_id)
        history = _recipient_history(project_id)
    except ApprovalRecipientBindingError as exc:
        raise _fail(stored_code) from exc
    if (
        approval["approval_snapshot_id"] != approval_id
        or approval["approval_snapshot_hash"] != approval_hash
        or approval["report_content_hash"] != report_hash
    ):
        raise _fail(stored_code)

    selected = _find_version(history, version_id=config_id, version_no=config_no)
    if selected is None or selected.get("recipients_hash") != recipients_hash:
        raise _fail(stored_code)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "report_version_id": report_version_id,
        "approval_snapshot_id": approval_id,
        "approval_snapshot_hash": approval_hash,
        "report_content_hash": report_hash,
        "recipient_config_version_id": config_id,
        "recipient_config_version_no": config_no,
        "recipients_hash": recipients_hash,
        "confirmed_by": confirmed_by,
        "confirmed_at": confirmed_at,
        "confirmed_timezone": confirmed_timezone,
        "confirmed_utc_offset_minutes": offset,
        "human_acknowledged": 1,
        "idempotency_key": idem,
        "created_at": created_at,
    }
    expected_hash = _binding_hash(payload)
    if row["binding_hash"] != expected_hash:
        raise _fail(stored_code)

    return {
        "id": _positive_int(row["id"], code=stored_code),
        **payload,
        "binding_hash": expected_hash,
        "to_recipients": list(selected["to_recipients"]),
    }


def get_approval_recipient_binding(
    *, project_id: int, report_version_id: int
) -> dict[str, object] | None:
    project_id = _positive_int(project_id, code="REPORT_APPROVAL_RECIPIENT_INPUT_INVALID")
    report_version_id = _positive_int(
        report_version_id, code="REPORT_APPROVAL_RECIPIENT_INPUT_INVALID"
    )
    _ensure_schema()
    try:
        with get_connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE project_id = ? AND report_version_id = ?",
                (project_id, report_version_id),
            ).fetchone()
        return None if row is None else _close_row(row)
    except ApprovalRecipientBindingError:
        raise
    except sqlite3.Error as exc:
        raise _fail("REPORT_APPROVAL_RECIPIENT_STORED_INVALID") from exc


def create_approval_recipient_binding(
    *,
    project_id: int,
    report_version_id: int,
    expected_recipient_config_version_id: int,
    expected_recipient_config_version_no: int,
    confirmed_by: str,
    confirmed_timezone: str,
    confirmed_utc_offset_minutes: int,
    human_confirmed: bool,
    idempotency_key: str,
) -> dict[str, object]:
    """Bind the exact current RecipientConfig to an immutable ApprovalSnapshot once.

    The binding is an explicit Human action and is intentionally separate from report
    approval itself, so mail configuration can never block report approval. Once bound,
    later RecipientConfig versions do not mutate or silently rebind this report.
    """
    input_code = "REPORT_APPROVAL_RECIPIENT_INPUT_INVALID"
    project_id = _positive_int(project_id, code=input_code)
    report_version_id = _positive_int(report_version_id, code=input_code)
    expected_config_id = _positive_int(expected_recipient_config_version_id, code=input_code)
    expected_config_no = _positive_int(expected_recipient_config_version_no, code=input_code)
    if human_confirmed is not True:
        raise _fail("REPORT_APPROVAL_RECIPIENT_HUMAN_CONFIRMATION_REQUIRED")
    confirmer = _identity_text(confirmed_by, code=input_code)
    timezone_name = _identity_text(confirmed_timezone, code=input_code, max_chars=128)
    offset = _utc_offset(confirmed_utc_offset_minutes, code=input_code)
    idem = _idempotency_key(idempotency_key, code=input_code)
    approval = _approval_fact(project_id=project_id, report_version_id=report_version_id)
    _ensure_schema()

    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")

            replay = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE idempotency_key = ?",
                (idem,),
            ).fetchone()
            if replay is not None:
                closed = _close_row(replay)
                replay_matches = (
                    closed["project_id"] == project_id
                    and closed["report_version_id"] == report_version_id
                    and closed["recipient_config_version_id"] == expected_config_id
                    and closed["recipient_config_version_no"] == expected_config_no
                    and closed["confirmed_by"] == confirmer
                    and closed["confirmed_timezone"] == timezone_name
                    and closed["confirmed_utc_offset_minutes"] == offset
                )
                if not replay_matches:
                    raise _fail("REPORT_APPROVAL_RECIPIENT_IDEMPOTENCY_CONFLICT")
                conn.rollback()
                return closed

            existing = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE project_id = ? AND report_version_id = ?",
                (project_id, report_version_id),
            ).fetchone()
            if existing is not None:
                # Re-close every stored authority before presenting it as a legitimate
                # prior binding. Corrupt or dangling rows must surface STORED_INVALID,
                # never be masked as a normal ALREADY_BOUND conflict.
                _close_row(existing)
                raise _fail("REPORT_APPROVAL_RECIPIENT_ALREADY_BOUND")

            # BEGIN IMMEDIATE holds the writer reservation while the immutable R1 owner is
            # read through its public API. A concurrent RecipientConfig append cannot slide
            # between this current-version proof and the binding INSERT.
            history = _recipient_history(project_id)
            if not history:
                raise _fail("REPORT_APPROVAL_RECIPIENT_CONFIG_REQUIRED")
            current = history[-1]
            if current.get("id") != expected_config_id or current.get("version_no") != expected_config_no:
                raise _fail("REPORT_APPROVAL_RECIPIENT_CONFIG_STALE")
            recipients_hash = _hash(
                current.get("recipients_hash"), code="REPORT_APPROVAL_RECIPIENT_CONFIG_INVALID"
            )

            confirmed_at = _now()
            created_at = confirmed_at
            payload = {
                "schema_version": SCHEMA_VERSION,
                "project_id": project_id,
                "report_version_id": report_version_id,
                "approval_snapshot_id": approval["approval_snapshot_id"],
                "approval_snapshot_hash": approval["approval_snapshot_hash"],
                "report_content_hash": approval["report_content_hash"],
                "recipient_config_version_id": expected_config_id,
                "recipient_config_version_no": expected_config_no,
                "recipients_hash": recipients_hash,
                "confirmed_by": confirmer,
                "confirmed_at": confirmed_at,
                "confirmed_timezone": timezone_name,
                "confirmed_utc_offset_minutes": offset,
                "human_acknowledged": 1,
                "idempotency_key": idem,
                "created_at": created_at,
            }
            binding_hash = _binding_hash(payload)
            cursor = conn.execute(
                f"""
                INSERT INTO {_TABLE} (
                    schema_version, project_id, report_version_id, approval_snapshot_id,
                    approval_snapshot_hash, report_content_hash, recipient_config_version_id,
                    recipient_config_version_no, recipients_hash, confirmed_by, confirmed_at,
                    confirmed_timezone, confirmed_utc_offset_minutes, human_acknowledged,
                    idempotency_key, binding_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    SCHEMA_VERSION, project_id, report_version_id,
                    approval["approval_snapshot_id"], approval["approval_snapshot_hash"],
                    approval["report_content_hash"], expected_config_id, expected_config_no,
                    recipients_hash, confirmer, confirmed_at, timezone_name, offset,
                    idem, binding_hash, created_at,
                ),
            )
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            if row is None:
                raise _fail("REPORT_APPROVAL_RECIPIENT_STORED_INVALID")
            closed = _close_row(row)
            conn.commit()
            return closed
    except ApprovalRecipientBindingError:
        raise
    except sqlite3.IntegrityError as exc:
        # BEGIN IMMEDIATE plus the explicit replay/already-bound checks above serialize
        # ordinary duplicate/concurrent Human actions. Any residual constraint/trigger
        # failure is therefore storage/integrity evidence, not a normal user conflict.
        raise _fail("REPORT_APPROVAL_RECIPIENT_STORED_INVALID") from exc
    except sqlite3.Error as exc:
        raise _fail("REPORT_APPROVAL_RECIPIENT_STORED_INVALID") from exc
