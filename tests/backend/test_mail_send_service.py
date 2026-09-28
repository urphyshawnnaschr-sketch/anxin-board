from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.mail_send_service as service
from app.mail_durable_admission import MailDurableAdmissionError
from app.mail_send_service import MailSendServiceError, send_current_approved_report_once


REPORT = {
    "schema_version": "anxin_board_report_v3",
    "report_version_id": 19,
    "report_content_hash": "a" * 64,
    "profile_id": 31,
}
RECIPIENT = {
    "id": 7,
    "version_no": 1,
    "recipients_hash": "b" * 64,
    "to_recipients": ["recipient@example.test"],
}
BINDING = {
    "recipient_config_version_id": 7,
    "recipient_config_version_no": 1,
    "recipients_hash": "b" * 64,
}
CANDIDATE = SimpleNamespace(
    report_version_id=19,
    report_content_hash="a" * 64,
    render_identity="mail_report_render_v2:" + "c" * 64,
    render_hash="d" * 64,
    html_sha256="e" * 64,
    subject="项目｜2026-09-20 安心看板",
)
ATTEMPT = SimpleNamespace(send_attempt_id="mail-admission-test")
RENDER = SimpleNamespace(
    render_identity=CANDIDATE.render_identity,
    render_hash=CANDIDATE.render_hash,
    html_sha256=CANDIDATE.html_sha256,
    subject=CANDIDATE.subject,
    html_body="<html><body>approved</body></html>",
)
TERMINAL_SENT = SimpleNamespace(is_terminal=True, state="sent")
TERMINAL_UNKNOWN = SimpleNamespace(is_terminal=True, state="unknown")


def _call(**overrides):
    payload = {
        "project_id": 1,
        "expected_report_version_id": 19,
        "expected_recipient_config_version_no": 1,
        "confirmed_timezone": "America/New_York",
        "confirmed_utc_offset_minutes": -240,
        "human_confirmed": True,
        "idempotency_key": "mail-send-test-1",
    }
    payload.update(overrides)
    return send_current_approved_report_once(**payload)


def _wire_happy(monkeypatch: pytest.MonkeyPatch, *, terminal=TERMINAL_SENT):
    calls: list[str] = []
    monkeypatch.setattr(service, 'current_document_predecessor', lambda *args: None)
    monkeypatch.setattr(service, "load_latest_anxin_board_report", lambda *, project_id: dict(REPORT))
    monkeypatch.setattr(service, "get_current_recipient_config", lambda project_id: dict(RECIPIENT))
    monkeypatch.setattr(service, "get_approval_recipient_binding", lambda **kwargs: None)

    def create_binding(**kwargs):
        calls.append("binding")
        assert kwargs["human_confirmed"] is True
        assert kwargs["expected_recipient_config_version_id"] == 7
        assert kwargs["expected_recipient_config_version_no"] == 1
        return dict(BINDING)

    monkeypatch.setattr(service, "create_approval_recipient_binding", create_binding)
    monkeypatch.setattr(
        service,
        "get_mail_readiness",
        lambda project_id: {
            "candidate_ready": True,
            "candidate": {"admission_hash": "f" * 64},
        },
    )
    monkeypatch.setattr(
        service,
        "prepare_durable_mail_admission",
        lambda **kwargs: (SimpleNamespace(candidate=CANDIDATE), ATTEMPT),
    )
    monkeypatch.setattr(service, "read_bound_project_profile_for_report", lambda profile_id: {"id": profile_id})
    monkeypatch.setattr(
        service,
        "get_report_approval_snapshot",
        lambda **kwargs: {"approval_snapshot_id": 11},
    )
    narrative = {"approved-content": "exact-report"}
    monkeypatch.setattr(service, "load_approved_report_narrative", lambda **kwargs: narrative)
    module_narrative = {'synthetic-confirmed-module-note': 'exact-bound-source'}
    monkeypatch.setattr(service, 'get_approved_module_narrative', lambda **kwargs: module_narrative)
    git_metrics = {'synthetic-git-metrics': 'exact-frozen-snapshot'}
    monkeypatch.setattr(service, 'load_approved_report_git_metrics', lambda *args, **kwargs: git_metrics)
    def render(*args, **kwargs):
        assert kwargs["approved_narrative"] is narrative
        assert kwargs['approved_module_narrative'] is module_narrative
        assert kwargs['approved_git_metrics'] is git_metrics
        return RENDER
    monkeypatch.setattr(service, "render_approved_report_for_mail", render)
    monkeypatch.setattr(service, "get_admitted_smtp_gateway_config", lambda attempt_id: object())

    store = object()
    gateway = object()

    def store_factory():
        calls.append("credential-store")
        return store

    def gateway_factory(config, actual_store):
        calls.append("gateway")
        assert actual_store is store
        return gateway

    def execute(**kwargs):
        calls.append("execute")
        assert kwargs["gateway"] is gateway
        assert kwargs["exact_html"] == RENDER.html_body
        return terminal

    monkeypatch.setattr(service, "execute_send_attempt_once", execute)
    return calls, store_factory, gateway_factory


