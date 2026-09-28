from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
from threading import Barrier, Lock

import pytest
from fastapi import HTTPException

from app import deepseek_execution as subject

H = lambda text: hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(autouse=True)
def reset_claims():
    with subject._SEND_CLAIM_LOCK:
        subject._SEND_CLAIMED_MODEL_CALL_IDS.clear()
    yield
    with subject._SEND_CLAIM_LOCK:
        subject._SEND_CLAIMED_MODEL_CALL_IDS.clear()


def preflight(**overrides):
    value = {
        "schema_version": "model_gateway_preflight_v1",
        "model_call_id": 7,
        "call_identity_hash": H("call"),
        "final_context_manifest_hash": H("manifest"),
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "task_type": "daily_report_generate",
        "output_schema_version": "daily-report/1.0",
        "current_qualification_authority_state": "verified",
        "current_qualification_authority_ref": {"source": "owner"},
        "current_qualification_evidence_hash": H("qualification"),
        "current_authorization_authority_state": "verified",
        "current_authorization_authority_ref": "owner/ref",
        "current_authorization_evidence_hash": H("authorization"),
        "current_authorization_data_scope_hash": H("scope"),
        "current_authorization_purpose_id": "authority-owned-purpose-v2",
        "framed_payload_hash": H("payload"),
        "framed_payload_utf8_bytes": 17,
        "request_envelope_hash": H("request"),
        "final_request_fit_state": "fit_by_conservative_upper_bound",
        "provider_compatibility_state": "compatible",
        "local_gateway_state": "ready_for_provider_transport",
        "network_send_state": "not_attempted",
        "gateway_preflight_hash": H("preflight"),
    }
    value.update(overrides)
    return value


def transient(p=None, **overrides):
    p = p or preflight()
    value = {
        "schema_version": "model_gateway_transient_request_v1",
        "model_call_id": p["model_call_id"],
        "call_identity_hash": p["call_identity_hash"],
        "final_context_manifest_hash": p["final_context_manifest_hash"],
        "provider": p["provider"],
        "model_id": p["model_id"],
        "model_version": p["model_version"],
        "task_type": p["task_type"],
        "output_schema_version": p["output_schema_version"],
        "framed_payload_hash": p["framed_payload_hash"],
        "framed_payload_utf8_bytes": p["framed_payload_utf8_bytes"],
        "request_envelope_hash": p["request_envelope_hash"],
        "conservative_local_request_upper_bound": 15000,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": "fake-only"}],
    }
    value.update(overrides)
    return value


def receipt():
    return {"provider": "deepseek", "provider_response_id": "r", "actual_model": "deepseek-flash"}


def durable():
    return {"schema_version": "model_execution_result_v1", "model_result_id": 99}


def install(monkeypatch, *, p=None, t=None, transport=None, ledger=None):
    p = p or preflight()
    t = t or transient(p)
    r = receipt()
    d = durable()
    calls = {"preflight": 0, "transient": 0, "send": 0, "ledger": 0}
    seen = {}

    def gateway_preflight(*, model_call_id, budget_record):
        calls["preflight"] += 1
        return p

    def gateway_transient(*, model_call_id, budget_record):
        calls["transient"] += 1
        return t

    def send(*, messages, max_tokens):
        calls["send"] += 1
        seen["messages"] = messages
        seen["max_tokens"] = max_tokens
        if transport:
            return transport(messages=messages, max_tokens=max_tokens)
        return r

    def record(*, model_call_id, receipt):
        calls["ledger"] += 1
        seen["receipt"] = receipt
        if ledger:
            return ledger(model_call_id=model_call_id, receipt=receipt)
        return d

    monkeypatch.setattr(subject.model_gateway, "build_model_gateway_preflight", gateway_preflight)
    monkeypatch.setattr(subject.model_gateway, "materialize_model_gateway_request_transient", gateway_transient)
    monkeypatch.setattr(subject.deepseek_transport, "send_deepseek_v4_flash", send)
    monkeypatch.setattr(subject.model_execution_results, "record_model_execution_result", record)
    return calls, seen, r, d, t


def run():
    return subject.execute_deepseek_model_call(model_call_id=7, budget_record={"budget": "fake"})


