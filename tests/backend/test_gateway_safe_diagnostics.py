from __future__ import annotations

import json
import pytest
from fastapi import HTTPException
from app import deepseek_provider_adapter, model_execution, model_provider_gateway
from app.model_gateway_diagnostics import safe_authority_error
from test_model_provider_gateway import _manifest, _SyntheticAdapter, _install, _budget
from test_model_execution import _preflight


@pytest.mark.parametrize("source,stage", [
    ("DEEPSEEK_AUTHORITY_CREDENTIAL_UNAVAILABLE", "credential"),
    ("DEEPSEEK_AUTHORITY_QUALIFICATION_UNAVAILABLE", "model_availability"),
    ("DEEPSEEK_AUTHORITY_MODEL_METADATA_UNAVAILABLE", "model_metadata"),
    ("DEEPSEEK_AUTHORITY_MODEL_VERSION_CHANGED", "model_metadata"),
    ("DEEPSEEK_AUTHORITY_DATA_SEND_NOT_AUTHORIZED", "authorization"),
])
def test_source_failure_survives_adapter_gateway_execution_without_secret(monkeypatch, source, stage):
    manifest = _manifest()
    adapter = _SyntheticAdapter(manifest)
    adapter.authority_failure = deepseek_provider_adapter._map_authority_failure(
        HTTPException(status_code=409, detail={"code": source, "message": "DO-NOT-ECHO-SECRET", "raw": "DO-NOT-ECHO-SECRET"})
    )
    _install(monkeypatch, manifest, adapter)
    with pytest.raises(HTTPException) as error:
        model_execution.build_ready_model_execution_preflight(model_call_id=7, budget_record=_budget())
    assert error.value.detail["cause_code"] == source
    assert error.value.detail["stage"] == stage
    assert "DO-NOT-ECHO-SECRET" not in json.dumps(error.value.detail)
    assert error.value.detail["network_send_state"] == "not_attempted"
    assert adapter.execute_calls == 0


def test_unknown_error_detail_never_echoes_original_message_or_code():
    error = safe_authority_error({"code": "DO-NOT-ECHO-SECRET", "message": "DO-NOT-ECHO-SECRET"})
    assert error.detail["cause_code"] == "MODEL_PROVIDER_GATEWAY_DIAGNOSTIC_UNAVAILABLE"
    assert "DO-NOT-ECHO-SECRET" not in json.dumps(error.detail)


@pytest.mark.parametrize("state,stage", [
    ("blocked_final_manifest", "context_manifest"),
    ("blocked_provider_compatibility", "provider_compatibility"),
    ("blocked_request_budget", "request_budget"),
    ("blocked_current_authorization", "authorization"),
])
def test_other_blocked_states_are_actionable_and_still_blocked(state, stage):
    preflight = _preflight(model_call_id=7)
    preflight["local_gateway_state"] = state
    with pytest.raises(HTTPException) as error:
        model_execution._require_ready_preflight(preflight, model_call_id=7)
    assert error.value.detail["stage"] == stage
    assert error.value.detail["code"] == "MODEL_EXECUTION_GATEWAY_NOT_READY"


def test_default_gateway_contract_remains_unchanged(monkeypatch):
    manifest = _manifest()
    adapter = _SyntheticAdapter(manifest)
    adapter.authority_failure = deepseek_provider_adapter._map_authority_failure(
        HTTPException(status_code=409, detail={"code": "DEEPSEEK_AUTHORITY_CREDENTIAL_UNAVAILABLE"})
    )
    _install(monkeypatch, manifest, adapter)
    result = model_provider_gateway.build_model_provider_gateway_preflight(model_call_id=7, budget_record=_budget())
    assert set(result) == model_execution._PREFLIGHT_KEYS
    assert result["local_gateway_state"] == "blocked_current_authority_unavailable"
    assert adapter.execute_calls == 0


@pytest.mark.parametrize("credential_code", ["SECRET_NOT_FOUND", "SECRET_ACCESS_DENIED", "SECRET_STORE_OS_FAILURE", "SECRET_STORE_UNSUPPORTED_PLATFORM", "SECRET_VALUE_INVALID"])
def test_credential_failure_is_actionable_and_logs_only_safe_enums(monkeypatch, caplog, credential_code):
    manifest = _manifest()
    adapter = _SyntheticAdapter(manifest)
    adapter.authority_failure = deepseek_provider_adapter._map_authority_failure(
        HTTPException(status_code=409, detail={
            "code": "DEEPSEEK_AUTHORITY_CREDENTIAL_UNAVAILABLE",
            "credential_error_code": credential_code,
            "message": "DO-NOT-ECHO-SECRET", "authorization": "DO-NOT-ECHO-SECRET",
        })
    )
    _install(monkeypatch, manifest, adapter)
    with pytest.raises(HTTPException) as error:
        model_execution.build_ready_model_execution_preflight(model_call_id=7, budget_record=_budget())
    assert error.value.detail["credential_error_code"] == credential_code
    assert credential_code in caplog.text
    assert "DO-NOT-ECHO-SECRET" not in caplog.text + json.dumps(error.value.detail)
    assert adapter.execute_calls == 0


def test_unknown_credential_error_is_not_echoed(caplog):
    error = safe_authority_error({
        "code": "MODEL_PROVIDER_AUTHORITY_UNAVAILABLE",
        "cause_code": "DEEPSEEK_AUTHORITY_CREDENTIAL_UNAVAILABLE",
        "credential_error_code": "DO-NOT-ECHO-SECRET",
    })
    assert "credential_error_code" not in error.detail
    assert "DO-NOT-ECHO-SECRET" not in caplog.text + json.dumps(error.detail)


def test_batch_preflight_preserves_diagnostic_before_claim_or_send(monkeypatch):
    from contextlib import nullcontext
    from app import report_generation_batches as batches, report_send_authorization
    manifest = _manifest()
    adapter = _SyntheticAdapter(manifest)
    adapter.authority_failure = deepseek_provider_adapter._map_authority_failure(
        HTTPException(status_code=409, detail={"code": "DEEPSEEK_AUTHORITY_MODEL_METADATA_UNAVAILABLE"})
    )
    _install(monkeypatch, manifest, adapter)
    plan = {"batches": [{"ordinal": 1, "model_call_id": 7}], "states": [{"state": "pending"}]}
    monkeypatch.setattr(batches, "get_plan", lambda _: plan)
    monkeypatch.setattr(batches, "_recover_completed_claims", lambda value: value)
    monkeypatch.setattr(batches, "summary", lambda _: {"can_resume": True})
    monkeypatch.setattr(batches, "validate_plan", lambda *_: None)
    monkeypatch.setattr(report_send_authorization, "authorized_batch_scope", lambda *_: nullcontext())
    monkeypatch.setattr(batches, "_set_state", lambda *_: pytest.fail("must not claim a failed preflight"))
    with pytest.raises(HTTPException) as error:
        batches.execute_plan(task={"state": "running"}, parent={"model_call_id": 1}, budget_record=_budget())
    assert error.value.detail["cause_code"] == "DEEPSEEK_AUTHORITY_MODEL_METADATA_UNAVAILABLE"
    assert error.value.detail["stage"] == "model_metadata"
    assert adapter.execute_calls == 0