@pytest.mark.parametrize('expected', [None, '0' * 64])
def test_module_explanation_changed_since_display_blocks_before_attempt_or_gateway(monkeypatch, expected):
    calls, store_factory, gateway_factory = _wire_happy(monkeypatch)
    revised = SimpleNamespace(**{**vars(RENDER), 'render_identity': 'mail_report_render_v11:'+'c'*64+':'+'7'*64})
    monkeypatch.setattr(service, 'render_approved_report_for_mail', lambda *args, **kwargs: revised)
    monkeypatch.setattr(service, 'prepare_durable_mail_admission', lambda **kwargs: pytest.fail('stale display created mail attempt'))
    with pytest.raises(MailSendServiceError) as caught:
        _call(expected_module_narrative_hash=expected, secret_store_factory=store_factory, gateway_factory=gateway_factory)
    assert caught.value.code == 'MAIL_SEND_MODULE_NARRATIVE_STALE'
    assert 'execute' not in calls and 'credential-store' not in calls


def test_v11_matching_displayed_module_hash_admits_exact_document(monkeypatch):
    calls, store_factory, gateway_factory = _wire_happy(monkeypatch)
    identity = 'mail_report_render_v11:' + 'c' * 64 + ':' + '7' * 64
    revised = SimpleNamespace(**{**vars(RENDER), 'render_identity': identity})
    candidate = SimpleNamespace(**{**vars(CANDIDATE), 'render_identity': identity})
    monkeypatch.setattr(service, 'render_approved_report_for_mail', lambda *args, **kwargs: revised)
    monkeypatch.setattr(service, 'prepare_durable_mail_admission', lambda **kwargs: (SimpleNamespace(candidate=candidate), ATTEMPT))
    terminal = _call(expected_module_narrative_hash='7' * 64,
                     secret_store_factory=store_factory, gateway_factory=gateway_factory)
    assert terminal.state == 'sent'
    assert calls == ['binding', 'credential-store', 'gateway', 'execute']


def test_human_confirmation_is_required_before_any_report_or_gateway_access(monkeypatch):
    accessed: list[str] = []
    monkeypatch.setattr(
        service,
        "load_latest_anxin_board_report",
        lambda **kwargs: accessed.append("report"),
    )

    with pytest.raises(MailSendServiceError) as caught:
        _call(human_confirmed=False)

    assert caught.value.code == "MAIL_SEND_HUMAN_CONFIRMATION_REQUIRED"
    assert accessed == []


def test_stale_report_version_blocks_before_recipient_binding_or_gateway(monkeypatch):
    monkeypatch.setattr(
        service,
        "load_latest_anxin_board_report",
        lambda *, project_id: {**REPORT, "report_version_id": 20},
    )
    touched: list[str] = []
    monkeypatch.setattr(service, "get_current_recipient_config", lambda project_id: touched.append("recipient"))

    with pytest.raises(MailSendServiceError) as caught:
        _call()

    assert caught.value.code == "MAIL_SEND_REPORT_STALE"
    assert touched == []


