from __future__ import annotations

from pathlib import Path
import sys

from fastapi import HTTPException
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

import app.mail_settings_api as mail_settings_api  # noqa: E402
from app.db import init_db  # noqa: E402
from app.mail_settings_api import (  # noqa: E402
    MailTransportCredentialPayload,
    configure_mail_transport,
    get_mail_transport_settings,
)
from app.mail_transport_profile import get_current_mail_transport_profile  # noqa: E402
from app.secret_store import (  # noqa: E402
    InMemorySecretStore,
    SecretAccessDeniedError,
    SecretNotFoundError,
    SecretStoreOSError,
)


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
        "from_identity": "reports@example.test",
        "password": "test-password-not-real",
        "timeout_seconds": 30,
        "expected_version_no": 0,
    }
    values.update(overrides)
    return MailTransportCredentialPayload(**values)


def test_s01_get_settings_is_nonsecret_and_does_not_open_secret_store(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    called = {"factory": 0}

    def forbidden_factory():
        called["factory"] += 1
        raise AssertionError("GET must not open SecretStore")

    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", forbidden_factory)
    result = get_mail_transport_settings()

    assert result == {
        "schema_version": "mail_transport_settings_v1",
        "configured": False,
        "credential_reference_bound": False,
        "profile": None,
    }
    assert called["factory"] == 0


def test_s02_explicit_local_write_persists_unique_secret_ref_but_never_returns_secret(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    store = InMemorySecretStore()
    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", lambda: store)
    monkeypatch.setattr(mail_settings_api, "_secret_ref_factory", lambda: "smtp-password-test-1")

    result = configure_mail_transport(_payload(), object())
    profile = get_current_mail_transport_profile()

    assert result["configured"] is True
    assert result["credential_reference_bound"] is True
    assert "password" not in repr(result)
    assert "smtp-password-test-1" not in repr(result)
    assert profile is not None
    assert profile.secret_ref == "smtp-password-test-1"
    assert store.get(profile.secret_ref) == "test-password-not-real"


def test_s03_each_explicit_password_write_gets_new_credential_identity_and_profile_version(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    store = InMemorySecretStore()
    refs = iter(("smtp-password-test-1", "smtp-password-test-2"))
    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", lambda: store)
    monkeypatch.setattr(mail_settings_api, "_secret_ref_factory", lambda: next(refs))

    first = configure_mail_transport(_payload(), object())
    second = configure_mail_transport(
        _payload(password="rotated-test-password", expected_version_no=1),
        object(),
    )

    assert first["profile"]["version_no"] == 1
    assert second["profile"]["version_no"] == 2
    assert store.get("smtp-password-test-1") == "test-password-not-real"
    assert store.get("smtp-password-test-2") == "rotated-test-password"
    assert get_current_mail_transport_profile().secret_ref == "smtp-password-test-2"  # type: ignore[union-attr]


def test_s04_stale_expected_version_is_rejected_before_any_credential_write(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    store = InMemorySecretStore()
    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", lambda: store)
    monkeypatch.setattr(mail_settings_api, "_secret_ref_factory", lambda: "smtp-password-test-1")
    configure_mail_transport(_payload(), object())

    calls = {"factory": 0}

    def counting_factory():
        calls["factory"] += 1
        return store

    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", counting_factory)
    with pytest.raises(HTTPException) as caught:
        configure_mail_transport(_payload(expected_version_no=0), object())

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "MAIL_TRANSPORT_PROFILE_VERSION_CONFLICT"
    assert calls["factory"] == 0


def test_s05_invalid_profile_after_secret_write_deletes_new_secret_and_keeps_no_profile(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    store = InMemorySecretStore()
    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", lambda: store)
    monkeypatch.setattr(mail_settings_api, "_secret_ref_factory", lambda: "smtp-password-test-invalid")

    with pytest.raises(HTTPException) as caught:
        configure_mail_transport(_payload(from_identity="not-an-address"), object())

    assert caught.value.status_code == 400
    with pytest.raises(SecretNotFoundError):
        store.get("smtp-password-test-invalid")
    assert get_current_mail_transport_profile() is None


def test_s06_secret_store_failure_cannot_create_transport_profile(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    class DeniedStore:
        def put(self, _ref: str, _secret: str) -> None:
            raise SecretAccessDeniedError()

        def get(self, _ref: str) -> str:
            raise AssertionError

        def delete(self, _ref: str) -> None:
            raise AssertionError

    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", DeniedStore)
    monkeypatch.setattr(mail_settings_api, "_secret_ref_factory", lambda: "smtp-password-denied")

    with pytest.raises(HTTPException) as caught:
        configure_mail_transport(_payload(), object())

    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "MAIL_CREDENTIAL_ACCESS_DENIED"
    assert get_current_mail_transport_profile() is None


def test_s07_failed_profile_and_failed_secret_cleanup_reports_hard_failure_without_profile(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    class CleanupFailStore(InMemorySecretStore):
        def delete(self, ref: str) -> None:
            raise SecretStoreOSError()

    store = CleanupFailStore()
    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", lambda: store)
    monkeypatch.setattr(mail_settings_api, "_secret_ref_factory", lambda: "smtp-password-orphan")

    with pytest.raises(HTTPException) as caught:
        configure_mail_transport(_payload(from_identity="not-an-address"), object())

    assert caught.value.status_code == 500
    assert caught.value.detail["code"] == "MAIL_CREDENTIAL_ROLLBACK_FAILED"
    assert get_current_mail_transport_profile() is None
    assert store.get("smtp-password-orphan") == "test-password-not-real"


def test_s08_local_session_guard_runs_before_secret_store_factory(
    settings_db: Path, monkeypatch: pytest.MonkeyPatch
):
    called = {"factory": 0}

    def reject_guard(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail={"code": "LOCAL_SESSION_TEST_REJECTED"})

    def forbidden_factory():
        called["factory"] += 1
        raise AssertionError("guard failure must stop before credential access")

    monkeypatch.setattr(mail_settings_api, "require_local_write_request", reject_guard)
    monkeypatch.setattr(mail_settings_api, "_secret_store_factory", forbidden_factory)

    with pytest.raises(HTTPException) as caught:
        configure_mail_transport(_payload(), object())

    assert caught.value.status_code == 403
    assert called["factory"] == 0
