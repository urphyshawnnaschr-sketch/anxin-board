"""Acceptance for DeepSeek V4 Flash Transport V1; all HTTP is fake."""

from __future__ import annotations

from copy import deepcopy
import inspect
import json
from pathlib import Path
import sys

import httpx
import pytest
from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import deepseek_transport as transport  # noqa: E402

from app import deepseek_credential
from app.secret_store import InMemorySecretStore


@pytest.fixture(autouse=True)
def fake_report_credential_store(monkeypatch):
    monkeypatch.setattr(deepseek_credential, "_secret_store_factory", InMemorySecretStore)



MESSAGES = [
    {"role": "system", "content": "Return JSON."},
    {"role": "user", "content": "Summarize frozen evidence."},
]


def _success_payload(**overrides):
    value = {
        "id": "resp-001",
        "model": "deepseek-flash-runtime",
        "system_fingerprint": "fp-runtime-001",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({"stage": "开发中"}, ensure_ascii=False),
                    "reasoning_content": "private-reasoning-must-not-escape",
                },
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        },
    }
    value.update(overrides)
    return value


def _body(payload=None, *, prefix=b""):
    actual = _success_payload() if payload is None else payload
    return prefix + json.dumps(actual, ensure_ascii=False).encode("utf-8")


def _code(caught: pytest.ExceptionInfo[HTTPException]) -> str:
    return caught.value.detail["code"]


class _FakeResponse:
    def __init__(self, *, status=200, body=None, headers=None, redirect=False, chunks=None):
        self.status_code = status
        self._body = _body() if body is None else body
        self.headers = headers or {}
        self.is_redirect = redirect
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_bytes(self):
        if self._chunks is not None:
            yield from self._chunks
            return
        for index in range(0, len(self._body), 13):
            yield self._body[index:index + 13]


class _FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response or _FakeResponse()
        self.error = error
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def stream(self, method, url, *, headers=None, json=None):
        self.requests.append({
            "method": method,
            "url": url,
            "headers": deepcopy(headers),
            "json": deepcopy(json),
        })
        if self.error is not None:
            raise self.error
        return self.response


def _install_client(monkeypatch, *, response=None, error=None):
    client = _FakeClient(response=response, error=error)
    monkeypatch.setattr(transport, "_new_client", lambda: client)
    return client


def _send(monkeypatch, *, response=None, error=None, key="dummy-test-key", max_tokens=123, messages=None):
    monkeypatch.setenv("DEEPSEEK_API_KEY", key)
    client = _install_client(monkeypatch, response=response, error=error)
    result = transport.send_deepseek_v4_flash(
        messages=deepcopy(MESSAGES if messages is None else messages),
        max_tokens=max_tokens,
    )
    return result, client


def test_t01_capability_exact_deterministic_and_zero_io(monkeypatch):
    monkeypatch.setattr(transport, "_new_client", lambda: pytest.fail("capability must be zero I/O"))
    first = transport.get_deepseek_transport_capability()
    second = transport.get_deepseek_transport_capability()
    expected = {
        "schema_version": "deepseek_transport_capability_v1",
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "endpoint_origin": "https://api.deepseek.com",
        "endpoint_path": "/chat/completions",
        "task_type": "daily_report_generate",
        "output_schema_version": "daily-report/1.0",
        "response_format": "json_object",
        "stream": False,
        "thinking": "enabled",
        "reasoning_effort": "high",
        "max_output_tokens": 384000,
    }
    assert first == expected == second
    assert first is not second
    first["provider"] = "mutated"
    assert transport.get_deepseek_transport_capability() == expected


def test_t02_sender_signature_exposes_only_messages_and_max_tokens():
    signature = inspect.signature(transport.send_deepseek_v4_flash)
    assert list(signature.parameters) == ["messages", "max_tokens"]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


@pytest.mark.parametrize("value", [True, False, 0, -1, 384001, 1.0, "1", None])
def test_t03_max_tokens_strict_bounds_fail_before_credential_or_http(monkeypatch, value):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(transport, "_new_client", lambda: pytest.fail("HTTP must be zero"))
    with pytest.raises(HTTPException) as caught:
        transport.send_deepseek_v4_flash(messages=deepcopy(MESSAGES), max_tokens=value)
    assert _code(caught) == "DEEPSEEK_TRANSPORT_INPUT_INVALID"


