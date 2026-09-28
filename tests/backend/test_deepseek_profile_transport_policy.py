from __future__ import annotations

from pathlib import Path
import json
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

import app.deepseek_profile_transport_policy as policy  # noqa: E402


class _PostClient:
    def __init__(self) -> None:
        self.body = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, **kwargs):
        self.body = kwargs["content"]
        payload = (
            '{"id":"resp-1","model":"deepseek-flash",'
            '"choices":[{"finish_reason":"stop","message":{"content":"{\\"ok\\":true}"}}],'
            '"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}'
        )
        return httpx.Response(
            200,
            content=payload.encode("utf-8"),
            request=httpx.Request("POST", url),
        )


def test_profile_transport_exact_estimator_matches_actual_post_body() -> None:
    messages = ({"role": "user", "content": "json 请返回"},)
    body = policy.profile_request_body_bytes(
        messages=messages,
        max_tokens=100,
        model_id="deepseek-flash",
    )
    assert policy.estimate_profile_request_utf8_bytes(
        messages=messages,
        max_tokens=100,
        model_id="deepseek-flash",
    ) == len(body)
    parsed = json.loads(body.decode("utf-8"))
    assert parsed == {
        "model": "deepseek-flash",
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": "json 请返回"}],
        "response_format": {"type": "json_object"},
        "stream": False,
        "max_tokens": 100,
    }


def test_profile_transport_disables_thinking_for_json_extraction(monkeypatch) -> None:
    client = _PostClient()
    monkeypatch.setattr(policy, "_client", lambda: client)
    messages = ({"role": "user", "content": "json please"},)

    receipt = policy.send_profile_with_api_key(
        messages=messages,
        max_tokens=100,
        api_key="secret",
        model_id="deepseek-flash",
    )

    assert receipt.provider_response_id == "resp-1"
    assert client.body == policy.profile_request_body_bytes(
        messages=messages,
        max_tokens=100,
        model_id="deepseek-flash",
    )
    parsed = json.loads(client.body.decode("utf-8"))
    assert parsed["thinking"] == {"type": "disabled"}
    assert parsed["stream"] is False
    assert parsed["response_format"] == {"type": "json_object"}


def _network_exception_detail(monkeypatch, exc_factory):
    class _FailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, **_kwargs):
            request = httpx.Request("POST", url)
            raise exc_factory(request)

    monkeypatch.setattr(policy, "_client", lambda: _FailingClient())

    try:
        policy.send_profile_with_api_key(
            messages=({"role": "user", "content": "json please"},),
            max_tokens=100,
            api_key="secret",
            model_id="deepseek-flash",
        )
    except Exception as exc:
        detail = getattr(exc, "detail", {})
        assert detail.get("code") == "PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN"
        assert "secret" not in str(detail)
        return detail
    raise AssertionError("network ambiguity must fail closed")


def test_profile_transport_preserves_read_timeout_category(monkeypatch) -> None:
    detail = _network_exception_detail(
        monkeypatch,
        lambda request: httpx.ReadTimeout("timeout", request=request),
    )
    assert "返回结果超时" in detail["message"]
    assert "不会自动重发" in detail["message"]


def test_profile_transport_preserves_connect_error_category(monkeypatch) -> None:
    detail = _network_exception_detail(
        monkeypatch,
        lambda request: httpx.ConnectError("connect failed", request=request),
    )
    assert "HTTPS 连接" in detail["message"]
    assert "不会自动重发" in detail["message"]
