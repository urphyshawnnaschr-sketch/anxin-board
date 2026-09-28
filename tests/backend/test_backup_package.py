"""Acceptance tests for the explicit bounded backup package core."""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import backup_package, storage  # noqa: E402


FIXED_TIME = "2026-08-28T10:00:00Z"
_GIT_POLICY_TESTS = {
    "test_git_workspace_is_excluded_by_default_even_for_regular_file",
    "test_nested_git_workspace_source_root_is_excluded_by_default",
}


@pytest.fixture(autouse=True)
def _isolate_unrelated_host_git_ancestry(request, monkeypatch):
    """Keep non-Git unit cases independent from an unrelated host-level .git marker.

    The two dedicated Git exclusion tests intentionally use the real production
    ancestor scan. Every other test isolates a different contract and must not be
    converted into a Git-policy test merely because a CI host keeps Temp below a
    repository-like ancestor.
    """

    if request.node.name not in _GIT_POLICY_TESTS:
        monkeypatch.setattr(storage.Storage, "is_git_workspace_root", lambda self: False)


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _entry(store, source_path, archive_path, logical_type="report"):
    return backup_package.BackupEntry(
        logical_type=logical_type,
        source=store,
        source_path=source_path,
        archive_path=archive_path,
    )


def test_manifest_is_versioned_ordered_and_hash_size_consistent(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "prd.md").write_bytes("需求".encode("utf-8"))
    (source_root / "report.html").write_bytes(b"<html>ok</html>")
    store = storage.Storage(source_root, root_id="project-data")
    output = tmp_path / "backup.zip"

    manifest = backup_package.build_backup_package(
        [
            _entry(store, "prd.md", "project/prd.md", "prd"),
            _entry(store, "report.html", "reports/report.html", "report_html"),
        ],
        output,
        created_at=FIXED_TIME,
    )

    assert manifest["schema_version"] == backup_package.BACKUP_SCHEMA_VERSION
    assert manifest["created_at"] == FIXED_TIME
    assert [item["archive_path"] for item in manifest["entries"]] == [
        "project/prd.md",
        "reports/report.html",
    ]
    payload = {key: manifest[key] for key in ("schema_version", "created_at", "entries")}
    assert manifest["manifest_sha256"] == hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["project/prd.md", "reports/report.html", "manifest.json"]
        stored_manifest = json.loads(archive.read("manifest.json"))
        assert stored_manifest == manifest
        for item in manifest["entries"]:
            body = archive.read(item["archive_path"])
            assert len(body) == item["size"]
            assert hashlib.sha256(body).hexdigest() == item["sha256"]


def test_same_inputs_and_fixed_time_produce_deterministic_manifest(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "a.txt").write_bytes(b"a")
    store = storage.Storage(source_root, root_id="root-A")
    entries = [_entry(store, "a.txt", "a.txt", "data")]
    first = backup_package.build_backup_package(entries, tmp_path / "one.zip", created_at=FIXED_TIME)
    second = backup_package.build_backup_package(entries, tmp_path / "two.zip", created_at=FIXED_TIME)
    assert first == second


@pytest.mark.parametrize(
    "archive_path",
    ["../evil.txt", "a/../evil.txt", "/evil.txt", "//server/evil", "C:/evil", r"a\evil.txt", "a//b"],
)
def test_zip_slip_and_noncanonical_archive_paths_are_impossible(tmp_path, archive_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "safe.txt").write_bytes(b"safe")
    store = storage.Storage(source_root, root_id="root-A")
    with pytest.raises(backup_package.BackupInvalidEntryError):
        backup_package.build_backup_package(
            [_entry(store, "safe.txt", archive_path)],
            tmp_path / "backup.zip",
            created_at=FIXED_TIME,
        )
    assert not (tmp_path / "backup.zip").exists()


def test_duplicate_archive_path_fails_closed_including_case_collision(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "a.txt").write_bytes(b"a")
    (source_root / "b.txt").write_bytes(b"b")
    store = storage.Storage(source_root, root_id="root-A")
    with pytest.raises(backup_package.BackupDuplicateArchivePathError):
        backup_package.build_backup_package(
            [
                _entry(store, "a.txt", "same.txt"),
                _entry(store, "b.txt", "SAME.TXT"),
            ],
            tmp_path / "backup.zip",
            created_at=FIXED_TIME,
        )