@pytest.mark.parametrize(
    "messages",
    [None, {}, "x", [1], [{"role": "user", "content": float("nan")}], [{"x": object()}]],
)
def test_t04_messages_fail_closed_before_credential_or_http(monkeypatch, messages):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(transport, "_new_client", lambda: pytest.fail("HTTP must be zero"))
    with pytest.raises(HTTPException) as caught:
        transport.send_deepseek_v4_flash(messages=messages, max_tokens=1)
    assert _code(caught) == "DEEPSEEK_TRANSPORT_INPUT_INVALID"


@pytest.mark.parametrize("key", [None, "", "   "])
def test_t05_missing_or_blank_key_is_zero_http(monkeypatch, key):
    if key is None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", key)
    monkeypatch.setattr(transport, "_new_client", lambda: pytest.fail("HTTP must be zero"))
    with pytest.raises(HTTPException) as caught:
        transport.send_deepseek_v4_flash(messages=deepcopy(MESSAGES), max_tokens=1)
    assert _code(caught) == "DEEPSEEK_TRANSPORT_CREDENTIAL_UNAVAILABLE"


def test_t06_exact_single_request_shape_and_scoped_authorization(monkeypatch):
    result, client = _send(monkeypatch, key="dummy-test-key", max_tokens=123)
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://api.deepseek.com/chat/completions"
    assert request["headers"] == {
        "Authorization": "Bearer dummy-test-key",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    assert request["json"] == {
        "model": "deepseek-flash",
        "messages": MESSAGES,
        "response_format": {"type": "json_object"},
        "stream": False,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens": 123,
    }
    assert set(request["json"]) == {
        "model", "messages", "response_format", "stream", "thinking", "reasoning_effort", "max_tokens"
    }
    assert result["provider"] == "deepseek"


def test_t07_success_receipt_exact_and_reasoning_is_discarded(monkeypatch):
    result, _client = _send(monkeypatch)
    assert result == {
        "provider": "deepseek",
        "provider_response_id": "resp-001",
        "actual_model": "deepseek-flash-runtime",
        "provider_runtime_fingerprint": "fp-runtime-001",
        "finish_reason": "stop",
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "result": {"stage": "开发中"},
    }
    serialized = json.dumps(result, ensure_ascii=False)
    assert "private-reasoning-must-not-escape" not in serialized
    assert "reasoning_content" not in serialized
    assert "DeepSeek-V4.1-Flash" not in serialized


def test_t08_provider_returned_model_and_runtime_fingerprint_remain_distinct(monkeypatch):
    payload = _success_payload(
        model="provider-returned-model-id",
        system_fingerprint="runtime-config-fingerprint",
    )
    result, _client = _send(monkeypatch, response=_FakeResponse(body=_body(payload)))
    assert result["actual_model"] == "provider-returned-model-id"
    assert result["provider_runtime_fingerprint"] == "runtime-config-fingerprint"
    assert "model_version" not in result


def test_t09_legal_leading_keepalive_whitespace_still_parses(monkeypatch):
    response = _FakeResponse(body=_body(prefix=b"\n\n   \r\n"))
    result, client = _send(monkeypatch, response=response)
    assert result["provider_response_id"] == "resp-001"
    assert len(client.requests) == 1


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("timeout"), httpx.ConnectError("network")],
)
def test_t10_network_or_timeout_is_one_request_no_retry(monkeypatch, error):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-test-key")
    client = _install_client(monkeypatch, error=error)
    with pytest.raises(HTTPException) as caught:
        transport.send_deepseek_v4_flash(messages=deepcopy(MESSAGES), max_tokens=1)
    assert _code(caught) == "DEEPSEEK_TRANSPORT_NETWORK_ERROR"
    assert len(client.requests) == 1
    assert "dummy-test-key" not in str(caught.value.detail)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 429, 500, 503])
