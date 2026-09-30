"""Safe user-facing offline restore guard for AnxinBoard.

A database backup can contain durable records for model/provider calls, email and WeChat sends.
Those external effects cannot be undone by restoring an older SQLite file. Therefore a
normal user restore is allowed only when it cannot make the current product forget any
already-recorded external-effect authority. The caller must also hold the accepted
Windows launcher lifecycle lock and the runtime ownership record must be absent; the
lower-level product backup primitive independently rechecks the latter condition.
"""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Final

from app.db import get_db_path
from app.product_backup import (
    PRODUCT_DATABASE_SOURCE_PATH,
    ProductBackupError,
    restore_product_backup_offline,
    validate_product_backup,
)


RESTORE_AUTHORITY_ENV: Final[str] = "ANXINBOARD_OFFLINE_RESTORE_AUTHORITY"
RESTORE_AUTHORITY_VALUE: Final[str] = "launcher-lock-held-v1"

# These tables are durable memory that can prevent a repeated provider, mail or WeChat side
# effect. Coordination-only process leases and short-lived in-memory Human permits are
# intentionally excluded: restore is offline and the lifecycle lock excludes a live
# process. Equality is deliberately conservative for V1. If current durable effect
# memory exists, a normal user restore may not replace it with an older/different view.
_EXTERNAL_EFFECT_TABLES: Final[tuple[str, ...]] = (
    "profile_generation_attempts",
    "model_calls",
    "model_execution_results",
    "report_generation_tasks",
    "report_generation_task_attempts",
    "report_batch_plans",
    "report_generation_batches",
    "model_execution_aggregate_sources",
    "report_contradiction_send_claims",
    "mail_durable_admissions",
    "mail_send_attempts",
    "mail_send_recipient_results",
    "wechat_configs",
    "wechat_previews",
    "wechat_preview_images",
    "wechat_attempts",
    "wechat_attempt_pages",
    "wechat_request_keys",
    "wechat_preview_attempt_bindings",
    "wechat_bindings",
)
_SQLITE_SIDECARS: Final[tuple[str, ...]] = (
    "anxinboard.db-wal",
    "anxinboard.db-shm",
    "anxinboard.db-journal",
)


class ProductRestoreSafetyError(ProductBackupError):
    code = "PRODUCT_RESTORE_SAFETY_BLOCKED"


class ProductRestoreLifecycleAuthorityError(ProductRestoreSafetyError):
    code = "PRODUCT_RESTORE_LIFECYCLE_AUTHORITY_REQUIRED"


class ProductRestoreExternalAuthorityRollbackError(ProductRestoreSafetyError):
    code = "PRODUCT_RESTORE_EXTERNAL_AUTHORITY_ROLLBACK"


class ProductRestoreCurrentDatabaseUnverifiableError(ProductRestoreSafetyError):
    code = "PRODUCT_RESTORE_CURRENT_DATABASE_UNVERIFIABLE"


class ProductRestoreSQLiteSidecarError(ProductRestoreSafetyError):
    code = "PRODUCT_RESTORE_SQLITE_SIDECAR_PRESENT"


def _data_root(data_root: str | os.PathLike[str] | None) -> Path:
    if data_root is None:
        return get_db_path().parent.absolute()
    root = Path(os.fspath(data_root))
    if not root.is_absolute():
        raise ProductRestoreSafetyError("product data root must be absolute")
    return root.absolute()


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _canonical_sql_value(value: object) -> object:
    if value is None or type(value) in {int, str}:
        return value
    if type(value) is float:
        return {"sqlite_float": repr(value)}
    if type(value) is bytes:
        return {"sqlite_blob_hex": value.hex()}
    raise ProductRestoreCurrentDatabaseUnverifiableError(
        "external-effect ledger contains an unsupported SQLite value"
    )


def _table_projection(conn: sqlite3.Connection, table: str) -> dict[str, object] | None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        return None

    columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quoted_identifier(table)})")]
    if not columns:
        raise ProductRestoreCurrentDatabaseUnverifiableError(
            f"external-effect table has no columns: {table}"
        )
    rows = conn.execute(f"SELECT * FROM {_quoted_identifier(table)}").fetchall()
    canonical_rows: list[str] = []
    for row in rows:
        payload = {
            column: _canonical_sql_value(row[index])
            for index, column in enumerate(columns)
        }
        canonical_rows.append(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
        )
    canonical_rows.sort()
    return {"columns": columns, "rows": canonical_rows}