def test_source_root_id_cannot_alias_two_different_roots(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "a.txt").write_bytes(b"a")
    (second_root / "b.txt").write_bytes(b"b")
    first = storage.Storage(first_root, root_id="same-root")
    second = storage.Storage(second_root, root_id="same-root")

    with pytest.raises(backup_package.BackupInvalidEntryError):
        backup_package.build_backup_package(
            [
                _entry(first, "a.txt", "a.txt"),
                _entry(second, "b.txt", "b.txt"),
            ],
            tmp_path / "backup.zip",
            created_at=FIXED_TIME,
        )


def test_same_source_root_cannot_claim_two_root_ids(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "a.txt").write_bytes(b"a")
    (source_root / "b.txt").write_bytes(b"b")
    first = storage.Storage(source_root, root_id="root-A")
    second = storage.Storage(source_root, root_id="root-B")

    with pytest.raises(backup_package.BackupInvalidEntryError):
        backup_package.build_backup_package(
            [
                _entry(first, "a.txt", "a.txt"),
                _entry(second, "b.txt", "b.txt"),
            ],
            tmp_path / "backup.zip",
            created_at=FIXED_TIME,
        )


def test_git_workspace_is_excluded_by_default_even_for_regular_file(tmp_path):
    source_root = tmp_path / "repo"
    source_root.mkdir()
    (source_root / ".git").mkdir()
    (source_root / "README.md").write_bytes(b"workspace")
    store = storage.Storage(source_root, root_id="repo-root")
    with pytest.raises(backup_package.BackupInvalidEntryError):
        backup_package.build_backup_package(
            [_entry(store, "README.md", "README.md")],
            tmp_path / "backup.zip",
            created_at=FIXED_TIME,
        )


def test_nested_git_workspace_source_root_is_excluded_by_default(tmp_path):
    repo = tmp_path / "repo"
    source_root = repo / "data" / "reports"
    source_root.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    (source_root / "report.html").write_bytes(b"workspace-data")
    store = storage.Storage(source_root, root_id="nested-repo-data")
    output = tmp_path / "backup.zip"

    with pytest.raises(backup_package.BackupInvalidEntryError):
        backup_package.build_backup_package(
            [_entry(store, "report.html", "report.html")],
            output,
            created_at=FIXED_TIME,
        )
    assert not output.exists()


@pytest.mark.parametrize(
    "source_path",
    [
        ".env",
        "credentials/key.json",
        "credentials.json",
        "project-secret.txt",
        "smtp_token.json",
        "password.txt",
        "passwd.txt",
        "api_key.json",
        "runtime/session.json",
        "runtime_session.json",
        "session.json",
        "sessions/current.json",
        "logs/app.log",
        "diagnostics/state.json",
    ],
)
def test_secret_runtime_and_unapproved_logs_are_excluded_by_default(tmp_path, source_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    target = source_root.joinpath(*source_path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"blocked")
    store = storage.Storage(source_root, root_id="root-A")
    with pytest.raises(backup_package.BackupInvalidEntryError):
        backup_package.build_backup_package(
            [_entry(store, source_path, "safe/export.bin")],
            tmp_path / "backup.zip",
            created_at=FIXED_TIME,
        )


@pytest.mark.parametrize(
    "logical_type",
    [
        "secret",
        "credentials",
        "smtp_token",
        "password",
        "api_key",
        "runtime_session",
        "diagnostics_log",
    ],
)
def test_sensitive_logical_type_is_excluded_even_when_paths_are_generic(tmp_path, logical_type):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "config.json").write_bytes(b"blocked")
    store = storage.Storage(source_root, root_id="root-A")
    output = tmp_path / "backup.zip"

    with pytest.raises(backup_package.BackupInvalidEntryError):
        backup_package.build_backup_package(
            [_entry(store, "config.json", "safe/config.json", logical_type)],
            output,
            created_at=FIXED_TIME,
        )
    assert not output.exists()