def test_t11_redirect_and_non200_are_one_request_no_retry(monkeypatch, status):
    response = _FakeResponse(
        status=status,
        body=b"raw-secret-provider-body",
        redirect=300 <= status < 400,
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-test-key")
    client = _install_client(monkeypatch, response=response)
    with pytest.raises(HTTPException) as caught:
        transport.send_deepseek_v4_flash(messages=deepcopy(MESSAGES), max_tokens=1)
    assert _code(caught) == "DEEPSEEK_TRANSPORT_HTTP_ERROR"
    assert len(client.requests) == 1
    rendered = str(caught.value.detail)
    assert "raw-secret-provider-body" not in rendered
    assert "dummy-test-key" not in rendered


def test_t12_declared_and_streamed_oversize_fail_closed(monkeypatch):
    monkeypatch.setattr(transport, "_RESPONSE_BYTE_CAP", 5)
    declared = _FakeResponse(body=b"{}", headers={"content-length": "6"})
    with pytest.raises(HTTPException) as caught:
        _send(monkeypatch, response=declared)
    assert _code(caught) == "DEEPSEEK_TRANSPORT_RESPONSE_TOO_LARGE"

    streamed = _FakeResponse(headers={}, chunks=[b"123", b"456"])
    with pytest.raises(HTTPException) as caught:
        _send(monkeypatch, response=streamed)
    assert _code(caught) == "DEEPSEEK_TRANSPORT_RESPONSE_TOO_LARGE"


@pytest.mark.parametrize("length", ["nope", "-1"])
def test_t13_invalid_content_length_is_response_invalid(monkeypatch, length):
    response = _FakeResponse(headers={"content-length": length})
    with pytest.raises(HTTPException) as caught:
        _send(monkeypatch, response=response)
    assert _code(caught) == "DEEPSEEK_TRANSPORT_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not-json",
        b"[]",
        b"\xff\xfe",
        b'{"id":"a","id":"b"}',
        b'{"x":NaN}',
    ],
)
def test_t14_malformed_top_level_response_fails_closed(monkeypatch, raw):
    response = _FakeResponse(body=raw)
    with pytest.raises(HTTPException) as caught:
        _send(monkeypatch, response=response)
    assert _code(caught) == "DEEPSEEK_TRANSPORT_RESPONSE_INVALID"


@pytest.mark.parametrize("field", ["id", "model", "system_fingerprint"])
@pytest.mark.parametrize("value", [None, "", "   ", 1])
def test_t15_response_identity_fields_must_be_nonempty_strings(monkeypatch, field, value):
    payload = _success_payload(**{field: value})
    with pytest.raises(HTTPException) as caught:
        _send(monkeypatch, response=_FakeResponse(body=_body(payload)))
    assert _code(caught) == "DEEPSEEK_TRANSPORT_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "choices",
    [None, {}, [], [_success_payload()["choices"][0], _success_payload()["choices"][0]], ["bad"]],
)
def test_t16_exact_one_choice_required(monkeypatch, choices):
    payload = _success_payload(choices=choices)
    with pytest.raises(HTTPException) as caught:
        _send(monkeypatch, response=_FakeResponse(body=_body(payload)))
    assert _code(caught) == "DEEPSEEK_TRANSPORT_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "finish_reason",
    [None, "length", "content_filter", "tool_calls", "insufficient_system_resource", "stop "],
)
def test_t17_only_exact_stop_is_complete(monkeypatch, finish_reason):
    payload = _success_payload()
    payload["choices"][0]["finish_reason"] = finish_reason
    with pytest.raises(HTTPException) as caught:
        _send(monkeypatch, response=_FakeResponse(body=_body(payload)))
    assert _code(caught) == "DEEPSEEK_TRANSPORT_COMPLETION_INCOMPLETE"


def test_t18_tool_calls_cannot_hide_under_stop(monkeypatch):
    payload = _success_payload()
    payload["choices"][0]["message"]["tool_calls"] = [{"id": "call-1"}]
    with pytest.raises(HTTPException) as caught:
        _send(monkeypatch, response=_FakeResponse(body=_body(payload)))
    assert _code(caught) == "DEEPSEEK_TRANSPORT_COMPLETION_INCOMPLETE"


