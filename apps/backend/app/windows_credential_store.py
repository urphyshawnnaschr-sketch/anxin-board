"""Windows Credential Manager adapter for the bounded SecretStore port.

The production adapter uses only Generic Credentials in the fixed ``AnxinBoard/``
namespace. There is intentionally no enumeration API and callers cannot select native
targets, persistence scope, usernames, attributes, or WinAPI flags.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
from typing import Protocol

from app.secret_store import (
    MAX_SECRET_BYTES,
    InvalidSecretValueError,
    SecretAccessDeniedError,
    SecretNotFoundError,
    SecretStoreOSError,
    UnsupportedSecretStorePlatformError,
    validate_secret_ref,
    validate_secret_value,
)

_TARGET_PREFIX = "AnxinBoard/"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_ACCESS_DENIED = 5
_ERROR_NOT_FOUND = 1168


class _NativeCredentialApi(Protocol):
    def write(self, *, target: str, secret_blob: bytes) -> None: ...

    def read(self, *, target: str) -> bytes: ...

    def delete(self, *, target: str) -> None: ...


def _target_for_ref(ref: str) -> str:
    return f"{_TARGET_PREFIX}{validate_secret_ref(ref)}"


class WindowsCredentialStore:
    """SecretStore backed by the current Windows user's Credential Manager."""

    def __init__(self) -> None:
        self._native = _load_native_api()

    def put(self, ref: str, secret: str) -> None:
        target = _target_for_ref(ref)
        secret = validate_secret_value(secret)
        try:
            blob = secret.encode("utf-16-le", errors="strict")
        except UnicodeError:
            raise InvalidSecretValueError() from None
        self._native.write(target=target, secret_blob=blob)

    def get(self, ref: str) -> str:
        target = _target_for_ref(ref)
        blob = self._native.read(target=target)
        if (
            type(blob) is not bytes
            or not blob
            or len(blob) > MAX_SECRET_BYTES
            or len(blob) % 2 != 0
        ):
            raise SecretStoreOSError()
        try:
            secret = blob.decode("utf-16-le", errors="strict")
        except UnicodeError:
            raise SecretStoreOSError() from None
        try:
            validate_secret_value(secret)
        except InvalidSecretValueError:
            raise SecretStoreOSError() from None
        return secret

    def delete(self, ref: str) -> None:
        target = _target_for_ref(ref)
        self._native.delete(target=target)


class _CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTEW)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)


class _CtypesCredentialApi:
    """Minimal non-enumerating Advapi32 boundary."""

    def __init__(self) -> None:
        try:
            advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        except (AttributeError, OSError):
            raise UnsupportedSecretStorePlatformError() from None

        self._cred_write = advapi32.CredWriteW
        self._cred_write.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._cred_write.restype = wintypes.BOOL

        self._cred_read = advapi32.CredReadW
        self._cred_read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_PCREDENTIALW),
        ]
        self._cred_read.restype = wintypes.BOOL

        self._cred_delete = advapi32.CredDeleteW
        self._cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._cred_delete.restype = wintypes.BOOL

        self._cred_free = advapi32.CredFree
        self._cred_free.argtypes = [ctypes.c_void_p]
        self._cred_free.restype = None

    def write(self, *, target: str, secret_blob: bytes) -> None:
        buffer = (ctypes.c_ubyte * len(secret_blob)).from_buffer_copy(secret_blob)
        credential = _CREDENTIALW()
        credential.Flags = 0
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.Comment = None
        credential.CredentialBlobSize = len(secret_blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.AttributeCount = 0
        credential.Attributes = None
        credential.TargetAlias = None
        credential.UserName = None
        if not self._cred_write(ctypes.byref(credential), 0):
            _raise_native_failure(ctypes.get_last_error(), not_found=False)

    def read(self, *, target: str) -> bytes:
        credential_ptr = _PCREDENTIALW()
        if not self._cred_read(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(credential_ptr)):
            _raise_native_failure(ctypes.get_last_error(), not_found=True)
        try:
            credential = credential_ptr.contents
            if credential.CredentialBlobSize == 0 or not credential.CredentialBlob:
                raise SecretStoreOSError()
            if credential.CredentialBlobSize > MAX_SECRET_BYTES:
                raise SecretStoreOSError()
            return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        finally:
            self._cred_free(credential_ptr)

    def delete(self, *, target: str) -> None:
        if not self._cred_delete(target, _CRED_TYPE_GENERIC, 0):
            _raise_native_failure(ctypes.get_last_error(), not_found=True)


def _raise_native_failure(error_code: int, *, not_found: bool) -> None:
    if not_found and error_code == _ERROR_NOT_FOUND:
        raise SecretNotFoundError()
    if error_code == _ERROR_ACCESS_DENIED:
        raise SecretAccessDeniedError()
    raise SecretStoreOSError()


def _load_native_api() -> _NativeCredentialApi:
    if sys.platform != "win32":
        raise UnsupportedSecretStorePlatformError()
    return _CtypesCredentialApi()
