from __future__ import annotations

from pathlib import Path
import sys

from fastapi import HTTPException
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

import app.deepseek_live_profile_adapter as live  # noqa: E402
import app.deepseek_profile_transport_policy as profile_policy  # noqa: E402
from app.model_provider_contract import ProviderCredentialRequest, ProviderReceipt  # noqa: E402
from app.secret_store import InMemorySecretStore  # noqa: E402


class _ClientContext:
    def __init__(self, response: httpx.Response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, *_args, **_kwargs):
        return self.response


def _response(status: int, payload: str) -> httpx.Response:
    return httpx.Response(status, content=payload.encode("utf-8"), request=httpx.Request("GET", live.MODELS_URL))


def test_live_models_are_read_from_provider_response(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _response(
        200,
        '{"object":"list","data":[{"id":"deepseek-flash"},{"id":"deepseek-v4-pro"}]}'
    )
    monkeypatch.setattr(live, "_client", lambda: _ClientContext(response))

    assert live.list_deepseek_models_with_api_key("secret") == (
        "deepseek-flash",
        "deepseek-v4-pro",
    )


def test_connection_test_maps_auth_and_rate_limit_without_secret_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    for status, code in ((401, "MODEL_CONNECTION_AUTH_FAILED"), (429, "MODEL_CONNECTION_RATE_LIMITED")):
        monkeypatch.setattr(live, "_client", lambda status=status: _ClientContext(_response(status, "{}")))
        with pytest.raises(HTTPException) as caught:
            live.list_deepseek_models_with_api_key("super-secret-value")
        assert caught.value.detail["code"] == code
        assert "super-secret-value" not in str(caught.value.detail)


def test_profile_capability_uses_exact_selected_live_model() -> None:
    store = InMemorySecretStore()
    store.put(live.MODEL_SELECTION_REF, "deepseek-v4-pro")
    adapter = live.LiveDeepSeekProfileAdapter(store_factory=lambda: store)

    capability = adapter.get_capability(task_type=live.PROFILE_TASK, output_schema_version=live.PROFILE_SCHEMA)

    assert capability.provider == "deepseek"
    assert capability.model_id == "deepseek-v4-pro"
    assert capability.model_version == "deepseek-v4-pro"
    assert capability.max_output_tokens == live.PROFILE_MAX_OUTPUT_TOKENS


def test_live_profile_post_delegates_to_hardened_transport_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = ProviderReceipt(
        provider="deepseek",
        provider_response_id="resp-policy",
        actual_model="deepseek-v4-pro",
        provider_runtime_fingerprint="fp",
        finish_reason="stop",
        prompt_tokens=1,
        completion_tokens=2,
        total_tokens=3,
        result={"ok": True},
    )
    captured = {}

    def fake_policy_send(**kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(profile_policy, "send_profile_with_api_key", fake_policy_send)

    receipt = live.send_profile_with_api_key(
        messages=({"role": "user", "content": "json please"},),
        max_tokens=100,
        api_key="secret",
        model_id="deepseek-v4-pro",
    )

    assert receipt is expected
    assert captured == {
        "messages": ({"role": "user", "content": "json please"},),
        "max_tokens": 100,
        "api_key": "secret",
        "model_id": "deepseek-v4-pro",
    }


def test_profile_execution_rechecks_model_membership_before_post(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemorySecretStore()
    store.put(live.MODEL_SELECTION_REF, "deepseek-flash")
    adapter = live.LiveDeepSeekProfileAdapter(store_factory=lambda: store)
    request = ProviderCredentialRequest(
        local_task_id="task-1",
        provider="deepseek",
        model_id="deepseek-flash",
        model_version="deepseek-flash",
        task_type=live.PROFILE_TASK,
        output_schema_version=live.PROFILE_SCHEMA,
        messages=({"role": "user", "content": "test"},),
        max_output_tokens=100,
    )
    monkeypatch.setattr(live, "list_deepseek_models_with_api_key", lambda _key: ("deepseek-flash", "deepseek-v4-pro"))
    expected = ProviderReceipt(
        provider="deepseek",
        provider_response_id="resp-1",
        actual_model="deepseek-flash",
        provider_runtime_fingerprint="fp",
        finish_reason="stop",
        prompt_tokens=1,
        completion_tokens=2,
        total_tokens=3,
        result={"ok": True},
    )
    captured = {}

    def fake_send(**kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(live, "send_profile_with_api_key", fake_send)

    receipt = adapter.execute_with_credential(request, "secret")

    assert receipt is expected
    assert captured["model_id"] == "deepseek-flash"
    assert captured["api_key"] == "secret"


def test_stale_selection_fails_before_profile_post(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemorySecretStore()
    store.put(live.MODEL_SELECTION_REF, "deepseek-flash")
    adapter = live.LiveDeepSeekProfileAdapter(store_factory=lambda: store)
    request = ProviderCredentialRequest(
        local_task_id="task-1",
        provider="deepseek",
        model_id="deepseek-flash",
        model_version="deepseek-flash",
        task_type=live.PROFILE_TASK,
        output_schema_version=live.PROFILE_SCHEMA,
        messages=({"role": "user", "content": "test"},),
        max_output_tokens=100,
    )
    monkeypatch.setattr(live, "list_deepseek_models_with_api_key", lambda _key: ("deepseek-v4-pro",))
    called = {"post": 0}

    def forbidden_send(**_kwargs):
        called["post"] += 1
        raise AssertionError("stale selection must fail before provider POST")

    monkeypatch.setattr(live, "send_profile_with_api_key", forbidden_send)

    with pytest.raises(HTTPException) as caught:
        adapter.execute_with_credential(request, "secret")

    assert caught.value.detail["code"] == "PROFILE_GENERATION_MODEL_SELECTION_STALE"
    assert called["post"] == 0
