"""Synthetic responses only: safe, durable classification after the POST response."""
import json
import sqlite3

import httpx
import pytest
from fastapi import HTTPException

from app.deepseek_live_profile_adapter import _normalize_profile_receipt
from app.profile_reconciliation_provider import _classify
from app import project_profile_generation_attempt as attempts


def envelope():
    return dict(id="fixture", model="deepseek-flash", choices=[dict(
        finish_reason="stop", message=dict(content='{"ok":true}'))],
        usage=dict(prompt_tokens=2, completion_tokens=3, total_tokens=5))


CASES = [
    ("envelope", "ENVELOPE_INVALID"), ("id", "ID_INVALID"),
    ("choices", "CHOICES_INVALID"), ("length", "OUTPUT_TRUNCATED"),
    ("finish", "FINISH_INVALID"), ("content", "CONTENT_MISSING"),
    ("json", "CONTENT_JSON_INVALID"), ("duplicate", "CONTENT_JSON_INVALID"),
    ("nonfinite", "CONTENT_JSON_INVALID"), ("object", "CONTENT_NOT_OBJECT"),
    ("usage", "USAGE_MISSING"), ("tokens", "USAGE_INVALID"),
    ("total", "USAGE_MISMATCH"),
]


def response(case):
    payload = envelope()
    if case == "envelope":
        return httpx.Response(200, content=b"private-invalid-body")
    if case == "id": payload["id"] = None
    if case == "choices": payload["choices"] = []
    if case == "length": payload["choices"][0]["finish_reason"] = "length"
    if case == "finish": payload["choices"][0]["finish_reason"] = "private-reason"
    content = {"content": "", "json": "private-invalid-json", "duplicate": '{"x":1,"x":2}',
               "nonfinite": '{"x":NaN}', "object": "[]"}
    if case in content: payload["choices"][0]["message"]["content"] = content[case]
    if case == "usage": payload.pop("usage")
    if case == "tokens": payload["usage"]["prompt_tokens"] = True
    if case == "total": payload["usage"]["total_tokens"] = 100
    return httpx.Response(200, json=payload)


@pytest.mark.parametrize("case,suffix", CASES)
def test_safe_response_codes_are_durable_after_send(case, suffix, tmp_path, monkeypatch):
    code = "PROFILE_GENERATION_RESPONSE_" + suffix
    with pytest.raises(HTTPException) as caught:
        _normalize_profile_receipt(response(case), requested_model="deepseek-flash")
    assert caught.value.detail["code"] == code
    assert "private" not in json.dumps(caught.value.detail)
    assert _classify(caught.value) == ("failed_after_send", code)
    db = tmp_path / "attempt.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db))
    calls = []
    def operation():
        calls.append("fake-dispatch")
        raise caught.value
    for _ in range(2):
        with pytest.raises(HTTPException):
            attempts.execute_profile_generation_once(project_id=2, idempotency_key="synthetic-response-001", operation=operation)
    assert calls == ["fake-dispatch"]
    with sqlite3.connect(db) as conn:
        status, error_code = conn.execute("SELECT status, error_code FROM profile_generation_attempts").fetchone()
    assert (status, error_code) == ("failed_after_send", code)


def test_valid_response_and_ambiguous_network_remain_distinct():
    receipt = _normalize_profile_receipt(httpx.Response(200, json=envelope()), requested_model="deepseek-flash")
    assert receipt.result == {"ok": True}
    error = HTTPException(502, detail={"code": "PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN"})
    assert _classify(error) == ("unknown", "PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN")


@pytest.mark.parametrize("raw", [b"", b"\xff", b"[]", b'{"id":1,"id":2}', b'{"x":NaN}'])
def test_malformed_post_envelopes_are_after_send(raw):
    with pytest.raises(HTTPException) as caught:
        _normalize_profile_receipt(httpx.Response(200, content=raw), requested_model="deepseek-flash")
    assert _classify(caught.value) == ("failed_after_send", "PROFILE_GENERATION_RESPONSE_ENVELOPE_INVALID")


def test_models_discovery_keeps_separate_pre_send_classification():
    from app.deepseek_live_profile_adapter import _strict_json
    with pytest.raises(HTTPException) as caught:
        _strict_json(b"private-broken-json")
    assert caught.value.detail["code"] == "MODEL_CONNECTION_RESPONSE_INVALID"
    error = HTTPException(502, detail={"code": "PROFILE_GENERATION_MODEL_PREFLIGHT_RESPONSE_INVALID"})
    assert _classify(error) == ("failed_pre_send", "PROFILE_GENERATION_MODEL_PREFLIGHT_RESPONSE_INVALID")


def test_unpaired_unicode_in_inner_content_is_classified():
    payload = envelope()
    payload["choices"][0]["message"]["content"] = '\ud800'
    with pytest.raises(HTTPException) as caught:
        _normalize_profile_receipt(httpx.Response(200, content=json.dumps(payload).encode()), requested_model="deepseek-flash")
    assert caught.value.detail["code"] == "PROFILE_GENERATION_RESPONSE_CONTENT_JSON_INVALID"


@pytest.mark.parametrize("inner", [False, True])
def test_excessive_json_nesting_is_received_response_failure(inner):
    nested = "[" * 2000 + "0" + "]" * 2000
    if inner:
        payload = envelope()
        payload["choices"][0]["message"]["content"] = nested
        raw = json.dumps(payload).encode()
    else:
        raw = nested.encode()
    with pytest.raises(HTTPException) as caught:
        _normalize_profile_receipt(httpx.Response(200, content=raw), requested_model="deepseek-flash")
    suffix = "CONTENT_JSON_INVALID" if inner else "ENVELOPE_INVALID"
    assert _classify(caught.value) == ("failed_after_send", "PROFILE_GENERATION_RESPONSE_" + suffix)
