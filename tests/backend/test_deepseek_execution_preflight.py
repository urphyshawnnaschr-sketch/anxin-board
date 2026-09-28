from __future__ import annotations

import hashlib
import inspect

import pytest
from fastapi import HTTPException

from app import deepseek_execution as subject


H = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()


def _preflight(**overrides):
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
        "current_authorization_purpose_id": "anxin_board_daily_report_v1",
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


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def test_public_ready_preflight_calls_gateway_once_and_never_materializes_or_sends(monkeypatch):
    calls = {"preflight": 0, "transient": 0, "send": 0, "ledger": 0}
    expected = _preflight()

    def build(**kwargs):
        calls["preflight"] += 1
        assert kwargs == {"model_call_id": 7, "budget_record": {"budget": "fake"}}
        return expected

    monkeypatch.setattr(subject.model_gateway, "build_model_gateway_preflight", build)
    monkeypatch.setattr(subject.model_gateway, "materialize_model_gateway_request_transient", lambda **_: calls.__setitem__("transient", calls["transient"] + 1))
    monkeypatch.setattr(subject.deepseek_transport, "send_deepseek_v4_flash", lambda **_: calls.__setitem__("send", calls["send"] + 1))
    monkeypatch.setattr(subject.model_execution_results, "record_model_execution_result", lambda **_: calls.__setitem__("ledger", calls["ledger"] + 1))

    result = subject.build_ready_deepseek_execution_preflight(
        model_call_id=7,
        budget_record={"budget": "fake"},
    )

    assert result == expected
    assert result is not expected
    assert calls == {"preflight": 1, "transient": 0, "send": 0, "ledger": 0}


def test_public_ready_preflight_rejects_nonready_without_downstream_send(monkeypatch):
    monkeypatch.setattr(
        subject.model_gateway,
        "build_model_gateway_preflight",
        lambda **_: _preflight(local_gateway_state="blocked"),
    )
    with pytest.raises(HTTPException) as exc:
        subject.build_ready_deepseek_execution_preflight(
            model_call_id=7,
            budget_record={},
        )
    assert _code(exc) == "DEEPSEEK_EXECUTION_GATEWAY_NOT_READY"


def test_execute_reuses_the_public_ready_preflight_owner():
    source = inspect.getsource(subject.execute_deepseek_model_call)
    assert "build_ready_deepseek_execution_preflight(" in source
    assert "build_model_gateway_preflight(" not in source
