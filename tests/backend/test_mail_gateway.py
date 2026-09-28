from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
import hashlib
import inspect
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app.fake_mail_gateway import FakeMailGateway  # noqa: E402
from app.mail_gateway import (  # noqa: E402
    MAX_ADDRESS_CHARS,
    MAX_HTML_BODY_BYTES,
    MAX_MESSAGE_ID_CHARS,
    MAX_RECIPIENTS,
    MAX_SUBJECT_CHARS,
    MailContractError,
    MailGateway,
    MailGatewayCallError,
    MailMessage,
    MailSendResult,
    RecipientOutcome,
    RecipientResult,
)


def _message(*, recipients=None, body="<p>A\r\nB</p>", **overrides):
    values = {
        "message_id": "<report-1@example.test>",
        "from_identity": "reports@example.test",
        "to_recipients": recipients or ["a@example.test", "b@example.test"],
        "subject": "Daily report",
        "html_body": body,
        "attempt_correlation_id": "attempt-1",
    }
    values.update(overrides)
    return MailMessage(**values)


def _outcomes(result):
    return [item.outcome for item in result.recipient_results]


def test_t01_message_identity_is_immutable_and_gateway_protocol_matches():
    message = _message()
    with pytest.raises(FrozenInstanceError):
        message.message_id = "<changed@example.test>"  # type: ignore[misc]
    fake = FakeMailGateway()
    assert isinstance(fake, MailGateway)


def test_t02_all_accepted_has_one_result_per_recipient_without_delivery_claim():
    result = FakeMailGateway().send(_message())
    assert result.requested_recipients == ("a@example.test", "b@example.test")
    assert [item.recipient for item in result.recipient_results] == list(result.requested_recipients)
    assert _outcomes(result) == [RecipientOutcome.ACCEPTED, RecipientOutcome.ACCEPTED]
    assert all("delivered" not in item.summary.lower() for item in result.recipient_results)
    assert all("read" not in item.summary.lower() or "not asserted" in item.summary.lower() for item in result.recipient_results)


def test_t03_partial_and_all_rejected_are_scriptable():
    partial = FakeMailGateway(
        recipient_outcomes={"b@example.test": RecipientOutcome.REJECTED}
    ).send(_message())
    assert _outcomes(partial) == [RecipientOutcome.ACCEPTED, RecipientOutcome.REJECTED]

    rejected = FakeMailGateway(
        recipient_outcomes={
            "a@example.test": "rejected",
            "b@example.test": "rejected",
        }
    ).send(_message())
    assert _outcomes(rejected) == [RecipientOutcome.REJECTED, RecipientOutcome.REJECTED]


def test_t04_unknown_is_first_class_and_not_collapsed_to_rejected():
    result = FakeMailGateway(
        recipient_outcomes={"a@example.test": RecipientOutcome.UNKNOWN}
    ).send(_message())
    assert _outcomes(result) == [RecipientOutcome.UNKNOWN, RecipientOutcome.ACCEPTED]
    assert result.recipient_results[0].error_code == "MAIL_RECIPIENT_UNKNOWN"
    assert RecipientOutcome.UNKNOWN is not RecipientOutcome.REJECTED


def test_t05_call_level_response_loss_maps_every_recipient_to_unknown():
    fake = FakeMailGateway(
        recipient_outcomes={"a@example.test": RecipientOutcome.REJECTED},
        response_loss_calls={1},
    )
    result = fake.send(_message())
    assert _outcomes(result) == [RecipientOutcome.UNKNOWN, RecipientOutcome.UNKNOWN]
    assert all(item.error_code == "MAIL_GATEWAY_RESPONSE_LOST" for item in result.recipient_results)
    assert fake.calls[0].connection_lost is False
    assert fake.calls[0].response_lost is True
    assert fake.calls[0].failure_code is None


def test_t06_call_level_connection_interruption_maps_to_unknown_not_failed():
    fake = FakeMailGateway(connection_loss_calls={1})
    result = fake.send(_message())

    assert _outcomes(result) == [RecipientOutcome.UNKNOWN, RecipientOutcome.UNKNOWN]
    assert all(
        item.error_code == "MAIL_GATEWAY_CONNECTION_UNKNOWN"
        for item in result.recipient_results
    )
    call = fake.calls[0]
    assert call.connection_lost is True
    assert call.response_lost is False
    assert call.failure_code is None


