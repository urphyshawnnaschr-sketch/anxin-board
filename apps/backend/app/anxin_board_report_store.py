"""Local SQLite persistence for validated formal Anxin Board reports."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

from app.anxin_board_report import (
    SCHEMA_VERSION as V1_SCHEMA_VERSION,
    validate_anxin_board_report,
)
from app.anxin_board_report_v2 import (
    SCHEMA_VERSION as V2_SCHEMA_VERSION,
    validate_anxin_board_report_v2,
)
from app.anxin_board_report_v3 import (
    SCHEMA_VERSION as V3_SCHEMA_VERSION,
    validate_anxin_board_report_v3,
)
from app.db import get_connection
from app.project_profiles import (
    ProjectProfileAuthorityError,
    read_bound_project_profile_for_report,
    read_current_confirmed_project_profile,
)


_SQLITE_SIGNED_INTEGER_MAX = 2**63 - 1
_REPORT_SCHEMA_VERSIONS = frozenset({V1_SCHEMA_VERSION, V2_SCHEMA_VERSION, V3_SCHEMA_VERSION})
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_REPORT_COLUMNS = (
    "id, project_id, schema_version, report_date, report_hash, report_json, created_at"
)
_REPORT_COLUMNS = (
    f"{_LEGACY_REPORT_COLUMNS}, approval_snapshot_id, approval_snapshot_hash"
)
_REQUIRED_LEGACY_COLUMNS = frozenset(
    {
        "id",
        "project_id",
        "schema_version",
        "report_date",
        "report_hash",
        "report_json",
        "created_at",
    }
)
_V3_ADMISSION_COLUMNS = frozenset({"approval_snapshot_id", "approval_snapshot_hash"})


class AnxinBoardReportProjectNotFoundError(LookupError):
    code = "PROJECT_NOT_FOUND"


class AnxinBoardReportProjectMismatchError(ValueError):
    code = "ANXIN_BOARD_REPORT_PROJECT_MISMATCH"


class AnxinBoardReportStoredInvalidError(RuntimeError):
    code = "ANXIN_BOARD_REPORT_STORED_INVALID"


class AnxinBoardReportApprovalRequiredError(RuntimeError):
    code = "ANXIN_BOARD_REPORT_APPROVAL_REQUIRED"


class AnxinBoardReportHistoryInputError(ValueError):
    code = "ANXIN_BOARD_REPORT_HISTORY_INPUT_INVALID"

    def __init__(self, message: str = code):
        super().__init__(message)


def _canonical_report_json(report: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            report,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise AnxinBoardReportStoredInvalidError() from exc


def _canonical_approval_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise AnxinBoardReportStoredInvalidError() from exc


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _schema_of(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    schema = value.get("schema_version")
    return schema if type(schema) is str else None


def _store_columns(conn: sqlite3.Connection) -> set[str]:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='anxin_board_reports'"
    ).fetchone()
    if table is None:
        raise AnxinBoardReportStoredInvalidError()
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(anxin_board_reports)").fetchall()
    }
    if not _REQUIRED_LEGACY_COLUMNS.issubset(columns):
        raise AnxinBoardReportStoredInvalidError()
    present_v3 = _V3_ADMISSION_COLUMNS.intersection(columns)
    if present_v3 and present_v3 != _V3_ADMISSION_COLUMNS:
        raise AnxinBoardReportStoredInvalidError()
    return columns


def _read_columns(conn: sqlite3.Connection) -> str:
    """Pure-read projection supporting pre-V3 historical databases without migration."""
    columns = _store_columns(conn)
    if _V3_ADMISSION_COLUMNS.issubset(columns):
        return _REPORT_COLUMNS
    return (
        f"{_LEGACY_REPORT_COLUMNS}, "
        "NULL AS approval_snapshot_id, NULL AS approval_snapshot_hash"
    )


def _ensure_store_schema(conn: sqlite3.Connection) -> None:
    """Write-path-only V3 migration; historical bytes are never rewritten."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS anxin_board_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            schema_version TEXT NOT NULL,
            report_date TEXT NOT NULL,
            report_hash TEXT NOT NULL,
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            approval_snapshot_id INTEGER NULL,
            approval_snapshot_hash TEXT NULL,
            UNIQUE (project_id, report_hash)
        )
        """
    )
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(anxin_board_reports)").fetchall()
    }
    if not _REQUIRED_LEGACY_COLUMNS.issubset(columns):
        raise AnxinBoardReportStoredInvalidError()
    if "approval_snapshot_id" not in columns:
        conn.execute(
            "ALTER TABLE anxin_board_reports ADD COLUMN approval_snapshot_id INTEGER NULL"
        )
    if "approval_snapshot_hash" not in columns:
        conn.execute(
            "ALTER TABLE anxin_board_reports ADD COLUMN approval_snapshot_hash TEXT NULL"
        )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_anxin_board_reports_approval_snapshot_id
        ON anxin_board_reports(approval_snapshot_id)
        WHERE approval_snapshot_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_anxin_board_reports_approval_snapshot_hash
        ON anxin_board_reports(approval_snapshot_hash)
        WHERE approval_snapshot_hash IS NOT NULL
        """
    )
    if not _V3_ADMISSION_COLUMNS.issubset(_store_columns(conn)):
        raise AnxinBoardReportStoredInvalidError()


