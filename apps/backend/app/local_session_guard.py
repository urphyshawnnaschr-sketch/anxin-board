"""Pure in-memory local session guard core for future loopback API integration.

This module intentionally has no FastAPI, database, filesystem, network, cookie,
credential-store, or launcher side effects.  It only defines reusable validation
primitives for a later integration slice.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from typing import Final


SESSION_ENTROPY_BYTES: Final = 32
GENERATED_BOOTSTRAP_ENTROPY_BYTES: Final = 32
MAX_SECRET_INPUT_CHARS: Final = 512
MAX_REQUEST_ID_CHARS: Final = 128
MAX_IDEMPOTENCY_KEY_CHARS: Final = 128
MAX_HOST_HEADER_CHARS: Final = 255
MAX_ORIGIN_CHARS: Final = 512

_ID_PATTERN: Final = r"[A-Za-z0-9][A-Za-z0-9._:-]*"


class LocalSessionGuardError(RuntimeError):
    """Stable, non-secret failure for local session guard validation."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class LocalSessionGuard:
    """One-bootstrap, one-session in-memory guard for a single local runtime."""

    __slots__ = (
        "_bootstrap_secret",
        "_bootstrap_consumed",
        "_session_token",
        "_invalidated",
        "_lock",
        "_handoff",
        "_handoff_deadline",
    )

    def __init__(self, bootstrap_secret: str) -> None:
        if not _is_bounded_secret(bootstrap_secret):
            raise LocalSessionGuardError("BOOTSTRAP_SECRET_INVALID")
        self._bootstrap_secret: str | None = bootstrap_secret
        self._bootstrap_consumed = False
        self._session_token: str | None = None
        self._invalidated = False
        self._lock = threading.Lock()
        self._handoff: str | None = None
        self._handoff_deadline = 0.0

    @classmethod
    def with_generated_bootstrap(cls) -> tuple["LocalSessionGuard", str]:
        """Create a guard and return its one-time bootstrap secret to the launcher."""

        bootstrap_secret = secrets.token_urlsafe(GENERATED_BOOTSTRAP_ENTROPY_BYTES)
        return cls(bootstrap_secret), bootstrap_secret

    @property
    def bootstrap_consumed(self) -> bool:
        with self._lock:
            return self._bootstrap_consumed

    @property
    def session_active(self) -> bool:
        with self._lock:
            return self._session_token is not None and not self._invalidated

    def exchange_bootstrap(self, bootstrap_secret: str | None) -> str:
        """Consume the bootstrap secret exactly once and mint a temporary session."""

        with self._lock:
            if self._handoff is not None and time.monotonic() >= self._handoff_deadline:
                self._handoff = None
            if not self._invalidated and self._handoff is not None and _is_bounded_secret(bootstrap_secret):
                if secrets.compare_digest(bootstrap_secret.encode("utf-8"), self._handoff.encode("utf-8")):
                    self._handoff = None
                    self._bootstrap_secret = None
                    self._bootstrap_consumed = True
                    if self._session_token is None:
                        self._session_token = _new_session_token_not_equal_to(bootstrap_secret)
                    return self._session_token
            if self._invalidated or self._bootstrap_consumed or self._bootstrap_secret is None:
                raise LocalSessionGuardError("BOOTSTRAP_UNAVAILABLE")
            if not _is_bounded_secret(bootstrap_secret):
                raise LocalSessionGuardError("BOOTSTRAP_INVALID")

            expected = self._bootstrap_secret
            if not secrets.compare_digest(
                bootstrap_secret.encode("utf-8"),
                expected.encode("utf-8"),
            ):
                raise LocalSessionGuardError("BOOTSTRAP_INVALID")

            session_token = _new_session_token_not_equal_to(expected)
            self._bootstrap_secret = None
            self._bootstrap_consumed = True
            self._session_token = session_token
            return session_token

    def mint_handoff(self) -> str:
        """Called only through the current-user launcher pipe, never an HTTP route."""
        with self._lock:
            if self._invalidated:
                raise LocalSessionGuardError("BOOTSTRAP_UNAVAILABLE")
            self._handoff = secrets.token_urlsafe(GENERATED_BOOTSTRAP_ENTROPY_BYTES)
            self._handoff_deadline = time.monotonic() + 60.0
            return self._handoff

    def validate_session(self, session_token: str | None) -> None:
        """Fail closed unless *session_token* is the current live session."""

        with self._lock:
            expected = self._session_token
            if self._invalidated or expected is None or not _is_bounded_secret(session_token):
                raise LocalSessionGuardError("SESSION_INVALID")
            if not secrets.compare_digest(
                session_token.encode("utf-8"),
                expected.encode("utf-8"),
            ):
                raise LocalSessionGuardError("SESSION_INVALID")

    def invalidate(self) -> None:
        """Permanently invalidate bootstrap and session state for this guard instance."""

        with self._lock:
            self._bootstrap_secret = None
            self._bootstrap_consumed = True
            self._session_token = None
            self._handoff = None
            self._invalidated = True

    def validate_write_request(
        self,
        *,
        session_token: str | None,
        host: str | None,
        origin: str | None,
        request_id: str | None,
        idempotency_key: str | None = None,
        require_idempotency_key: bool = False,
    ) -> None:
        """Validate the frozen local-browser write-request contract."""

        self.validate_session(session_token)
        validate_loopback_host(host)
        validate_exact_http_origin(origin, host)
        validate_request_id(request_id)
        validate_idempotency_key(
            idempotency_key,
            required=require_idempotency_key,
        )

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"{type(self).__name__}(bootstrap_consumed={self._bootstrap_consumed!r}, "
                f"session_active={(self._session_token is not None and not self._invalidated)!r}, "
                f"invalidated={self._invalidated!r})"
            )


