from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from app.backup_package import BackupEntry, BackupInvalidEntryError, build_backup_package
from app.backup_restore import BackupRestoreInvalidPackageError
from app.product_backup import (
    PRODUCT_DATABASE_ARCHIVE_PATH,
    PRODUCT_DATABASE_LOGICAL_TYPE,
    PRODUCT_DATABASE_SOURCE_PATH,
    PRODUCT_DATA_ROOT_ID,
    ProductBackupUnavailableError,
    ProductRestoreInvalidDatabaseError,
    ProductRestoreRuntimeActiveError,
    create_product_backup,
    restore_product_backup_offline,
    validate_product_backup,
)
from app.storage import Storage


@pytest.fixture(autouse=True)
def _isolate_product_data_from_runner_git_ancestry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Storage, "is_git_workspace_root", lambda self: False)


def _create_database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        conn.execute("INSERT INTO marker(value) VALUES (?)", (value,))
        conn.commit()


def _read_marker(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT value FROM marker").fetchone()
    assert row is not None
    return str(row[0])


def _symlink_or_skip(target: Path, link: Path, *, target_is_directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable in this test environment: {exc}")


def test_product_backup_is_consistent_database_only_package(tmp_path: Path) -> None:
    source_root = tmp_path / "source-data"
    db_path = source_root / "anxinboard.db"
    _create_database(db_path, "before-backup")
    output = tmp_path / "backup.zip"

    manifest = create_product_backup(
        output,
        data_root=source_root,
        created_at="2026-09-06T00:00:00Z",
    )

    assert manifest["schema_version"] == "rd_agent_backup_package_v1"
    assert len(manifest["entries"]) == 1
    entry = manifest["entries"][0]
    assert entry["logical_type"] == PRODUCT_DATABASE_LOGICAL_TYPE
    assert entry["source_root_id"] == PRODUCT_DATA_ROOT_ID
    assert entry["source_path"] == PRODUCT_DATABASE_SOURCE_PATH
    assert entry["archive_path"] == PRODUCT_DATABASE_ARCHIVE_PATH

    validated = validate_product_backup(output)
    assert len(validated.entries) == 1
    assert validated.entries[0].source_root_id == PRODUCT_DATA_ROOT_ID


def test_product_generated_snapshot_is_not_blocked_by_unrelated_host_git_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source-data"
    _create_database(source_root / PRODUCT_DATABASE_SOURCE_PATH, "runner-temp-snapshot")

    ordinary_root = tmp_path / "ordinary-source"
    ordinary_root.mkdir()
    (ordinary_root / "report.txt").write_text("must remain excluded", encoding="utf-8")
    ordinary_store = Storage(ordinary_root, root_id="ordinary-root")

    monkeypatch.setattr(Storage, "is_git_workspace_root", lambda self: True)

    with pytest.raises(BackupInvalidEntryError):
        build_backup_package(
            [
                BackupEntry(
                    logical_type="report",
                    source=ordinary_store,
                    source_path="report.txt",
                    archive_path="report.txt",
                )
            ],
            tmp_path / "ordinary.zip",
            created_at="2026-09-06T00:00:00Z",
        )

    manifest = create_product_backup(
        tmp_path / "product.zip",
        data_root=source_root,
        created_at="2026-09-06T00:00:00Z",
    )
    assert manifest["schema_version"] == "rd_agent_backup_package_v1"
    assert len(manifest["entries"]) == 1
    assert manifest["entries"][0]["archive_path"] == PRODUCT_DATABASE_ARCHIVE_PATH


def test_product_backup_restore_round_trip_targets_only_product_database(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _create_database(source_root / "anxinboard.db", "restored-value")
    package = tmp_path / "product-backup.zip"
    create_product_backup(package, data_root=source_root)

    target_root = tmp_path / "target"
    _create_database(target_root / "anxinboard.db", "old-value")
    keep = target_root / "keep.txt"
    keep.write_text("must remain", encoding="utf-8")

    result = restore_product_backup_offline(package, data_root=target_root)

    assert result["product_schema_version"] == "anxin_product_backup_restore_v1"
    assert result["restored_data"] == "sqlite_database_only"
    assert result["credential_store"] == "not_read_not_modified"
    assert result["launcher_lifecycle_authority"] == "required_before_user_facing_integration"
    assert _read_marker(target_root / "anxinboard.db") == "restored-value"
    assert keep.read_text(encoding="utf-8") == "must remain"


def test_product_restore_refuses_while_runtime_ownership_record_exists(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _create_database(source_root / "anxinboard.db", "new-value")
    package = tmp_path / "product-backup.zip"
    create_product_backup(package, data_root=source_root)

    target_root = tmp_path / "target"
    _create_database(target_root / "anxinboard.db", "old-value")
    (target_root / "local-run.json").write_text('{"synthetic":true}', encoding="utf-8")

    with pytest.raises(ProductRestoreRuntimeActiveError):
        restore_product_backup_offline(package, data_root=target_root)

    assert _read_marker(target_root / "anxinboard.db") == "old-value"


def test_product_restore_treats_dangling_runtime_ownership_symlink_as_active(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _create_database(source_root / "anxinboard.db", "new-value")
    package = tmp_path / "product-backup.zip"
    create_product_backup(package, data_root=source_root)

    target_root = tmp_path / "target"
    _create_database(target_root / "anxinboard.db", "old-value")
    _symlink_or_skip(target_root / "missing-runtime-state-target", target_root / "local-run.json")

    with pytest.raises(ProductRestoreRuntimeActiveError):
        restore_product_backup_offline(package, data_root=target_root)

    assert _read_marker(target_root / "anxinboard.db") == "old-value"


def test_product_backup_rejects_symlinked_database_source(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "outside.db"
    _create_database(outside, "must-never-export")
    source_root = tmp_path / "source"
    source_root.mkdir()
    _symlink_or_skip(outside, source_root / "anxinboard.db")

    with pytest.raises(ProductBackupUnavailableError):
        create_product_backup(tmp_path / "backup.zip", data_root=source_root)


def test_product_backup_rejects_symlinked_sqlite_sidecar(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _create_database(source_root / "anxinboard.db", "safe-db")
    outside = tmp_path / "outside-sidecar"
    outside.write_bytes(b"outside-sidecar-data")
    _symlink_or_skip(outside, source_root / "anxinboard.db-wal")

    with pytest.raises(ProductBackupUnavailableError):
        create_product_backup(tmp_path / "backup.zip", data_root=source_root)


def test_product_validation_rejects_wrong_logical_backup_shape(tmp_path: Path) -> None:
    source_root = tmp_path / "wrong-source"
    source_root.mkdir()
    (source_root / "other.db").write_bytes(b"not-the-product-contract")
    storage = Storage(source_root, root_id="wrong-root")
    package = tmp_path / "wrong.zip"
    build_backup_package(
        [
            BackupEntry(
                logical_type="other_data",
                source=storage,
                source_path="other.db",
                archive_path="data/other.db",
            )
        ],
        package,
    )

    with pytest.raises(BackupRestoreInvalidPackageError):
        validate_product_backup(package)


def test_product_validation_rejects_integrity_valid_package_with_invalid_sqlite(tmp_path: Path) -> None:
    source_root = tmp_path / "invalid-db-source"
    source_root.mkdir()
    (source_root / PRODUCT_DATABASE_SOURCE_PATH).write_bytes(b"this-is-not-sqlite")
    storage = Storage(source_root, root_id=PRODUCT_DATA_ROOT_ID)
    package = tmp_path / "invalid-sqlite.zip"
    build_backup_package(
        [
            BackupEntry(
                logical_type=PRODUCT_DATABASE_LOGICAL_TYPE,
                source=storage,
                source_path=PRODUCT_DATABASE_SOURCE_PATH,
                archive_path=PRODUCT_DATABASE_ARCHIVE_PATH,
            )
        ],
        package,
    )

    with pytest.raises(ProductRestoreInvalidDatabaseError):
        validate_product_backup(package)


def test_product_backup_module_never_imports_or_reads_credential_store() -> None:
    source = (Path(__file__).resolve().parents[2] / "apps" / "backend" / "app" / "product_backup.py").read_text(
        encoding="utf-8"
    ).casefold()
    assert "windows_credential_store" not in source
    assert "cmdkey" not in source
    assert "get-storedcredential" not in source
    assert '"credential_store": "not_read_not_modified"' in source
