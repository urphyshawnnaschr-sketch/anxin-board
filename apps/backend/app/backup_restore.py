"""Fail-closed validation and explicit restore for local backup packages.

Restore is intentionally separate from backup creation. The whole package is read and
validated before any target mutation. Each target must be an already-approved
``Storage`` root whose ``root_id`` exactly matches the manifest. Writes are atomic per
entry; a caller may safely retry an interrupted restore with ``replace_existing=True``
after the same package is revalidated. Real-user restore remains a product Human Gate.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
from typing import Final, Mapping
import zipfile

from app.backup_package import (
    BACKUP_SCHEMA_VERSION,
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_PACKAGE_BYTES,
    DEFAULT_MAX_TOTAL_SOURCE_BYTES,
    MANIFEST_MEMBER,
    _SHA256_RE,
    _LOGICAL_TYPE_RE,
    _canonical_archive_path,
    _canonical_created_at,
    _canonical_json_bytes,
    _component_is_sensitive,
    _logical_type_is_sensitive,
)
from app.storage import Storage, StorageError, canonical_relative_path


MAX_MANIFEST_BYTES: Final[int] = 2 * 1024 * 1024


class BackupRestoreError(Exception):
    code = "BACKUP_RESTORE_ERROR"


class BackupRestoreInvalidPackageError(BackupRestoreError):
    code = "BACKUP_RESTORE_INVALID_PACKAGE"


class BackupRestoreTooLargeError(BackupRestoreError):
    code = "BACKUP_RESTORE_TOO_LARGE"


class BackupRestoreTargetError(BackupRestoreError):
    code = "BACKUP_RESTORE_TARGET_INVALID"


class BackupRestoreConflictError(BackupRestoreError):
    code = "BACKUP_RESTORE_TARGET_EXISTS"


@dataclass(frozen=True)
class ValidatedRestoreEntry:
    logical_type: str
    source_root_id: str
    source_path: str
    archive_path: str
    size: int
    sha256: str
    data: bytes


@dataclass(frozen=True)
class ValidatedBackupPackage:
    schema_version: str
    created_at: str
    manifest_sha256: str
    entries: tuple[ValidatedRestoreEntry, ...]
    total_bytes: int


def _invalid(message: str) -> BackupRestoreInvalidPackageError:
    return BackupRestoreInvalidPackageError(message)


def _package_bytes(
    package_path: str | os.PathLike[str], *, max_package_bytes: int
) -> bytes:
    if max_package_bytes <= 0:
        raise ValueError("max_package_bytes must be positive")
    raw = os.fspath(package_path)
    if not isinstance(raw, str) or not raw:
        raise BackupRestoreInvalidPackageError("backup package path is invalid")
    path = Path(raw)
    if not path.is_absolute():
        path = path.absolute()
    try:
        source = Storage(
            path.parent,
            root_id="backup-restore-input",
            max_read_bytes=max_package_bytes,
            max_write_bytes=1,
        )
        return source.read_bytes(path.name, max_bytes=max_package_bytes)
    except StorageError as exc:
        raise BackupRestoreInvalidPackageError(
            "backup package is not a plain bounded local file"
        ) from exc


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    if limit < 0 or info.file_size < 0 or info.file_size > limit:
        raise BackupRestoreTooLargeError("backup member exceeds restore bounds")
    if info.flag_bits & 0x1:
        raise BackupRestoreInvalidPackageError("encrypted backup members are not supported")
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if info.is_dir() or file_type not in (0, stat.S_IFREG):
        raise BackupRestoreInvalidPackageError("backup contains a non-regular member")
    try:
        with archive.open(info, "r") as handle:
            data = handle.read(limit + 1)
            if len(data) > limit or handle.read(1):
                raise BackupRestoreTooLargeError("backup member exceeds restore bounds")
    except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
        raise BackupRestoreInvalidPackageError("backup member could not be decoded") from exc
    if len(data) != info.file_size:
        raise BackupRestoreInvalidPackageError("backup member size is inconsistent")
    return data


def _manifest_entry(entry: object) -> tuple[str, str, str, str, int, str]:
    if not isinstance(entry, dict) or set(entry) != {
        "archive_path",
        "logical_type",
        "source_root_id",
        "source_path",
        "size",
        "sha256",
    }:
        raise _invalid("backup manifest entry shape is invalid")

    logical_type = entry["logical_type"]
    root_id = entry["source_root_id"]
    source_path_raw = entry["source_path"]
    archive_path_raw = entry["archive_path"]
    size = entry["size"]
    digest = entry["sha256"]

    if not isinstance(logical_type, str) or _LOGICAL_TYPE_RE.fullmatch(logical_type) is None:
        raise _invalid("backup manifest logical type is invalid")
    if _logical_type_is_sensitive(logical_type):
        raise _invalid("backup manifest contains a sensitive logical type")
    if (
        not isinstance(root_id, str)
        or not root_id
        or root_id != root_id.strip()
        or len(root_id) > 128
        or any(ord(ch) < 32 for ch in root_id)
    ):
        raise _invalid("backup manifest root identity is invalid")
    try:
        source_path = canonical_relative_path(source_path_raw)
        archive_path = _canonical_archive_path(archive_path_raw)
    except (StorageError, Exception) as exc:
        if isinstance(exc, BackupRestoreError):
            raise
        raise _invalid("backup manifest path is invalid") from exc
    if any(_component_is_sensitive(part) for part in source_path.split("/")):
        raise _invalid("backup manifest source path is sensitive")
    if any(_component_is_sensitive(part) for part in archive_path.split("/")):
        raise _invalid("backup manifest archive path is sensitive")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise _invalid("backup manifest size is invalid")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise _invalid("backup manifest digest is invalid")
    return logical_type, root_id, source_path, archive_path, size, digest


def validate_backup_package(
    package_path: str | os.PathLike[str],
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_SOURCE_BYTES,
    max_package_bytes: int = DEFAULT_MAX_PACKAGE_BYTES,
) -> ValidatedBackupPackage:
    """Read and completely validate one V1 package without mutating restore targets."""

    if max_entries <= 0 or max_total_bytes <= 0 or max_package_bytes <= 0:
        raise ValueError("restore bounds must be positive")
    package = _package_bytes(package_path, max_package_bytes=max_package_bytes)

    try:
        archive_context = zipfile.ZipFile(BytesIO(package), mode="r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise _invalid("backup package is not a valid ZIP") from exc

    with archive_context as archive:
        infos = archive.infolist()
        if not infos or len(infos) > max_entries + 1:
            raise BackupRestoreTooLargeError("backup member count exceeds restore bounds")
        names = [info.filename for info in infos]
        if len({name.casefold() for name in names}) != len(names):
            raise _invalid("backup contains duplicate member names")
        if names.count(MANIFEST_MEMBER) != 1:
            raise _invalid("backup manifest is missing or duplicated")

        by_name = {info.filename: info for info in infos}
        manifest_info = by_name[MANIFEST_MEMBER]
        manifest_bytes = _read_member(archive, manifest_info, MAX_MANIFEST_BYTES)
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _invalid("backup manifest is not canonical UTF-8 JSON") from exc
        if not isinstance(manifest, dict) or set(manifest) != {
            "schema_version",
            "created_at",
            "entries",
            "manifest_sha256",
        }:
            raise _invalid("backup manifest shape is invalid")
        if manifest["schema_version"] != BACKUP_SCHEMA_VERSION:
            raise _invalid("backup schema version is not supported")
        try:
            created_at = _canonical_created_at(manifest["created_at"])
        except Exception as exc:
            raise _invalid("backup created_at is invalid") from exc
        if created_at != manifest["created_at"]:
            raise _invalid("backup created_at is not canonical")
        manifest_sha256 = manifest["manifest_sha256"]
        if not isinstance(manifest_sha256, str) or _SHA256_RE.fullmatch(manifest_sha256) is None:
            raise _invalid("backup manifest digest is invalid")

        payload = {
            "schema_version": manifest["schema_version"],
            "created_at": created_at,
            "entries": manifest["entries"],
        }
        actual_manifest_hash = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
        if actual_manifest_hash != manifest_sha256:
            raise _invalid("backup manifest integrity check failed")
        if _canonical_json_bytes(manifest) != manifest_bytes:
            raise _invalid("backup manifest serialization is not canonical")

        entries_raw = manifest["entries"]
        if not isinstance(entries_raw, list) or not entries_raw or len(entries_raw) > max_entries:
            raise _invalid("backup manifest entry count is invalid")

        parsed: list[tuple[str, str, str, str, int, str]] = []
        archive_paths: set[str] = set()
        source_keys: set[tuple[str, str]] = set()
        expected_total = 0
        for raw_entry in entries_raw:
            item = _manifest_entry(raw_entry)
            logical_type, root_id, source_path, archive_path, size, digest = item
            archive_key = archive_path.casefold()
            source_key = (root_id.casefold(), source_path.casefold())
            if archive_key in archive_paths or source_key in source_keys:
                raise _invalid("backup manifest contains duplicate restore identity")
            archive_paths.add(archive_key)
            source_keys.add(source_key)
            expected_total += size
            if expected_total > max_total_bytes:
                raise BackupRestoreTooLargeError("backup restore bytes exceed restore bounds")
            parsed.append(item)

        expected_names = {MANIFEST_MEMBER, *(item[3] for item in parsed)}
        if set(names) != expected_names:
            raise _invalid("backup members do not exactly match manifest entries")

        validated_entries: list[ValidatedRestoreEntry] = []
        actual_total = 0
        for logical_type, root_id, source_path, archive_path, size, digest in parsed:
            info = by_name[archive_path]
            if info.file_size != size:
                raise _invalid("backup member size does not match manifest")
            data = _read_member(archive, info, min(size, max_total_bytes - actual_total))
            if hashlib.sha256(data).hexdigest() != digest:
                raise _invalid("backup member digest does not match manifest")
            actual_total += len(data)
            validated_entries.append(
                ValidatedRestoreEntry(
                    logical_type=logical_type,
                    source_root_id=root_id,
                    source_path=source_path,
                    archive_path=archive_path,
                    size=size,
                    sha256=digest,
                    data=data,
                )
            )

    return ValidatedBackupPackage(
        schema_version=BACKUP_SCHEMA_VERSION,
        created_at=created_at,
        manifest_sha256=manifest_sha256,
        entries=tuple(validated_entries),
        total_bytes=actual_total,
    )


def restore_backup_package(
    package_path: str | os.PathLike[str],
    targets: Mapping[str, Storage],
    *,
    replace_existing: bool = False,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_SOURCE_BYTES,
    max_package_bytes: int = DEFAULT_MAX_PACKAGE_BYTES,
) -> dict[str, object]:
    """Validate first, then restore to explicit matching approved Storage roots.

    Package-level interruption is restartable rather than silently atomic: every file
    replacement is atomic, and a retry revalidates the full immutable package before
    converging the same bytes. Real-user invocation must remain behind the restore Gate.
    """

    validated = validate_backup_package(
        package_path,
        max_entries=max_entries,
        max_total_bytes=max_total_bytes,
        max_package_bytes=max_package_bytes,
    )
    if not isinstance(targets, Mapping):
        raise BackupRestoreTargetError("restore targets must be an explicit mapping")

    plans: list[tuple[ValidatedRestoreEntry, Storage]] = []
    for entry in validated.entries:
        target = targets.get(entry.source_root_id)
        if not isinstance(target, Storage) or target.root_id != entry.source_root_id:
            raise BackupRestoreTargetError("required restore root is not explicitly approved")
        try:
            if target.is_git_workspace_root():
                raise BackupRestoreTargetError("Git workspace roots are not restore targets")
            exists = target.exists(entry.source_path)
        except StorageError as exc:
            raise BackupRestoreTargetError("restore target preflight failed") from exc
        if exists and not replace_existing:
            raise BackupRestoreConflictError("restore target already exists")
        if entry.size > target.max_write_bytes:
            raise BackupRestoreTargetError("restore entry exceeds target write bound")
        plans.append((entry, target))

    restored = 0
    restored_bytes = 0
    try:
        for entry, target in plans:
            target.atomic_write_bytes(entry.source_path, entry.data, max_bytes=entry.size or 1)
            if target.sha256(entry.source_path, max_bytes=entry.size or 1) != entry.sha256:
                raise BackupRestoreTargetError("restored entry failed post-write integrity check")
            restored += 1
            restored_bytes += entry.size
    except StorageError as exc:
        raise BackupRestoreTargetError(
            "restore was interrupted; revalidate and retry the same package explicitly"
        ) from exc

    return {
        "schema_version": "rd_agent_backup_restore_result_v1",
        "backup_schema_version": validated.schema_version,
        "manifest_sha256": validated.manifest_sha256,
        "restored_entries": restored,
        "restored_bytes": restored_bytes,
        "replace_existing": bool(replace_existing),
    }
