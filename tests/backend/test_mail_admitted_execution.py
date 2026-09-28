from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import app.mail_admitted_execution as execution_module
from app.db import init_db
from app.fake_mail_gateway import FakeMailGateway
from app.mail_admission_candidate import MailAdmissionCandidate
from app.mail_admitted_execution import (
    MailAdmittedExecutionError,
    execute_admitted_send_attempt_once_for_validation,
    get_admitted_smtp_gateway_config,
)
from app.mail_durable_admission import MailDurableAdmission, MailDurableAdmissionError
from app.mail_gateway import RecipientOutcome
from app.mail_send_attempts import SendAttemptBinding, create_send_attempt, get_send_attempt
from app.mail_transport_profile import save_mail_transport_profile


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _candidate(
    *,
    html: str,
    send_attempt_id: str,
    transport_profile_id: int = 41,
    transport_profile_version_no: int = 3,
    transport_profile_hash: str | None = None,
    secret_ref: str = "smtp.admitted.test.v3",
    from_identity: str = "reports@example.test",
) -> MailAdmissionCandidate:
    recipients = ("alpha@example.test", "beta@example.test")
    recipients_json = json.dumps(list(recipients), ensure_ascii=True, separators=(",", ":"))
    recipients_hash = _h(recipients_json)
    formal_hash = _h("formal-admitted-execution")
    profile_hash = transport_profile_hash or _h("transport-profile-admitted")
    payload = {
        "schema_version": "mail_admission_candidate_v1",
        "project_id": 7,
        "report_version_id": 19,
        "report_content_hash": _h("report-content-admitted"),
        "approval_snapshot_id": 11,
        "approval_snapshot_hash": _h("approval-admitted"),
        "approval_recipient_binding_hash": _h("binding-admitted"),
        "recipient_config_version_id": 31,
        "recipient_config_version_no": 4,
        "recipients_hash": recipients_hash,
        "to_recipients": list(recipients),
        "transport_profile_id": transport_profile_id,
        "transport_profile_version_no": transport_profile_version_no,
        "transport_profile_hash": profile_hash,
        "secret_ref": secret_ref,
        "from_identity": from_identity,
        "formal_report_hash": formal_hash,
        "render_identity": f"mail_report_render_v1:{formal_hash}",
        "render_hash": _h("render-admitted"),
        "html_sha256": _h(html),
        "subject": "Anxin Board admitted execution",
        "message_id": f"<anxin-{formal_hash[:32]}@anxinboard.local>",
    }
    return MailAdmissionCandidate(
        schema_version="mail_admission_candidate_v1",
        project_id=7,
        report_version_id=19,
        report_content_hash=payload["report_content_hash"],
        approval_snapshot_id=11,
        approval_snapshot_hash=payload["approval_snapshot_hash"],
        approval_recipient_binding_hash=payload["approval_recipient_binding_hash"],
        recipient_config_version_id=31,
        recipient_config_version_no=4,
        recipients_hash=recipients_hash,
        to_recipients=recipients,
        transport_profile_id=transport_profile_id,
        transport_profile_version_no=transport_profile_version_no,
        transport_profile_hash=profile_hash,
        secret_ref=secret_ref,
        from_identity=from_identity,
        formal_report_hash=formal_hash,
        render_identity=payload["render_identity"],
        render_hash=payload["render_hash"],
        html_sha256=payload["html_sha256"],
        subject=payload["subject"],
        message_id=payload["message_id"],
        admission_hash=_h(_canonical(payload)),
    )


def _admission(candidate: MailAdmissionCandidate, send_attempt_id: str) -> MailDurableAdmission:
    return MailDurableAdmission(
        id=1,
        schema_version="mail_durable_admission_v1",
        send_attempt_id=send_attempt_id,
        candidate=candidate,
        predecessor_send_attempt_id=None,
        created_at="2026-09-06T12:00:00+00:00",
        record_hash=_h("record-admitted"),
    )


