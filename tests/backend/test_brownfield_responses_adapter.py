import json
from dataclasses import replace

import httpx
import pytest
from fastapi import HTTPException

from app import brownfield_responses_adapter as adapter
from app.model_provider_contract import ProviderCapability, ProviderCredentialRequest


def request():
    return ProviderCredentialRequest("test", "deepseek", "deepseek-flash", "deepseek-flash", "project_profile_build", "project-profile-build/2.0", ({"role": "system", "content": "audit"}, {"role": "user", "content": json.dumps({"required_output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}, "note": "中文"})}), 100)


def envelope():
    return {"object": "response", "id": "resp-test", "model": "deepseek-flash", "status": "completed", "error": None, "output": [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": '{"ok":true}'}]}], "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}}


def setup(monkeypatch, payload=None, error=None, status=200, model="deepseek-flash"):
    cap = ProviderCapability("deepseek", model, model, "project_profile_build", "project-profile-build/2.0", 1_000_000, 24000)
    class Base:
        def get_capability(self, **kwargs):
            return cap
    monkeypatch.setattr(adapter, "build_live_deepseek_profile_adapter", Base)
    sent = []
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            sent.append((url, kwargs))
            if error: raise error
            return httpx.Response(status, json=envelope() if payload is None else payload)
    monkeypatch.setattr(adapter, "_client", Client)
    return adapter.build_brownfield_responses_adapter(), sent


def test_wire_is_exact_canonical_estimate_and_schema_payload(monkeypatch):
    live, sent = setup(monkeypatch)
    req = request()
    receipt = live.execute_with_credential(req, "test-only")
    url, kwargs = sent[0]
    assert url == "https://api.deepseek.com/responses"
    raw = kwargs["content"]
    assert len(raw) == adapter.estimate_request_bytes(messages=req.messages, max_output_tokens=100, model_id=req.model_id)
    assert len(raw) == live.estimate_request_bytes(messages=req.messages, max_output_tokens=100, model_id=req.model_id)
    payload = json.loads(raw)
    assert payload["model"] == req.model_id
    assert payload["input"] == list(req.messages)
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["temperature"] == 0
    assert b'"temperature":0' in raw
    assert payload["stream"] is False and payload["max_output_tokens"] == 100
    assert payload["text"]["format"] == {"type": "json_schema", "name": "brownfield_atlas", "schema": json.loads(req.messages[-1]["content"])["required_output_schema"]}
    assert not {"strict", "store", "background"}.intersection(payload)
    assert receipt.result == {"ok": True}
    assert receipt.provider_runtime_fingerprint == "not-provided-by-provider"


@pytest.mark.parametrize("model", sorted(adapter.SUPPORTED_MODELS))
def test_payload_explicit_nonthinking_sampling_policy(model):
    payload = adapter.build_payload(messages=request().messages, max_output_tokens=100, model_id=model)
    assert payload["model"] == model
    assert payload["reasoning"] == {"effort": "none"}
    assert type(payload["temperature"]) is int and payload["temperature"] == 0
    assert set(payload) == {"model", "input", "reasoning", "temperature", "max_output_tokens", "stream", "text"}


@pytest.mark.parametrize("error", [httpx.ReadTimeout("private"), httpx.ConnectError("private")])
def test_unknown_is_one_send_no_retry(monkeypatch, error):
    live, sent = setup(monkeypatch, error=error)
    with pytest.raises(HTTPException) as exc:
        live.execute_with_credential(request(), "test-only")
    assert exc.value.detail["code"] == "PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN"
    assert "private" not in str(exc.value.detail)
    assert len(sent) == 1


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 429, 500])
def test_http_errors_are_fixed_safe_codes(monkeypatch, status):
    live, sent = setup(monkeypatch, payload={"private": "do not expose"}, status=status)
    with pytest.raises(HTTPException) as exc:
        live.execute_with_credential(request(), "test-only")
    assert "do not expose" not in str(exc.value.detail)
    assert len(sent) == 1


@pytest.mark.parametrize("change", [{"model": "other"}, {"object": "other"}, {"status": "incomplete"}, {"error": {"secret": "hidden"}}, {"id": ""}, {"output": [{"type": "function_call"}]}, {"output": [{"type": "message", "role": "assistant", "content": [{"type": "refusal"}]}]}, {"usage": {"input_tokens": True, "output_tokens": 3, "total_tokens": 4}}, {"usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 6}}])
def test_response_rejects_invalid_envelope_and_usage(monkeypatch, change):
    live, _ = setup(monkeypatch, payload={**envelope(), **change})
    with pytest.raises(HTTPException): live.execute_with_credential(request(), "test-only")


@pytest.mark.parametrize("text", ['{"x":1,"x":2}', '{"x":NaN}', '[]', 'not json'])
def test_strict_output_json(monkeypatch, text):
    payload = envelope()
    payload["output"][0]["content"][0]["text"] = text
    live, _ = setup(monkeypatch, payload=payload)
    with pytest.raises(HTTPException): live.execute_with_credential(request(), "test-only")


@pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-future", "deepseek-v4-pro"])
def test_model_change_or_unsupported_never_sends(monkeypatch, model):
    live, sent = setup(monkeypatch, model=model)
    with pytest.raises(HTTPException): live.execute_with_credential(request(), "test-only")
    assert sent == []


def test_output_cap_fail_before_send(monkeypatch):
    live, sent = setup(monkeypatch)
    with pytest.raises(HTTPException): live.execute_with_credential(replace(request(), max_output_tokens=24001), "test-only")
    assert sent == []


def test_wire_budget_fail_before_send(monkeypatch):
    live, sent = setup(monkeypatch)
    req = request()
    messages = (dict(req.messages[0], content="x" * 1_000_000), req.messages[1])
    with pytest.raises(HTTPException) as exc:
        live.execute_with_credential(replace(req, messages=messages), "test-only")
    assert exc.value.detail["code"] == "PROFILE_GENERATION_RESPONSES_REQUEST_OVER_BUDGET"
    assert sent == []


def test_pro_same_model_and_split_output_text(monkeypatch):
    payload = dict(envelope(), model="deepseek-v4-pro")
    payload["output"][0]["content"] = [{"type": "output_text", "text": '{"ok":'}, {"type": "output_text", "text": 'true}'}]
    live, sent = setup(monkeypatch, model="deepseek-v4-pro", payload=payload)
    receipt = live.execute_with_credential(replace(request(), model_id="deepseek-v4-pro", model_version="deepseek-v4-pro"), "test-only")
    assert receipt.actual_model == "deepseek-v4-pro"
    assert json.loads(sent[0][1]["content"])["model"] == "deepseek-v4-pro"


@pytest.mark.parametrize("content", ['{}', '[]', '{"required_output_schema":null}', '{"required_output_schema":{},"required_output_schema":{}}'])
def test_missing_or_invalid_schema_never_sends(monkeypatch, content):
    live, sent = setup(monkeypatch)
    req = request()
    with pytest.raises(HTTPException):
        live.execute_with_credential(replace(req, messages=(req.messages[0], {"role": "user", "content": content})), "test-only")
    assert sent == []


@pytest.mark.parametrize("raw", [b'{}', b'{"object":"response","object":"response"}', b'not json', b''])
def test_strict_response_envelope_bytes(raw):
    with pytest.raises(HTTPException): adapter._receipt(httpx.Response(200, content=raw), "deepseek-flash")


def test_client_security_options(monkeypatch):
    from app import deepseek_live_profile_adapter as base
    options = []
    monkeypatch.setattr(base.httpx, "Client", lambda **kwargs: options.append(kwargs))
    base._client()
    assert options[0]["follow_redirects"] is False
    assert options[0]["trust_env"] is False
    assert options[0]["verify"] is True
    assert options[0]["timeout"].read == 180.0


@pytest.mark.parametrize("text,suffix", [
    ('{"private-body-marker":1,"private-body-marker":2}', "CONTENT_JSON_DUPLICATE_KEY_INVALID"),
    ('{"private-body-marker":NaN}', "CONTENT_JSON_NONFINITE_INVALID"),
    ('{"private-body-marker":Infinity}', "CONTENT_JSON_NONFINITE_INVALID"),
    ('{"private-body-marker":-Infinity}', "CONTENT_JSON_NONFINITE_INVALID"),
    ('  ```json\n{"private-body-marker":true}\n```', "CONTENT_JSON_FENCE_INVALID"),
    ('{"private-body-marker":\n}', "CONTENT_JSON_SYNTAX_INVALID"),
    ('["private-body-marker"]', "CONTENT_NOT_OBJECT"),
    ('{"private-body-marker":"\ud800"}', "CONTENT_UTF8_INVALID"),
    ('[' * 2000 + '"private-body-marker"' + ']' * 2000, "CONTENT_JSON_RECURSION_INVALID"),
])
def test_content_diagnostics_are_fixed_codes_and_bounded_numbers_only(text, suffix):
    payload = envelope()
    payload["output"][0]["content"][0]["text"] = text
    response = httpx.Response(200, content=json.dumps(payload).encode("utf-8"))
    with pytest.raises(HTTPException) as exc:
        adapter._receipt(response, "deepseek-flash", 3)
    detail = exc.value.detail
    assert detail["code"] == "PROFILE_GENERATION_RESPONSES_" + suffix
    assert "private-body-marker" not in str(detail)
    diagnostic = detail["diagnostic"]
    assert set(diagnostic) <= {"content_utf8_bytes", "chunk_count", "output_tokens", "output_limit_reached", "json_error_line", "json_error_column", "json_error_offset"}
    assert all(type(value) is bool or type(value) is int and 0 <= value <= 2**31 - 1 for value in diagnostic.values())
    assert diagnostic["chunk_count"] == 1
    assert diagnostic["output_tokens"] == 3
    assert diagnostic["output_limit_reached"] is True
    if suffix != "CONTENT_UTF8_INVALID":
        assert diagnostic["content_utf8_bytes"] == len(text.encode("utf-8"))
    if suffix == "CONTENT_JSON_SYNTAX_INVALID":
        assert diagnostic["json_error_line"] == 2
        assert diagnostic["json_error_column"] == 1
        assert diagnostic["json_error_offset"] == text.index("}")


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": True, "output_tokens": 3, "total_tokens": 4}, {"input_tokens": 2, "output_tokens": 3, "total_tokens": 99}])
def test_invalid_usage_not_used_for_content_diagnostic(usage):
    payload = envelope()
    payload["usage"] = usage
    payload["output"][0]["content"][0]["text"] = '{"private-body-marker":'
    with pytest.raises(HTTPException) as exc:
        adapter._receipt(httpx.Response(200, json=payload), "deepseek-flash", 3)
    assert "output_tokens" not in exc.value.detail["diagnostic"]
    assert "output_limit_reached" not in exc.value.detail["diagnostic"]
    assert "private-body-marker" not in str(exc.value.detail)


def test_diagnostic_request_limit_passed_through_and_split_chunks(monkeypatch):
    payload = envelope()
    payload["output"][0]["content"] = [{"type": "output_text", "text": '{"private-body-marker":'}, {"type": "output_text", "text": 'invalid}'}]
    live, sent = setup(monkeypatch, payload=payload)
    with pytest.raises(HTTPException) as exc:
        live.execute_with_credential(request(), "test-only")
    assert len(sent) == 1
    assert exc.value.detail["diagnostic"]["chunk_count"] == 2
    assert exc.value.detail["diagnostic"]["output_limit_reached"] is False
    assert "private-body-marker" not in str(exc.value.detail)


def test_diagnostic_unknown_limit_omitted_and_large_tokens_bounded():
    payload = envelope()
    payload["usage"] = {"input_tokens": 2, "output_tokens": 2**40, "total_tokens": 2**40 + 2}
    payload["output"][0]["content"][0]["text"] = 'invalid'
    with pytest.raises(HTTPException) as exc:
        adapter._receipt(httpx.Response(200, json=payload), "deepseek-flash")
    assert "output_tokens" not in exc.value.detail["diagnostic"]
    assert "output_limit_reached" not in exc.value.detail["diagnostic"]