def validate_loopback_host(host: str | None) -> str:
    """Return the canonical loopback authority or fail closed.

    Accepted host names are exactly ``localhost`` and ``127.0.0.1`` with an
    optional canonical decimal TCP port. IPv6, wildcard/listen addresses,
    trailing-dot forms, LAN addresses, suffix tricks, paths, and userinfo are
    deliberately outside this V1 contract.
    """

    if not isinstance(host, str) or not host or len(host) > MAX_HOST_HEADER_CHARS:
        raise LocalSessionGuardError("HOST_NOT_LOOPBACK")
    if not _is_visible_ascii(host):
        raise LocalSessionGuardError("HOST_NOT_LOOPBACK")
    if host.count(":") > 1:
        raise LocalSessionGuardError("HOST_NOT_LOOPBACK")

    hostname = host
    port_text: str | None = None
    if ":" in host:
        hostname, port_text = host.split(":", 1)
        if not port_text or not port_text.isascii() or not port_text.isdecimal():
            raise LocalSessionGuardError("HOST_NOT_LOOPBACK")
        port = int(port_text, 10)
        if port < 1 or port > 65535 or port_text != str(port):
            raise LocalSessionGuardError("HOST_NOT_LOOPBACK")

    canonical_hostname = hostname.lower()
    if canonical_hostname not in {"localhost", "127.0.0.1"}:
        raise LocalSessionGuardError("HOST_NOT_LOOPBACK")

    if port_text is None:
        return canonical_hostname
    return f"{canonical_hostname}:{port_text}"


def validate_exact_http_origin(origin: str | None, host: str | None) -> str:
    """Require the exact HTTP Origin derived from the current loopback Host."""

    canonical_host = validate_loopback_host(host)
    if not isinstance(origin, str) or not origin or len(origin) > MAX_ORIGIN_CHARS:
        raise LocalSessionGuardError("ORIGIN_INVALID")
    if not _is_visible_ascii(origin):
        raise LocalSessionGuardError("ORIGIN_INVALID")

    expected_origin = f"http://{canonical_host}"
    if origin != expected_origin:
        raise LocalSessionGuardError("ORIGIN_INVALID")
    return origin


def validate_request_id(request_id: str | None) -> str:
    """Require a bounded log-safe ASCII X-Request-ID value."""

    return _validate_bounded_identifier(
        request_id,
        required=True,
        max_chars=MAX_REQUEST_ID_CHARS,
        missing_code="REQUEST_ID_REQUIRED",
        invalid_code="REQUEST_ID_INVALID",
    )


def validate_idempotency_key(
    idempotency_key: str | None,
    *,
    required: bool = False,
) -> str | None:
    """Validate an optional bounded Local-Idempotency-Key value."""

    if idempotency_key is None and not required:
        return None
    return _validate_bounded_identifier(
        idempotency_key,
        required=required,
        max_chars=MAX_IDEMPOTENCY_KEY_CHARS,
        missing_code="IDEMPOTENCY_KEY_REQUIRED",
        invalid_code="IDEMPOTENCY_KEY_INVALID",
    )


def _validate_bounded_identifier(
    value: str | None,
    *,
    required: bool,
    max_chars: int,
    missing_code: str,
    invalid_code: str,
) -> str:
    if value is None or value == "":
        if required:
            raise LocalSessionGuardError(missing_code)
        raise LocalSessionGuardError(invalid_code)
    if not isinstance(value, str) or len(value) > max_chars or not value.isascii():
        raise LocalSessionGuardError(invalid_code)
    if re.fullmatch(_ID_PATTERN, value, flags=re.ASCII) is None:
        raise LocalSessionGuardError(invalid_code)
    return value


def _is_visible_ascii(value: str) -> bool:
    return value.isascii() and all(0x21 <= ord(char) <= 0x7E for char in value)


def _is_bounded_secret(value: str | None) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_SECRET_INPUT_CHARS


def _new_session_token_not_equal_to(bootstrap_secret: str) -> str:
    for _ in range(8):
        session_token = secrets.token_urlsafe(SESSION_ENTROPY_BYTES)
        if not secrets.compare_digest(
            session_token.encode("utf-8"),
            bootstrap_secret.encode("utf-8"),
        ):
            return session_token
    raise LocalSessionGuardError("SESSION_GENERATION_FAILED")
