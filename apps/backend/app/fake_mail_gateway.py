"""Scriptable no-I/O FakeMailGateway for deterministic mail state-machine tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.mail_gateway import (
    MAX_RECIPIENTS,
    MailContractError,
    MailGatewayCallError,
    MailMessage,
    MailSendResult,
    RecipientOutcome,
    RecipientResult,
    _validate_address,
)


_DEFAULT_RESULT = {
    RecipientOutcome.ACCEPTED: (
        "MAIL_GATEWAY_ACCEPTED",
        "accepted by gateway; delivery/read status is not asserted",
    ),
    RecipientOutcome.REJECTED: (
        "MAIL_RECIPIENT_REJECTED",
        "recipient rejected by gateway",
    ),
    RecipientOutcome.UNKNOWN: (
        "MAIL_RECIPIENT_UNKNOWN",
        "gateway response is unknown; do not infer failure or automatic retry",
    ),
}
_CALL_FAILURE_CODE = "MAIL_GATEWAY_CALL_FAILED"
_CONNECTION_UNKNOWN_CODE = "MAIL_GATEWAY_CONNECTION_UNKNOWN"
_RESPONSE_LOST_CODE = "MAIL_GATEWAY_RESPONSE_LOST"


@dataclass(frozen=True, slots=True)
class FakeMailCall:
    call_number: int
    recipients: tuple[str, ...]
    message_id: str
    body_identity_sha256: str
    connection_lost: bool
    response_lost: bool
    failure_code: str | None
    scripted_results: tuple[RecipientResult, ...]


def _validated_outcome(value: object) -> RecipientOutcome:
    if isinstance(value, RecipientOutcome):
        return value
    try:
        return RecipientOutcome(value)
    except (TypeError, ValueError) as exc:
        raise MailContractError("MAIL_FAKE_OUTCOME_INVALID") from exc


def _validated_outcome_map(value: object) -> dict[str, RecipientOutcome]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > MAX_RECIPIENTS:
        raise MailContractError("MAIL_FAKE_SCRIPT_INVALID")
    result: dict[str, RecipientOutcome] = {}
    for raw_recipient, raw_outcome in value.items():
        recipient = _validate_address(raw_recipient, code="MAIL_RECIPIENT_INVALID")
        result[recipient] = _validated_outcome(raw_outcome)
    return result


def _validated_call_scripts(value: object) -> dict[int, dict[str, RecipientOutcome]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > 100:
        raise MailContractError("MAIL_FAKE_SCRIPT_INVALID")
    scripts: dict[int, dict[str, RecipientOutcome]] = {}
    for raw_call, raw_outcomes in value.items():
        if type(raw_call) is not int or raw_call <= 0:
            raise MailContractError("MAIL_FAKE_SCRIPT_INVALID")
        scripts[raw_call] = _validated_outcome_map(raw_outcomes)
    return scripts


def _validated_call_numbers(value: object) -> frozenset[int]:
    if value is None:
        return frozenset()
    if not isinstance(value, (set, frozenset, list, tuple)) or len(value) > 100:
        raise MailContractError("MAIL_FAKE_SCRIPT_INVALID")
    calls: set[int] = set()
    for raw in value:
        if type(raw) is not int or raw <= 0:
            raise MailContractError("MAIL_FAKE_SCRIPT_INVALID")
        calls.add(raw)
    return frozenset(calls)


class FakeMailGateway:
    """In-memory fake with recipient, definite-failure, and unknown scripting."""

    def __init__(
        self,
        *,
        recipient_outcomes: Mapping[str, RecipientOutcome | str] | None = None,
        per_call_outcomes: Mapping[int, Mapping[str, RecipientOutcome | str]] | None = None,
        failure_calls: set[int] | frozenset[int] | list[int] | tuple[int, ...] | None = None,
        connection_loss_calls: set[int] | frozenset[int] | list[int] | tuple[int, ...] | None = None,
        response_loss_calls: set[int] | frozenset[int] | list[int] | tuple[int, ...] | None = None,
    ) -> None:
        self._recipient_outcomes = _validated_outcome_map(recipient_outcomes)
        self._per_call_outcomes = _validated_call_scripts(per_call_outcomes)
        self._failure_calls = _validated_call_numbers(failure_calls)
        self._connection_loss_calls = _validated_call_numbers(connection_loss_calls)
        self._response_loss_calls = _validated_call_numbers(response_loss_calls)
        scripted_call_sets = (
            self._failure_calls,
            self._connection_loss_calls,
            self._response_loss_calls,
        )
        if any(
            scripted_call_sets[left] & scripted_call_sets[right]
            for left in range(len(scripted_call_sets))
            for right in range(left + 1, len(scripted_call_sets))
        ):
            raise MailContractError("MAIL_FAKE_SCRIPT_INVALID")
        self._calls: list[FakeMailCall] = []

    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def calls(self) -> tuple[FakeMailCall, ...]:
        return tuple(self._calls)

    def send(self, message: MailMessage) -> MailSendResult:
        if not isinstance(message, MailMessage):
            raise TypeError("message must be MailMessage")

        call_number = len(self._calls) + 1
        if call_number in self._failure_calls:
            self._calls.append(
                FakeMailCall(
                    call_number=call_number,
                    recipients=message.to_recipients,
                    message_id=message.message_id,
                    body_identity_sha256=message.body_identity_sha256,
                    connection_lost=False,
                    response_lost=False,
                    failure_code=_CALL_FAILURE_CODE,
                    scripted_results=(),
                )
            )
            raise MailGatewayCallError(_CALL_FAILURE_CODE)

        call_overrides = self._per_call_outcomes.get(call_number, {})
        connection_lost = call_number in self._connection_loss_calls
        response_lost = call_number in self._response_loss_calls
        results: list[RecipientResult] = []

        for recipient in message.to_recipients:
            if connection_lost:
                outcome = RecipientOutcome.UNKNOWN
                code = _CONNECTION_UNKNOWN_CODE
                summary = (
                    "gateway connection interrupted; recipient outcome is unknown; "
                    "do not retry automatically"
                )
            elif response_lost:
                outcome = RecipientOutcome.UNKNOWN
                code = _RESPONSE_LOST_CODE
                summary = (
                    "gateway response lost; recipient outcome is unknown; "
                    "do not retry automatically"
                )
            else:
                outcome = call_overrides.get(
                    recipient,
                    self._recipient_outcomes.get(recipient, RecipientOutcome.ACCEPTED),
                )
                code, summary = _DEFAULT_RESULT[outcome]
            results.append(
                RecipientResult(
                    recipient=recipient,
                    outcome=outcome,
                    error_code=code,
                    summary=summary,
                )
            )

        result = MailSendResult(
            message_id=message.message_id,
            requested_recipients=message.to_recipients,
            recipient_results=tuple(results),
        )
        self._calls.append(
            FakeMailCall(
                call_number=call_number,
                recipients=message.to_recipients,
                message_id=message.message_id,
                body_identity_sha256=message.body_identity_sha256,
                connection_lost=connection_lost,
                response_lost=response_lost,
                failure_code=None,
                scripted_results=result.recipient_results,
            )
        )
        return result
