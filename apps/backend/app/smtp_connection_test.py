"""No-submission SMTP connection/authentication preflight.

This module reuses the exact SMTP transport configuration and SecretStore boundary used
by the production adapter, but it deliberately has no message/envelope API. A preflight
may connect, negotiate TLS, authenticate, issue NOOP, and close. It must never execute
MAIL FROM, RCPT TO, DATA, sendmail, or send_message.

Connection-test capability is determined from authoritative transport facts, never a
frontend provider selector. Known V1 transports that require an unsupported auth model
fail closed before SecretStore access or network activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import smtplib
import ssl
from typing import Callable, Protocol

from app.secret_store import SecretStore, SecretStoreError, validate_secret_value
from app.smtp_mail_gateway import SmtpGatewayConfig, SmtpSecurity


class _SmtpPreflightClient(Protocol):
    def ehlo(self): ...
    def starttls(self, *, context): ...
    def login(self, user: str, password: str): ...
    def noop(self): ...
    def quit(self): ...
    def close(self): ...


PreflightClientFactory = Callable[[SmtpGatewayConfig], _SmtpPreflightClient]


class SmtpConnectionTestError(RuntimeError):
    """Stable, non-secret failure returned by the bounded SMTP preflight."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SmtpConnectionTestCapability(str, Enum):
    BASIC_AUTH_PREFLIGHT = "basic_auth_preflight"
    UNSUPPORTED_AUTH_MODEL = "unsupported_auth_model"


_BLOCKED_CONNECTION_TEST_TRANSPORTS = frozenset(
    {
        ("smtp-mail.outlook.com", 587, SmtpSecurity.STARTTLS.value),
    }
)


@dataclass(frozen=True, slots=True)
class SmtpConnectionTestResult:
    credential_read: bool
    smtp_connect: bool
    tls_ready: bool
    smtp_auth: bool
    post_auth_noop: bool


def _fail(code: str) -> SmtpConnectionTestError:
    return SmtpConnectionTestError(code)


def _auth_failure_code(error: Exception, client: _SmtpPreflightClient) -> str:
    """Classify types/capabilities only; never inspect server or credential text."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "MAIL_SMTP_TEST_AUTH_FAILED"
    # smtplib.getreply wraps socket errors in SMTPServerDisconnected, retaining
    # the original exception as context. Bound traversal also handles cycles.
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, TimeoutError):
            return "MAIL_SMTP_TEST_AUTH_TIMEOUT"
        current = current.__cause__ or current.__context__
    if isinstance(error, smtplib.SMTPServerDisconnected):
        return "MAIL_SMTP_TEST_AUTH_CONNECTION_LOST"
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return "MAIL_SMTP_TEST_AUTH_UNSUPPORTED"
    features = getattr(client, "esmtp_features", None)
    advertised = features.get("auth") if isinstance(features, dict) else None
    if (isinstance(error, smtplib.SMTPException) and isinstance(advertised, str)
            and not {"CRAM-MD5", "PLAIN", "LOGIN"}.intersection(advertised.split())):
        return "MAIL_SMTP_TEST_AUTH_UNSUPPORTED"
    return "MAIL_SMTP_TEST_AUTH_PROTOCOL_FAILED"


def classify_smtp_connection_test_capability(
    config: SmtpGatewayConfig,
) -> SmtpConnectionTestCapability:
    """Classify one validated transport tuple without credentials or network access."""

    if not isinstance(config, SmtpGatewayConfig):
        raise TypeError("config must be SmtpGatewayConfig")
    transport_facts = (
        config.host.strip().lower(),
        config.port,
        config.security.value,
    )
    if transport_facts in _BLOCKED_CONNECTION_TEST_TRANSPORTS:
        return SmtpConnectionTestCapability.UNSUPPORTED_AUTH_MODEL
    return SmtpConnectionTestCapability.BASIC_AUTH_PREFLIGHT


def require_smtp_connection_test_capability(
    config: SmtpGatewayConfig,
) -> SmtpConnectionTestCapability:
    capability = classify_smtp_connection_test_capability(config)
    if capability is not SmtpConnectionTestCapability.BASIC_AUTH_PREFLIGHT:
        raise _fail("MAIL_SMTP_TEST_TRANSPORT_UNSUPPORTED")
    return capability


def _default_client_factory(config: SmtpGatewayConfig) -> _SmtpPreflightClient:
    context = ssl.create_default_context()
    if config.security is SmtpSecurity.IMPLICIT_TLS:
        return smtplib.SMTP_SSL(
            config.host,
            config.port,
            timeout=config.timeout_seconds,
            context=context,
        )
    return smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds)


def _close_client(client: _SmtpPreflightClient | None) -> None:
    if client is None:
        return
    try:
        client.quit()
        return
    except Exception:
        pass
    try:
        client.close()
    except Exception:
        pass


def test_smtp_connection(
    *,
    config: SmtpGatewayConfig,
    secret_store: SecretStore,
    client_factory: PreflightClientFactory | None = None,
) -> SmtpConnectionTestResult:
    """Read exactly one bound credential and prove SMTP TLS/auth without submission."""

    require_smtp_connection_test_capability(config)
    if not isinstance(secret_store, SecretStore):
        raise TypeError("secret_store must implement SecretStore")

    try:
        secret = validate_secret_value(secret_store.get(config.secret_ref))
    except SecretStoreError as exc:
        raise _fail("MAIL_SMTP_TEST_SECRET_UNAVAILABLE") from exc

    factory = client_factory or _default_client_factory
    client: _SmtpPreflightClient | None = None
    try:
        try:
            client = factory(config)
        except Exception as exc:
            raise _fail("MAIL_SMTP_TEST_CONNECT_FAILED") from exc

        if config.security is SmtpSecurity.STARTTLS:
            try:
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            except Exception as exc:
                raise _fail("MAIL_SMTP_TEST_TLS_FAILED") from exc

        try:
            client.login(config.username, secret)
        except Exception as exc:
            raise _fail(_auth_failure_code(exc, client)) from exc

        try:
            code, _message = client.noop()
            if int(code) >= 400:
                raise _fail("MAIL_SMTP_TEST_POST_AUTH_FAILED")
        except SmtpConnectionTestError:
            raise
        except Exception as exc:
            raise _fail("MAIL_SMTP_TEST_POST_AUTH_FAILED") from exc

        return SmtpConnectionTestResult(
            credential_read=True,
            smtp_connect=True,
            tls_ready=True,
            smtp_auth=True,
            post_auth_noop=True,
        )
    finally:
        secret = None
        _close_client(client)
