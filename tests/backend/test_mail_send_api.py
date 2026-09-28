from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import app.mail_readiness_api as api
from app.mail_send_service import MailSendServiceError


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(api, "require_local_write_request", lambda request, require_idempotency_key: None)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def _body(**overrides):
    body = {
        "expected_report_version_id": 19,
        "expected_recipient_config_version_no": 1,
        "confirmed_timezone": "America/New_York",
        "confirmed_utc_offset_minutes": -240,
        "human_confirmed": True,
    }
    body.update(overrides)
    return body


def test_send_route_requires_local_idempotency_key_even_after_session_guard(client):
    response = client.post("/api/projects/1/mail-send", json=_body())

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MAIL_SEND_IDEMPOTENCY_REQUIRED"


@pytest.mark.parametrize('module_hash', [None, '7' * 64])
def test_send_route_passes_exact_human_confirmed_identity_to_service(client, monkeypatch, module_hash):
    captured = {}
    terminal = object()

    def send_once(**kwargs):
        captured.update(kwargs)
        return terminal

    monkeypatch.setattr(api, "send_current_approved_report_once", send_once)
    monkeypatch.setattr(
        api,
        "safe_send_attempt_response",
        lambda attempt: {
            "schema_version": "mail_send_action_v1",
            "state": "sent",
            "terminal": True,
        },
    )

    response = client.post(
        "/api/projects/1/mail-send",
        json=_body(expected_module_narrative_hash=module_hash),
        headers={"Local-Idempotency-Key": "page08-send-api-test"},
    )

    assert response.status_code == 200
    assert response.json()["state"] == "sent"
    assert captured == {
        "project_id": 1,
        "expected_report_version_id": 19,
        "expected_recipient_config_version_no": 1,
        "confirmed_timezone": "America/New_York",
        "confirmed_utc_offset_minutes": -240,
        "human_confirmed": True,
        "idempotency_key": "page08-send-api-test",
        "expected_module_narrative_hash": module_hash,
    }


def test_send_route_maps_stale_report_to_conflict_without_retry_claim(client, monkeypatch):
    monkeypatch.setattr(
        api,
        "send_current_approved_report_once",
        lambda **kwargs: (_ for _ in ()).throw(MailSendServiceError("MAIL_SEND_REPORT_STALE")),
    )

    response = client.post(
        "/api/projects/1/mail-send",
        json=_body(),
        headers={"Local-Idempotency-Key": "page08-send-stale-test"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MAIL_SEND_REPORT_STALE"
    assert "没有重复发送" in response.json()["detail"]["message"]


def test_send_route_maps_missing_human_confirmation_to_bad_request(client, monkeypatch):
    monkeypatch.setattr(
        api,
        "send_current_approved_report_once",
        lambda **kwargs: (_ for _ in ()).throw(
            MailSendServiceError("MAIL_SEND_HUMAN_CONFIRMATION_REQUIRED")
        ),
    )

    response = client.post(
        "/api/projects/1/mail-send",
        json=_body(human_confirmed=False),
        headers={"Local-Idempotency-Key": "page08-send-no-human"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MAIL_SEND_HUMAN_CONFIRMATION_REQUIRED"