def test_existing_binding_to_different_recipient_version_fails_closed(monkeypatch):
    monkeypatch.setattr(service, "load_latest_anxin_board_report", lambda *, project_id: dict(REPORT))
    monkeypatch.setattr(service, "get_current_recipient_config", lambda project_id: dict(RECIPIENT))
    monkeypatch.setattr(
        service,
        "get_approval_recipient_binding",
        lambda **kwargs: {**BINDING, "recipient_config_version_no": 2},
    )
    touched: list[str] = []
    monkeypatch.setattr(service, "get_mail_readiness", lambda project_id: touched.append("readiness"))

    with pytest.raises(MailSendServiceError) as caught:
        _call()

    assert caught.value.code == "MAIL_SEND_REPORT_BOUND_TO_DIFFERENT_RECIPIENTS"
    assert touched == []


def test_happy_path_binds_then_admits_then_reads_credential_and_invokes_gateway_once(monkeypatch):
    calls, store_factory, gateway_factory = _wire_happy(monkeypatch)

    terminal = _call(secret_store_factory=store_factory, gateway_factory=gateway_factory)

    assert terminal.state == "sent"
    assert calls == ["binding", "credential-store", "gateway", "execute"]


def test_render_identity_mismatch_blocks_before_secret_read_or_gateway(monkeypatch):
    calls, store_factory, gateway_factory = _wire_happy(monkeypatch)
    monkeypatch.setattr(
        service,
        "render_approved_report_for_mail",
        lambda *args, **kwargs: SimpleNamespace(
            **{**RENDER.__dict__, "render_hash": "0" * 64}
        ),
    )

    with pytest.raises(MailSendServiceError) as caught:
        _call(secret_store_factory=store_factory, gateway_factory=gateway_factory)

    assert caught.value.code == "MAIL_SEND_RENDER_IDENTITY_MISMATCH"
    assert calls == ["binding"]


def test_unknown_is_terminal_and_second_action_cannot_reinvoke_gateway(monkeypatch):
    calls, store_factory, gateway_factory = _wire_happy(monkeypatch, terminal=TERMINAL_UNKNOWN)

    first = _call(secret_store_factory=store_factory, gateway_factory=gateway_factory)
    assert first.state == "unknown"
    assert calls.count("execute") == 1


    # The durable admission owner rejects replay once the deterministic SendAttempt is
    # no longer prepared.  This mirrors the real lower-level repository semantics.
    monkeypatch.setattr(service, "get_approval_recipient_binding", lambda **kwargs: dict(BINDING))
    monkeypatch.setattr(
        service,
        "prepare_durable_mail_admission",
        lambda **kwargs: (_ for _ in ()).throw(
            MailDurableAdmissionError("MAIL_ADMISSION_SEND_ATTEMPT_NOT_PREPARED")
        ),
    )

    with pytest.raises(MailSendServiceError) as caught:
        _call(
            idempotency_key="mail-send-test-2",
            secret_store_factory=store_factory,
            gateway_factory=gateway_factory,
        )

    assert caught.value.code == "MAIL_ADMISSION_SEND_ATTEMPT_NOT_PREPARED"
    assert calls.count("execute") == 1


def test_missing_approved_work_content_blocks_before_credentials_and_send(monkeypatch):
    calls, store_factory, gateway_factory = _wire_happy(monkeypatch)
    def missing(**kwargs):
        raise service.ApprovedReportNarrativeError()
    monkeypatch.setattr(service, "load_approved_report_narrative", missing)
    with pytest.raises(MailSendServiceError) as caught:
        _call(secret_store_factory=store_factory, gateway_factory=gateway_factory)
    assert caught.value.code == "MAIL_SEND_NARRATIVE_INVALID"
    assert calls == ["binding"]


def test_invalid_confirmed_module_content_blocks_before_credentials_and_send(monkeypatch):
    calls, store_factory, gateway_factory = _wire_happy(monkeypatch)
    def invalid(**kwargs):
        raise service.ApprovedModuleNarrativeError()
    monkeypatch.setattr(service, 'get_approved_module_narrative', invalid)
    with pytest.raises(MailSendServiceError) as caught:
        _call(secret_store_factory=store_factory, gateway_factory=gateway_factory)
    assert caught.value.code == 'MAIL_SEND_NARRATIVE_INVALID'
    assert calls == ['binding']
