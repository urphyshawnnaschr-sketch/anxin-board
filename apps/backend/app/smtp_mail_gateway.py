"""Narrow standard-library SMTP adapter behind the existing MailGateway contract.

The adapter owns transport translation only. It does not own approval, recipient
policy, SendAttempt lifecycle, retry policy, persistence, UI state, or checkpoint
advancement. Production credentials stay behind the injected SecretStore; tests can
inject a fake SMTP client so no network or real credential is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from enum import Enum
import re
import smtplib
import ssl
from typing import Callable, Protocol

from app.mail_gateway import (
    MailGatewayCallError,
    MailMessage,
    MailSendResult,
    RecipientOutcome,
    RecipientResult,
)
from app.secret_store import SecretStore, SecretStoreError, validate_secret_ref, validate_secret_value
from app.mail_brand_asset import validate_inline_assets, standalone_customer_html


_MAX_HOST_CHARS = 253
_MAX_USERNAME_CHARS = 320
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class SmtpSecurity(str, Enum):
    IMPLICIT_TLS = "implicit_tls"
    STARTTLS = "starttls"


class _SmtpClient(Protocol):
    def ehlo(self): ...
    def starttls(self, *, context): ...
    def login(self, user: str, password: str): ...
    def send_message(self, msg, from_addr=None, to_addrs=None): ...
    def quit(self): ...
    def close(self): ...


@dataclass(frozen=True, slots=True)
class SmtpGatewayConfig:
    host: str
    port: int
    security: SmtpSecurity
    username: str
    secret_ref: str
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        host = self.host
        if (
            type(host) is not str
            or not host
            or len(host) > _MAX_HOST_CHARS
            or host != host.strip()
            or _HOST_RE.fullmatch(host) is None
            or ".." in host
        ):
            raise ValueError("MAIL_SMTP_HOST_INVALID")
        object.__setattr__(self, "host", host.lower())
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("MAIL_SMTP_PORT_INVALID")
        if not isinstance(self.security, SmtpSecurity):
            raise ValueError("MAIL_SMTP_SECURITY_INVALID")
        username = self.username
        if (
            type(username) is not str
            or not username
            or len(username) > _MAX_USERNAME_CHARS
            or username != username.strip()
            or any(
                ord(ch) < 32
                or 127 <= ord(ch) <= 159
                or ch in {"\u2028", "\u2029"}
                for ch in username
            )
        ):
            raise ValueError("MAIL_SMTP_USERNAME_INVALID")
        object.__setattr__(self, "secret_ref", validate_secret_ref(self.secret_ref))
        timeout = self.timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 1 <= float(timeout) <= 60
        ):
            raise ValueError("MAIL_SMTP_TIMEOUT_INVALID")
        object.__setattr__(self, "timeout_seconds", float(timeout))


ClientFactory = Callable[[SmtpGatewayConfig], _SmtpClient]


def _default_client_factory(config: SmtpGatewayConfig) -> _SmtpClient:
    context = ssl.create_default_context()
    if config.security is SmtpSecurity.IMPLICIT_TLS:
        return smtplib.SMTP_SSL(
            config.host,
            config.port,
            timeout=config.timeout_seconds,
            context=context,
        )
    return smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds)


def _result(
    message: MailMessage,
    outcomes: dict[str, tuple[RecipientOutcome, str, str]],
) -> MailSendResult:
    return MailSendResult(
        message_id=message.message_id,
        requested_recipients=message.to_recipients,
        recipient_results=tuple(
            RecipientResult(
                recipient=recipient,
                outcome=outcomes[recipient][0],
                error_code=outcomes[recipient][1],
                summary=outcomes[recipient][2],
            )
            for recipient in message.to_recipients
        ),
    )


def _unknown_result(message: MailMessage) -> MailSendResult:
    return _result(
        message,
        {
            recipient: (
                RecipientOutcome.UNKNOWN,
                "MAIL_SMTP_OUTCOME_UNKNOWN",
                "SMTP invocation ended without provable recipient outcome",
            )
            for recipient in message.to_recipients
        },
    )


def _message_rejected_result(message: MailMessage) -> MailSendResult:
    return _result(
        message,
        {
            recipient: (
                RecipientOutcome.REJECTED,
                "MAIL_SMTP_MESSAGE_REJECTED",
                "SMTP server explicitly rejected this message submission",
            )
            for recipient in message.to_recipients
        },
    )


def _refusal_result(message: MailMessage, refused: object) -> MailSendResult:
    """Translate the normal send_message refusal dict.

    In the normal return contract, requested recipients absent from the refusal dict
    were accepted by the server. Unknown/non-requested keys are not trustworthy and
    therefore collapse the complete outcome to UNKNOWN.
    """
    if not isinstance(refused, dict):
        return _unknown_result(message)
    keys = tuple(refused.keys())
    if any(type(item) is not str for item in keys):
        return _unknown_result(message)
    requested = set(message.to_recipients)
    if any(item not in requested for item in keys):
        return _unknown_result(message)
    outcomes: dict[str, tuple[RecipientOutcome, str, str]] = {}
    for recipient in message.to_recipients:
        if recipient in refused:
            outcomes[recipient] = (
                RecipientOutcome.REJECTED,
                "MAIL_SMTP_RECIPIENT_REJECTED",
                "SMTP server explicitly rejected this recipient",
            )
        else:
            outcomes[recipient] = (
                RecipientOutcome.ACCEPTED,
                "MAIL_SMTP_ACCEPTED",
                "SMTP server accepted this recipient for transport",
            )
    return _result(message, outcomes)


def _all_recipients_refused_result(message: MailMessage, refused: object) -> MailSendResult:
    """Translate SMTPRecipientsRefused without manufacturing acceptance.

    smtplib raises SMTPRecipientsRefused only when all recipients are refused. If an
    injected or malformed exception does not carry exactly the complete requested set,
    the evidence shape is inconsistent and must fail closed to UNKNOWN for everyone.
    """
    if not isinstance(refused, dict):
        return _unknown_result(message)
    keys = tuple(refused.keys())
    if any(type(item) is not str for item in keys):
        return _unknown_result(message)
    if set(keys) != set(message.to_recipients) or len(keys) != len(message.to_recipients):
        return _unknown_result(message)
    return _result(
        message,
        {
            recipient: (
                RecipientOutcome.REJECTED,
                "MAIL_SMTP_RECIPIENT_REJECTED",
                "SMTP server explicitly rejected this recipient",
            )
            for recipient in message.to_recipients
        },
    )


def _materialize_email(message: MailMessage) -> EmailMessage:
    try:
        assets = validate_inline_assets(message.html_body)
        attachment = standalone_customer_html(message.html_body)
    except ValueError:
        raise MailGatewayCallError("MAIL_SMTP_INLINE_ASSET_INVALID") from None
    outbound = EmailMessage(policy=SMTP)
    outbound["Message-ID"] = message.message_id
    outbound["From"] = message.from_identity
    outbound["To"] = ", ".join(message.to_recipients)
    outbound["Subject"] = message.subject
    outbound.set_content(message.html_body, subtype="html", charset="utf-8")
    for cid, content in assets:
        outbound.add_related(content, maintype="image", subtype="png", cid=f"<{cid}>",
                             disposition="inline", filename="anxin-calligraphy.png")
    if attachment is not None:
        outbound.add_attachment(attachment, subtype="html", charset="utf-8", filename="anxin-board.html")
    return outbound


def _close_client(client: _SmtpClient | None) -> None:
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


class StdlibSmtpMailGateway:
    """Synchronous MailGateway adapter using Python's mature SMTP implementation."""

    def __init__(
        self,
        *,
        config: SmtpGatewayConfig,
        secret_store: SecretStore,
        client_factory: ClientFactory | None = None,
    ) -> None:
        if not isinstance(config, SmtpGatewayConfig):
            raise TypeError("config must be SmtpGatewayConfig")
        if not isinstance(secret_store, SecretStore):
            raise TypeError("secret_store must implement SecretStore")
        self._config = config
        self._secret_store = secret_store
        self._client_factory = client_factory or _default_client_factory

    def send(self, message: MailMessage) -> MailSendResult:
        if not isinstance(message, MailMessage):
            raise TypeError("message must be MailMessage")

        outbound = _materialize_email(message)
        try:
            secret = validate_secret_value(self._secret_store.get(self._config.secret_ref))
        except SecretStoreError as exc:
            raise MailGatewayCallError("MAIL_SMTP_SECRET_UNAVAILABLE") from exc

        client: _SmtpClient | None = None
        try:
            # No message envelope/submission has started before this block completes.
            # Any failure here is therefore a provable pre-submission failure.
            try:
                client = self._client_factory(self._config)
                if self._config.security is SmtpSecurity.STARTTLS:
                    client.ehlo()
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                client.login(self._config.username, secret)
            except smtplib.SMTPAuthenticationError as exc:
                raise MailGatewayCallError("MAIL_SMTP_AUTH_FAILED") from exc
            except Exception as exc:
                raise MailGatewayCallError("MAIL_SMTP_PRE_SUBMIT_FAILED") from exc

            try:
                refused = client.send_message(
                    outbound,
                    from_addr=message.from_identity,
                    to_addrs=list(message.to_recipients),
                )
            except smtplib.SMTPRecipientsRefused as exc:
                return _all_recipients_refused_result(message, exc.recipients)
            except smtplib.SMTPSenderRefused as exc:
                # MAIL FROM was explicitly refused, so message submission did not start.
                raise MailGatewayCallError("MAIL_SMTP_SENDER_REJECTED") from exc
            except smtplib.SMTPDataError:
                # SMTP DATA returned an explicit rejection response. This is positive
                # evidence of rejection, unlike timeout/disconnect/response loss.
                return _message_rejected_result(message)
            except Exception:
                # send_message() was entered. Any unclassified timeout, disconnect,
                # response loss, or partial protocol failure is terminal UNKNOWN.
                return _unknown_result(message)

            return _refusal_result(message, refused)
        finally:
            _close_client(client)