def test_source_changed_during_read_fails_closed_and_leaves_no_output(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source_root.mkdir()
    target = source_root / "changing.bin"
    target.write_bytes(b"stable")
    store = storage.Storage(source_root, root_id="root-A")
    real_read = storage._read_fd_bounded

    def read_then_touch(fd, limit):
        data = real_read(fd, limit)
        current = target.stat().st_mtime_ns
        os.utime(target, ns=(current, current + 10_000_000))
        return data

    monkeypatch.setattr(storage, "_read_fd_bounded", read_then_touch)
    with pytest.raises(backup_package.BackupSourceChangedError):
        backup_package.build_backup_package(
            [_entry(store, "changing.bin", "changing.bin")],
            tmp_path / "backup.zip",
            created_at=FIXED_TIME,
        )
    assert not (tmp_path / "backup.zip").exists()
    assert not list(tmp_path.glob(".backup.zip.tmp-*"))


def test_expected_hash_or_size_drift_fails_before_replacing_old_valid_backup(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "data.bin").write_bytes(b"old")
    store = storage.Storage(source_root, root_id="root-A")
    output = tmp_path / "backup.zip"

    backup_package.build_backup_package(
        [_entry(store, "data.bin", "data.bin")], output, created_at=FIXED_TIME
    )
    old_valid = output.read_bytes()
    (source_root / "data.bin").write_bytes(b"new")

    stale_entry = backup_package.BackupEntry(
        logical_type="data",
        source=store,
        source_path="data.bin",
        archive_path="data.bin",
        expected_sha256=hashlib.sha256(b"old").hexdigest(),
        expected_size=len(b"old"),
    )
    with pytest.raises(backup_package.BackupSourceChangedError):
        backup_package.build_backup_package([stale_entry], output, created_at=FIXED_TIME)
    assert output.read_bytes() == old_valid
    with zipfile.ZipFile(output) as archive:
        assert archive.read("data.bin") == b"old"


def test_successful_build_uses_bound_atomic_replace(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "data.bin").write_bytes(b"new")
    store = storage.Storage(source_root, root_id="root-A")
    output = tmp_path / "backup.zip"
    output.write_bytes(b"old-placeholder")
    calls = []

    if os.name == "nt":
        real_replace = storage._windows_rename_handle_relative

        def recording_replace(file_handle, directory_handle, target_name):
            calls.append((file_handle, directory_handle, target_name))
            return real_replace(file_handle, directory_handle, target_name)

        monkeypatch.setattr(storage, "_windows_rename_handle_relative", recording_replace)
    else:
        real_replace = storage.os.replace

        def recording_replace(source, target, *args, **kwargs):
            calls.append((source, target, kwargs.copy()))
            return real_replace(source, target, *args, **kwargs)

        monkeypatch.setattr(storage.os, "replace", recording_replace)

    backup_package.build_backup_package(
        [_entry(store, "data.bin", "data.bin")], output, created_at=FIXED_TIME
    )
    assert len(calls) == 1
    if os.name == "nt":
        _file_handle, _directory_handle, target_name = calls[0]
        assert target_name == "backup.zip"
    else:
        source, target, kwargs = calls[0]
        assert Path(source).name.startswith(".backup.zip.tmp-")
        assert target == "backup.zip"
        assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
    with zipfile.ZipFile(output) as archive:
        assert archive.read("data.bin") == b"new"


@pytest.mark.parametrize(
    "bad_output_name",
    ["backup.zip:ads", "NUL", "CON.txt", "backup.zip.", "backup.zip "],
)
def test_backup_output_rejects_windows_alias_or_ads_names_before_temp_write(tmp_path, bad_output_name):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "data.bin").write_bytes(b"trusted")
    store = storage.Storage(source_root, root_id="root-A")
    output = tmp_path / bad_output_name

    with pytest.raises(storage.StorageInvalidPathError):
        backup_package.build_backup_package(
            [_entry(store, "data.bin", "data.bin")],
            output,
            created_at=FIXED_TIME,
        )
    assert not list(tmp_path.glob(".*.tmp-*"))