def _external_effect_projection(path: Path) -> dict[str, dict[str, object]]:
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise ProductRestoreCurrentDatabaseUnverifiableError(
                    "database integrity cannot be established"
                )
            projection: dict[str, dict[str, object]] = {}
            for table in _EXTERNAL_EFFECT_TABLES:
                value = _table_projection(conn, table)
                if value is not None:
                    projection[table] = value
            return projection
    except ProductRestoreSafetyError:
        raise
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise ProductRestoreCurrentDatabaseUnverifiableError(
            "external-effect authority cannot be read safely"
        ) from exc


def _has_effect_rows(projection: dict[str, dict[str, object]]) -> bool:
    return any(bool(value.get("rows")) for value in projection.values())


def _assert_no_sidecars(root: Path) -> None:
    for name in _SQLITE_SIDECARS:
        path = root / name
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProductRestoreSQLiteSidecarError(
                "SQLite sidecar state cannot be safely classified"
            ) from exc
        raise ProductRestoreSQLiteSidecarError(
            "SQLite sidecar remains after shutdown; restore is refused"
        )


def _plain_current_database(root: Path) -> Path | None:
    current_db = root / PRODUCT_DATABASE_SOURCE_PATH
    try:
        current_db.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProductRestoreCurrentDatabaseUnverifiableError(
            "current database identity cannot be safely classified"
        ) from exc
    if current_db.is_symlink() or not current_db.is_file():
        raise ProductRestoreCurrentDatabaseUnverifiableError(
            "current database is not a plain local file"
        )
    return current_db


def _candidate_database(validated, temp_root: Path) -> Path:
    if len(validated.entries) != 1:
        raise ProductRestoreSafetyError("validated product backup shape changed unexpectedly")
    entry = validated.entries[0]
    if entry.source_path != PRODUCT_DATABASE_SOURCE_PATH:
        raise ProductRestoreSafetyError("validated product backup database identity changed")
    candidate = temp_root / PRODUCT_DATABASE_SOURCE_PATH
    candidate.write_bytes(entry.data)
    return candidate


def assert_restore_preserves_external_effect_authority(
    package_path: str | os.PathLike[str],
    *,
    data_root: str | os.PathLike[str] | None = None,
) -> None:
    """Fail closed if restore could forget already-recorded external effects."""

    root = _data_root(data_root)
    validated = validate_product_backup(package_path)

    # Check sidecars before deciding whether a main DB exists. A crashed/missing main DB
    # plus stale WAL/SHM is still ambiguous SQLite state and must never be overwritten.
    _assert_no_sidecars(root)
    current_db = _plain_current_database(root)
    if current_db is None:
        return

    current_projection = _external_effect_projection(current_db)
    if not _has_effect_rows(current_projection):
        return

    with tempfile.TemporaryDirectory(prefix="anxinboard-restore-authority-") as temp:
        candidate = _candidate_database(validated, Path(temp))
        candidate_projection = _external_effect_projection(candidate)

    if candidate_projection != current_projection:
        raise ProductRestoreExternalAuthorityRollbackError(
            "backup would change durable model/mail/WeChat external-effect authority"
        )

    # Re-read immediately before mutation. The accepted Windows caller holds the launcher
    # lifecycle lock, but this second read also makes unexpected local DB drift fail closed.
    _assert_no_sidecars(root)
    current_db_again = _plain_current_database(root)
    if current_db_again is None or _external_effect_projection(current_db_again) != current_projection:
        raise ProductRestoreCurrentDatabaseUnverifiableError(
            "current external-effect authority changed during restore preflight"
        )


def restore_product_backup_user_safe(
    package_path: str | os.PathLike[str],
    *,
    data_root: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Perform one explicit offline restore behind launcher lifecycle authority."""

    if os.environ.get(RESTORE_AUTHORITY_ENV) != RESTORE_AUTHORITY_VALUE:
        raise ProductRestoreLifecycleAuthorityError(
            "accepted launcher lifecycle authority is required for user restore"
        )

    # Validate package and anti-rollback before the lower-level atomic database replace.
    # The lower-level primitive independently revalidates package integrity and runtime
    # ownership immediately before mutation; no credential store is touched here.
    assert_restore_preserves_external_effect_authority(package_path, data_root=data_root)
    result = restore_product_backup_offline(package_path, data_root=data_root)
    return {
        **result,
        "product_schema_version": "anxin_product_backup_restore_user_safe_v1",
        "launcher_lifecycle_authority": "shared_lock_required_and_asserted",
        "external_effect_authority": "preserved_or_no_current_effect_rows",
    }