def code(exc):
    return exc.detail["code"]


def test_signature_and_happy_exact_forwarding(monkeypatch):
    sig = inspect.signature(subject.execute_deepseek_model_call)
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values())
    calls, seen, r, d, t = install(monkeypatch)
    assert run() is d
    assert calls == {"preflight": 1, "transient": 1, "send": 1, "ledger": 1}
    assert seen["messages"] is t["messages"]
    assert seen["max_tokens"] == t["max_tokens"]
    assert seen["receipt"] is r


def test_purpose_is_owner_fact_not_local_exact_authorization(monkeypatch):
    calls, *_ = install(monkeypatch, p=preflight(current_authorization_purpose_id="future-purpose"))
    run()
    assert calls["send"] == calls["ledger"] == 1


@pytest.mark.parametrize("purpose", [None, "", "   ", 1, True])
def test_purpose_requires_presence_and_shape(monkeypatch, purpose):
    calls, *_ = install(monkeypatch, p=preflight(current_authorization_purpose_id=purpose))
    with pytest.raises(HTTPException) as e:
        run()
    assert code(e.value) == "DEEPSEEK_EXECUTION_PREFLIGHT_INVALID"
    assert calls["transient"] == calls["send"] == calls["ledger"] == 0


@pytest.mark.parametrize("field", [
    "provider", "model_id", "model_version", "task_type", "output_schema_version",
    "current_qualification_authority_state", "current_authorization_authority_state",
    "final_request_fit_state", "provider_compatibility_state", "local_gateway_state", "network_send_state",
])
def test_nonready_preflight_stops_before_transient(monkeypatch, field):
    calls, *_ = install(monkeypatch, p=preflight(**{field: "wrong"}))
    with pytest.raises(HTTPException) as e:
        run()
    assert code(e.value) == "DEEPSEEK_EXECUTION_GATEWAY_NOT_READY"
    assert calls["transient"] == calls["send"] == calls["ledger"] == 0


@pytest.mark.parametrize("field", [
    "call_identity_hash", "final_context_manifest_hash", "current_qualification_evidence_hash",
    "current_authorization_evidence_hash", "current_authorization_data_scope_hash",
    "framed_payload_hash", "request_envelope_hash", "gateway_preflight_hash",
])
def test_invalid_preflight_hash_stops_before_transient(monkeypatch, field):
    calls, *_ = install(monkeypatch, p=preflight(**{field: "bad"}))
    with pytest.raises(HTTPException) as e:
        run()
    assert code(e.value) == "DEEPSEEK_EXECUTION_PREFLIGHT_INVALID"
    assert calls["transient"] == calls["send"] == calls["ledger"] == 0


@pytest.mark.parametrize("field", [
    "model_call_id", "call_identity_hash", "final_context_manifest_hash", "provider", "model_id",
    "model_version", "task_type", "output_schema_version", "framed_payload_hash",
    "framed_payload_utf8_bytes", "request_envelope_hash",
])
def test_transient_cross_step_mismatch_stops_before_transport(monkeypatch, field):
    p = preflight()
    replacement = 999 if field in {"model_call_id", "framed_payload_utf8_bytes"} else "wrong"
    calls, *_ = install(monkeypatch, p=p, t=transient(p, **{field: replacement}))
    with pytest.raises(HTTPException) as e:
        run()
    assert code(e.value) == "DEEPSEEK_EXECUTION_TRANSIENT_MISMATCH"
    assert calls["send"] == calls["ledger"] == 0


@pytest.mark.parametrize("max_tokens", [384001, 0, True])
def test_out_of_domain_max_tokens_is_forwarded_once_for_transport_to_reject(monkeypatch, max_tokens):
    p = preflight()
    t = transient(p, max_tokens=max_tokens)
    def reject(*, messages, max_tokens):
        assert messages is t["messages"]
        assert max_tokens == t["max_tokens"]
        raise HTTPException(status_code=400, detail={"code": "DEEPSEEK_TRANSPORT_INPUT_INVALID"})
    calls, seen, *_ = install(monkeypatch, p=p, t=t, transport=reject)
    with pytest.raises(HTTPException) as e:
        run()
    assert code(e.value) == "DEEPSEEK_TRANSPORT_INPUT_INVALID"
    assert seen["max_tokens"] == max_tokens
    assert calls == {"preflight": 1, "transient": 1, "send": 1, "ledger": 0}


