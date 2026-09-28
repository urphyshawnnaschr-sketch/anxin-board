"""Pure MailGateway contract for the FR-21/22 foundation slice.

This module defines immutable, bounded mail request/result values only. It does not
perform SMTP/network I/O, credential access, persistence, report transitions, or
checkpoint advancement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import re
from typing import Protocol, runtime_checkable


MAX_RECIPIENTS = 100
MAX_ADDRESS_CHARS = 320
MAX_MESSAGE_ID_CHARS = 255
MAX_SUBJECT_CHARS = 998
MAX_ATTEMPT_CORRELATION_ID_CHARS = 128
MAX_HTML_BODY_BYTES = 1024 * 1024
MAX_ERROR_CODE_CHARS = 64
MAX_RESULT_SUMMARY_CHARS = 256

_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MESSAGE_ID_RE = re.compile(r"^<[^<>@\s]+@[^<>@\s]+>$")


class MailContractError(ValueError):
    """Stable, non-secret validation failure for the local mail contract."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class MailGatewayCallError(RuntimeError):
    """Explicit call-level gateway failure with a stable non-secret code.

    A call error means the gateway can positively report that the attempt failed at
    the call boundary. It is deliberately distinct from a returned UNKNOWN recipient
    result, where the send outcome cannot be proven and automatic retry is unsafe.
    """

    __slots__ = ("code",)

    def __init__(self, code: str = "MAIL_GATEWAY_CALL_FAILED") -> None:
        if type(code) is not str or _ERROR_CODE_RE.fullmatch(code) is None:
            raise MailContractError("MAIL_GATEWAY_CALL_ERROR_CODE_INVALID")
        self.code = code
        super().__init__(code)


def _has_header_control(value: str) -> bool:
    return any(
        ord(char) < 32
        or 127 <= ord(char) <= 159
        or char in {"\u2028", "\u2029"}
        for char in value
    )


def _require_header_text(
    value: object,
    *,
    code: str,
    max_chars: int,
    allow_empty: bool = False,
    forbid_any_whitespace: bool = False,
) -> str:
    if type(value) is not str or len(value) > max_chars:
        raise MailContractError(code)
    if not allow_empty and not value:
        raise MailContractError(code)
    if value != value.strip() or _has_header_control(value):
        raise MailContractError(code)
    if forbid_any_whitespace and any(char.isspace() for char in value):
        raise MailContractError(code)
    return value


def _validate_message_id(value: object) -> str:
    message_id = _require_header_text(
        value,
        code="MAIL_MESSAGE_ID_INVALID",
        max_chars=MAX_MESSAGE_ID_CHARS,
        forbid_any_whitespace=True,
    )
    if _MESSAGE_ID_RE.fullmatch(message_id) is None:
        raise MailContractError("MAIL_MESSAGE_ID_INVALID")
    return message_id


def _validate_address(value: object, *, code: str) -> str:
    return _require_header_text(value, code=code, max_chars=MAX_ADDRESS_CHARS)


def _validate_subject(value: object) -> str:
    return _require_header_text(
        value,
        code="MAIL_SUBJECT_INVALID",
        max_chars=MAX_SUBJECT_CHARS,
        allow_empty=True,
    )


def _validate_attempt_correlation_id(value: object) -> str | None:
    if value is None:
        return None
    return _require_header_text(
        value,
        code="MAIL_ATTEMPT_CORRELATION_ID_INVALID",
        max_chars=MAX_ATTEMPT_CORRELATION_ID_CHARS,
    )


def _validate_html_body(value: object) -> tuple[str, str]:
    if type(value) is not str:
        raise MailContractError("MAIL_HTML_BODY_INVALID")
    try:
        body_bytes = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MailContractError("MAIL_HTML_BODY_INVALID") from exc
    if len(body_bytes) > MAX_HTML_BODY_BYTES:
        raise MailContractError("MAIL_HTML_BODY_TOO_LARGE")
    return value, hashlib.sha256(body_bytes).hexdigest()


def _dedupe_recipients(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value or len(value) > MAX_RECIPIENTS:
        raise MailContractError("MAIL_RECIPIENTS_INVALID")
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in value:
        recipient = _validate_address(raw, code="MAIL_RECIPIENT_INVALID")
        if recipient not in seen:
            seen.add(recipient)
            ordered.append(recipient)
    if not ordered:
        raise MailContractError("MAIL_RECIPIENTS_INVALID")
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class MailMessage:
    """Immutable request identity; To order is first-occurrence deterministic."""

    message_id: str
    from_identity: str
    to_recipients: tuple[str, ...]
    subject: str
    html_body: str
    attempt_correlation_id: str | None = None
    body_identity_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "message_id", _validate_message_id(self.message_id))
        object.__setattr__(
            self,
            "from_identity",
            _validate_address(self.from_identity, code="MAIL_FROM_IDENTITY_INVALID"),
        )
        object.__setattr__(self, "to_recipients", _dedupe_recipients(self.to_recipients))
        object.__setattr__(self, "subject", _validate_subject(self.subject))
        body, body_hash = _validate_html_body(self.html_body)
        object.__setattr__(self, "html_body", body)
        object.__setattr__(self, "body_identity_sha256", body_hash)
        object.__setattr__(
            self,
            "attempt_correlation_id",
            _validate_attempt_correlation_id(self.attempt_correlation_id),
        )


class RecipientOutcome(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RecipientResult:
    recipient: str
    outcome: RecipientOutcome
    error_code: str
    summary: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "recipient",
            _validate_address(self.recipient, code="MAIL_RECIPIENT_INVALID"),
        )
        if not isinstance(self.outcome, RecipientOutcome):
            raise MailContractError("MAIL_RECIPIENT_OUTCOME_INVALID")
        if (
            type(self.error_code) is not str
            or len(self.error_code) > MAX_ERROR_CODE_CHARS
            or _ERROR_CODE_RE.fullmatch(self.error_code) is None
        ):
            raise MailContractError("MAIL_RESULT_ERROR_CODE_INVALID")
        if (
            type(self.summary) is not str
            or not self.summary
            or len(self.summary) > MAX_RESULT_SUMMARY_CHARS
            or _has_header_control(self.summary)
        ):
            raise MailContractError("MAIL_RESULT_SUMMARY_INVALID")


@dataclass(frozen=True, slots=True)
class MailSendResult:
    """One and only one result for every requested recipient, in request order."""

    message_id: str
    requested_recipients: tuple[str, ...]
    recipient_results: tuple[RecipientResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "message_id", _validate_message_id(self.message_id))
        requested = _dedupe_recipients(self.requested_recipients)
        object.__setattr__(self, "requested_recipients", requested)
        if not isinstance(self.recipient_results, (list, tuple)):
            raise MailContractError("MAIL_RESULT_RECIPIENTS_INVALID")
        results = tuple(self.recipient_results)
        if any(not isinstance(item, RecipientResult) for item in results):
            raise MailContractError("MAIL_RESULT_RECIPIENTS_INVALID")
        if tuple(item.recipient for item in results) != requested:
            raise MailContractError("MAIL_RESULT_RECIPIENTS_INVALID")
        object.__setattr__(self, "recipient_results", results)


@runtime_checkable
class MailGateway(Protocol):
    def send(self, message: MailMessage) -> MailSendResult:
        """Attempt the exact immutable message without implying delivery or read state."""
        ...
