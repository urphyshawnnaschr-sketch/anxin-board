"""Acceptance tests for the bounded approved-root Storage contract."""

from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import sys

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import storage  # noqa: E402


def test_temp_storage_read_write_exists_stat_and_hash_round_trip():
    with storage.TempStorage(root_id="unit") as store:
        assert store.exists("payload.bin") is False
        meta = store.atomic_write_bytes("payload.bin", b"hello-storage")
        assert meta.size == len(b"hello-storage")
        assert store.exists("payload.bin") is True
        assert store.read_bytes("payload.bin") == b"hello-storage"
        assert store.sha256("payload.bin") == hashlib.sha256(b"hello-storage").hexdigest()
        assert store.stat("payload.bin").size == len(b"hello-storage")


def test_windows_native_same_directory_rename_contract_uses_null_root(monkeypatch):
    target_name = "target.bin"
    encoded = target_name.encode("utf-16-le")
    captured = {}

    def fake_nt_set(file_handle, iosb, raw, total, information_class):
        del file_handle, iosb
        info = ctypes.cast(
            raw, ctypes.POINTER(storage._FILE_RENAME_INFORMATION)
        ).contents
        captured["information_class"] = information_class
        captured["replace"] = int(info.ReplaceIfExists)
        captured["root"] = storage._windows_handle_value(info.RootDirectory)
        captured["name_length"] = int(info.FileNameLength)
        captured["total"] = int(total)
        captured["payload"] = ctypes.string_at(
            ctypes.addressof(raw) + storage._FILE_RENAME_INFORMATION.FileName.offset,
            len(encoded),
        )
        return 0

    monkeypatch.setattr(storage, "_windows_nt_set_information_file", lambda: fake_nt_set)
    storage._windows_rename_handle_relative(101, 202, target_name)

    assert captured == {
        "information_class": storage._FILE_RENAME_INFORMATION_CLASS_NT,
        "replace": 1,
        "root": 0,
        "name_length": len(encoded),
        "total": ctypes.sizeof(storage._FILE_RENAME_INFORMATION) + len(encoded),
        "payload": encoded,
    }


def test_windows_native_rename_failure_reports_ntstatus_and_winerror(monkeypatch):
    status_invalid_parameter = ctypes.c_int32(0xC000000D).value

    def fake_nt_set(_file_handle, _iosb, _raw, _total, _information_class):
        return status_invalid_parameter

    monkeypatch.setattr(storage, "_windows_nt_set_information_file", lambda: fake_nt_set)
    monkeypatch.setattr(storage, "_windows_ntstatus_to_dos_error", lambda _status: 87)

    with pytest.raises(storage.StorageAccessError) as exc_info:
        storage._windows_rename_handle_relative(101, 202, "target.bin")

    message = str(exc_info.value)
    assert "ntstatus=0xC000000D" in message
    assert "winerror=87" in message
    assert exc_info.value.code == "STORAGE_ACCESS_DENIED"


@pytest.mark.parametrize(
    "bad_path",
    [
        "../escape.txt",
        "a/../escape.txt",
        "/absolute.txt",
        "//server/share/file.txt",
        r"\\server\share\file.txt",
        "C:/escape.txt",
        "C:escape.txt",
        r"folder\file.txt",
        "./file.txt",
        "folder//file.txt",
        "file.txt:ads",
        "NUL",
        "con.txt",
        "folder/NUL.txt",
        "trailing.",
        "folder/trailing. ",
    ],
)
def test_path_escape_grammar_fails_closed(tmp_path, bad_path):
    store = storage.Storage(tmp_path, root_id="approved")
    with pytest.raises(storage.StorageInvalidPathError) as exc_info:
        store.exists(bad_path)
    assert exc_info.value.code == "STORAGE_INVALID_PATH"


@pytest.mark.parametrize(
    "bad_name",
    ["backup.zip:ads", "NUL", "CON.txt", "backup.zip.", "backup.zip "],
)
def test_bound_directory_rejects_windows_alias_or_ads_names(tmp_path, bad_name):
    with storage._AtomicDirectory(tmp_path) as bound:
        with pytest.raises(storage.StorageInvalidPathError):
            bound.lstat(bad_name)


