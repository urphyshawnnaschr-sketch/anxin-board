from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

import app.mail_settings_api as mail_settings_api  # noqa: E402
from app.db import init_db  # noqa: E402
from app.mail_settings_api import (  # noqa: E402
    MailTransportCredentialPayload,
    configure_mail_transport,
    test_mail_transport_connection as run_mail_transport_connection_test,
)
from app.secret_store import InMemorySecretStore  # noqa: E402
from app.smtp_connection_test import SmtpConnectionTestError  # noqa: E402


@pytest.fixture()
def settings_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "anxinboard.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(path))
    init_db()
    monkeypatch.setattr(mail_settings_api, "require_local_write_request", lambda *_args, **_kwargs: None)
    return path


def _payload(**overrides) -> MailTransportCredentialPayload:
    values = {
        "host": "smtp.example.test",
        "port": 587,
        "security": "starttls",
        "username": "mailer@example.test",
        "from_identity": "mailer@example.test",
        "password": "test-password-not-real",
        "timeout_seconds": 30,
        "expected_version_no": 0,
    }
    values.update(overrides)
    return MailTransportCredentialPayload(**values)


def _configured_store(monkeypatch: pytest.MonkeyPatch) -> InMemorySecretStore:
    store = InMemorySecretStore()
    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", lambda: store)
    monkeypatch.setattr(mail_settings_api, "_secret_ref_factory", lambda: "smtp-password-test")
    configure_mail_transport(_payload(), object())
    return store


