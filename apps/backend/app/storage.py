"""Bounded local storage primitives for approved product-owned roots.

This module is intentionally filesystem-only. It does not discover roots, scan the
machine, access network locations, export secrets, or provide restore/delete flows.
Callers must construct ``Storage`` with one already-approved local root and use
canonical forward-slash relative file paths beneath that root.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import errno
import hashlib
import ntpath
import os
from pathlib import Path, PurePosixPath
import secrets
import stat as stat_module
import tempfile
from typing import Final


DEFAULT_MAX_READ_BYTES: Final[int] = 64 * 1024 * 1024
DEFAULT_MAX_WRITE_BYTES: Final[int] = 64 * 1024 * 1024
_REPARSE_POINT: Final[int] = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

_FILE_ATTRIBUTE_DIRECTORY: Final[int] = 0x10
_FILE_ATTRIBUTE_NORMAL: Final[int] = 0x80
_FILE_READ_DATA: Final[int] = 0x0001
_FILE_WRITE_DATA: Final[int] = 0x0002
_FILE_TRAVERSE: Final[int] = 0x0020
_FILE_READ_ATTRIBUTES: Final[int] = 0x0080
_DELETE: Final[int] = 0x00010000
_SYNCHRONIZE: Final[int] = 0x00100000
_FILE_SHARE_READ: Final[int] = 0x1
_FILE_SHARE_WRITE: Final[int] = 0x2
_FILE_SHARE_DELETE: Final[int] = 0x4
_OPEN_EXISTING: Final[int] = 3
_FILE_FLAG_BACKUP_SEMANTICS: Final[int] = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT: Final[int] = 0x00200000
_INVALID_HANDLE_VALUE: Final[int] = ctypes.c_void_p(-1).value

# NT create/open constants used only on Windows. RootDirectory makes each operation
# relative to an already-open directory identity rather than a caller-visible path.
_OBJ_CASE_INSENSITIVE: Final[int] = 0x00000040
_FILE_OPEN: Final[int] = 0x00000001
_FILE_CREATE: Final[int] = 0x00000002
_FILE_DIRECTORY_FILE: Final[int] = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT: Final[int] = 0x00000020
_FILE_NON_DIRECTORY_FILE: Final[int] = 0x00000040
_FILE_OPEN_REPARSE_POINT_NT: Final[int] = 0x00200000
_FILE_RENAME_INFORMATION_CLASS_NT: Final[int] = 10
_FILE_DISPOSITION_INFO_CLASS: Final[int] = 4
_DUPLICATE_SAME_ACCESS: Final[int] = 0x00000002
_STATUS_OBJECT_NAME_NOT_FOUND: Final[int] = 0xC0000034
_STATUS_OBJECT_PATH_NOT_FOUND: Final[int] = 0xC000003A
_STATUS_OBJECT_NAME_COLLISION: Final[int] = 0xC0000035
_STATUS_ACCESS_DENIED: Final[int] = 0xC0000022
_STATUS_SHARING_VIOLATION: Final[int] = 0xC0000043

_WINDOWS_RESERVED_STEMS: Final[frozenset[str]] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "conin$",
        "conout$",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)


class StorageError(Exception):
    """Base class carrying a stable, non-path-bearing error code."""

    code = "STORAGE_ERROR"


class StorageInvalidPathError(StorageError):
    code = "STORAGE_INVALID_PATH"


class StorageAccessError(StorageError):
    code = "STORAGE_ACCESS_DENIED"


class StorageTooLargeError(StorageError):
    code = "STORAGE_TOO_LARGE"


class StorageChangedDuringReadError(StorageError):
    code = "STORAGE_CHANGED_DURING_READ"


class StorageNotFoundError(StorageError):
    code = "STORAGE_NOT_FOUND"


@dataclass(frozen=True)
class StorageStat:
    """Stable metadata for one regular file without leaking its absolute path."""

    size: int
    mtime_ns: int
    device: int
    inode: int


def _raise_invalid() -> None:
    raise StorageInvalidPathError("path is outside the approved storage contract")


def _plain_component(value: object) -> str:
    """Validate one portable local path component without Windows alias semantics."""

    if not isinstance(value, str) or not value or value in (".", ".."):
        _raise_invalid()
    if "/" in value or "\\" in value or "\x00" in value or ":" in value:
        _raise_invalid()
    if any(ord(ch) < 32 for ch in value):
        _raise_invalid()
    if value.endswith((" ", ".")):
        _raise_invalid()
    stem = value.split(".", 1)[0].rstrip(" ").casefold()
    if stem in _WINDOWS_RESERVED_STEMS:
        _raise_invalid()
    return value


def canonical_relative_path(value: str | os.PathLike[str]) -> str:
    """Return a strict canonical relative path or fail closed."""

    raw = os.fspath(value)
    if not isinstance(raw, str):
        _raise_invalid()
    if not raw or raw != raw.strip() or "\x00" in raw:
        _raise_invalid()
    if any(ord(ch) < 32 for ch in raw):
        _raise_invalid()
    if "\\" in raw or ":" in raw:
        _raise_invalid()
    if raw.startswith("/") or raw.startswith("//"):
        _raise_invalid()
    drive, _tail = ntpath.splitdrive(raw)
    if drive or ntpath.isabs(raw):
        _raise_invalid()

    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _raise_invalid()
    for part in parts:
        _plain_component(part)

    pure = PurePosixPath(raw)
    canonical = pure.as_posix()
    if canonical != raw or pure.is_absolute():
        _raise_invalid()
    return canonical


def _is_network_path(raw_path: str) -> bool:
    if raw_path.startswith(("\\\\", "//")):
        return True
    if os.name != "nt":
        return False
    drive, _tail = ntpath.splitdrive(os.path.abspath(raw_path))
    if not drive or not drive.endswith(":"):
        return False
    try:
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\")
    except (AttributeError, OSError):
        return True
    return drive_type == 4  # DRIVE_REMOTE


def _is_reparse_point(st: os.stat_result) -> bool:
    attrs = int(getattr(st, "st_file_attributes", 0) or 0)
    return stat_module.S_ISLNK(st.st_mode) or bool(attrs & _REPARSE_POINT)


def _fingerprint(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(st.st_dev),
        int(st.st_ino),
        int(st.st_size),
        int(st.st_mtime_ns),
        int(stat_module.S_IFMT(st.st_mode)),
    )


def _translate_os_error(exc: OSError) -> StorageError:
    if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
        return StorageAccessError("approved file cannot be accessed")
    if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT:
        return StorageNotFoundError("approved file does not exist")
    return StorageAccessError("approved file operation failed")


def _read_fd_bounded(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(1024 * 1024, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise StorageTooLargeError("approved file exceeds the read limit")
        chunks.append(chunk)
    return b"".join(chunks)


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IO_STATUS_BLOCK_UNION(ctypes.Union):
    _fields_ = [("Status", wintypes.LONG), ("Pointer", ctypes.c_void_p)]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("u", _IO_STATUS_BLOCK_UNION), ("Information", ctypes.c_size_t)]


class _FILE_RENAME_FLAGS(ctypes.Union):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("Flags", wintypes.DWORD),
    ]


class _FILE_RENAME_INFORMATION(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("u", _FILE_RENAME_FLAGS),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
        ("FileName", wintypes.WCHAR * 1),
    ]


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOL)]


def _windows_kernel_functions():
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        raise StorageAccessError("approved filesystem object cannot be guarded") from None

    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE

    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    get_info.restype = wintypes.BOOL

    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    set_info.restype = wintypes.BOOL

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    duplicate_handle = kernel32.DuplicateHandle
    duplicate_handle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    duplicate_handle.restype = wintypes.BOOL

    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    return create_file, get_info, set_info, close_handle, duplicate_handle, get_current_process


def _windows_nt_create_file():
    try:
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    except (AttributeError, OSError):
        raise StorageAccessError("approved filesystem object cannot be opened by identity") from None
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(_OBJECT_ATTRIBUTES),
        ctypes.POINTER(_IO_STATUS_BLOCK),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    ]
    nt_create_file.restype = wintypes.LONG
    return nt_create_file


def _windows_nt_set_information_file():
    try:
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    except (AttributeError, OSError):
        raise StorageAccessError("approved filesystem object cannot be renamed by identity") from None
    nt_set_information_file = ntdll.NtSetInformationFile
    nt_set_information_file.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IO_STATUS_BLOCK),
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    nt_set_information_file.restype = wintypes.LONG
    return nt_set_information_file


def _ntstatus_code(status: int) -> int:
    return int(ctypes.c_ulong(status).value & 0xFFFFFFFF)


def _windows_ntstatus_to_dos_error(status: int) -> int:
    try:
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        convert = ntdll.RtlNtStatusToDosError
        convert.argtypes = [wintypes.LONG]
        convert.restype = wintypes.ULONG
        return int(convert(wintypes.LONG(status)))
    except (AttributeError, OSError):
        return 0


def _raise_ntstatus(status: int, *, collision_is_exists: bool = False) -> None:
    code = _ntstatus_code(status)
    if collision_is_exists and code == _STATUS_OBJECT_NAME_COLLISION:
        raise FileExistsError("approved relative name already exists")
    if code in (_STATUS_OBJECT_NAME_NOT_FOUND, _STATUS_OBJECT_PATH_NOT_FOUND):
        raise StorageNotFoundError("approved filesystem object does not exist")
    if code in (_STATUS_ACCESS_DENIED, _STATUS_SHARING_VIOLATION):
        raise StorageAccessError("approved filesystem object cannot be accessed")
    raise StorageAccessError("approved filesystem identity operation failed")


def _raise_rename_ntstatus(status: int) -> None:
    code = _ntstatus_code(status)
    winerror = _windows_ntstatus_to_dos_error(status)
    diagnostic = f"ntstatus=0x{code:08X}, winerror={winerror}"
    if code in (_STATUS_OBJECT_NAME_NOT_FOUND, _STATUS_OBJECT_PATH_NOT_FOUND):
        raise StorageNotFoundError(
            f"approved file replacement target disappeared ({diagnostic})"
        )
    if code in (_STATUS_ACCESS_DENIED, _STATUS_SHARING_VIOLATION):
        raise StorageAccessError(f"approved file replacement was denied ({diagnostic})")
    raise StorageAccessError(
        f"approved file could not be atomically replaced ({diagnostic})"
    )


def _windows_handle_value(handle: object) -> int:
    value = getattr(handle, "value", handle)
    if value is None:
        return 0
    return int(value)


def _windows_close_handle(handle: object) -> None:
    _create_file, _get_info, _set_info, close_handle, _dup, _proc = _windows_kernel_functions()
    close_handle(wintypes.HANDLE(_windows_handle_value(handle)))


def _windows_handle_info(handle: object) -> _BY_HANDLE_FILE_INFORMATION:
    _create_file, get_info, _set_info, _close, _dup, _proc = _windows_kernel_functions()
    info = _BY_HANDLE_FILE_INFORMATION()
    if not get_info(wintypes.HANDLE(_windows_handle_value(handle)), ctypes.byref(info)):
        raise StorageAccessError("approved filesystem object cannot be inspected")
    return info


def _windows_duplicate_handle(handle: object) -> int:
    _create_file, _get_info, _set_info, _close, duplicate_handle, get_current_process = (
        _windows_kernel_functions()
    )
    process = get_current_process()
    duplicate = wintypes.HANDLE()
    if not duplicate_handle(
        process,
        wintypes.HANDLE(_windows_handle_value(handle)),
        process,
        ctypes.byref(duplicate),
        0,
        False,
        _DUPLICATE_SAME_ACCESS,
    ):
        raise StorageAccessError("approved filesystem handle cannot be duplicated")
    return _windows_handle_value(duplicate)


def _windows_handle_to_fd(handle: object, flags: int) -> int:
    try:
        import msvcrt

        return msvcrt.open_osfhandle(_windows_handle_value(handle), flags)
    except (ImportError, OSError, ValueError):
        _windows_close_handle(handle)
        raise StorageAccessError("approved filesystem handle cannot be used") from None


def _windows_open_relative_handle(
    directory_handle: object,
    name: str,
    *,
    desired_access: int,
    share_access: int,
    create_disposition: int,
    create_options: int,
    collision_is_exists: bool = False,
) -> int:
    """Open/create one plain relative name beneath a bound Windows directory handle."""

    name = _plain_component(name)
    encoded = name.encode("utf-16-le", errors="strict")
    if len(encoded) > 0xFFFE:
        _raise_invalid()
    buffer = ctypes.create_unicode_buffer(name)
    unicode_name = _UNICODE_STRING(
        Length=len(encoded),
        MaximumLength=len(encoded) + 2,
        Buffer=ctypes.cast(buffer, wintypes.LPWSTR),
    )
    attributes = _OBJECT_ATTRIBUTES(
        Length=ctypes.sizeof(_OBJECT_ATTRIBUTES),
        RootDirectory=wintypes.HANDLE(_windows_handle_value(directory_handle)),
        ObjectName=ctypes.pointer(unicode_name),
        Attributes=_OBJ_CASE_INSENSITIVE,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    iosb = _IO_STATUS_BLOCK()
    result = wintypes.HANDLE()
    status = _windows_nt_create_file()(
        ctypes.byref(result),
        desired_access,
        ctypes.byref(attributes),
        ctypes.byref(iosb),
        None,
        _FILE_ATTRIBUTE_NORMAL,
        share_access,
        create_disposition,
        create_options,
        None,
        0,
    )
    if int(status) < 0:
        _raise_ntstatus(status, collision_is_exists=collision_is_exists)
    return _windows_handle_value(result)


def _windows_rename_handle_relative(
    file_handle: object,
    directory_handle: object,
    target_name: str,
) -> None:
    """Atomically rename/replace a file inside its bound Windows directory identity."""

    target_name = _plain_component(target_name)
    if not _windows_handle_value(file_handle) or not _windows_handle_value(directory_handle):
        raise StorageAccessError("approved file replacement identity is not available")

    encoded = target_name.encode("utf-16-le", errors="strict")
    structure_size = ctypes.sizeof(_FILE_RENAME_INFORMATION)
    if len(encoded) > 0xFFFFFFFF - structure_size:
        _raise_invalid()

    # This is always a same-directory temp -> target rename. The native
    # FileRenameInformation contract requires a simple filename with RootDirectory
    # NULL for that case. The source handle was itself created relative to the bound
    # directory handle and is retained until this call, so a caller-visible parent
    # swap cannot redirect the rename to a different directory identity.
    name_offset = _FILE_RENAME_INFORMATION.FileName.offset
    total = structure_size + len(encoded)
    raw = ctypes.create_string_buffer(total)
    info = ctypes.cast(raw, ctypes.POINTER(_FILE_RENAME_INFORMATION)).contents
    info.ReplaceIfExists = 1
    info.RootDirectory = None
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(raw) + name_offset, encoded, len(encoded))

    iosb = _IO_STATUS_BLOCK()
    status = _windows_nt_set_information_file()(
        wintypes.HANDLE(_windows_handle_value(file_handle)),
        ctypes.byref(iosb),
        raw,
        total,
        _FILE_RENAME_INFORMATION_CLASS_NT,
    )
    if int(status) < 0:
        _raise_rename_ntstatus(status)


def _windows_delete_handle(file_handle: object) -> None:
    disposition = _FILE_DISPOSITION_INFO(DeleteFile=True)
    _create_file, _get_info, set_info, _close, _dup, _proc = _windows_kernel_functions()
    if not set_info(
        wintypes.HANDLE(_windows_handle_value(file_handle)),
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise StorageAccessError("approved temp file could not be removed")


def _open_windows_plain_directory_chain(directory: Path) -> tuple[list[int], object]:
    """Resolve a plain absolute directory by handle-relative identity on Windows."""

    create_file, get_info, _set_info, close_handle, _dup, _proc = _windows_kernel_functions()
    absolute = directory.absolute()
    parts = absolute.parts
    if not parts:
        _raise_invalid()

    anchor = Path(parts[0])
    handle = create_file(
        str(anchor),
        _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if _windows_handle_value(handle) == _INVALID_HANDLE_VALUE:
        raise StorageAccessError("approved directory anchor cannot be opened")
    current = _windows_handle_value(handle)

    try:
        info = _BY_HANDLE_FILE_INFORMATION()
        if not get_info(wintypes.HANDLE(current), ctypes.byref(info)):
            raise StorageAccessError("approved directory anchor cannot be inspected")
        if not (int(info.dwFileAttributes) & _FILE_ATTRIBUTE_DIRECTORY) or (
            int(info.dwFileAttributes) & _REPARSE_POINT
        ):
            _raise_invalid()

        for part in parts[1:]:
            _plain_component(part)
            next_handle = _windows_open_relative_handle(
                current,
                part,
                desired_access=_FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                create_disposition=_FILE_OPEN,
                create_options=(
                    _FILE_DIRECTORY_FILE
                    | _FILE_SYNCHRONOUS_IO_NONALERT
                    | _FILE_OPEN_REPARSE_POINT_NT
                ),
            )
            try:
                next_info = _windows_handle_info(next_handle)
                attrs = int(next_info.dwFileAttributes)
                if not (attrs & _FILE_ATTRIBUTE_DIRECTORY) or (attrs & _REPARSE_POINT):
                    _raise_invalid()
            except Exception:
                _windows_close_handle(next_handle)
                raise
            close_handle(wintypes.HANDLE(current))
            current = next_handle
        return [current], close_handle
    except Exception:
        close_handle(wintypes.HANDLE(current))
        raise


def _open_posix_plain_directory(directory: Path) -> int:
    """Walk an absolute directory with openat/O_NOFOLLOW and return its bound fd."""

    absolute = directory.absolute()
    parts = absolute.parts
    if not parts:
        _raise_invalid()

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    fd = -1
    try:
        fd = os.open(parts[0], flags)
        for part in parts[1:]:
            _plain_component(part)
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (getattr(errno, "ELOOP", -1), getattr(errno, "ENOTDIR", -1)):
                    _raise_invalid()
                raise
            os.close(fd)
            fd = next_fd
        st = os.fstat(fd)
        if not stat_module.S_ISDIR(st.st_mode):
            _raise_invalid()
        return fd
    except StorageError:
        if fd >= 0:
            os.close(fd)
        raise
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        raise _translate_os_error(exc) from None


class _AtomicDirectory:
    """Directory-identity-bound capability for fail-closed local file operations."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        raw = os.fspath(directory)
        if not isinstance(raw, str) or _is_network_path(raw):
            raise StorageInvalidPathError("approved directory must be local")
        path = Path(raw)
        if not path.is_absolute():
            path = path.absolute()
        for part in path.parts[1:]:
            _plain_component(part)
        self.path = path
        self._posix_fd = -1
        self._windows_handles: list[int] = []
        self._close_windows = None
        self._windows_temp_handles: dict[str, int] = {}

    def __enter__(self) -> "_AtomicDirectory":
        if os.name == "nt":
            handles, close_handle = _open_windows_plain_directory_chain(self.path)
            self._windows_handles = handles
            self._close_windows = close_handle
        else:
            self._posix_fd = _open_posix_plain_directory(self.path)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._posix_fd >= 0:
            os.close(self._posix_fd)
            self._posix_fd = -1
        if self._close_windows is not None:
            for name, handle in tuple(self._windows_temp_handles.items()):
                try:
                    _windows_delete_handle(handle)
                except StorageError:
                    pass
                self._close_windows(wintypes.HANDLE(handle))
                self._windows_temp_handles.pop(name, None)
            for handle in reversed(self._windows_handles):
                self._close_windows(wintypes.HANDLE(handle))
            self._windows_handles = []
            self._close_windows = None

    def _validate_name(self, name: str) -> str:
        return _plain_component(name)

    def _windows_directory(self) -> int:
        if not self._windows_handles:
            raise StorageAccessError("approved directory identity is not open")
        return self._windows_handles[-1]

    def lstat(self, name: str) -> os.stat_result | None:
        name = self._validate_name(name)
        if os.name == "nt":
            try:
                handle = _windows_open_relative_handle(
                    self._windows_directory(),
                    name,
                    desired_access=_FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                    share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                    create_disposition=_FILE_OPEN,
                    create_options=_FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT_NT,
                )
            except StorageNotFoundError:
                return None
            info = _windows_handle_info(handle)
            if int(info.dwFileAttributes) & _REPARSE_POINT:
                _windows_close_handle(handle)
                _raise_invalid()
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            fd = _windows_handle_to_fd(handle, flags)
            try:
                return os.fstat(fd)
            except OSError as exc:
                raise _translate_os_error(exc) from None
            finally:
                os.close(fd)

        try:
            return os.stat(name, dir_fd=self._posix_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _translate_os_error(exc) from None

    def open_read(self, name: str) -> int:
        """Open one final regular file relative to the validated directory identity."""

        name = self._validate_name(name)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)

        if os.name == "nt":
            handle = _windows_open_relative_handle(
                self._windows_directory(),
                name,
                desired_access=_FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                create_disposition=_FILE_OPEN,
                create_options=(
                    _FILE_NON_DIRECTORY_FILE
                    | _FILE_SYNCHRONOUS_IO_NONALERT
                    | _FILE_OPEN_REPARSE_POINT_NT
                ),
            )
            info = _windows_handle_info(handle)
            attrs = int(info.dwFileAttributes)
            if (attrs & _FILE_ATTRIBUTE_DIRECTORY) or (attrs & _REPARSE_POINT):
                _windows_close_handle(handle)
                _raise_invalid()
            return _windows_handle_to_fd(handle, flags)

        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            return os.open(name, flags, dir_fd=self._posix_fd)
        except OSError as exc:
            if exc.errno in (getattr(errno, "ELOOP", -1), getattr(errno, "ENOTDIR", -1)):
                _raise_invalid()
            raise _translate_os_error(exc) from None

    def create_temp(self, prefix: str) -> tuple[int, str]:
        if not isinstance(prefix, str) or not prefix or "/" in prefix or "\\" in prefix:
            _raise_invalid()
        if ":" in prefix or "\x00" in prefix or any(ord(ch) < 32 for ch in prefix):
            _raise_invalid()

        if os.name == "nt":
            for _attempt in range(128):
                name = f"{prefix}{secrets.token_hex(12)}"
                _plain_component(name)
                try:
                    handle = _windows_open_relative_handle(
                        self._windows_directory(),
                        name,
                        desired_access=(
                            _FILE_READ_DATA
                            | _FILE_WRITE_DATA
                            | _FILE_READ_ATTRIBUTES
                            | _DELETE
                            | _SYNCHRONIZE
                        ),
                        share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                        create_disposition=_FILE_CREATE,
                        create_options=_FILE_NON_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT,
                        collision_is_exists=True,
                    )
                except FileExistsError:
                    continue
                info = _windows_handle_info(handle)
                attrs = int(info.dwFileAttributes)
                if (attrs & _FILE_ATTRIBUTE_DIRECTORY) or (attrs & _REPARSE_POINT):
                    _windows_close_handle(handle)
                    _raise_invalid()
                duplicate = _windows_duplicate_handle(handle)
                self._windows_temp_handles[name] = handle
                fd_flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
                try:
                    fd = _windows_handle_to_fd(duplicate, fd_flags)
                except Exception:
                    self._windows_temp_handles.pop(name, None)
                    _windows_close_handle(handle)
                    raise
                return fd, name
            raise StorageAccessError("approved temp file could not be created")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        for _attempt in range(128):
            name = f"{prefix}{secrets.token_hex(12)}"
            try:
                fd = os.open(name, flags, 0o600, dir_fd=self._posix_fd)
                return fd, name
            except FileExistsError:
                continue
            except OSError as exc:
                raise _translate_os_error(exc) from None
        raise StorageAccessError("approved temp file could not be created")

    def replace(self, temp_name: str, target_name: str) -> None:
        temp_name = self._validate_name(temp_name)
        target_name = self._validate_name(target_name)
        if os.name == "nt":
            handle = self._windows_temp_handles.get(temp_name)
            if handle is None:
                raise StorageAccessError("approved temp file identity is not available")
            _windows_rename_handle_relative(handle, self._windows_directory(), target_name)
            if self._close_windows is not None:
                self._close_windows(wintypes.HANDLE(handle))
            self._windows_temp_handles.pop(temp_name, None)
            return

        try:
            os.replace(
                temp_name,
                target_name,
                src_dir_fd=self._posix_fd,
                dst_dir_fd=self._posix_fd,
            )
        except OSError as exc:
            raise _translate_os_error(exc) from None

    def unlink(self, name: str) -> None:
        name = self._validate_name(name)
        if os.name == "nt":
            handle = self._windows_temp_handles.get(name)
            opened_here = False
            if handle is None:
                try:
                    handle = _windows_open_relative_handle(
                        self._windows_directory(),
                        name,
                        desired_access=_DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                        share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                        create_disposition=_FILE_OPEN,
                        create_options=(
                            _FILE_NON_DIRECTORY_FILE
                            | _FILE_SYNCHRONOUS_IO_NONALERT
                            | _FILE_OPEN_REPARSE_POINT_NT
                        ),
                    )
                except StorageNotFoundError:
                    return
                opened_here = True
            try:
                _windows_delete_handle(handle)
            finally:
                if self._close_windows is not None:
                    self._close_windows(wintypes.HANDLE(handle))
                if not opened_here:
                    self._windows_temp_handles.pop(name, None)
            return

        try:
            os.unlink(name, dir_fd=self._posix_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise _translate_os_error(exc) from None

    def fsync(self) -> None:
        if os.name == "nt":
            return
        try:
            os.fsync(self._posix_fd)
        except OSError:
            pass


class Storage:
    """A bounded filesystem capability rooted at one approved local directory."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        root_id: str,
        max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
        max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES,
    ) -> None:
        if not isinstance(root_id, str) or not root_id or root_id != root_id.strip():
            raise StorageInvalidPathError("storage root identity is invalid")
        if len(root_id) > 128 or any(ord(ch) < 32 for ch in root_id):
            raise StorageInvalidPathError("storage root identity is invalid")
        if max_read_bytes <= 0 or max_write_bytes <= 0:
            raise ValueError("storage size limits must be positive")

        raw_root = os.fspath(root)
        if not isinstance(raw_root, str) or _is_network_path(raw_root):
            raise StorageInvalidPathError("storage root must be a local path")
        root_path = Path(raw_root)
        if not root_path.is_absolute():
            raise StorageInvalidPathError("storage root must be absolute")
        root_path = root_path.absolute()

        # Validate the complete root chain with the same identity-bound primitive used
        # by operations; do not resolve/follow a caller-visible symlink after validation.
        try:
            with _AtomicDirectory(root_path):
                pass
        except StorageError:
            raise
        except OSError as exc:
            raise _translate_os_error(exc) from None

        self._root = root_path
        self.root_id = root_id
        self.max_read_bytes = int(max_read_bytes)
        self.max_write_bytes = int(max_write_bytes)

    @property
    def root_path(self) -> Path:
        return self._root

    def is_git_workspace_root(self) -> bool:
        """Return True when this approved root is at or below a Git worktree marker."""

        current = self._root
        while True:
            marker = current / ".git"
            try:
                os.lstat(marker)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise _translate_os_error(exc) from None
            else:
                return True
            parent = current.parent
            if parent == current:
                return False
            current = parent

    def _candidate(self, relative_path: str | os.PathLike[str]) -> tuple[str, Path]:
        canonical = canonical_relative_path(relative_path)
        candidate = self._root.joinpath(*canonical.split("/"))
        try:
            candidate.relative_to(self._root)
        except ValueError:
            _raise_invalid()
        return canonical, candidate

    def exists(self, relative_path: str | os.PathLike[str]) -> bool:
        _canonical, candidate = self._candidate(relative_path)
        try:
            with _AtomicDirectory(candidate.parent) as bound:
                st = bound.lstat(candidate.name)
        except StorageNotFoundError:
            return False
        if st is None:
            return False
        if _is_reparse_point(st) or not stat_module.S_ISREG(st.st_mode):
            _raise_invalid()
        return True

    def stat(self, relative_path: str | os.PathLike[str]) -> StorageStat:
        _canonical, candidate = self._candidate(relative_path)
        with _AtomicDirectory(candidate.parent) as bound:
            st = bound.lstat(candidate.name)
            if st is None:
                raise StorageNotFoundError("approved file does not exist")
            if _is_reparse_point(st) or not stat_module.S_ISREG(st.st_mode):
                _raise_invalid()
            return StorageStat(
                size=int(st.st_size),
                mtime_ns=int(st.st_mtime_ns),
                device=int(st.st_dev),
                inode=int(st.st_ino),
            )

    def read_bytes(
        self,
        relative_path: str | os.PathLike[str],
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        _canonical, candidate = self._candidate(relative_path)
        limit = self.max_read_bytes if max_bytes is None else int(max_bytes)
        if limit <= 0:
            raise ValueError("read limit must be positive")
        limit = min(limit, self.max_read_bytes)

        with _AtomicDirectory(candidate.parent) as bound:
            pre = bound.lstat(candidate.name)
            if pre is None:
                raise StorageNotFoundError("approved file does not exist")
            if _is_reparse_point(pre) or not stat_module.S_ISREG(pre.st_mode):
                _raise_invalid()
            if pre.st_size > limit:
                raise StorageTooLargeError("approved file exceeds the read limit")

            fd = bound.open_read(candidate.name)
            try:
                before_fd = os.fstat(fd)
                if _fingerprint(pre) != _fingerprint(before_fd):
                    raise StorageChangedDuringReadError("approved file changed before read")
                if not stat_module.S_ISREG(before_fd.st_mode):
                    _raise_invalid()

                data = _read_fd_bounded(fd, limit)
                after_fd = os.fstat(fd)
            except StorageError:
                raise
            except OSError as exc:
                raise _translate_os_error(exc) from None
            finally:
                os.close(fd)

            post = bound.lstat(candidate.name)
            if post is None:
                raise StorageChangedDuringReadError("approved file changed during read")
            fingerprints = {
                _fingerprint(pre),
                _fingerprint(before_fd),
                _fingerprint(after_fd),
                _fingerprint(post),
            }
            if len(fingerprints) != 1 or _is_reparse_point(post):
                raise StorageChangedDuringReadError("approved file changed during read")
            return data

    def sha256(self, relative_path: str | os.PathLike[str], *, max_bytes: int | None = None) -> str:
        return hashlib.sha256(self.read_bytes(relative_path, max_bytes=max_bytes)).hexdigest()

    def atomic_write_bytes(
        self,
        relative_path: str | os.PathLike[str],
        data: bytes,
        *,
        max_bytes: int | None = None,
    ) -> StorageStat:
        if not isinstance(data, bytes):
            raise TypeError("atomic_write_bytes requires bytes")
        _canonical, candidate = self._candidate(relative_path)
        limit = self.max_write_bytes if max_bytes is None else int(max_bytes)
        if limit <= 0:
            raise ValueError("write limit must be positive")
        limit = min(limit, self.max_write_bytes)
        if len(data) > limit:
            raise StorageTooLargeError("approved write exceeds the write limit")

        temp_name: str | None = None
        fd = -1
        with _AtomicDirectory(candidate.parent) as atomic:
            existing = atomic.lstat(candidate.name)
            if existing is not None and (
                _is_reparse_point(existing) or not stat_module.S_ISREG(existing.st_mode)
            ):
                _raise_invalid()

            try:
                fd, temp_name = atomic.create_temp(f".{candidate.name}.tmp-")
                with os.fdopen(fd, "wb", closefd=True) as handle:
                    fd = -1
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())

                existing_now = atomic.lstat(candidate.name)
                if existing_now is not None and (
                    _is_reparse_point(existing_now) or not stat_module.S_ISREG(existing_now.st_mode)
                ):
                    _raise_invalid()

                atomic.replace(temp_name, candidate.name)
                temp_name = None
                atomic.fsync()
                replaced = atomic.lstat(candidate.name)
                if replaced is None or _is_reparse_point(replaced) or not stat_module.S_ISREG(replaced.st_mode):
                    raise StorageAccessError("approved file replacement could not be verified")
                result = StorageStat(
                    size=int(replaced.st_size),
                    mtime_ns=int(replaced.st_mtime_ns),
                    device=int(replaced.st_dev),
                    inode=int(replaced.st_ino),
                )
            finally:
                if fd >= 0:
                    os.close(fd)
                if temp_name is not None:
                    try:
                        atomic.unlink(temp_name)
                    except StorageError:
                        pass

        return result


class TempStorage(Storage):
    """Temporary approved-root Storage for tests and isolated local verification."""

    def __init__(
        self,
        *,
        root_id: str = "temp-storage",
        max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
        max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES,
    ) -> None:
        self._temp_directory = tempfile.TemporaryDirectory(prefix="rd-agent-storage-")
        super().__init__(
            Path(self._temp_directory.name),
            root_id=root_id,
            max_read_bytes=max_read_bytes,
            max_write_bytes=max_write_bytes,
        )

    def cleanup(self) -> None:
        self._temp_directory.cleanup()

    def __enter__(self) -> "TempStorage":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()