"""Product-owned backup/restore contract for the AnxinBoard SQLite data store.

This module turns the generic backup package primitives into one explicit product
contract. The ordinary V1 package contains only a transactionally consistent SQLite
snapshot. Windows Credential Manager remains the secret authority and is never read or
serialized here.

Restore is deliberately an *offline* primitive. It refuses while the durable runtime
ownership record exists, validates the complete package and SQLite integrity before any
data-root mutation, then atomically replaces the database through ``Storage``. The
final user-facing restore invocation must additionally share the accepted launcher
lifecycle authority; that integration remains gated on the independently accepted #130
semantics and is not duplicated here.
"""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Final

from app.backup_package import BackupEntry, BackupPackageError, build_backup_package
from app.backup_restore import (
    BackupRestoreInvalidPackageError,
    BackupRestoreTargetError,
    ValidatedBackupPackage,
    restore_backup_package,
    validate_backup_package,
)
from app.db import get_db_path
from app.storage import Storage, StorageError, StorageStat


PRODUCT_DATA_ROOT_ID: Final[str] = "anxinboard-data-v1"
PRODUCT_DATABASE_LOGICAL_TYPE: Final[str] = "sqlite_database"
PRODUCT_DATABASE_SOURCE_PATH: Final[str] = "anxinboard.db"
PRODUCT_DATABASE_ARCHIVE_PATH: Final[str] = "data/anxinboard.db"
RUNTIME_STATE_NAME: Final[str] = "local-run.json"
_SQLITE_SIDECARS: Final[tuple[str, ...]] = (
    "anxinboard.db-wal",
    "anxinboard.db-shm",
    "anxinboard.db-journal",
)


class ProductBackupError(RuntimeError):
    code = "PRODUCT_BACKUP_ERROR"


class ProductBackupUnavailableError(ProductBackupError):
    code = "PRODUCT_BACKUP_UNAVAILABLE"


class ProductRestoreRuntimeActiveError(ProductBackupError):
    code = "PRODUCT_RESTORE_RUNTIME_ACTIVE"


class ProductRestoreInvalidDatabaseError(ProductBackupError):
    code = "PRODUCT_RESTORE_INVALID_DATABASE"


class _ProductSnapshotStorage(Storage):
    """Storage for one SQLite snapshot generated inside ``create_product_backup``.

    The generic backup core rejects caller-selected source roots located anywhere under
    a Git workspace. That policy remains correct for ordinary ``Storage`` instances.
    Product backup is different: SQLite has already copied the validated live database
    into a fresh ``TemporaryDirectory`` owned by this call, and the only exported member
    is the fixed ``anxinboard.db`` snapshot. CI/self-hosted runners may place the process
    temp directory below an unrelated Git ancestor, which must not transform this
    product-generated snapshot into repository content.

    Only the Git-ancestry classification is specialized here. Absolute/local path,
    reparse-point, regular-file, bounded-read, identity and atomic-output protections
    continue to come from ``Storage`` and ``build_backup_package`` unchanged.
    """

    def is_git_workspace_root(self) -> bool:
        return False


def _data_root(data_root: str | os.PathLike[str] | None = None) -> Path:
    if data_root is None:
        return get_db_path().parent.absolute()
    root = Path(os.fspath(data_root))
    if not root.is_absolute():
        raise ProductBackupError("product data root must be absolute")
    return root.absolute()


def _canonical_restore_root(root: Path) -> bool:
    """Admit only the configured product DB, never an arbitrary Git restore root."""
    database = get_db_path()
    if not database.is_absolute() or '..' in database.parts or '..' in root.parts:
        return False
    if database.name != PRODUCT_DATABASE_SOURCE_PATH or database.parent != root:
        return False
    local = Path(os.environ.get('LOCALAPPDATA', ''))
    return local.is_absolute() and '..' not in local.parts and database == local / 'AnxinBoard' / PRODUCT_DATABASE_SOURCE_PATH


