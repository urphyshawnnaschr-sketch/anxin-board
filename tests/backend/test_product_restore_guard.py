from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from app.product_backup import create_product_backup
from app.product_restore_guard import (
    RESTORE_AUTHORITY_ENV,
    RESTORE_AUTHORITY_VALUE,
    ProductRestoreCurrentDatabaseUnverifiableError,
    ProductRestoreExternalAuthorityRollbackError,
    ProductRestoreLifecycleAuthorityError,
    ProductRestoreSQLiteSidecarError,
    restore_product_backup_user_safe,
)
from app.storage import Storage


@pytest.fixture(autouse=True)
def _isolate_product_data_from_runner_git_ancestry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Storage, "is_git_workspace_root", lambda self: False)


def _database(path: Path, marker: str, *, effect: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        conn.execute("INSERT INTO marker(value) VALUES (?)", (marker,))
        if effect is not None:
            conn.execute("CREATE TABLE profile_generation_attempts(effect_identity TEXT NOT NULL)")
            conn.execute("INSERT INTO profile_generation_attempts(effect_identity) VALUES (?)", (effect,))
        conn.commit()


def _read_marker(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT value FROM marker").fetchone()
    assert row is not None
    return str(row[0])


def _package(tmp_path: Path, *, marker: str, effect: str | None = None) -> Path:
    source = tmp_path / f"source-{marker}"
    _database(source / "anxinboard.db", marker, effect=effect)
    package = tmp_path / f"{marker}.zip"
    create_product_backup(package, data_root=source)
    return package


def _authorize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RESTORE_AUTHORITY_ENV, RESTORE_AUTHORITY_VALUE)


def _symlink_or_skip(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")


def test_user_restore_requires_launcher_lifecycle_authority(tmp_path: Path) -> None:
    package = _package(tmp_path, marker="backup")
    target = tmp_path / "target"
    _database(target / "anxinboard.db", "current")

    with pytest.raises(ProductRestoreLifecycleAuthorityError):
        restore_product_backup_user_safe(package, data_root=target)

    assert _read_marker(target / "anxinboard.db") == "current"


def test_user_restore_allows_data_recovery_when_no_current_external_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(tmp_path, marker="backup")
    target = tmp_path / "target"
    _database(target / "anxinboard.db", "current")
    _authorize(monkeypatch)

    result = restore_product_backup_user_safe(package, data_root=target)

    assert result["product_schema_version"] == "anxin_product_backup_restore_user_safe_v1"
    assert result["external_effect_authority"] == "preserved_or_no_current_effect_rows"
    assert _read_marker(target / "anxinboard.db") == "backup"


def test_user_restore_refuses_to_forget_current_external_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(tmp_path, marker="older-backup")
    target = tmp_path / "target"
    _database(target / "anxinboard.db", "current", effect="already-paid-model-call")
    _authorize(monkeypatch)

    with pytest.raises(ProductRestoreExternalAuthorityRollbackError):
        restore_product_backup_user_safe(package, data_root=target)

    assert _read_marker(target / "anxinboard.db") == "current"


def test_user_restore_allows_other_data_change_when_effect_projection_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(tmp_path, marker="wanted-data", effect="same-external-effect")
    target = tmp_path / "target"
    _database(target / "anxinboard.db", "current-data", effect="same-external-effect")
    _authorize(monkeypatch)

    restore_product_backup_user_safe(package, data_root=target)

    assert _read_marker(target / "anxinboard.db") == "wanted-data"


def test_user_restore_refuses_while_sqlite_sidecar_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(tmp_path, marker="backup", effect="same-external-effect")
    target = tmp_path / "target"
    _database(target / "anxinboard.db", "current", effect="same-external-effect")
    (target / "anxinboard.db-wal").write_bytes(b"stale-sidecar")
    _authorize(monkeypatch)

    with pytest.raises(ProductRestoreSQLiteSidecarError):
        restore_product_backup_user_safe(package, data_root=target)

    assert _read_marker(target / "anxinboard.db") == "current"


def test_user_restore_refuses_orphan_sidecar_even_without_main_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(tmp_path, marker="backup")
    target = tmp_path / "target"
    target.mkdir()
    (target / "anxinboard.db-wal").write_bytes(b"orphan-sidecar")
    _authorize(monkeypatch)

    with pytest.raises(ProductRestoreSQLiteSidecarError):
        restore_product_backup_user_safe(package, data_root=target)

    assert not (target / "anxinboard.db").exists()


def test_user_restore_refuses_dangling_database_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(tmp_path, marker="backup")
    target = tmp_path / "target"
    target.mkdir()
    link = target / "anxinboard.db"
    _symlink_or_skip(target / "missing.db", link)
    _authorize(monkeypatch)

    with pytest.raises(ProductRestoreCurrentDatabaseUnverifiableError):
        restore_product_backup_user_safe(package, data_root=target)

    assert link.is_symlink()


def test_restore_guard_never_mentions_or_imports_credential_store() -> None:
    source = (Path(__file__).resolve().parents[2] / "apps" / "backend" / "app" / "product_restore_guard.py").read_text(
        encoding="utf-8"
    ).casefold()
    assert "windows_credential_store" not in source
    assert "cmdkey" not in source
    assert "get-storedcredential" not in source
    assert "deepseek-api-key" not in source


@pytest.mark.parametrize('table', ['report_batch_plans', 'report_generation_batches', 'model_execution_aggregate_sources'])
def test_restore_does_not_forget_report_batch_authority(tmp_path, monkeypatch, table):
    package = _package(tmp_path, marker='before-batch')
    target = tmp_path / 'target'
    _database(target / 'anxinboard.db', 'after-batch')
    with sqlite3.connect(target / 'anxinboard.db') as conn:
        conn.execute(f'CREATE TABLE {table}(identity TEXT NOT NULL)')
        conn.execute(f'INSERT INTO {table} VALUES (?)', ('claimed-or-verified',))
    _authorize(monkeypatch)
    with pytest.raises(ProductRestoreExternalAuthorityRollbackError):
        restore_product_backup_user_safe(package, data_root=target)
    assert _read_marker(target / 'anxinboard.db') == 'after-batch'
