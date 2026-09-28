from __future__ import annotations

import pytest

import app.mail_admitted_execution as execution_module
from app.fake_mail_gateway import FakeMailGateway
from app.mail_admitted_execution import (
    MailAdmittedExecutionError,
    execute_admitted_send_attempt_once_for_validation,
)


class _AdversarialFakeSubclass(FakeMailGateway):
    """Looks like the fake by inheritance but must never be executable pre-P5."""

    def __init__(self) -> None:
        super().__init__()
        self.override_send_calls = 0

    def send(self, message):  # pragma: no cover - exact gate must reject first
        self.override_send_calls += 1
        raise AssertionError("subclass override must never reach MailWorkflow")


def test_fake_gateway_subclass_override_is_rejected_before_any_authority_or_workflow(
    monkeypatch: pytest.MonkeyPatch,
):
    gateway = _AdversarialFakeSubclass()
    authority_reads = 0
    workflow_calls = 0

    def forbidden_admission_read(_send_attempt_id):
        nonlocal authority_reads
        authority_reads += 1
        raise AssertionError("fake-subclass gate must fail before admission read")

    def forbidden_workflow(**_kwargs):
        nonlocal workflow_calls
        workflow_calls += 1
        raise AssertionError("fake-subclass gate must fail before MailWorkflow")

    monkeypatch.setattr(
        execution_module,
        "get_durable_mail_admission",
        forbidden_admission_read,
    )
    monkeypatch.setattr(
        execution_module,
        "execute_send_attempt_once",
        forbidden_workflow,
    )

    with pytest.raises(MailAdmittedExecutionError) as caught:
        execute_admitted_send_attempt_once_for_validation(
            send_attempt_id="adversarial-fake-subclass",
            exact_html="<p>must not execute</p>",
            gateway=gateway,
        )

    assert caught.value.code == "MAIL_EXECUTION_REAL_GATEWAY_NOT_AUTHORIZED"
    assert type(gateway) is not FakeMailGateway
    assert isinstance(gateway, FakeMailGateway) is True
    assert authority_reads == 0
    assert workflow_calls == 0
    assert gateway.override_send_calls == 0