def _prepare_attempt(candidate: MailAdmissionCandidate, send_attempt_id: str, *, subject: str | None = None):
    return create_send_attempt(
        SendAttemptBinding(
            send_attempt_id=send_attempt_id,
            project_id=candidate.project_id,
            approval_snapshot_id=candidate.approval_snapshot_id,
            approval_snapshot_hash=candidate.approval_snapshot_hash,
            report_version_id=candidate.report_version_id,
            report_content_hash=candidate.report_content_hash,
            render_identity=candidate.render_identity,
            render_hash=candidate.render_hash,
            html_sha256=candidate.html_sha256,
            message_id=candidate.message_id,
            to_recipients=candidate.to_recipients,
            subject=subject or candidate.subject,
            from_identity=candidate.from_identity,
        )
    )


@pytest.fixture()
def admitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "anxinboard.db"))
    init_db()
    html = "<html><body>approved admitted mail</body></html>"
    send_attempt_id = "mail-admitted-execution-test"
    candidate = _candidate(html=html, send_attempt_id=send_attempt_id)
    admission = _admission(candidate, send_attempt_id)
    _prepare_attempt(candidate, send_attempt_id)
    monkeypatch.setattr(
        execution_module,
        "get_durable_mail_admission",
        lambda actual: admission if actual == send_attempt_id else (_ for _ in ()).throw(
            MailDurableAdmissionError("MAIL_ADMISSION_NOT_FOUND")
        ),
    )
    return html, send_attempt_id, candidate, admission


def test_e01_r4_admission_gates_real_mailworkflow_with_fake_gateway(admitted):
    html, send_attempt_id, _, _ = admitted
    gateway = FakeMailGateway()

    terminal = execute_admitted_send_attempt_once_for_validation(
        send_attempt_id=send_attempt_id,
        exact_html=html,
        gateway=gateway,
    )

    assert terminal.state == "sent"
    assert gateway.call_count == 1
    assert gateway.calls[0].body_identity_sha256 == _h(html)


def test_e02_partial_result_uses_existing_mailworkflow_semantics(admitted):
    html, send_attempt_id, _, _ = admitted
    gateway = FakeMailGateway(
        recipient_outcomes={"beta@example.test": RecipientOutcome.REJECTED}
    )

    terminal = execute_admitted_send_attempt_once_for_validation(
        send_attempt_id=send_attempt_id,
        exact_html=html,
        gateway=gateway,
    )

    assert terminal.state == "partial"
    assert tuple(item.outcome for item in terminal.recipient_results) == (
        RecipientOutcome.ACCEPTED,
        RecipientOutcome.REJECTED,
    )


def test_e03_unknown_is_terminal_and_second_bridge_call_never_reinvokes_gateway(admitted):
    html, send_attempt_id, _, _ = admitted
    gateway = FakeMailGateway(response_loss_calls={1})

    first = execute_admitted_send_attempt_once_for_validation(
        send_attempt_id=send_attempt_id,
        exact_html=html,
        gateway=gateway,
    )
    second = execute_admitted_send_attempt_once_for_validation(
        send_attempt_id=send_attempt_id,
        exact_html=html,
        gateway=gateway,
    )

    assert first.state == "unknown"
    assert second.state == "unknown"
    assert gateway.call_count == 1


def test_e04_definite_fake_pre_submit_failure_closes_failed_without_retry(admitted):
    html, send_attempt_id, _, _ = admitted
    gateway = FakeMailGateway(failure_calls={1})

    terminal = execute_admitted_send_attempt_once_for_validation(
        send_attempt_id=send_attempt_id,
        exact_html=html,
        gateway=gateway,
    )
    replay = execute_admitted_send_attempt_once_for_validation(
        send_attempt_id=send_attempt_id,
        exact_html=html,
        gateway=gateway,
    )

    assert terminal.state == "failed"
    assert replay.state == "failed"
    assert gateway.call_count == 1


def test_e05_missing_r4_admission_blocks_before_gateway_or_attempt_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "anxinboard.db"))
    init_db()
    gateway = FakeMailGateway()
    monkeypatch.setattr(
        execution_module,
        "get_durable_mail_admission",
        lambda _: (_ for _ in ()).throw(MailDurableAdmissionError("MAIL_ADMISSION_NOT_FOUND")),
    )

    with pytest.raises(MailAdmittedExecutionError) as caught:
        execute_admitted_send_attempt_once_for_validation(
            send_attempt_id="missing-admission",
            exact_html="<p>no</p>",
            gateway=gateway,
        )

    assert caught.value.code == "MAIL_EXECUTION_ADMISSION_INVALID"
    assert gateway.call_count == 0