def test_t01_connection_test_requires_saved_profile_before_secret_access(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    called = {"store": 0, "preflight": 0}

    def store_factory():
        called["store"] += 1
        raise AssertionError("SecretStore must not open without a profile")

    def preflight(**_kwargs):
        called["preflight"] += 1
        raise AssertionError("preflight must not run without a profile")

    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", store_factory)
    monkeypatch.setattr(mail_settings_api, "_smtp_connection_test_factory", preflight)

    with pytest.raises(HTTPException) as caught:
        run_mail_transport_connection_test(object(), expected_version_no=1)

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "MAIL_TRANSPORT_NOT_CONFIGURED"
    assert called == {"store": 0, "preflight": 0}


def test_t02_success_uses_current_exact_profile_and_returns_only_nonsecret_checks(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    store = _configured_store(monkeypatch)
    captured = {}

    def preflight(*, config, secret_store):
        captured["config"] = config
        captured["store"] = secret_store
        return SimpleNamespace(
            credential_read=True,
            smtp_connect=True,
            tls_ready=True,
            smtp_auth=True,
            post_auth_noop=True,
        )

    monkeypatch.setattr(mail_settings_api, "_smtp_connection_test_factory", preflight)
    result = run_mail_transport_connection_test(object(), expected_version_no=1)

    assert captured["store"] is store
    assert captured["config"].host == "smtp.example.test"
    assert captured["config"].secret_ref == "smtp-password-test"
    assert result == {
        "schema_version": "mail_transport_connection_test_v1",
        "status": "passed",
        "profile_version": 1,
        "checks": {
            "credential_read": True,
            "smtp_connect": True,
            "tls_ready": True,
            "smtp_auth": True,
            "post_auth_noop": True,
            "message_submission": False,
        },
    }
    rendered = repr(result)
    assert "test-password-not-real" not in rendered
    assert "smtp-password-test" not in rendered


def test_t03_auth_failure_is_human_readable_without_secret_material(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    _configured_store(monkeypatch)

    def preflight(**_kwargs):
        raise SmtpConnectionTestError("MAIL_SMTP_TEST_AUTH_FAILED")

    monkeypatch.setattr(mail_settings_api, "_smtp_connection_test_factory", preflight)

    with pytest.raises(HTTPException) as caught:
        run_mail_transport_connection_test(object(), expected_version_no=1)

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "MAIL_SMTP_TEST_AUTH_FAILED"
    assert "授权码" in caught.value.detail["message"]
    assert "test-password-not-real" not in repr(caught.value.detail)


def test_t04_local_session_guard_stops_before_profile_or_secret_access(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    called = {"store": 0}

    def reject_guard(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail={"code": "LOCAL_SESSION_TEST_REJECTED"})

    def store_factory():
        called["store"] += 1
        raise AssertionError("guard failure must stop before SecretStore")

    monkeypatch.setattr(mail_settings_api, "require_local_write_request", reject_guard)
    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", store_factory)

    with pytest.raises(HTTPException) as caught:
        run_mail_transport_connection_test(object(), expected_version_no=1)

    assert caught.value.status_code == 403
    assert called["store"] == 0


def test_t05_stale_viewed_profile_version_fails_before_secret_or_network_access(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    store = _configured_store(monkeypatch)
    monkeypatch.setattr(mail_settings_api, "_secret_ref_factory", lambda: "smtp-password-test-2")
    configure_mail_transport(
        _payload(password="rotated-test-password", expected_version_no=1),
        object(),
    )

    calls = {"store": 0, "preflight": 0}

    def counting_store_factory():
        calls["store"] += 1
        return store

    def forbidden_preflight(**_kwargs):
        calls["preflight"] += 1
        raise AssertionError("stale UI version must stop before network preflight")

    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", counting_store_factory)
    monkeypatch.setattr(mail_settings_api, "_smtp_connection_test_factory", forbidden_preflight)

    with pytest.raises(HTTPException) as caught:
        run_mail_transport_connection_test(object(), expected_version_no=1)

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "MAIL_TRANSPORT_PROFILE_VERSION_CONFLICT"
    assert calls == {"store": 0, "preflight": 0}


def test_t06_invalid_test_version_fails_before_profile_or_secret_access(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = {"store": 0}

    def forbidden_store_factory():
        calls["store"] += 1
        raise AssertionError("invalid version must stop before SecretStore")

    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", forbidden_store_factory)

    with pytest.raises(HTTPException) as caught:
        run_mail_transport_connection_test(object(), expected_version_no=0)

    assert caught.value.status_code == 400
    assert caught.value.detail["code"] == "MAIL_TRANSPORT_TEST_VERSION_INVALID"
    assert calls["store"] == 0


def test_t07_saved_outlook_transport_fails_before_secret_store_open_or_preflight(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    setup_store = InMemorySecretStore()
    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", lambda: setup_store)
    monkeypatch.setattr(mail_settings_api, "_secret_ref_factory", lambda: "smtp-password-outlook-test")
    configure_mail_transport(
        _payload(
            host="SMTP-MAIL.OUTLOOK.COM",
            port=587,
            security="starttls",
        ),
        object(),
    )

    calls = {"store": 0, "preflight": 0}

    def forbidden_store_factory():
        calls["store"] += 1
        raise AssertionError("blocked transport must fail before SecretStore construction")

    def forbidden_preflight(**_kwargs):
        calls["preflight"] += 1
        raise AssertionError("blocked transport must fail before SMTP preflight")

    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", forbidden_store_factory)
    monkeypatch.setattr(mail_settings_api, "_smtp_connection_test_factory", forbidden_preflight)

    with pytest.raises(HTTPException) as caught:
        run_mail_transport_connection_test(object(), expected_version_no=1)

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "MAIL_SMTP_TEST_TRANSPORT_UNSUPPORTED"
    assert "未支持的认证方式" in caught.value.detail["message"]
    assert calls == {"store": 0, "preflight": 0}

@pytest.mark.parametrize('code,phrase', [
    ('MAIL_SMTP_TEST_AUTH_CONNECTION_LOST', '连接中断'),
    ('MAIL_SMTP_TEST_AUTH_TIMEOUT', '超时'),
    ('MAIL_SMTP_TEST_AUTH_UNSUPPORTED', '认证方式'),
])
def test_safe_actionable_auth_messages(code, phrase):
    with pytest.raises(HTTPException) as caught:
        mail_settings_api._raise_connection_test(SmtpConnectionTestError(code))
    assert caught.value.status_code == 502
    assert phrase in caught.value.detail['message']
    assert '未发送邮件' in caught.value.detail['message']