def test_git_workspace_detection_walks_ancestor_marker(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    nested = repo / "data" / "reports"
    nested.mkdir(parents=True)
    # A .git file is used by linked worktrees; a directory marker behaves the same
    # for this bounded metadata-only exclusion check.
    (repo / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    store = storage.Storage(nested, root_id="nested-project-data")

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_store = storage.Storage(outside, root_id="outside-data")

    # CI hosts may themselves place the OS temp tree below an unrelated .git marker.
    # Make this unit proof deterministic without weakening the production scan: only
    # the marker created by this test counts as a Git ancestor in this fixture.
    real_lstat = storage.os.lstat

    def controlled_lstat(path):
        candidate = Path(path)
        if candidate == repo / ".git":
            return real_lstat(path)
        if candidate.name == ".git":
            raise FileNotFoundError(path)
        return real_lstat(path)

    monkeypatch.setattr(storage.os, "lstat", controlled_lstat)
    assert store.is_git_workspace_root() is True
    assert outside_store.is_git_workspace_root() is False


def test_symlink_escape_fails_closed_when_platform_can_create_it(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"do-not-read")
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this test environment")

    store = storage.Storage(tmp_path, root_id="approved")
    with pytest.raises(storage.StorageInvalidPathError):
        store.read_bytes("linked/secret.txt")


def test_read_parent_swap_after_bound_lstat_cannot_follow_new_link(tmp_path, monkeypatch):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "state.bin").write_bytes(b"trusted-payload")
    outside = tmp_path.parent / f"{tmp_path.name}-read-outside"
    outside.mkdir()
    (outside / "state.bin").write_bytes(b"attacker-payload")
    parked = tmp_path / "parked-nested-read"
    store = storage.Storage(tmp_path, root_id="approved")

    real_open_read = storage._AtomicDirectory.open_read
    attempted = False

    def swap_then_open(bound, name):
        nonlocal attempted
        if not attempted:
            attempted = True
            try:
                nested.rename(parked)
                nested.symlink_to(outside, target_is_directory=True)
            except OSError:
                # A platform may block the visible swap; the identity-bound read must
                # still never resolve a newly substituted parent.
                pass
        return real_open_read(bound, name)

    monkeypatch.setattr(storage._AtomicDirectory, "open_read", swap_then_open)
    try:
        data = store.read_bytes("nested/state.bin")
    except storage.StorageError:
        data = None

    assert data != b"attacker-payload"
    if data is not None:
        assert data == b"trusted-payload"
    assert (outside / "state.bin").read_bytes() == b"attacker-payload"
    if parked.exists():
        assert (parked / "state.bin").read_bytes() == b"trusted-payload"


def test_read_final_target_swap_after_bound_lstat_fails_closed(tmp_path, monkeypatch):
    target = tmp_path / "state.bin"
    target.write_bytes(b"trusted-payload")
    outside = tmp_path.parent / f"{tmp_path.name}-target-outside"
    outside.mkdir()
    attacker = outside / "state.bin"
    attacker.write_bytes(b"attacker-payload")
    parked = tmp_path / "parked-state.bin"
    store = storage.Storage(tmp_path, root_id="approved")

    real_open_read = storage._AtomicDirectory.open_read
    attempted = False

    def swap_target_then_open(bound, name):
        nonlocal attempted
        if not attempted:
            attempted = True
            try:
                target.rename(parked)
                target.symlink_to(attacker)
            except OSError:
                pass
        return real_open_read(bound, name)

    monkeypatch.setattr(storage._AtomicDirectory, "open_read", swap_target_then_open)
    try:
        data = store.read_bytes("state.bin")
    except storage.StorageError:
        data = None

    assert data != b"attacker-payload"
    assert attacker.read_bytes() == b"attacker-payload"
    if data is not None:
        assert data == b"trusted-payload"


def test_oversized_read_and_write_fail_with_stable_error(tmp_path):
    (tmp_path / "large.bin").write_bytes(b"12345")
    store = storage.Storage(tmp_path, root_id="approved", max_read_bytes=4, max_write_bytes=4)
    with pytest.raises(storage.StorageTooLargeError) as read_error:
        store.read_bytes("large.bin")
    assert read_error.value.code == "STORAGE_TOO_LARGE"
    with pytest.raises(storage.StorageTooLargeError) as write_error:
        store.atomic_write_bytes("write.bin", b"12345")
    assert write_error.value.code == "STORAGE_TOO_LARGE"


def test_changed_during_read_fails_closed(tmp_path, monkeypatch):
    target = tmp_path / "changing.bin"
    target.write_bytes(b"stable-bytes")
    store = storage.Storage(tmp_path, root_id="approved")
    real_read = storage._read_fd_bounded

    def read_then_touch(fd, limit):
        data = real_read(fd, limit)
        current = target.stat().st_mtime_ns
        os.utime(target, ns=(current, current + 10_000_000))
        return data

    monkeypatch.setattr(storage, "_read_fd_bounded", read_then_touch)
    with pytest.raises(storage.StorageChangedDuringReadError) as exc_info:
        store.read_bytes("changing.bin")
    assert exc_info.value.code == "STORAGE_CHANGED_DURING_READ"


def test_access_failure_is_stable_and_does_not_expose_path(tmp_path, monkeypatch):
    (tmp_path / "denied.bin").write_bytes(b"x")
    store = storage.Storage(tmp_path, root_id="approved")

    if os.name == "nt":
        real_open = storage._windows_open_relative_handle

        def denied_open(directory_handle, name, **kwargs):
            if name == "denied.bin":
                raise storage.StorageAccessError("approved file cannot be accessed")
            return real_open(directory_handle, name, **kwargs)

        monkeypatch.setattr(storage, "_windows_open_relative_handle", denied_open)
    else:
        real_open = storage.os.open

        def denied_open(path, flags, *args, **kwargs):
            if Path(path).name == "denied.bin":
                raise PermissionError("sensitive absolute path should not escape")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(storage.os, "open", denied_open)

    with pytest.raises(storage.StorageAccessError) as exc_info:
        store.read_bytes("denied.bin")
    assert exc_info.value.code == "STORAGE_ACCESS_DENIED"
    assert str(tmp_path) not in str(exc_info.value)


def test_atomic_write_failure_preserves_old_file_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "state.bin"
    target.write_bytes(b"old-valid")
    store = storage.Storage(tmp_path, root_id="approved")

    if os.name == "nt":
        def fail_replace(_file_handle, _directory_handle, _target_name):
            raise storage.StorageAccessError("approved file could not be atomically replaced")

        monkeypatch.setattr(storage, "_windows_rename_handle_relative", fail_replace)
    else:
        def fail_replace(_source, _target, *args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(storage.os, "replace", fail_replace)

    with pytest.raises(storage.StorageAccessError):
        store.atomic_write_bytes("state.bin", b"new-data")
    assert target.read_bytes() == b"old-valid"
    assert not list(tmp_path.glob(".state.bin.tmp-*"))


def test_atomic_write_parent_swap_before_temp_creation_cannot_escape(tmp_path, monkeypatch):
    nested = tmp_path / "nested"
    nested.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-create-outside"
    outside.mkdir()
    parked = tmp_path / "parked-nested-create"
    store = storage.Storage(tmp_path, root_id="approved")
    real_create_temp = storage._AtomicDirectory.create_temp
    attempted = False

    def swap_then_create(bound, prefix):
        nonlocal attempted
        if not attempted:
            attempted = True
            try:
                nested.rename(parked)
                nested.symlink_to(outside, target_is_directory=True)
            except OSError:
                pass
        return real_create_temp(bound, prefix)

    monkeypatch.setattr(storage._AtomicDirectory, "create_temp", swap_then_create)
    try:
        store.atomic_write_bytes("nested/state.bin", b"trusted-payload")
    except storage.StorageError:
        pass

    assert not (outside / "state.bin").exists()
    assert not list(outside.glob(".state.bin.tmp-*"))
    safe_parent = parked if parked.exists() else nested
    if safe_parent.exists() and not safe_parent.is_symlink():
        assert (safe_parent / "state.bin").read_bytes() == b"trusted-payload"
        assert not list(safe_parent.glob(".state.bin.tmp-*"))


def test_atomic_write_parent_swap_after_validation_cannot_escape(tmp_path, monkeypatch):
    nested = tmp_path / "nested"
    nested.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    parked = tmp_path / "parked-nested"
    store = storage.Storage(tmp_path, root_id="approved")
    attempted = False

    if os.name == "nt":
        real_replace = storage._windows_rename_handle_relative

        def swap_then_replace(file_handle, directory_handle, target_name):
            nonlocal attempted
            if not attempted:
                attempted = True
                try:
                    nested.rename(parked)
                    nested.symlink_to(outside, target_is_directory=True)
                except OSError:
                    pass
            return real_replace(file_handle, directory_handle, target_name)

        monkeypatch.setattr(storage, "_windows_rename_handle_relative", swap_then_replace)
    else:
        real_replace = storage.os.replace

        def swap_then_replace(source, target, *args, **kwargs):
            nonlocal attempted
            if not attempted:
                attempted = True
                try:
                    nested.rename(parked)
                    nested.symlink_to(outside, target_is_directory=True)
                    (outside / Path(source).name).write_bytes(b"attacker-decoy")
                except OSError:
                    pass
            return real_replace(source, target, *args, **kwargs)

        monkeypatch.setattr(storage.os, "replace", swap_then_replace)

    try:
        store.atomic_write_bytes("nested/state.bin", b"trusted-payload")
    except storage.StorageError:
        pass

    assert attempted is True
    assert not (outside / "state.bin").exists()
    decoys = list(outside.glob(".state.bin.tmp-*"))
    assert len(decoys) <= 1
    if decoys:
        assert decoys[0].read_bytes() == b"attacker-decoy"
    if parked.exists():
        assert (parked / "state.bin").read_bytes() == b"trusted-payload"
        assert not list(parked.glob(".state.bin.tmp-*"))
    elif not nested.is_symlink():
        assert (nested / "state.bin").read_bytes() == b"trusted-payload"
        assert not list(nested.glob(".state.bin.tmp-*"))


def test_root_cannot_traverse_symlink_or_reparse_ancestor(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    (real_parent / "approved").mkdir()
    link_parent = tmp_path / "link-parent"
    try:
        link_parent.symlink_to(real_parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this test environment")
    with pytest.raises(storage.StorageInvalidPathError):
        storage.Storage(link_parent / "approved", root_id="approved")


def test_root_itself_cannot_be_symlink(tmp_path):
    real_root = tmp_path / "real"
    real_root.mkdir()
    link_root = tmp_path / "link"
    try:
        link_root.symlink_to(real_root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this test environment")
    with pytest.raises(storage.StorageInvalidPathError):
        storage.Storage(link_root, root_id="approved")
