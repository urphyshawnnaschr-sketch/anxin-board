"""Report credential binding, using fake store and fake transport only."""
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/backend"))
from app import deepseek_credential as credential
from app import deepseek_current_authority as authority
from app import deepseek_transport as transport
from app.secret_store import InMemorySecretStore, SecretAccessDeniedError


@pytest.fixture
def store(monkeypatch):
    value = InMemorySecretStore()
    monkeypatch.setattr(credential, "_secret_store_factory", lambda: value)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    return value


def test_installed_authority_and_transport_use_same_settings_key(store, monkeypatch):
    store.put("deepseek-api-key", "fake-store-only-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unrelated-inherited-key")
    seen = []
    def fetch(**kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer fake-store-only-key"
        seen.append("authority")
        return b"models"
    def send(**kwargs):
        assert kwargs["api_key"] == "fake-store-only-key"
        seen.append("transport")
        return {"fake": True}
    monkeypatch.setattr(authority, "_read_bounded_source", fetch)
    monkeypatch.setattr(transport, "_send_with_key", send)
    assert authority._fetch_models_source() == b"models"
    assert transport.send_deepseek_v4_flash(messages=[{"role": "user", "content": "hello"}], max_tokens=1) == {"fake": True}
    assert seen == ["authority", "transport"]
    assert credential.os.environ["DEEPSEEK_API_KEY"] == "unrelated-inherited-key"


def test_unpacked_cli_explicit_environment_still_supported(store, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-cli-key")
    monkeypatch.setattr(credential, "_secret_store_factory", lambda: pytest.fail("explicit CLI does not read store"))
    assert credential.read_report_credential() == "fake-cli-key"


def test_development_ui_without_environment_uses_settings_store(store, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False)
    store.put("deepseek-api-key", "fake-ui-key")
    assert credential.read_report_credential() == "fake-ui-key"
    assert "DEEPSEEK_API_KEY" not in credential.os.environ


@pytest.mark.parametrize("failure, expected", [(None, "SECRET_NOT_FOUND"), (SecretAccessDeniedError(), "SECRET_ACCESS_DENIED"), (RuntimeError("secret-must-not-leak"), "SECRET_STORE_OS_FAILURE")])
def test_failure_has_safe_reason_and_zero_network(store, monkeypatch, failure, expected):
    if failure is not None:
        def fail():
            raise failure
        monkeypatch.setattr(credential, "_secret_store_factory", fail)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-fallback")
    monkeypatch.setattr(authority, "_read_bounded_source", lambda **_: pytest.fail("no network"))
    monkeypatch.setattr(transport, "_send_with_key", lambda **_: pytest.fail("no provider"))
    for operation in [authority._fetch_models_source, lambda: transport.send_deepseek_v4_flash(messages=[{"role":"user", "content":"hello"}], max_tokens=1)]:
        with pytest.raises(HTTPException) as caught:
            operation()
        assert caught.value.detail["credential_error_code"] == expected
        assert "secret-must-not-leak" not in str(caught.value.detail)
        assert "must-not-fallback" not in str(caught.value.detail)