@pytest.mark.parametrize(
    "content",
    [None, "", "   ", "not-json", "[]", "1", "null", '{"x":NaN}', '{"x":1,"x":2}'],
)
def test_t19_content_must_be_nonempty_strict_json_object(monkeypatch, content):
    payload = _success_payload()
    payload["choices"][0]["message"]["content"] = content
    with pytest.raises(HTTPException) as caught:
        _send(monkeypatch, response=_FakeResponse(body=_body(payload)))
    assert _code(caught) == "DEEPSEEK_TRANSPORT_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": True, "completion_tokens": 0, "total_tokens": 0},
        {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},
        {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 99},
        {"prompt_tokens": 1.0, "completion_tokens": 2, "total_tokens": 3},
    ],
)
def test_t20_usage_is_strict_nonnegative_integer_and_total_closure(monkeypatch, usage):
    payload = _success_payload(usage=usage)
    with pytest.raises(HTTPException) as caught:
        _send(monkeypatch, response=_FakeResponse(body=_body(payload)))
    assert _code(caught) == "DEEPSEEK_TRANSPORT_RESPONSE_INVALID"


def test_t21_errors_never_echo_key_or_raw_provider_body(monkeypatch):
    secret_key = "secret-api-key-DO-NOT-LEAK"
    raw_secret = b"raw-provider-secret-DO-NOT-LEAK"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret_key)
    client = _install_client(
        monkeypatch,
        response=_FakeResponse(status=500, body=raw_secret),
    )
    with pytest.raises(HTTPException) as caught:
        transport.send_deepseek_v4_flash(messages=deepcopy(MESSAGES), max_tokens=1)
    rendered = json.dumps(caught.value.detail, ensure_ascii=False)
    assert secret_key not in rendered
    assert raw_secret.decode() not in rendered
    assert len(client.requests) == 1


def test_t21b_sanitized_exception_chain_cannot_recover_secret_material(monkeypatch):
    secret_key = "secret-chain-api-key-DO-NOT-LEAK"
    request = httpx.Request(
        "POST",
        transport.ENDPOINT_URL,
        headers={"Authorization": f"Bearer {secret_key}"},
    )
    network_error = httpx.ConnectError("network", request=request)
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret_key)
    client = _install_client(monkeypatch, error=network_error)
    with pytest.raises(HTTPException) as caught:
        transport.send_deepseek_v4_flash(messages=deepcopy(MESSAGES), max_tokens=1)
    assert _code(caught) == "DEEPSEEK_TRANSPORT_NETWORK_ERROR"
    assert len(client.requests) == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret_key not in str(caught.value)

    raw_secret = b'{"raw_provider_secret":"DO-NOT-LEAK"'
    with pytest.raises(HTTPException) as caught:
        _send(monkeypatch, response=_FakeResponse(body=raw_secret))
    assert _code(caught) == "DEEPSEEK_TRANSPORT_RESPONSE_INVALID"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "raw_provider_secret" not in str(caught.value)

    payload = _success_payload()
    payload["choices"][0]["message"]["content"] = '{"nested_provider_secret":"DO-NOT-LEAK"'
    with pytest.raises(HTTPException) as caught:
        _send(monkeypatch, response=_FakeResponse(body=_body(payload)))
    assert _code(caught) == "DEEPSEEK_TRANSPORT_RESPONSE_INVALID"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "nested_provider_secret" not in str(caught.value)

    with pytest.raises(HTTPException) as caught:
        _send(
            monkeypatch,
            response=_FakeResponse(headers={"content-length": "raw-header-secret"}),
        )
    assert _code(caught) == "DEEPSEEK_TRANSPORT_RESPONSE_INVALID"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "raw-header-secret" not in str(caught.value)


def test_t22_module_has_no_gateway_authority_db_retry_or_logging_dependency():
    source = inspect.getsource(transport)
    forbidden_imports = (
        "from app.model_gateway",
        "from app.deepseek_current_authority",
        "from app.model_execution_results",
        "from app.db",
        "import logging",
    )
    assert all(value not in source for value in forbidden_imports)
    assert source.count("client.stream(") == 1
    assert "HTTPTransport(" not in source
    assert "retries=" not in source
    assert "follow_redirects=False" in source
    assert "trust_env=False" in source
    assert 'client.stream(\n                "POST",\n                ENDPOINT_URL' in source
    assert "system_fingerprint" in source
    assert '"model_version"' not in inspect.getsource(transport._normalize_success_response)