def test_orchestration_does_not_validate_provider_budget_or_messages(monkeypatch):
    p = preflight()
    messages = {"transport": "owns shape"}
    t = transient(p, conservative_local_request_upper_bound=-1, max_tokens="bad", messages=messages)
    def reject(*, messages, max_tokens):
        assert messages is t["messages"]
        assert max_tokens == "bad"
        raise HTTPException(status_code=400, detail={"code": "DEEPSEEK_TRANSPORT_INPUT_INVALID"})
    calls, *_ = install(monkeypatch, p=p, t=t, transport=reject)
    with pytest.raises(HTTPException):
        run()
    assert calls == {"preflight": 1, "transient": 1, "send": 1, "ledger": 0}


@pytest.mark.parametrize("failure", [
    HTTPException(status_code=502, detail={"code": "DEEPSEEK_TRANSPORT_NETWORK_ERROR"}),
    HTTPException(status_code=502, detail={"code": "DEEPSEEK_TRANSPORT_HTTP_ERROR"}),
    RuntimeError("post-send ambiguity"),
])
def test_transport_failure_is_terminal_no_retry_no_ledger(monkeypatch, failure):
    def fail(**_kwargs):
        raise failure
    calls, *_ = install(monkeypatch, transport=fail)
    with pytest.raises(type(failure)):
        run()
    assert calls == {"preflight": 1, "transient": 1, "send": 1, "ledger": 0}


def test_ledger_failure_never_resends(monkeypatch):
    def fail(**_kwargs):
        raise HTTPException(status_code=409, detail={"code": "MODEL_EXECUTION_RESULT_OUTPUT_INVALID"})
    calls, *_ = install(monkeypatch, ledger=fail)
    with pytest.raises(HTTPException):
        run()
    assert calls == {"preflight": 1, "transient": 1, "send": 1, "ledger": 1}


def test_same_process_second_send_is_blocked(monkeypatch):
    calls, *_ = install(monkeypatch)
    run()
    with pytest.raises(HTTPException) as e:
        run()
    assert code(e.value) == "DEEPSEEK_EXECUTION_REPLAY_BLOCKED"
    assert calls == {"preflight": 2, "transient": 2, "send": 1, "ledger": 1}


def test_concurrent_same_call_reaches_transport_at_most_once(monkeypatch):
    barrier = Barrier(2)
    lock = Lock()
    counts = {"send": 0, "ledger": 0}
    monkeypatch.setattr(subject.model_gateway, "build_model_gateway_preflight", lambda **kw: preflight())
    def materialize(**kw):
        barrier.wait()
        return transient()
    monkeypatch.setattr(subject.model_gateway, "materialize_model_gateway_request_transient", materialize)
    def send(**kw):
        with lock:
            counts["send"] += 1
        return receipt()
    monkeypatch.setattr(subject.deepseek_transport, "send_deepseek_v4_flash", send)
    monkeypatch.setattr(subject.model_execution_results, "record_model_execution_result", lambda **kw: counts.__setitem__("ledger", counts["ledger"] + 1) or durable())
    def one():
        try:
            return run()
        except HTTPException as e:
            return e
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: one(), range(2)))
    assert counts == {"send": 1, "ledger": 1}
    assert sum(isinstance(x, HTTPException) for x in results) == 1


def test_source_preserves_bounded_ownership():
    source = inspect.getsource(subject)
    assert "_MAX_OUTPUT_TOKENS" not in source
    assert "384_000" not in source
    assert '"anxin_board_daily_report_v1"' not in source
    assert source.count("build_model_gateway_preflight(") == 1
    assert source.count("materialize_model_gateway_request_transient(") == 1
    assert source.count("send_deepseek_v4_flash(") == 1
    assert source.count("record_model_execution_result(") == 1
    for forbidden in ("httpx", "os.environ", "sqlite3", "validate_ai_response", "resolve_deepseek_current_authority", "APIRouter"):
        assert forbidden not in source
