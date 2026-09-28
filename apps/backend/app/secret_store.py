"""Bounded secret-storage contract and in-memory test implementation.

This module deliberately stores only opaque references in caller-visible state. Secret
values must never be placed in logs, exception messages, SQLite, temporary files, or
artifacts by this layer.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

MAX_SECRET_REF_LENGTH = 64
MAX_SECRET_BYTES = 2048
# Windows Credential Manager target matching is case-insensitive. Requiring one
# canonical lowercase spelling keeps logical SecretStore ref identity injective and
# identical across the in-memory and Windows production implementations.
_SECRET_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", re.ASCII)


class SecretStoreError(Exception):
    """Base class for stable, non-secret SecretStore failures."""

    code = "SECRET_STORE_ERROR"
    message = "secret store operation failed"

    def __init__(self) -> None:
        super().__init__(self.message)


class InvalidSecretReferenceError(SecretStoreError):
    code = "SECRET_REFERENCE_INVALID"
    message = "secret reference is invalid"


class InvalidSecretValueError(SecretStoreError):
    code = "SECRET_VALUE_INVALID"
    message = "secret value is invalid"


class SecretNotFoundError(SecretStoreError):
    code = "SECRET_NOT_FOUND"
    message = "secret was not found"


class SecretAccessDeniedError(SecretStoreError):
    code = "SECRET_ACCESS_DENIED"
    message = "secret store access was denied"


class SecretStoreOSError(SecretStoreError):
    code = "SECRET_STORE_OS_FAILURE"
    message = "secret store operating-system operation failed"


class UnsupportedSecretStorePlatformError(SecretStoreError):
    code = "SECRET_STORE_UNSUPPORTED_PLATFORM"
    message = "secret store is unsupported on this platform"


def validate_secret_ref(ref: str) -> str:
    """Validate and return one canonical lowercase machine-safe reference."""

    if not isinstance(ref, str) or not _SECRET_REF_RE.fullmatch(ref):
        raise InvalidSecretReferenceError()
    if len(ref) > MAX_SECRET_REF_LENGTH:
        raise InvalidSecretReferenceError()
    return ref


def validate_secret_value(secret: str) -> str:
    """Validate a bounded text secret without exposing it in failures."""

    if not isinstance(secret, str) or not secret or "\x00" in secret:
        raise InvalidSecretValueError()
    try:
        encoded = secret.encode("utf-16-le", errors="strict")
    except UnicodeError:
        raise InvalidSecretValueError() from None
    if len(encoded) > MAX_SECRET_BYTES:
        raise InvalidSecretValueError()
    return secret


@runtime_checkable
class SecretStore(Protocol):
    """Port used by product code that needs bounded secret persistence."""

    def put(self, ref: str, secret: str) -> None:
        """Save or replace one secret under an opaque reference."""

    def get(self, ref: str) -> str:
        """Return the secret for ``ref`` or raise ``SecretNotFoundError``."""

    def delete(self, ref: str) -> None:
        """Delete exactly ``ref`` or raise ``SecretNotFoundError``."""


class InMemorySecretStore:
    """Process-local SecretStore implementation for tests and bounded injection."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def put(self, ref: str, secret: str) -> None:
        ref = validate_secret_ref(ref)
        secret = validate_secret_value(secret)
        self._values[ref] = secret

    def get(self, ref: str) -> str:
        ref = validate_secret_ref(ref)
        try:
            return self._values[ref]
        except KeyError:
            raise SecretNotFoundError() from None

    def delete(self, ref: str) -> None:
        ref = validate_secret_ref(ref)
        try:
            del self._values[ref]
        except KeyError:
            raise SecretNotFoundError() from None
