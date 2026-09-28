"""Explicit, bounded, fail-closed local backup package builder.

Only caller-supplied ordered entries from approved ``Storage`` roots are accepted.
There is no filesystem discovery, restore/import, deletion, cloud, network, secret
export, or Git-workspace backup behavior in this foundation slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import stat as stat_module
import unicodedata
from typing import Final, Iterable, Sequence
import zipfile

from app.storage import (
    Storage,
    StorageChangedDuringReadError,
    StorageError,
    StorageTooLargeError,
    _AtomicDirectory,
    _is_network_path,
    _is_reparse_point,
    canonical_relative_path,
)


BACKUP_SCHEMA_VERSION: Final[str] = "rd_agent_backup_package_v1"
MANIFEST_MEMBER: Final[str] = "manifest.json"
DEFAULT_MAX_ENTRIES: Final[int] = 512
DEFAULT_MAX_TOTAL_SOURCE_BYTES: Final[int] = 512 * 1024 * 1024
DEFAULT_MAX_PACKAGE_BYTES: Final[int] = 512 * 1024 * 1024
_LOGICAL_TYPE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BackupPackageError(Exception):
    code = "BACKUP_PACKAGE_ERROR"


class BackupInvalidEntryError(BackupPackageError):
    code = "BACKUP_INVALID_ENTRY"


class BackupDuplicateArchivePathError(BackupPackageError):
    code = "BACKUP_DUPLICATE_ARCHIVE_PATH"


class BackupSourceChangedError(BackupPackageError):
    code = "BACKUP_SOURCE_CHANGED_DURING_READ"


class BackupTooLargeError(BackupPackageError):
    code = "BACKUP_TOO_LARGE"


class BackupWriteError(BackupPackageError):
    code = "BACKUP_WRITE_FAILED"


@dataclass(frozen=True)
class BackupEntry:
    logical_type: str
    source: Storage
    source_path: str
    archive_path: str
    expected_sha256: str | None = None
    expected_size: int | None = None


_SECRET_MARKERS = {
    "credential",
    "credentials",
    "secret",
    "secrets",
    "token",
    "tokens",
    "password",
    "passwd",
    "apikey",
}
_RUNTIME_MARKERS = {
    "runtime",
    "session",
    "sessions",
}
_LOG_MARKERS = {"log", "logs", "diagnostic", "diagnostics"}
_FIXED_SENSITIVE_NAMES = {
    ".env",
    "private_key",
    "private-key",
    "id_rsa",
    "id_ed25519",
    "client_secret",
    "client-secret",
    "service-account",
    "service_account",
    "api_key",
    "api-key",
    "apikey",
    "password",
    "passwd",
}


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_archive_path(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BackupInvalidEntryError("archive path is invalid")
    if unicodedata.normalize("NFC", value) != value:
        raise BackupInvalidEntryError("archive path is not canonically normalized")
    if "\x00" in value or any(ord(ch) < 32 for ch in value):
        raise BackupInvalidEntryError("archive path is invalid")
    if "\\" in value or ":" in value or value.startswith(("/", "//")):
        raise BackupInvalidEntryError("archive path is invalid")
    drive, _tail = ntpath.splitdrive(value)
    if drive or ntpath.isabs(value):
        raise BackupInvalidEntryError("archive path is invalid")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise BackupInvalidEntryError("archive path is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or pure.as_posix() != value:
        raise BackupInvalidEntryError("archive path is invalid")
    if value.casefold() == MANIFEST_MEMBER.casefold():
        raise BackupInvalidEntryError("archive path is reserved")
    return value


def _component_is_sensitive(component: str) -> bool:
    lowered = component.casefold()
    if lowered == ".env" or lowered.startswith(".env."):
        return True
    if lowered.endswith(".log"):
        return True
    for fixed_name in _FIXED_SENSITIVE_NAMES:
        if lowered == fixed_name or lowered.startswith(
            (f"{fixed_name}.", f"{fixed_name}-", f"{fixed_name}_")
        ):
            return True
    markers = {part for part in re.split(r"[._-]+", lowered) if part}
    return bool(markers & (_SECRET_MARKERS | _RUNTIME_MARKERS | _LOG_MARKERS))


def _logical_type_is_sensitive(logical_type: str) -> bool:
    lowered = logical_type.casefold()
    if lowered in _FIXED_SENSITIVE_NAMES:
        return True
    markers = {part for part in re.split(r"[._-]+", lowered) if part}
    return bool(markers & (_SECRET_MARKERS | _RUNTIME_MARKERS | _LOG_MARKERS))


def _reject_default_exclusions(source: Storage, source_path: str, archive_path: str) -> None:
    source_parts = tuple(part.casefold() for part in source_path.split("/"))
    archive_parts = tuple(part.casefold() for part in archive_path.split("/"))
    if ".git" in source_parts or ".git" in archive_parts or source.is_git_workspace_root():
        raise BackupInvalidEntryError("Git workspace content is excluded by default")
    if any(_component_is_sensitive(part) for part in source_parts + archive_parts):
        raise BackupInvalidEntryError("sensitive/runtime/log content is excluded by default")


def _canonical_created_at(value: str | None) -> str:
    if value is None:
        dt = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        if not isinstance(value, str) or not value:
            raise BackupInvalidEntryError("created_at is invalid")
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise BackupInvalidEntryError("created_at is invalid") from None
        if dt.tzinfo is None:
            raise BackupInvalidEntryError("created_at must include timezone")
        dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    return info


def _validate_entry(entry: BackupEntry) -> tuple[str, str]:
    if not isinstance(entry, BackupEntry):
        raise BackupInvalidEntryError("backup entries must be BackupEntry values")
    if not _LOGICAL_TYPE_RE.fullmatch(entry.logical_type):
        raise BackupInvalidEntryError("logical type is invalid")
    if _logical_type_is_sensitive(entry.logical_type):
        raise BackupInvalidEntryError("sensitive/runtime/log logical type is excluded by default")
    try:
        source_path = canonical_relative_path(entry.source_path)
    except StorageError as exc:
        raise BackupInvalidEntryError("source path is invalid") from exc
    archive_path = _canonical_archive_path(entry.archive_path)
    if entry.expected_sha256 is not None and not _SHA256_RE.fullmatch(entry.expected_sha256):
        raise BackupInvalidEntryError("expected SHA-256 is invalid")
    if entry.expected_size is not None and entry.expected_size < 0:
        raise BackupInvalidEntryError("expected size is invalid")
    _reject_default_exclusions(entry.source, source_path, archive_path)
    return source_path, archive_path


def _validated_output_path(output_path: str | os.PathLike[str]) -> Path:
    raw = os.fspath(output_path)
    if not isinstance(raw, str) or _is_network_path(raw):
        raise BackupWriteError("backup output must be on a local filesystem")
    output = Path(raw)
    if not output.is_absolute():
        output = output.absolute()
    if not output.name:
        raise BackupWriteError("backup output path is invalid")
    return output


def _source_root_path_identity(source: Storage) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(source.root_path)))


class _BoundedPackageWriter:
    """Seekable ZIP output guard that rejects growth past the package limit."""

    def __init__(self, raw_file, max_bytes: int) -> None:
        self._raw_file = raw_file
        self._max_bytes = int(max_bytes)
        self._size = 0

    @property
    def size(self) -> int:
        return self._size

    def write(self, data) -> int:
        position = int(self._raw_file.tell())
        prospective_size = max(self._size, position + len(data))
        if prospective_size > self._max_bytes:
            raise BackupTooLargeError("backup package exceeds the package limit")
        written = self._raw_file.write(data)
        if written is None:
            written = len(data)
        self._size = max(self._size, position + int(written))
        return int(written)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return int(self._raw_file.seek(offset, whence))

    def tell(self) -> int:
        return int(self._raw_file.tell())

    def flush(self) -> None:
        self._raw_file.flush()

    def fileno(self) -> int:
        return int(self._raw_file.fileno())

    def __getattr__(self, name: str):
        return getattr(self._raw_file, name)


def _collect_entries_bounded(
    entries: Iterable[BackupEntry], max_entries: int
) -> list[BackupEntry]:
    collected: list[BackupEntry] = []
    try:
        iterator = iter(entries)
    except TypeError:
        raise BackupInvalidEntryError("entries must be an explicit ordered iterable") from None

    for entry in iterator:
        if len(collected) >= max_entries:
            raise BackupTooLargeError("backup entry count exceeds the package limit")
        collected.append(entry)

    if not collected:
        raise BackupInvalidEntryError("backup package requires at least one entry")
    return collected


def build_backup_package(
    entries: Sequence[BackupEntry],
    output_path: str | os.PathLike[str],
    *,
    created_at: str | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_total_source_bytes: int = DEFAULT_MAX_TOTAL_SOURCE_BYTES,
    max_package_bytes: int = DEFAULT_MAX_PACKAGE_BYTES,
) -> dict[str, object]:
    """Build one versioned local ZIP package with an integrity-bound manifest.

    The input order is preserved exactly. Source bytes are read once through
    ``Storage``; the exact bytes hashed are the exact bytes written. The output
    temp file and final replace are bound to one validated local directory
    identity, so a post-validation path swap cannot redirect the mutation.
    """

    if max_entries <= 0 or max_total_source_bytes <= 0 or max_package_bytes <= 0:
        raise ValueError("backup bounds must be positive")
    if isinstance(entries, (str, bytes)):
        raise BackupInvalidEntryError("entries must be an explicit ordered sequence")
    entry_list = _collect_entries_bounded(entries, max_entries)

    canonical_entries: list[tuple[BackupEntry, str, str]] = []
    seen_archive_paths: set[str] = set()
    source_root_by_id: dict[str, str] = {}
    source_id_by_root: dict[str, str] = {}
    for entry in entry_list:
        source_path, archive_path = _validate_entry(entry)
        collision_key = archive_path.casefold()
        if collision_key in seen_archive_paths:
            raise BackupDuplicateArchivePathError("duplicate archive path")
        seen_archive_paths.add(collision_key)

        root_path_identity = _source_root_path_identity(entry.source)
        prior_root = source_root_by_id.get(entry.source.root_id)
        if prior_root is not None and prior_root != root_path_identity:
            raise BackupInvalidEntryError("source root identity is ambiguous")
        prior_id = source_id_by_root.get(root_path_identity)
        if prior_id is not None and prior_id != entry.source.root_id:
            raise BackupInvalidEntryError("source root identity is ambiguous")
        source_root_by_id[entry.source.root_id] = root_path_identity
        source_id_by_root[root_path_identity] = entry.source.root_id

        canonical_entries.append((entry, source_path, archive_path))

    output = _validated_output_path(output_path)
    created_at_value = _canonical_created_at(created_at)
    manifest_entries: list[dict[str, object]] = []
    total_source_bytes = 0
    temp_name: str | None = None
    fd = -1

    try:
        try:
            atomic_context = _AtomicDirectory(output.parent)
            atomic = atomic_context.__enter__()
        except StorageError as exc:
            raise BackupWriteError("backup output parent is not a plain local directory") from exc

        try:
            target_st = atomic.lstat(output.name)
            if target_st is not None and (
                _is_reparse_point(target_st) or not stat_module.S_ISREG(target_st.st_mode)
            ):
                raise BackupWriteError("backup output target is not a plain file")

            try:
                fd, temp_name = atomic.create_temp(f".{output.name}.tmp-")
            except StorageError as exc:
                raise BackupWriteError("backup temp file could not be created") from exc

            try:
                with os.fdopen(fd, "w+b", closefd=True) as raw_file:
                    fd = -1
                    package_file = _BoundedPackageWriter(raw_file, max_package_bytes)
                    with zipfile.ZipFile(
                        package_file,
                        mode="w",
                        compression=zipfile.ZIP_DEFLATED,
                    ) as archive:
                        for entry, source_path, archive_path in canonical_entries:
                            remaining_source_bytes = max_total_source_bytes - total_source_bytes
                            if remaining_source_bytes <= 0:
                                raise BackupTooLargeError(
                                    "backup source bytes exceed the package limit"
                                )
                            if (
                                entry.expected_size is not None
                                and entry.expected_size > remaining_source_bytes
                            ):
                                raise BackupTooLargeError(
                                    "backup source bytes exceed the package limit"
                                )
                            try:
                                data = entry.source.read_bytes(
                                    source_path,
                                    max_bytes=remaining_source_bytes,
                                )
                            except StorageTooLargeError as exc:
                                if remaining_source_bytes <= entry.source.max_read_bytes:
                                    raise BackupTooLargeError(
                                        "backup source bytes exceed the package limit"
                                    ) from exc
                                raise
                            except StorageChangedDuringReadError as exc:
                                raise BackupSourceChangedError("source changed during read") from exc
                            except StorageError:
                                raise

                            size = len(data)
                            digest = hashlib.sha256(data).hexdigest()
                            if entry.expected_size is not None and size != entry.expected_size:
                                raise BackupSourceChangedError("source size no longer matches expectation")
                            if entry.expected_sha256 is not None and digest != entry.expected_sha256:
                                raise BackupSourceChangedError("source hash no longer matches expectation")
                            if size > remaining_source_bytes:
                                raise BackupTooLargeError(
                                    "backup source bytes exceed the package limit"
                                )

                            total_source_bytes += size
                            archive.writestr(_zip_info(archive_path), data)
                            manifest_entries.append(
                                {
                                    "archive_path": archive_path,
                                    "logical_type": entry.logical_type,
                                    "source_root_id": entry.source.root_id,
                                    "source_path": source_path,
                                    "size": size,
                                    "sha256": digest,
                                }
                            )

                        manifest_payload: dict[str, object] = {
                            "schema_version": BACKUP_SCHEMA_VERSION,
                            "created_at": created_at_value,
                            "entries": manifest_entries,
                        }
                        manifest_sha256 = hashlib.sha256(
                            _canonical_json_bytes(manifest_payload)
                        ).hexdigest()
                        manifest: dict[str, object] = {
                            **manifest_payload,
                            "manifest_sha256": manifest_sha256,
                        }
                        archive.writestr(
                            _zip_info(MANIFEST_MEMBER),
                            _canonical_json_bytes(manifest),
                        )

                    package_file.flush()
                    os.fsync(package_file.fileno())
            except OSError as exc:
                raise BackupWriteError("backup temp file could not be written or synchronized") from exc

            target_now = atomic.lstat(output.name)
            if target_now is not None and (
                _is_reparse_point(target_now) or not stat_module.S_ISREG(target_now.st_mode)
            ):
                raise BackupWriteError("backup output target is not a plain file")

            try:
                atomic.replace(temp_name, output.name)
                temp_name = None
                atomic.fsync()
            except StorageError as exc:
                raise BackupWriteError("backup package could not be atomically replaced") from exc

            return manifest
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_name is not None:
                try:
                    atomic.unlink(temp_name)
                except StorageError:
                    pass
            atomic_context.__exit__(None, None, None)
    except BackupPackageError:
        raise