class _ProductRestoreStorage(Storage):
    """Fixed product DB destination; preserve all Storage handle/reparse protections."""

    def is_git_workspace_root(self) -> bool:
        if not _canonical_restore_root(self.root_path):
            return super().is_git_workspace_root()
        # An unrelated ancestor marker does not turn this private app-data directory
        # into source code. A marker on the destination itself remains a refusal.
        try:
            (self.root_path / '.git').lstat()
        except FileNotFoundError:
            return False
        except OSError:
            raise BackupRestoreTargetError('product restore target cannot be classified') from None
        return True

    def _database_only(self, relative_path):
        if os.fspath(relative_path) != PRODUCT_DATABASE_SOURCE_PATH:
            raise BackupRestoreTargetError('product restore permits only its exact database entry')

    def exists(self, relative_path):
        self._database_only(relative_path)
        return super().exists(relative_path)

    def atomic_write_bytes(self, relative_path, data, **kwargs):
        self._database_only(relative_path)
        return super().atomic_write_bytes(relative_path, data, **kwargs)

    def sha256(self, relative_path, **kwargs):
        self._database_only(relative_path)
        return super().sha256(relative_path, **kwargs)


def _open_source_read_only(path: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    except (sqlite3.Error, ValueError) as exc:
        raise ProductBackupUnavailableError("product database cannot be opened for backup") from exc


def _assert_sqlite_integrity(path: Path) -> None:
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
    except (sqlite3.Error, ValueError) as exc:
        raise ProductRestoreInvalidDatabaseError("restored database is not a valid SQLite database") from exc
    if row is None or row[0] != "ok":
        raise ProductRestoreInvalidDatabaseError("restored database failed SQLite integrity check")


def _same_file_identity(before: StorageStat, after: StorageStat) -> bool:
    return before.device == after.device and before.inode == after.inode


def _bind_live_database_source(root: Path) -> tuple[Storage, StorageStat, Path]:
    """Bind the live DB to the approved product root before SQLite opens it by path.

    ``sqlite3`` does not expose a portable handle-based VFS entry point here, so the
    product first validates the full root/final-file chain through ``Storage`` and then
    re-attests the same device/inode after the online backup. Content size/mtime may
    legitimately change while SQLite creates a consistent snapshot; file identity may
    not. Existing SQLite sidecars are also required to be plain files under the same
    approved root rather than reparse escapes.
    """

    try:
        storage = Storage(root, root_id=PRODUCT_DATA_ROOT_ID)
        before = storage.stat(PRODUCT_DATABASE_SOURCE_PATH)
        for sidecar in _SQLITE_SIDECARS:
            if storage.exists(sidecar):
                storage.stat(sidecar)
    except StorageError as exc:
        raise ProductBackupUnavailableError("product database is outside the approved local data root") from exc
    return storage, before, storage.root_path / PRODUCT_DATABASE_SOURCE_PATH


def _recheck_live_database_source(storage: Storage, before: StorageStat) -> None:
    try:
        after = storage.stat(PRODUCT_DATABASE_SOURCE_PATH)
        for sidecar in _SQLITE_SIDECARS:
            if storage.exists(sidecar):
                storage.stat(sidecar)
    except StorageError as exc:
        raise ProductBackupUnavailableError("product database identity changed during backup") from exc
    if not _same_file_identity(before, after):
        raise ProductBackupUnavailableError("product database identity changed during backup")


def _runtime_ownership_exists(root: Path) -> bool:
    """Treat any final filesystem object at local-run.json as active ownership.

    ``Path.exists`` follows symlinks and returns false for a dangling link. Restore must
    fail closed instead: a file, directory, symlink, junction/reparse endpoint, or other
    final object occupying the durable ownership name all block offline replacement.
    """

    state_path = root / RUNTIME_STATE_NAME
    try:
        state_path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ProductRestoreRuntimeActiveError(
            "runtime ownership record cannot be safely classified; offline restore is refused"
        ) from exc
    return True


def _validate_product_shape(validated: ValidatedBackupPackage) -> bytes:
    if len(validated.entries) != 1:
        raise BackupRestoreInvalidPackageError("product backup must contain exactly one database entry")
    entry = validated.entries[0]
    if (
        entry.logical_type != PRODUCT_DATABASE_LOGICAL_TYPE
        or entry.source_root_id != PRODUCT_DATA_ROOT_ID
        or entry.source_path != PRODUCT_DATABASE_SOURCE_PATH
        or entry.archive_path != PRODUCT_DATABASE_ARCHIVE_PATH
    ):
        raise BackupRestoreInvalidPackageError("backup package does not match the AnxinBoard data contract")
    return entry.data


def create_product_backup(
    output_path: str | os.PathLike[str],
    *,
    data_root: str | os.PathLike[str] | None = None,
    created_at: str | None = None,
) -> dict[str, object]:
    """Create a consistent database-only product backup package.

    SQLite's online backup API snapshots committed database state even when the local
    product is running. The live database must remain the same plain file identity under
    the approved product-owned root for the whole snapshot. The snapshot itself is
    created in a fresh isolated temporary root carrying the same *logical* root identity
    used by offline restore. No credential store, environment enumeration, raw log,
    repository, or adjacent user file is exported.
    """

    root = _data_root(data_root)
    storage, source_identity, db_path = _bind_live_database_source(root)

    with tempfile.TemporaryDirectory(prefix="anxinboard-db-snapshot-") as temp:
        snapshot_root = Path(temp)
        snapshot_path = snapshot_root / PRODUCT_DATABASE_SOURCE_PATH
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source = _open_source_read_only(db_path)
            destination = sqlite3.connect(snapshot_path)
            source.backup(destination)
            destination.commit()
        except sqlite3.Error as exc:
            raise ProductBackupUnavailableError("product database snapshot failed") from exc
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()

        _recheck_live_database_source(storage, source_identity)
        _assert_sqlite_integrity(snapshot_path)
        snapshot_storage = _ProductSnapshotStorage(snapshot_root, root_id=PRODUCT_DATA_ROOT_ID)
        entry = BackupEntry(
            logical_type=PRODUCT_DATABASE_LOGICAL_TYPE,
            source=snapshot_storage,
            source_path=PRODUCT_DATABASE_SOURCE_PATH,
            archive_path=PRODUCT_DATABASE_ARCHIVE_PATH,
        )
        try:
            return build_backup_package([entry], output_path, created_at=created_at)
        except (BackupPackageError, StorageError) as exc:
            raise ProductBackupUnavailableError("product backup package could not be written safely") from exc


def validate_product_backup(
    package_path: str | os.PathLike[str],
) -> ValidatedBackupPackage:
    """Validate package identity plus SQLite integrity without mutating user data."""

    validated = validate_backup_package(package_path)
    database_bytes = _validate_product_shape(validated)
    with tempfile.TemporaryDirectory(prefix="anxinboard-restore-validate-") as temp:
        candidate = Path(temp) / PRODUCT_DATABASE_SOURCE_PATH
        candidate.write_bytes(database_bytes)
        _assert_sqlite_integrity(candidate)
    return validated


def restore_product_backup_offline(
    package_path: str | os.PathLike[str],
    *,
    data_root: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Restore the exact product database only while runtime ownership is absent.

    This is not a launcher. A future user-facing caller must hold the accepted shared
    lifecycle authority before entering this function. Until #130 is independently
    accepted and integrated, this function is available only as an internal/testable
    offline primitive and must not be wired to an automatic UI action.
    """

    root = _data_root(data_root)
    if _runtime_ownership_exists(root):
        raise ProductRestoreRuntimeActiveError(
            "runtime ownership record exists; offline restore is refused"
        )

    validated = validate_product_backup(package_path)
    _validate_product_shape(validated)
    root.mkdir(parents=True, exist_ok=True)
    try:
        target = _ProductRestoreStorage(root, root_id=PRODUCT_DATA_ROOT_ID)
        result = restore_backup_package(
            package_path,
            {PRODUCT_DATA_ROOT_ID: target},
            replace_existing=True,
        )
    except StorageError as exc:
        raise BackupRestoreTargetError("product restore target is unavailable") from exc

    # Generic restore already verifies the exact entry SHA-256 through the identity-bound
    # Storage target after atomic replacement. The SQLite bytes were fully integrity-
    # checked before mutation, so reopening the destination by caller-visible path here
    # would only add a new TOCTOU surface without increasing evidence quality.
    return {
        **result,
        "product_schema_version": "anxin_product_backup_restore_v1",
        "restored_data": "sqlite_database_only",
        "credential_store": "not_read_not_modified",
        "launcher_lifecycle_authority": "required_before_user_facing_integration",
    }
