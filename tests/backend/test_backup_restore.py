from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

import pytest

from app.backup_package import BackupEntry, _canonical_json_bytes, build_backup_package
from app.backup_restore import (
    BackupRestoreConflictError,
    BackupRestoreInvalidPackageError,
    BackupRestoreTargetError,
    restore_backup_package,
    validate_backup_package,
)
from app.storage import StorageAccessError, TempStorage


@pytest.fixture(autouse=True)
def _isolate_temp_storage_from_runner_git_ancestry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TempStorage, "is_git_workspace_root", lambda self: False)


def _build_package(path: Path, source: TempStorage, entries: list[tuple[str, bytes]]) -> None:
    backup_entries: list[BackupEntry] = []
    for name, data in entries:
        source.atomic_write_bytes(name, data)
        backup_entries.append(
            BackupEntry(
                logical_type="product-data",
                source=source,
                source_path=name,
                archive_path=f"data/{name}",
            )
        )
    build_backup_package(
        backup_entries,
        path,
        created_at="2026-09-06T00:00:00Z",
    )


def _rewrite_zip(path: Path, transform) -> None:
    with zipfile.ZipFile(path, "r") as archive:
        members = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    rewritten = transform(dict(members))
    temp = path.with_suffix(".rewrite.zip")
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in rewritten.items():
            archive.writestr(name, data)
    temp.replace(path)


def test_validate_and_restore_round_trip_with_explicit_replace() -> None:
    with tempfile.TemporaryDirectory() as tmp, TempStorage(root_id="product-root") as source, TempStorage(
        root_id="product-root"
    ) as target:
        package = Path(tmp) / "backup.zip"
        _build_package(package, source, [("one.json", b"alpha"), ("two.db", b"bravo")])

        validated = validate_backup_package(package)
        assert validated.schema_version == "rd_agent_backup_package_v1"
        assert validated.total_bytes == 10
        assert [entry.source_path for entry in validated.entries] == ["one.json", "two.db"]

        result = restore_backup_package(package, {"product-root": target})
        assert result["restored_entries"] == 2
        assert result["restored_bytes"] == 10
        assert target.read_bytes("one.json") == b"alpha"
        assert target.read_bytes("two.db") == b"bravo"

        with pytest.raises(BackupRestoreConflictError):
            restore_backup_package(package, {"product-root": target})

        replaced = restore_backup_package(
            package,
            {"product-root": target},
            replace_existing=True,
        )
        assert replaced["replace_existing"] is True
        assert target.read_bytes("one.json") == b"alpha"


def test_rejects_tampered_member_before_any_target_write() -> None:
    with tempfile.TemporaryDirectory() as tmp, TempStorage(root_id="product-root") as source, TempStorage(
        root_id="product-root"
    ) as target:
        package = Path(tmp) / "backup.zip"
        _build_package(package, source, [("one.json", b"alpha")])

        def tamper(members: dict[str, bytes]) -> dict[str, bytes]:
            members["data/one.json"] = b"omega"
            return members

        _rewrite_zip(package, tamper)
        with pytest.raises(BackupRestoreInvalidPackageError):
            restore_backup_package(package, {"product-root": target})
        assert not target.exists("one.json")


def test_rejects_unsupported_schema_even_with_self_consistent_manifest_hash() -> None:
    with tempfile.TemporaryDirectory() as tmp, TempStorage(root_id="product-root") as source:
        package = Path(tmp) / "backup.zip"
        _build_package(package, source, [("one.json", b"alpha")])

        def change_schema(members: dict[str, bytes]) -> dict[str, bytes]:
            manifest = json.loads(members["manifest.json"].decode("utf-8"))
            manifest["schema_version"] = "rd_agent_backup_package_v2"
            payload = {
                "schema_version": manifest["schema_version"],
                "created_at": manifest["created_at"],
                "entries": manifest["entries"],
            }
            manifest["manifest_sha256"] = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
            members["manifest.json"] = _canonical_json_bytes(manifest)
            return members

        _rewrite_zip(package, change_schema)
        with pytest.raises(BackupRestoreInvalidPackageError):
            validate_backup_package(package)


def test_rejects_manifest_path_escape_before_restore() -> None:
    with tempfile.TemporaryDirectory() as tmp, TempStorage(root_id="product-root") as source:
        package = Path(tmp) / "backup.zip"
        _build_package(package, source, [("one.json", b"alpha")])

        def escape(members: dict[str, bytes]) -> dict[str, bytes]:
            manifest = json.loads(members.pop("manifest.json").decode("utf-8"))
            entry = manifest["entries"][0]
            old_archive = entry["archive_path"]
            data = members.pop(old_archive)
            entry["archive_path"] = "../escape.json"
            payload = {
                "schema_version": manifest["schema_version"],
                "created_at": manifest["created_at"],
                "entries": manifest["entries"],
            }
            manifest["manifest_sha256"] = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
            members["../escape.json"] = data
            members["manifest.json"] = _canonical_json_bytes(manifest)
            return members

        _rewrite_zip(package, escape)
        with pytest.raises(BackupRestoreInvalidPackageError):
            validate_backup_package(package)


def test_restore_requires_exact_approved_root_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp, TempStorage(root_id="product-root") as source, TempStorage(
        root_id="other-root"
    ) as wrong_target:
        package = Path(tmp) / "backup.zip"
        _build_package(package, source, [("one.json", b"alpha")])
        with pytest.raises(BackupRestoreTargetError):
            restore_backup_package(package, {"product-root": wrong_target})


def test_interrupted_prefix_can_converge_by_revalidating_same_package(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp, TempStorage(root_id="product-root") as source, TempStorage(
        root_id="product-root"
    ) as target:
        package = Path(tmp) / "backup.zip"
        _build_package(package, source, [("one.json", b"alpha"), ("two.db", b"bravo")])

        original_write = target.atomic_write_bytes
        calls = 0

        def fail_second(path, data, *, max_bytes=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise StorageAccessError("synthetic interruption")
            return original_write(path, data, max_bytes=max_bytes)

        monkeypatch.setattr(target, "atomic_write_bytes", fail_second)
        with pytest.raises(BackupRestoreTargetError):
            restore_backup_package(package, {"product-root": target})
        assert target.read_bytes("one.json") == b"alpha"
        assert not target.exists("two.db")

        monkeypatch.setattr(target, "atomic_write_bytes", original_write)
        result = restore_backup_package(
            package,
            {"product-root": target},
            replace_existing=True,
        )
        assert result["restored_entries"] == 2
        assert target.read_bytes("one.json") == b"alpha"
        assert target.read_bytes("two.db") == b"bravo"