def test_t23_capability_discovery_is_exact_closed_world_deterministic_and_zero_io(monkeypatch):
    monkeypatch.setattr(transport, "read_report_credential", lambda: pytest.fail("capability must not read credentials"))
    monkeypatch.setattr(transport, "_new_client", lambda: pytest.fail("capability discovery must be zero HTTP"))

    first = transport.get_deepseek_transport_capabilities()
    second = transport.get_deepseek_transport_capabilities()
    assert first == second
    assert first is not second
    assert tuple(
        (item["task_type"], item["output_schema_version"])
        for item in first
    ) == (
        ("daily_report_generate", "daily-report/1.0"),
        ("daily_report_regenerate", "daily-report-regenerate/1.0"),
    )
    assert len(first) == 2
    first[0]["provider"] = "mutated"
    assert transport.get_deepseek_transport_capabilities()[0]["provider"] == "deepseek"


def test_t24_generate_compatibility_and_exact_regenerate_lookup_are_zero_io(monkeypatch):
    monkeypatch.setattr(transport, "read_report_credential", lambda: pytest.fail("capability must not read credentials"))
    monkeypatch.setattr(transport, "_new_client", lambda: pytest.fail("capability lookup must be zero HTTP"))

    generate = transport.get_deepseek_transport_capability()
    regenerate = transport.get_deepseek_transport_capability(
        task_type="daily_report_regenerate",
        output_schema_version="daily-report-regenerate/1.0",
    )
    expected_regenerate = deepcopy(generate)
    expected_regenerate["task_type"] = "daily_report_regenerate"
    expected_regenerate["output_schema_version"] = "daily-report-regenerate/1.0"
    assert regenerate == expected_regenerate
    assert generate["task_type"] == "daily_report_generate"
    assert generate["output_schema_version"] == "daily-report/1.0"


@pytest.mark.parametrize(
    ("task_type", "output_schema_version"),
    [
        (None, "daily-report/1.0"),
        ("", "daily-report/1.0"),
        (True, "daily-report/1.0"),
        ("daily_report_generate", None),
        ("daily_report_generate", ""),
        ("daily_report_generate", True),
        ("daily_report_regenerate", "daily-report/1.0"),
        ("daily_report_generate", "daily-report-regenerate/1.0"),
        ("daily_report_regenerate ", "daily-report-regenerate/1.0"),
        ("unsupported", "unsupported/1.0"),
    ],
)
def test_t25_malformed_or_unsupported_capability_lookup_fails_before_credential_or_http(
    monkeypatch,
    task_type,
    output_schema_version,
):
    class _ForbiddenEnvironment:
        @staticmethod
        def get(*_args, **_kwargs):
            pytest.fail("invalid capability lookup must not read credentials")

    monkeypatch.setattr(transport, "read_report_credential", lambda: pytest.fail("capability must not read credentials"))
    monkeypatch.setattr(transport, "_new_client", lambda: pytest.fail("invalid capability lookup must be zero HTTP"))

    with pytest.raises(HTTPException) as caught:
        transport.get_deepseek_transport_capability(
            task_type=task_type,
            output_schema_version=output_schema_version,
        )
    assert _code(caught) == "DEEPSEEK_TRANSPORT_INPUT_INVALID"


@pytest.mark.parametrize(
    "partial_kwargs",
    [
        {"task_type": "daily_report_generate"},
        {"output_schema_version": "daily-report/1.0"},
    ],
)
def test_t26_partial_capability_lookup_fails_before_credential_or_http(monkeypatch, partial_kwargs):
    class _ForbiddenEnvironment:
        @staticmethod
        def get(*_args, **_kwargs):
            pytest.fail("partial capability lookup must not read credentials")

    monkeypatch.setattr(transport, "read_report_credential", lambda: pytest.fail("capability must not read credentials"))
    monkeypatch.setattr(transport, "_new_client", lambda: pytest.fail("partial capability lookup must be zero HTTP"))

    with pytest.raises(HTTPException) as caught:
        transport.get_deepseek_transport_capability(**partial_kwargs)
    assert _code(caught) == "DEEPSEEK_TRANSPORT_INPUT_INVALID"