def _require_profile_confirmation_provenance(
    conn: sqlite3.Connection, profile: Mapping[str, object]
) -> None:
    row = conn.execute(
        "SELECT status, confirmed_by, confirmed_at FROM project_profiles WHERE id = ?",
        (profile["id"],),
    ).fetchone()
    if (
        row is None
        or row["status"] != profile["status"]
        or type(row["confirmed_by"]) is not str
        or not row["confirmed_by"].strip()
        or type(row["confirmed_at"]) is not str
        or not row["confirmed_at"].strip()
    ):
        raise AnxinBoardReportStoredInvalidError()


def _validate_v2_with_bound_profile(
    value: object,
    *,
    conn: sqlite3.Connection,
    expected_project_id: int,
    require_current_profile: bool,
) -> dict[str, object]:
    try:
        if require_current_profile:
            profile = read_current_confirmed_project_profile(
                expected_project_id, conn=conn
            )
            if not isinstance(value, Mapping) or value.get("profile_id") != profile["id"]:
                raise AnxinBoardReportStoredInvalidError()
        else:
            if not isinstance(value, Mapping):
                raise AnxinBoardReportStoredInvalidError()
            profile = read_bound_project_profile_for_report(
                value.get("profile_id"), conn=conn
            )
        if profile["project_id"] != expected_project_id:
            raise AnxinBoardReportStoredInvalidError()
        _require_profile_confirmation_provenance(conn, profile)
        return validate_anxin_board_report_v2(value, profile=profile)
    except ProjectProfileAuthorityError as exc:
        raise AnxinBoardReportStoredInvalidError() from exc


