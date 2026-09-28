"""One in-memory credential source for report authority and report transport.

Packaged desktop uses the same opaque reference as Settings. Unpackaged CLI
retains its explicit environment credential contract. Never copy store values
into environment, persistence, diagnostics or a fallback credential source.
"""
from __future__ import annotations

import os
import sys

from app.secret_store import SecretStoreError, validate_secret_value
from app.windows_credential_store import WindowsCredentialStore

_SECRET_REF = "deepseek-api-key"
_secret_store_factory = WindowsCredentialStore
_SAFE_CODES = frozenset({
    "SECRET_NOT_FOUND", "SECRET_ACCESS_DENIED", "SECRET_STORE_OS_FAILURE",
    "SECRET_STORE_UNSUPPORTED_PLATFORM", "SECRET_VALUE_INVALID",
})


class ReportCredentialError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code if code in _SAFE_CODES else "SECRET_STORE_OS_FAILURE"
        super().__init__(self.code)


def read_report_credential() -> str:
    """Read only at authority/dispatch time; no I/O during local planning."""
    try:
        # An installed desktop must never silently use an unrelated inherited key.
        if not getattr(sys, "frozen", False) and "DEEPSEEK_API_KEY" in os.environ:
            value = os.environ["DEEPSEEK_API_KEY"]
        else:
            value = _secret_store_factory().get(_SECRET_REF)
        validate_secret_value(value)
        if not value.strip():
            raise ReportCredentialError("SECRET_VALUE_INVALID")
        return value
    except ReportCredentialError:
        raise
    except SecretStoreError as exc:
        raise ReportCredentialError(exc.code) from None
    except Exception:
        # Native/custom store failures may contain a credential in their message.
        raise ReportCredentialError("SECRET_STORE_OS_FAILURE") from None