def test_e06_wrong_html_fails_before_mailworkflow_claim_and_gateway(admitted):
    _, send_attempt_id, _, _ = admitted
    gateway = FakeMailGateway()

    with pytest.raises(MailAdmittedExecutionError) as caught:
        execute_admitted_send_attempt_once_for_validation(
            send_attempt_id=send_attempt_id,
            exact_html="<html><body>wrong body</body></html>",
            gateway=gateway,
        )

    assert caught.value.code == "MAIL_EXECUTION_HTML_IDENTITY_MISMATCH"
    assert get_send_attempt(send_attempt_id).state == "prepared"
    assert gateway.call_count == 0


def test_e07_attempt_identity_mismatch_is_rejected_before_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "anxinboard.db"))
    init_db()
    html = "<p>identity</p>"
    send_attempt_id = "mail-admitted-identity-mismatch"
    candidate = _candidate(html=html, send_attempt_id=send_attempt_id)
    admission = _admission(candidate, send_attempt_id)
    _prepare_attempt(candidate, send_attempt_id, subject="Different frozen subject")
    monkeypatch.setattr(execution_module, "get_durable_mail_admission", lambda _: admission)
    gateway = FakeMailGateway()

    with pytest.raises(MailAdmittedExecutionError) as caught:
        execute_admitted_send_attempt_once_for_validation(
            send_attempt_id=send_attempt_id,
            exact_html=html,
            gateway=gateway,
        )

    assert caught.value.code == "MAIL_EXECUTION_ADMISSION_ATTEMPT_MISMATCH"
    assert get_send_attempt(send_attempt_id).state == "prepared"
    assert gateway.call_count == 0


def test_e08_admitted_smtp_config_uses_exact_historical_profile_without_secret_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "anxinboard.db"))
    init_db()
    v1 = save_mail_transport_profile(
        host="smtp-one.example.test",
        port=587,
        security="starttls",
        username="mailer-one@example.test",
        from_identity="reports@example.test",
        secret_ref="smtp.admitted.v1",
        timeout_seconds=20.0,
        configured_by="Execution Test",
        expected_version_no=0,
    )
    save_mail_transport_profile(
        host="smtp-two.example.test",
        port=465,
        security="implicit_tls",
        username="mailer-two@example.test",
        from_identity="reports2@example.test",
        secret_ref="smtp.admitted.v2",
        timeout_seconds=25.0,
        configured_by="Execution Test",
        expected_version_no=1,
    )
    html = "<p>historical transport</p>"
    send_attempt_id = "mail-admitted-historical-transport"
    candidate = _candidate(
        html=html,
        send_attempt_id=send_attempt_id,
        transport_profile_id=v1.id,
        transport_profile_version_no=v1.version_no,
        transport_profile_hash=v1.profile_hash,
        secret_ref=v1.secret_ref,
        from_identity=v1.from_identity,
    )
    admission = _admission(candidate, send_attempt_id)
    monkeypatch.setattr(execution_module, "get_durable_mail_admission", lambda _: admission)

    config = get_admitted_smtp_gateway_config(send_attempt_id)

    assert config.host == "smtp-one.example.test"
    assert config.port == 587
    assert config.username == "mailer-one@example.test"
    assert config.secret_ref == "smtp.admitted.v1"
    assert config.timeout_seconds == 20.0


def test_e09_non_fake_gateway_is_mechanically_rejected_before_any_execution(admitted):
    html, send_attempt_id, _, _ = admitted

    class UnexpectedGateway:
        def send(self, message):  # pragma: no cover - must never be reached
            raise AssertionError("real/unclassified gateway must not be invoked")

    with pytest.raises(MailAdmittedExecutionError) as caught:
        execute_admitted_send_attempt_once_for_validation(
            send_attempt_id=send_attempt_id,
            exact_html=html,
            gateway=UnexpectedGateway(),  # type: ignore[arg-type]
        )

    assert caught.value.code == "MAIL_EXECUTION_REAL_GATEWAY_NOT_AUTHORIZED"
    assert get_send_attempt(send_attempt_id).state == "prepared"