def _read_approval_snapshot_for_v3(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    approval_snapshot_id: int,
    approval_snapshot_hash: str,
) -> dict[str, object]:
    if (
        type(approval_snapshot_id) is not int
        or approval_snapshot_id <= 0
        or not _is_hash(approval_snapshot_hash)
    ):
        raise AnxinBoardReportStoredInvalidError()
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_approval_snapshots'"
    ).fetchone()
    if table is None:
        raise AnxinBoardReportStoredInvalidError()
    row = conn.execute(
        """
        SELECT *
        FROM report_approval_snapshots
        WHERE id = ? AND project_id = ? AND approval_snapshot_hash = ?
        """,
        (approval_snapshot_id, project_id, approval_snapshot_hash),
    ).fetchone()
    if row is None:
        raise AnxinBoardReportStoredInvalidError()
    try:
        payload = json.loads(row["snapshot_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise AnxinBoardReportStoredInvalidError() from exc
    if (
        not isinstance(payload, Mapping)
        or _canonical_approval_json(payload) != row["snapshot_json"]
        or hashlib.sha256(row["snapshot_json"].encode("utf-8")).hexdigest()
        != row["approval_snapshot_hash"]
        or payload.get("schema_version") != "approval_snapshot_v1"
        or payload.get("project_id") != project_id
        or payload.get("report_version_id") != row["report_version_id"]
        or payload.get("report_content_hash") != row["report_content_hash"]
    ):
        raise AnxinBoardReportStoredInvalidError()

    row_payload_keys = set(row.keys()) - {
        "id",
        "idempotency_key",
        "snapshot_json",
        "approval_snapshot_hash",
        "created_at",
    }
    if set(payload) != row_payload_keys:
        raise AnxinBoardReportStoredInvalidError()
    for key in row_payload_keys:
        actual = row[key]
        if key == "human_acknowledged":
            if actual != 1:
                raise AnxinBoardReportStoredInvalidError()
            actual = True
        if actual != payload[key]:
            raise AnxinBoardReportStoredInvalidError()

    report = conn.execute(
        """
        SELECT lifecycle, state_version, report_content_hash, version_no
        FROM report_versions
        WHERE id = ? AND project_id = ?
        """,
        (row["report_version_id"], project_id),
    ).fetchone()
    if (
        report is None
        or report["lifecycle"] != "approved"
        or report["state_version"] != row["report_state_version_after"]
        or report["report_content_hash"] != row["report_content_hash"]
        or report["version_no"] != payload.get("report_version_no")
    ):
        raise AnxinBoardReportStoredInvalidError()
    result = dict(payload)
    result["approval_snapshot_id"] = row["id"]
    result["approval_snapshot_hash"] = row["approval_snapshot_hash"]
    return result


def _validate_v3_with_bound_authority(
    value: object,
    *,
    conn: sqlite3.Connection,
    expected_project_id: int,
    require_current_profile: bool,
    approval_snapshot_id: int,
    approval_snapshot_hash: str,
) -> dict[str, object]:
    try:
        if not isinstance(value, Mapping):
            raise AnxinBoardReportStoredInvalidError()
        if require_current_profile:
            profile = read_current_confirmed_project_profile(
                expected_project_id, conn=conn
            )
        else:
            profile = read_bound_project_profile_for_report(
                value.get("profile_id"), conn=conn
            )
        if profile["project_id"] != expected_project_id:
            raise AnxinBoardReportStoredInvalidError()
        _require_profile_confirmation_provenance(conn, profile)
        approval = _read_approval_snapshot_for_v3(
            conn,
            project_id=expected_project_id,
            approval_snapshot_id=approval_snapshot_id,
            approval_snapshot_hash=approval_snapshot_hash,
        )
        if (
            value.get("approval_snapshot_id") != approval_snapshot_id
            or value.get("approval_snapshot_hash") != approval_snapshot_hash
        ):
            raise AnxinBoardReportStoredInvalidError()
        return validate_anxin_board_report_v3(
            value,
            profile=profile,
            approval_snapshot=approval,
        )
    except (ProjectProfileAuthorityError, TypeError, ValueError) as exc:
        raise AnxinBoardReportStoredInvalidError() from exc


def _validate_stored_row(
    row: sqlite3.Row,
    *,
    conn: sqlite3.Connection,
    expected_project_id: int,
    expected_project_name: str,
    require_current_profile: bool = False,
) -> tuple[dict[str, object], dict[str, object]]:
    try:
        raw = json.loads(row["report_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise AnxinBoardReportStoredInvalidError() from exc

    schema = _schema_of(raw)
    try:
        if schema == V1_SCHEMA_VERSION:
            if row["approval_snapshot_id"] is not None or row["approval_snapshot_hash"] is not None:
                raise AnxinBoardReportStoredInvalidError()
            report = validate_anxin_board_report(raw)
        elif schema == V2_SCHEMA_VERSION:
            if row["approval_snapshot_id"] is not None or row["approval_snapshot_hash"] is not None:
                raise AnxinBoardReportStoredInvalidError()
            report = _validate_v2_with_bound_profile(
                raw,
                conn=conn,
                expected_project_id=expected_project_id,
                require_current_profile=require_current_profile,
            )
        elif schema == V3_SCHEMA_VERSION:
            if (
                type(row["approval_snapshot_id"]) is not int
                or row["approval_snapshot_id"] <= 0
                or not _is_hash(row["approval_snapshot_hash"])
            ):
                raise AnxinBoardReportStoredInvalidError()
            report = _validate_v3_with_bound_authority(
                raw,
                conn=conn,
                expected_project_id=expected_project_id,
                require_current_profile=require_current_profile,
                approval_snapshot_id=row["approval_snapshot_id"],
                approval_snapshot_hash=row["approval_snapshot_hash"],
            )
        else:
            raise AnxinBoardReportStoredInvalidError()
    except (TypeError, ValueError) as exc:
        raise AnxinBoardReportStoredInvalidError() from exc

    if (
        type(row["id"]) is not int
        or row["project_id"] != expected_project_id
        or row["schema_version"] not in _REPORT_SCHEMA_VERSIONS
        or row["schema_version"] != report["schema_version"]
        or row["report_date"] != report["report_date"]
        or row["report_hash"] != report["anxin_board_report_hash"]
        or report["project_name"] != expected_project_name
        or type(row["created_at"]) is not str
        or not row["created_at"]
    ):
        raise AnxinBoardReportStoredInvalidError()

    record = {
        "id": row["id"],
        "project_id": row["project_id"],
        "schema_version": row["schema_version"],
        "report_date": row["report_date"],
        "report_hash": row["report_hash"],
        "created_at": row["created_at"],
        "report": report,
    }
    return record, report


def _validated_legacy_candidate(
    *, conn: sqlite3.Connection, project_id: int, report: Mapping[str, object]
) -> tuple[dict[str, object], str]:
    schema = _schema_of(report)
    if schema == V1_SCHEMA_VERSION:
        validated = validate_anxin_board_report(report)
    elif schema == V2_SCHEMA_VERSION:
        validated = _validate_v2_with_bound_profile(
            report,
            conn=conn,
            expected_project_id=project_id,
            require_current_profile=True,
        )
    else:
        raise AnxinBoardReportApprovalRequiredError()
    return validated, schema


def persist_anxin_board_report(
    *,
    project_id: int,
    report: Mapping[str, object],
) -> dict[str, object]:
    """Historical V1/V2 replay seam; creating any new formal row now requires V3 approval."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_store_schema(conn)
        project = conn.execute(
            "SELECT name FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise AnxinBoardReportProjectNotFoundError()
        validated, schema = _validated_legacy_candidate(
            conn=conn, project_id=project_id, report=report
        )
        if validated["project_name"] != project["name"]:
            raise AnxinBoardReportProjectMismatchError()
        existing = conn.execute(
            f"SELECT {_REPORT_COLUMNS} FROM anxin_board_reports "
            "WHERE project_id = ? AND report_hash = ?",
            (project_id, validated["anxin_board_report_hash"]),
        ).fetchone()
        if existing is None:
            raise AnxinBoardReportApprovalRequiredError()
        record, _ = _validate_stored_row(
            existing,
            conn=conn,
            expected_project_id=project_id,
            expected_project_name=project["name"],
            require_current_profile=schema == V2_SCHEMA_VERSION,
        )
        conn.commit()
        return record


def persist_anxin_board_report_v3_in_transaction(
    *,
    conn: sqlite3.Connection,
    project_id: int,
    report: Mapping[str, object],
    approval_snapshot_id: int,
    approval_snapshot_hash: str,
) -> dict[str, object]:
    """Append/replay V3 inside the caller's existing approval transaction; never commits."""
    if not isinstance(conn, sqlite3.Connection) or not conn.in_transaction:
        raise AnxinBoardReportStoredInvalidError()
    if _schema_of(report) != V3_SCHEMA_VERSION:
        raise AnxinBoardReportApprovalRequiredError()
    _ensure_store_schema(conn)
    project = conn.execute(
        "SELECT name FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if project is None:
        raise AnxinBoardReportProjectNotFoundError()
    validated = _validate_v3_with_bound_authority(
        report,
        conn=conn,
        expected_project_id=project_id,
        require_current_profile=False,
        approval_snapshot_id=approval_snapshot_id,
        approval_snapshot_hash=approval_snapshot_hash,
    )
    if validated["project_name"] != project["name"]:
        raise AnxinBoardReportProjectMismatchError()

    report_hash = validated["anxin_board_report_hash"]
    report_json = _canonical_report_json(validated)
    existing = conn.execute(
        f"SELECT {_REPORT_COLUMNS} FROM anxin_board_reports "
        "WHERE project_id = ? AND report_hash = ?",
        (project_id, report_hash),
    ).fetchone()
    if existing is not None:
        record, _ = _validate_stored_row(
            existing,
            conn=conn,
            expected_project_id=project_id,
            expected_project_name=project["name"],
            require_current_profile=False,
        )
        if (
            existing["approval_snapshot_id"] != approval_snapshot_id
            or existing["approval_snapshot_hash"] != approval_snapshot_hash
        ):
            raise AnxinBoardReportStoredInvalidError()
        return record

    collision = conn.execute(
        """
        SELECT id FROM anxin_board_reports
        WHERE approval_snapshot_id = ? OR approval_snapshot_hash = ?
        LIMIT 1
        """,
        (approval_snapshot_id, approval_snapshot_hash),
    ).fetchone()
    if collision is not None:
        raise AnxinBoardReportStoredInvalidError()

    created_at = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO anxin_board_reports (
            project_id, schema_version, report_date, report_hash, report_json, created_at,
            approval_snapshot_id, approval_snapshot_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            validated["schema_version"],
            validated["report_date"],
            report_hash,
            report_json,
            created_at,
            approval_snapshot_id,
            approval_snapshot_hash,
        ),
    )
    stored = conn.execute(
        f"SELECT {_REPORT_COLUMNS} FROM anxin_board_reports WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    if stored is None:
        raise AnxinBoardReportStoredInvalidError()
    record, _ = _validate_stored_row(
        stored,
        conn=conn,
        expected_project_id=project_id,
        expected_project_name=project["name"],
        require_current_profile=False,
    )
    return record


def load_latest_anxin_board_report(*, project_id: int) -> dict[str, object] | None:
    """Return only the newest validated formal report from one pure-read DB snapshot."""
    with get_connection() as conn:
        conn.execute("BEGIN")
        columns = _read_columns(conn)
        project = conn.execute(
            "SELECT name FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise AnxinBoardReportProjectNotFoundError()
        row = conn.execute(
            f"""
            SELECT {columns}
            FROM anxin_board_reports
            WHERE project_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        _, report = _validate_stored_row(
            row,
            conn=conn,
            expected_project_id=project_id,
            expected_project_name=project["name"],
        )
        conn.commit()
        return report


def load_anxin_board_report_history(
    *,
    project_id: int,
    before_id: int | None = None,
    limit: int = 20,
) -> list[dict[str, object]]:
    """Read one validated history page using an exclusive id cursor, without migration."""
    if (
        type(project_id) is not int
        or project_id <= 0
        or project_id > _SQLITE_SIGNED_INTEGER_MAX
    ):
        raise AnxinBoardReportProjectNotFoundError()
    if type(limit) is not int or not 1 <= limit <= 50:
        raise AnxinBoardReportHistoryInputError()
    if before_id is not None and (
        type(before_id) is not int
        or before_id <= 0
        or before_id > _SQLITE_SIGNED_INTEGER_MAX
    ):
        raise AnxinBoardReportHistoryInputError()

    with get_connection() as conn:
        conn.execute("BEGIN")
        columns = _read_columns(conn)
        project = conn.execute(
            "SELECT name FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise AnxinBoardReportProjectNotFoundError()
        if before_id is None:
            rows = conn.execute(
                f"""
                SELECT {columns}
                FROM anxin_board_reports
                WHERE project_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT {columns}
                FROM anxin_board_reports
                WHERE project_id = ? AND id < ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (project_id, before_id, limit),
            ).fetchall()
        records = [
            _validate_stored_row(
                row,
                conn=conn,
                expected_project_id=project_id,
                expected_project_name=project["name"],
            )[0]
            for row in rows
        ]
        conn.commit()
        return records
