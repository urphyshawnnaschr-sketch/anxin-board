from __future__ import annotations

from pathlib import Path
import sys

from fastapi import HTTPException
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

import app.model_settings_api as model_settings_api  # noqa: E402
from app.model_settings_api import (  # noqa: E402
    DeepSeekCredentialPayload,
    DeepSeekModelSelectionPayload,
    configure_deepseek_credential,
    get_deepseek_status,
    select_deepseek_model,
    test_deepseek_connection as run_connection_test,
)
from app.secret_store import InMemorySecretStore, SecretNotFoundError  # noqa: E402


def _payload(value: str = "test-only-deepseek-api-key") -> DeepSeekCredentialPayload:
    return DeepSeekCredentialPayload(api_key=value)


def _install_test_store(monkeypatch: pytest.MonkeyPatch) -> InMemorySecretStore:
    store = InMemorySecretStore()
    monkeypatch.setattr(model_settings_api, "require_local_write_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(model_settings_api, "_secret_store_factory", lambda: store)
    return store


def test_explicit_local_write_persists_secret_without_returning_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _install_test_store(monkeypatch)
    store.put("deepseek-model-id", "deepseek-stale")

    result = configure_deepseek_credential(_payload(), object())

    assert result == {
        "provider": "deepseek",
        "credential_ref": "deepseek-api-key",
        "configured": True,
        "selected_model": None,
        "connected": False,
    }
    assert "test-only-deepseek-api-key" not in repr(result)
    assert store.get("deepseek-api-key") == "test-only-deepseek-api-key"
    with pytest.raises(SecretNotFoundError):
        store.get("deepseek-model-id")


def test_status_never_claims_saved_key_is_live_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _install_test_store(monkeypatch)
    store.put("deepseek-api-key", "secret")
    store.put("deepseek-model-id", "deepseek-flash")

    result = get_deepseek_status()

    assert result == {
        "provider": "deepseek",
        "configured": True,
        "selected_model": "deepseek-flash",
        "connected": False,
    }
    assert "secret" not in repr(result)


def test_connection_test_returns_live_models_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _install_test_store(monkeypatch)
    store.put("deepseek-api-key", "secret")
    monkeypatch.setattr(
        model_settings_api,
        "list_deepseek_models_with_api_key",
        lambda key: ("deepseek-flash", "deepseek-v4-pro") if key == "secret" else (),
    )

    result = run_connection_test(object())

    assert result == {
        "provider": "deepseek",
        "configured": True,
        "connected": True,
        "available_models": ["deepseek-flash"],
        "selected_model": None,
    }
    assert "secret" not in repr(result)


def test_model_selection_requires_fresh_live_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _install_test_store(monkeypatch)
    store.put("deepseek-api-key", "secret")
    monkeypatch.setattr(
        model_settings_api,
        "list_deepseek_models_with_api_key",
        lambda _key: ("deepseek-flash", "deepseek-v4-pro"),
    )

    result = select_deepseek_model(DeepSeekModelSelectionPayload(model_id="deepseek-flash"), object())

    assert result["selected_model"] == "deepseek-flash"
    assert result["connected"] is True
    assert store.get("deepseek-model-id") == "deepseek-flash"

    with pytest.raises(HTTPException) as caught:
        select_deepseek_model(DeepSeekModelSelectionPayload(model_id="deepseek-v4-pro"), object())
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "MODEL_SELECTION_NOT_QUALIFIED"
    assert store.get("deepseek-model-id") == "deepseek-flash"


def test_local_session_guard_rejects_before_secret_store_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"factory": 0}

    def reject_guard(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail={"code": "LOCAL_SESSION_TEST_REJECTED"})

    def forbidden_factory():
        called["factory"] += 1
        raise AssertionError("credential store must not be opened before local Human gate")

    monkeypatch.setattr(model_settings_api, "require_local_write_request", reject_guard)
    monkeypatch.setattr(model_settings_api, "_secret_store_factory", forbidden_factory)

    with pytest.raises(HTTPException) as caught:
        configure_deepseek_credential(_payload(), object())

    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "LOCAL_SESSION_TEST_REJECTED"
    assert called["factory"] == 0