def test_backup_parent_swap_before_temp_creation_cannot_escape(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "data.bin").write_bytes(b"trusted")
    store = storage.Storage(source_root, root_id="root-A")

    output_parent = tmp_path / "output"
    output_parent.mkdir()
    output = output_parent / "backup.zip"
    outside = tmp_path / "outside-create"
    outside.mkdir()
    parked = tmp_path / "parked-output-create"
    real_create_temp = storage._AtomicDirectory.create_temp
    attempted = False

    def swap_then_create(bound, prefix):
        nonlocal attempted
        if not attempted:
            attempted = True
            try:
                output_parent.rename(parked)
                output_parent.symlink_to(outside, target_is_directory=True)
            except OSError:
                pass
        return real_create_temp(bound, prefix)

    monkeypatch.setattr(storage._AtomicDirectory, "create_temp", swap_then_create)
    try:
        backup_package.build_backup_package(
            [_entry(store, "data.bin", "data.bin")],
            output,
            created_at=FIXED_TIME,
        )
    except backup_package.BackupWriteError:
        pass

    assert attempted is True
    assert not (outside / "backup.zip").exists()
    assert not list(outside.glob(".backup.zip.tmp-*"))
    safe_output = parked / "backup.zip" if parked.exists() else output_parent / "backup.zip"
    if safe_output.exists():
        with zipfile.ZipFile(safe_output) as archive:
            assert archive.read("data.bin") == b"trusted"
        assert not list(safe_output.parent.glob(".backup.zip.tmp-*"))


def test_backup_parent_swap_after_validation_cannot_escape(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "data.bin").write_bytes(b"trusted")
    store = storage.Storage(source_root, root_id="root-A")

    output_parent = tmp_path / "output"
    output_parent.mkdir()
    output = output_parent / "backup.zip"
    outside = tmp_path / "outside"
    outside.mkdir()
    parked = tmp_path / "parked-output"
    attempted = False

    if os.name == "nt":
        real_replace = storage._windows_rename_handle_relative

        def swap_then_replace(file_handle, directory_handle, target_name):
            nonlocal attempted
            if not attempted:
                attempted = True
                try:
                    output_parent.rename(parked)
                    output_parent.symlink_to(outside, target_is_directory=True)
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
                    output_parent.rename(parked)
                    output_parent.symlink_to(outside, target_is_directory=True)
                    (outside / Path(source).name).write_bytes(b"attacker-decoy")
                except OSError:
                    pass
            return real_replace(source, target, *args, **kwargs)

        monkeypatch.setattr(storage.os, "replace", swap_then_replace)

    try:
        backup_package.build_backup_package(
            [_entry(store, "data.bin", "data.bin")],
            output,
            created_at=FIXED_TIME,
        )
    except backup_package.BackupWriteError:
        pass

    assert attempted is True
    assert not (outside / "backup.zip").exists()
    decoys = list(outside.glob(".backup.zip.tmp-*"))
    assert len(decoys) <= 1
    if decoys:
        assert decoys[0].read_bytes() == b"attacker-decoy"

    safe_output = parked / "backup.zip" if parked.exists() else output_parent / "backup.zip"
    assert safe_output.exists()
    with zipfile.ZipFile(safe_output) as archive:
        assert archive.read("data.bin") == b"trusted"
    assert not list(safe_output.parent.glob(".backup.zip.tmp-*"))


def test_manifest_hash_binds_complete_entries(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "data.bin").write_bytes(b"payload")
    store = storage.Storage(source_root, root_id="root-A")
    manifest = backup_package.build_backup_package(
        [_entry(store, "data.bin", "data.bin")],
        tmp_path / "backup.zip",
        created_at=FIXED_TIME,
    )
    tampered = json.loads(json.dumps(manifest))
    tampered["entries"][0]["size"] += 1
    tampered_payload = {key: tampered[key] for key in ("schema_version", "created_at", "entries")}
    assert hashlib.sha256(_canonical_bytes(tampered_payload)).hexdigest() != manifest["manifest_sha256"]