def test_t06b_definite_pre_send_call_failure_is_explicit_and_not_unknown():
    fake = FakeMailGateway(failure_calls={1})
    message = _message()

    with pytest.raises(MailGatewayCallError) as caught:
        fake.send(message)

    assert caught.value.code == "MAIL_GATEWAY_CALL_FAILED"
    assert fake.call_count == 1
    call = fake.calls[0]
    assert call.recipients == message.to_recipients
    assert call.message_id == message.message_id
    assert call.body_identity_sha256 == message.body_identity_sha256
    assert call.connection_lost is False
    assert call.response_lost is False
    assert call.failure_code == "MAIL_GATEWAY_CALL_FAILED"
    assert call.scripted_results == ()


def test_t06c_failure_connection_and_response_loss_scripts_must_be_disjoint():
    for kwargs in (
        {"failure_calls": {1}, "connection_loss_calls": {1}},
        {"failure_calls": {1}, "response_loss_calls": {1}},
        {"connection_loss_calls": {1}, "response_loss_calls": {1}},
    ):
        with pytest.raises(MailContractError) as caught:
            FakeMailGateway(**kwargs)
        assert caught.value.code == "MAIL_FAKE_SCRIPT_INVALID"


def test_t07_per_call_script_can_change_outcome_deterministically():
    fake = FakeMailGateway(
        recipient_outcomes={"a@example.test": "unknown"},
        per_call_outcomes={2: {"a@example.test": "accepted"}},
    )
    first = fake.send(_message(recipients=["a@example.test"]))
    second = fake.send(_message(recipients=["a@example.test"]))
    assert _outcomes(first) == [RecipientOutcome.UNKNOWN]
    assert _outcomes(second) == [RecipientOutcome.ACCEPTED]


def test_t08_recipient_order_is_preserved_and_duplicates_send_once():
    message = _message(
        recipients=[
            "b@example.test",
            "a@example.test",
            "b@example.test",
            "c@example.test",
            "a@example.test",
        ]
    )
    assert message.to_recipients == (
        "b@example.test",
        "a@example.test",
        "c@example.test",
    )
    result = FakeMailGateway().send(message)
    assert [item.recipient for item in result.recipient_results] == list(message.to_recipients)


def test_t09_cc_bcc_do_not_exist_and_cannot_be_injected_as_fields():
    parameters = inspect.signature(MailMessage).parameters
    assert "cc" not in parameters and "bcc" not in parameters
    with pytest.raises(TypeError):
        MailMessage(
            message_id="<m@example.test>",
            from_identity="from@example.test",
            to_recipients=["to@example.test"],
            subject="subject",
            html_body="body",
            cc=["hidden@example.test"],  # type: ignore[call-arg]
        )


def test_t10_crlf_and_control_header_injection_fails_closed():
    cases = [
        {"message_id": "<m@example.test>\r\nBcc:x@example.test"},
        {"from_identity": "from@example.test\nBcc:x@example.test"},
        {"recipients": ["to@example.test\r\nBcc:x@example.test"]},
        {"subject": "subject\r\nBcc:x@example.test"},
        {"attempt_correlation_id": "attempt\x00hidden"},
    ]
    for overrides in cases:
        with pytest.raises(MailContractError):
            _message(**overrides)


def test_t11_message_id_empty_whitespace_and_overlong_fail_closed():
    for value in (
        "",
        "bad id",
        "plain-id",
        "<missing-at>",
        " x",
        "x ",
        "<" + "x" * MAX_MESSAGE_ID_CHARS + "@e>",
    ):
        with pytest.raises(MailContractError) as caught:
            _message(message_id=value)
        assert caught.value.code == "MAIL_MESSAGE_ID_INVALID"


