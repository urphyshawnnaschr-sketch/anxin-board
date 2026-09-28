"""Settings must only expose models the report chain is qualified to use.

Even if /models lists additional models, the operator may only select a model
that the frozen report qualification chain can actually send with. Otherwise a
selectable-but-unusable model reintroduces the 'check your settings' dead end.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import model_settings_api  # noqa: E402
from app.model_settings_api import (  # noqa: E402
    DeepSeekModelSelectionPayload,
    select_deepseek_model,
    test_deepseek_connection as run_connection_test,
)
from app.secret_store import InMemorySecretStore  # noqa: E402


@pytest.fixture(autouse=True)
def local_write_bypass(monkeypatch: pytest.MonkeyPatch):
    store = InMemorySecretStore()
    monkeypatch.setattr(model_settings_api, "require_local_write_request", lambda *_a, **_k: None)
    monkeypatch.setattr(model_settings_api, "_secret_store_factory", lambda: store)
    return store


def test_connection_test_only_offers_report_qualified_models(
    local_write_bypass: InMemorySecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_write_bypass.put("deepseek-api-key", "secret")
    monkeypatch.setattr(
        model_settings_api,
        "list_deepseek_models_with_api_key",
        lambda key: ("deepseek-flash", "deepseek-v4-pro") if key == "secret" else (),
    )
    result = run_connection_test(object())
    assert result["available_models"] == ["deepseek-flash"]


def test_connection_test_reports_when_no_qualified_model_available(
    local_write_bypass: InMemorySecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_write_bypass.put("deepseek-api-key", "secret")
    monkeypatch.setattr(
        model_settings_api,
        "list_deepseek_models_with_api_key",
        lambda _key: ("deepseek-v4-pro",),
    )
    result = run_connection_test(object())
    assert result["available_models"] == []
    assert result["selected_model"] is None


def test_selection_of_report_unqualified_model_is_rejected(
    local_write_bypass: InMemorySecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_write_bypass.put("deepseek-api-key", "secret")
    monkeypatch.setattr(
        model_settings_api,
        "list_deepseek_models_with_api_key",
        lambda _key: ("deepseek-flash", "deepseek-v4-pro"),
    )
    with pytest.raises(HTTPException) as exc:
        select_deepseek_model(DeepSeekModelSelectionPayload(model_id="deepseek-v4-pro"), object())
    assert exc.value.detail["code"] == "MODEL_SELECTION_NOT_QUALIFIED"
    assert "deepseek-v4-pro" not in str(exc.value.detail.get("api_key", ""))


def test_selection_of_qualified_model_persists(
    local_write_bypass: InMemorySecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_write_bypass.put("deepseek-api-key", "secret")
    monkeypatch.setattr(
        model_settings_api,
        "list_deepseek_models_with_api_key",
        lambda _key: ("deepseek-flash", "deepseek-v4-pro"),
    )
    result = select_deepseek_model(DeepSeekModelSelectionPayload(model_id="deepseek-flash"), object())
    assert result["selected_model"] == "deepseek-flash"
    assert local_write_bypass.get("deepseek-model-id") == "deepseek-flash"