def test_module_has_no_network_or_cloud_side_effect_dependencies():
    source = Path(backup_package.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(
        {
            "socket",
            "httpx",
            "requests",
            "urllib",
            "smtplib",
            "boto3",
            "azure",
            "google",
        }
    )


def test_max_entries_stops_at_first_disallowed_item_and_preserves_old_backup(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "data.bin").write_bytes(b"payload")
    store = storage.Storage(source_root, root_id="root-A")
    output = tmp_path / "backup.zip"
    backup_package.build_backup_package(
        [_entry(store, "data.bin", "old.bin")], output, created_at=FIXED_TIME
    )
    old_valid = output.read_bytes()
    consumed = []

    def entries():
        for index in range(10):
            consumed.append(index)
            yield _entry(store, "data.bin", f"data-{index}.bin")

    with pytest.raises(backup_package.BackupTooLargeError):
        backup_package.build_backup_package(
            entries(),
            output,
            created_at=FIXED_TIME,
            max_entries=2,
        )

    assert consumed == [0, 1, 2]
    assert output.read_bytes() == old_valid
    assert not list(tmp_path.glob(".backup.zip.tmp-*"))


def test_total_source_bound_is_applied_to_each_read_and_preserves_old_backup(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "old.bin").write_bytes(b"old")
    (source_root / "first.bin").write_bytes(b"1234")
    (source_root / "second.bin").write_bytes(b"56789")
    store = storage.Storage(source_root, root_id="root-A")
    output = tmp_path / "backup.zip"
    backup_package.build_backup_package(
        [_entry(store, "old.bin", "old.bin")], output, created_at=FIXED_TIME
    )
    old_valid = output.read_bytes()

    read_limits = []
    body_read_limits = []
    real_read_bytes = storage.Storage.read_bytes
    real_read_fd_bounded = storage._read_fd_bounded

    def recording_read_bytes(self, relative_path, *, max_bytes=None):
        read_limits.append(max_bytes)
        return real_read_bytes(self, relative_path, max_bytes=max_bytes)

    def recording_read_fd_bounded(fd, limit):
        body_read_limits.append(limit)
        return real_read_fd_bounded(fd, limit)

    monkeypatch.setattr(storage.Storage, "read_bytes", recording_read_bytes)
    monkeypatch.setattr(storage, "_read_fd_bounded", recording_read_fd_bounded)

    with pytest.raises(backup_package.BackupTooLargeError):
        backup_package.build_backup_package(
            [
                _entry(store, "first.bin", "first.bin"),
                _entry(store, "second.bin", "second.bin"),
            ],
            output,
            created_at=FIXED_TIME,
            max_total_source_bytes=6,
        )

    assert read_limits == [6, 2]
    assert body_read_limits == [6]
    assert output.read_bytes() == old_valid
    assert not list(tmp_path.glob(".backup.zip.tmp-*"))


def test_package_bound_blocks_overwrite_before_temp_crosses_limit_and_preserves_old_backup(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "old.bin").write_bytes(b"old")
    (source_root / "payload.bin").write_bytes(bytes(range(256)) * 16)
    store = storage.Storage(source_root, root_id="root-A")
    output = tmp_path / "backup.zip"
    backup_package.build_backup_package(
        [_entry(store, "old.bin", "old.bin")], output, created_at=FIXED_TIME
    )
    old_valid = output.read_bytes()

    temp_sizes_before_cleanup = []
    real_unlink = storage._AtomicDirectory.unlink

    def recording_unlink(bound, name):
        st = bound.lstat(name)
        if st is not None and name.startswith(".backup.zip.tmp-"):
            temp_sizes_before_cleanup.append(int(st.st_size))
        return real_unlink(bound, name)

    monkeypatch.setattr(storage._AtomicDirectory, "unlink", recording_unlink)
    max_package_bytes = 80

    with pytest.raises(backup_package.BackupTooLargeError):
        backup_package.build_backup_package(
            [_entry(store, "payload.bin", "payload.bin")],
            output,
            created_at=FIXED_TIME,
            max_package_bytes=max_package_bytes,
        )

    assert temp_sizes_before_cleanup
    assert max(temp_sizes_before_cleanup) <= max_package_bytes
    assert output.read_bytes() == old_valid
    assert not list(tmp_path.glob(".backup.zip.tmp-*"))
