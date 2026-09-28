"""Synthetic restore targets only; no real product database or Git-marker mutation."""
import os
import sqlite3
from pathlib import Path

import pytest

from app.backup_restore import BackupRestoreTargetError, restore_backup_package
from app.product_backup import create_product_backup, PRODUCT_DATA_ROOT_ID, ProductBackupError, _ProductRestoreStorage
from app.product_restore_guard import restore_product_backup_user_safe, RESTORE_AUTHORITY_ENV, RESTORE_AUTHORITY_VALUE
from app.storage import Storage


@pytest.fixture
def restore_case(tmp_path, monkeypatch):
    (tmp_path / '.git').mkdir()
    local = tmp_path / 'local'
    local.mkdir()
    target = local / 'AnxinBoard'
    target.mkdir()
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.delenv('ANXINBOARD_DB_PATH', raising=False)
    monkeypatch.delenv('ANXINBOARD_INSTALLER_TEST_MODE', raising=False)
    monkeypatch.setenv(RESTORE_AUTHORITY_ENV, RESTORE_AUTHORITY_VALUE)
    source = tmp_path / 'source'
    source.mkdir()
    with sqlite3.connect(source / 'anxinboard.db') as conn:
        conn.execute('CREATE TABLE synthetic_marker(value INTEGER)')
        conn.execute('INSERT INTO synthetic_marker VALUES(19)')
    package = tmp_path / 'synthetic.zip'
    create_product_backup(package, data_root=source)
    return package, target


def test_canonical_product_restore_ignores_only_ancestor_git_marker(restore_case):
    package, target = restore_case
    assert Storage(target, root_id=PRODUCT_DATA_ROOT_ID).is_git_workspace_root()
    result = restore_product_backup_user_safe(package)
    assert result['product_schema_version'] == 'anxin_product_backup_restore_user_safe_v1'
    with sqlite3.connect(target / 'anxinboard.db') as conn:
        assert conn.execute('SELECT value FROM synthetic_marker').fetchone()[0] == 19


def test_generic_restore_still_rejects_even_canonical_product_git_ancestor(restore_case):
    package, target = restore_case
    with pytest.raises(BackupRestoreTargetError):
        restore_backup_package(package, {PRODUCT_DATA_ROOT_ID: Storage(target, root_id=PRODUCT_DATA_ROOT_ID)})
    assert not (target / 'anxinboard.db').exists()


@pytest.mark.parametrize('kind', ['other-root', 'db-override', 'wrong-db-name', 'root-marker'])
def test_product_exception_cannot_authorize_an_arbitrary_git_target(restore_case, monkeypatch, kind):
    package, canonical = restore_case
    target = canonical
    kwargs = {}
    if kind in {'other-root', 'db-override'}:
        target = canonical.parent / 'other'
        target.mkdir()
        if kind == 'other-root':
            kwargs['data_root'] = target
        else:
            monkeypatch.setenv('ANXINBOARD_DB_PATH', str(target / 'anxinboard.db'))
    elif kind == 'wrong-db-name':
        monkeypatch.setenv('ANXINBOARD_DB_PATH', str(target / 'not-product.db'))
    else:
        (target / '.git').write_text('synthetic marker', encoding='utf-8')
    with pytest.raises(BackupRestoreTargetError):
        restore_product_backup_user_safe(package, **kwargs)
    assert not (target / 'anxinboard.db').exists()


def test_installer_test_flag_does_not_exempt_arbitrary_database_override(restore_case, monkeypatch):
    package, canonical = restore_case
    target = canonical.parent / 'isolated-test'
    target.mkdir()
    monkeypatch.setenv('ANXINBOARD_INSTALLER_TEST_MODE', '1')
    monkeypatch.setenv('ANXINBOARD_DB_PATH', str(target / 'anxinboard.db'))
    with pytest.raises(BackupRestoreTargetError):
        restore_product_backup_user_safe(package, data_root=target)
    assert not (target / 'anxinboard.db').exists()
    wrong = canonical.parent / 'another-test'
    wrong.mkdir()
    with pytest.raises(BackupRestoreTargetError):
        restore_product_backup_user_safe(package, data_root=wrong)


def test_canonical_product_root_symlink_is_not_admitted(restore_case):
    package, target = restore_case
    target.rmdir()
    other = target.parent / 'outside'
    other.mkdir()
    try:
        os.symlink(other, target, target_is_directory=True)
    except OSError:
        pytest.skip('directory symlink privilege unavailable')
    with pytest.raises(BackupRestoreTargetError):
        restore_product_backup_user_safe(package)
    assert not (other / 'anxinboard.db').exists()


def test_entry_preserves_safe_restore_target_code_without_exception_text(monkeypatch, capsys):
    import product_entry
    def refused(*args, **kwargs):
        raise BackupRestoreTargetError('private synthetic exception body')
    monkeypatch.setattr(product_entry, 'restore_product_backup_user_safe', refused)
    assert product_entry._run_restore('synthetic.zip', confirmed=True) == 79
    assert capsys.readouterr().err == 'BACKUP_RESTORE_TARGET_INVALID: restore refused\n'


def test_exact_canonical_override_is_allowed_but_relative_override_is_not(restore_case, monkeypatch):
    package, target = restore_case
    monkeypatch.setenv('ANXINBOARD_DB_PATH', str(target / 'anxinboard.db').replace('\\', '/'))
    restore_product_backup_user_safe(package)
    monkeypatch.setenv('ANXINBOARD_DB_PATH', 'relative/anxinboard.db')
    with pytest.raises(BackupRestoreTargetError):
        restore_product_backup_user_safe(package, data_root=target)
    with pytest.raises(ProductBackupError):
        restore_product_backup_user_safe(package, data_root=Path('relative'))


@pytest.mark.parametrize('entry', ['other.db', '../anxinboard.db', 'nested/anxinboard.db'])
def test_product_storage_allowlist_cannot_write_or_inspect_other_entries(restore_case, entry):
    _, target = restore_case
    storage = _ProductRestoreStorage(target, root_id=PRODUCT_DATA_ROOT_ID)
    for operation in (lambda: storage.exists(entry), lambda: storage.atomic_write_bytes(entry, b'not-a-database'), lambda: storage.sha256(entry)):
        with pytest.raises(BackupRestoreTargetError):
            operation()


def test_entry_does_not_print_unrecognized_exception_code(monkeypatch, capsys):
    import product_entry
    class ForeignCode(BackupRestoreTargetError):
        code = 'private-path-or-content'
    def refused(*args, **kwargs):
        raise ForeignCode('private synthetic exception body')
    monkeypatch.setattr(product_entry, 'restore_product_backup_user_safe', refused)
    assert product_entry._run_restore('synthetic.zip', confirmed=True) == 79
    assert capsys.readouterr().err == 'PRODUCT_RESTORE_FAILED: restore refused\n'