def test_t12_html_body_is_opaque_and_hash_binds_exact_utf8_bytes():
    body = "<p>A\r\n B </p>\n"
    message = _message(body=body)
    fake = FakeMailGateway()
    result = fake.send(message)
    assert message.html_body == body
    assert message.body_identity_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert fake.calls[0].body_identity_sha256 == message.body_identity_sha256
    assert result.message_id == message.message_id


def test_t13_fake_records_exact_call_identity_without_body_copy():
    fake = FakeMailGateway(recipient_outcomes={"b@example.test": "rejected"})
    message = _message()
    fake.send(message)
    call = fake.calls[0]
    assert fake.call_count == 1
    assert call.recipients == message.to_recipients
    assert call.message_id == message.message_id
    assert call.body_identity_sha256 == message.body_identity_sha256
    assert not hasattr(call, "html_body")
    assert call.connection_lost is False
    assert call.response_lost is False
    assert call.failure_code is None
    assert [item.outcome for item in call.scripted_results] == [
        RecipientOutcome.ACCEPTED,
        RecipientOutcome.REJECTED,
    ]


def test_t14_retry_subset_is_exactly_what_fake_observes():
    fake = FakeMailGateway(
        recipient_outcomes={
            "a@example.test": "accepted",
            "b@example.test": "unknown",
            "c@example.test": "rejected",
        }
    )
    first = _message(recipients=["a@example.test", "b@example.test", "c@example.test"])
    retry = _message(
        recipients=["b@example.test"],
        message_id=first.message_id,
        body=first.html_body,
    )
    fake.send(first)
    fake.send(retry)
    assert fake.calls[0].recipients == (
        "a@example.test",
        "b@example.test",
        "c@example.test",
    )
    assert fake.calls[1].recipients == ("b@example.test",)
    assert fake.calls[1].message_id == fake.calls[0].message_id
    assert fake.calls[1].body_identity_sha256 == fake.calls[0].body_identity_sha256


def test_t15_result_contract_rejects_missing_duplicate_or_reordered_recipient_results():
    a = RecipientResult(
        "a@example.test",
        RecipientOutcome.ACCEPTED,
        "MAIL_GATEWAY_ACCEPTED",
        "accepted",
    )
    b = RecipientResult(
        "b@example.test",
        RecipientOutcome.UNKNOWN,
        "MAIL_RECIPIENT_UNKNOWN",
        "unknown",
    )
    with pytest.raises(MailContractError):
        MailSendResult("<m@example.test>", ("a@example.test", "b@example.test"), (a,))
    with pytest.raises(MailContractError):
        MailSendResult("<m@example.test>", ("a@example.test", "b@example.test"), (b, a))
    with pytest.raises(MailContractError):
        MailSendResult("<m@example.test>", ("a@example.test", "b@example.test"), (a, a))


def test_t16_inputs_are_bounded_without_normalizing_identity():
    with pytest.raises(MailContractError):
        _message(recipients=[f"u{i}@example.test" for i in range(MAX_RECIPIENTS + 1)])
    with pytest.raises(MailContractError):
        _message(recipients=["a" * (MAX_ADDRESS_CHARS + 1)])
    with pytest.raises(MailContractError):
        _message(subject="s" * (MAX_SUBJECT_CHARS + 1))
    with pytest.raises(MailContractError) as caught:
        _message(body="x" * (MAX_HTML_BODY_BYTES + 1))
    assert caught.value.code == "MAIL_HTML_BODY_TOO_LARGE"


def test_t17_modules_have_no_network_secret_file_db_report_or_checkpoint_dependency():
    root = Path(__file__).resolve().parents[2]
    allowed_import_roots = {
        "__future__",
        "collections",
        "dataclasses",
        "enum",
        "hashlib",
        "re",
        "typing",
        "app",
    }
    forbidden_text = {
        "smtplib",
        "socket",
        "secretstore",
        "get_connection",
        "open(",
    }
    for relative in (
        Path("apps/backend/app/mail_gateway.py"),
        Path("apps/backend/app/fake_mail_gateway.py"),
    ):
        source = (root / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert roots <= allowed_import_roots
        lowered = source.lower()
        assert all(token not in lowered for token in forbidden_text)
