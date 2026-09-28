"""Fake-only tests for exact-scope Human report send permits."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import os
import sys
import pytest
from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
from app import deepseek_current_authority as authority  # noqa: E402
from app import report_send_authorization as send_auth  # noqa: E402

CALL = {
    "model_call_id": 41, "project_id": 7, "local_task_id": "tsk-demo", "snapshot_id": 13,
    "task_type": "daily_report_generate", "provider": authority.PROVIDER,
    "model_id": authority.MODEL_ID, "model_version": authority.MODEL_VERSION,
    "rule_version": "rules/1.0", "output_schema_version": "daily-report/1.0",
    "benchmark_sample_pack_version": "samples/1.0", "call_identity_hash": "a" * 64,
    "preparation_state": "prepared",
}


def prepared(payload_hash="c" * 64):
    return {
        "preparation_state": "prepared", "provider_send_state": "not_attempted",
        "local_request_readiness_state": "ready_for_gateway_evaluation",
        "next_gate": "exact_human_send_authorization_required", "model_call_id": 41,
        "call_identity_hash": "a" * 64, "final_context_manifest_hash": "b" * 64,
        "framed_payload_hash": payload_hash, "admitted_target_count": 6, "denied_target_count": 0,
    }


def code(exc): return exc.value.detail["code"]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    send_auth.revoke_report_send_authorization()
    for key in ("ANXIN_DEEPSEEK_SEND_AUTHORIZED", "ANXIN_DEEPSEEK_SEND_PURPOSE_ID", "ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH"):
        monkeypatch.delenv(key, raising=False)
    yield
    send_auth.revoke_report_send_authorization()


def install(monkeypatch, value=None):
    value = deepcopy(value or prepared())
    monkeypatch.setattr(send_auth.report_generation_preparation, "prepare_report_generation_model_call", lambda **_kwargs: deepcopy(value))
    monkeypatch.setattr(send_auth.model_call_ledger, "get_model_call", lambda model_call_id: deepcopy(CALL) if model_call_id == 41 else None)


def preview():
    return send_auth.build_report_send_authorization_preview(project_id=7, local_task_id="tsk-demo", expected_model_call_id=41)


def test_batch_child_permit_is_removed_when_revoked_mid_scope(monkeypatch):
    from app import report_generation_batches, final_context_manifest
    install(monkeypatch, {**prepared(), "batch_plan": {"plan_hash": "f" * 64}})
    value = preview()
    send_auth.authorize_report_send_scope(project_id=7, local_task_id="tsk-demo", model_call_id=41,
        expected_data_scope_hash=value["data_scope_hash"], human_confirmed=True)
    send_auth.begin_authorized_report_execution(project_id=7, local_task_id="tsk-demo", model_call_id=41,
        expected_data_scope_hash=value["data_scope_hash"])
    monkeypatch.setattr(report_generation_batches, "get_plan", lambda _: {"plan_hash": "f" * 64})
    monkeypatch.setattr(report_generation_batches, "get_batch_parent_call_ids", lambda _: [42])
    monkeypatch.setattr(send_auth.model_call_ledger, "get_model_call", lambda i: {**CALL, "model_call_id": i})
    monkeypatch.setattr(final_context_manifest, "build_final_context_manifest", lambda **_: {
        "final_context_manifest_hash": "d" * 64, "framed_payload_hash": "e" * 64})
    with send_auth.authorized_batch_scope(41, 42, {}):
        assert os.environ[send_auth._ENV_SCOPE] != value["data_scope_hash"]
        send_auth.revoke_report_send_authorization()
        assert send_auth._ENV_SCOPE not in os.environ
    assert send_auth._ENV_SCOPE not in os.environ


def test_batch_scope_cannot_borrow_another_parent_permit(monkeypatch):
    from app import report_generation_batches
    install(monkeypatch, {**prepared(), "batch_plan": {"plan_hash": "f" * 64}})
    value = preview()
    send_auth.authorize_report_send_scope(project_id=7, local_task_id="tsk-demo", model_call_id=41,
        expected_data_scope_hash=value["data_scope_hash"], human_confirmed=True)
    send_auth.begin_authorized_report_execution(project_id=7, local_task_id="tsk-demo", model_call_id=41,
        expected_data_scope_hash=value["data_scope_hash"])
    monkeypatch.setattr(report_generation_batches, "get_plan", lambda _: {"plan_hash": "f" * 64})
    monkeypatch.setattr(report_generation_batches, "get_batch_parent_call_ids", lambda _: [42])
    with pytest.raises(HTTPException):
        with send_auth.authorized_batch_scope(999, 42, {}):
            pytest.fail("foreign parent must not execute")


def test_preview_reuses_current_authority_scope_without_provider_io(monkeypatch):
    install(monkeypatch)
    monkeypatch.setattr(authority, "_fetch_models_source", lambda: pytest.fail("no provider I/O"))
    monkeypatch.setattr(authority, "_fetch_metadata_source", lambda: pytest.fail("no provider I/O"))
    result = preview()
    payload = authority._data_scope_payload(call=deepcopy(CALL), final_context_manifest_hash="b" * 64, framed_payload_hash="c" * 64)
    assert result["data_scope_hash"] == authority._stable_hash(payload)
    assert result["provider_send_state"] == "not_attempted"
    assert "ANXIN_DEEPSEEK_SEND_AUTHORIZED" not in os.environ


def test_authorization_requires_explicit_confirmation_and_exact_scope(monkeypatch):
    install(monkeypatch)
    scope = preview()["data_scope_hash"]
    with pytest.raises(HTTPException) as missing:
        send_auth.authorize_report_send_scope(project_id=7, local_task_id="tsk-demo", model_call_id=41, expected_data_scope_hash=scope, human_confirmed=False)
    assert code(missing) == "REPORT_SEND_AUTHORIZATION_INVALID"
    with pytest.raises(HTTPException) as drift:
        send_auth.authorize_report_send_scope(project_id=7, local_task_id="tsk-demo", model_call_id=41, expected_data_scope_hash="d" * 64, human_confirmed=True)
    assert code(drift) == "REPORT_SEND_AUTHORIZATION_INVALID"
    assert "ANXIN_DEEPSEEK_SEND_AUTHORIZED" not in os.environ


def test_foreign_or_unowned_process_permit_is_never_overwritten(monkeypatch):
    install(monkeypatch)
    scope = preview()["data_scope_hash"]
    monkeypatch.setenv("ANXIN_DEEPSEEK_SEND_AUTHORIZED", "true")
    monkeypatch.setenv("ANXIN_DEEPSEEK_SEND_PURPOSE_ID", "another_exact_purpose")
    monkeypatch.setenv("ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH", "f" * 64)
    with pytest.raises(HTTPException) as blocked:
        send_auth.authorize_report_send_scope(project_id=7, local_task_id="tsk-demo", model_call_id=41, expected_data_scope_hash=scope, human_confirmed=True)
    assert code(blocked) == "REPORT_SEND_AUTHORIZATION_INVALID"
    assert os.environ["ANXIN_DEEPSEEK_SEND_AUTHORIZED"] == "true"
    assert os.environ["ANXIN_DEEPSEEK_SEND_PURPOSE_ID"] == "another_exact_purpose"
    assert os.environ["ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH"] == "f" * 64


def test_permit_is_short_lived_single_claim_and_cleared(monkeypatch):
    install(monkeypatch)
    clock = {"now": 100.0}
    monkeypatch.setattr(send_auth.time, "monotonic", lambda: clock["now"])
    scope = preview()["data_scope_hash"]
    result = send_auth.authorize_report_send_scope(project_id=7, local_task_id="tsk-demo", model_call_id=41, expected_data_scope_hash=scope, human_confirmed=True)
    assert result["authorization_state"] == "authorized_once"
    assert os.environ["ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH"] == scope
    send_auth.begin_authorized_report_execution(project_id=7, local_task_id="tsk-demo", model_call_id=41, expected_data_scope_hash=scope)
    send_auth.finish_authorized_report_execution(scope)
    assert "ANXIN_DEEPSEEK_SEND_AUTHORIZED" not in os.environ
    with pytest.raises(HTTPException) as reused:
        send_auth.begin_authorized_report_execution(project_id=7, local_task_id="tsk-demo", model_call_id=41, expected_data_scope_hash=scope)
    assert code(reused) == "REPORT_SEND_AUTHORIZATION_NOT_READY"


def test_duplicate_in_flight_request_is_rejected_without_revoking_first_claim(monkeypatch):
    install(monkeypatch)
    scope = preview()["data_scope_hash"]
    send_auth.authorize_report_send_scope(project_id=7, local_task_id="tsk-demo", model_call_id=41, expected_data_scope_hash=scope, human_confirmed=True)
    send_auth.begin_authorized_report_execution(project_id=7, local_task_id="tsk-demo", model_call_id=41, expected_data_scope_hash=scope)
    with pytest.raises(HTTPException) as duplicate:
        send_auth.begin_authorized_report_execution(project_id=7, local_task_id="tsk-demo", model_call_id=41, expected_data_scope_hash=scope)
    assert code(duplicate) == "REPORT_SEND_AUTHORIZATION_NOT_READY"
    assert os.environ["ANXIN_DEEPSEEK_SEND_AUTHORIZED"] == "true"
    assert os.environ["ANXIN_DEEPSEEK_SEND_PURPOSE_ID"] == authority.PURPOSE_ID
    assert os.environ["ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH"] == scope
    send_auth.finish_authorized_report_execution(scope)
    assert "ANXIN_DEEPSEEK_SEND_AUTHORIZED" not in os.environ


def test_expired_permit_is_rejected_and_cleared(monkeypatch):
    install(monkeypatch)
    clock = {"now": 200.0}
    monkeypatch.setattr(send_auth.time, "monotonic", lambda: clock["now"])
    scope = preview()["data_scope_hash"]
    send_auth.authorize_report_send_scope(project_id=7, local_task_id="tsk-demo", model_call_id=41, expected_data_scope_hash=scope, human_confirmed=True)
    clock["now"] += send_auth.PERMIT_TTL_SECONDS + 1
    with pytest.raises(HTTPException) as expired:
        send_auth.begin_authorized_report_execution(project_id=7, local_task_id="tsk-demo", model_call_id=41, expected_data_scope_hash=scope)
    assert code(expired) == "REPORT_SEND_AUTHORIZATION_NOT_READY"
    assert "ANXIN_DEEPSEEK_SEND_AUTHORIZED" not in os.environ


def test_scope_drift_after_confirmation_fails_closed(monkeypatch):
    install(monkeypatch)
    scope = preview()["data_scope_hash"]
    send_auth.authorize_report_send_scope(project_id=7, local_task_id="tsk-demo", model_call_id=41, expected_data_scope_hash=scope, human_confirmed=True)
    changed = prepared("e" * 64)
    monkeypatch.setattr(send_auth.report_generation_preparation, "prepare_report_generation_model_call", lambda **_kwargs: deepcopy(changed))
    with pytest.raises(HTTPException) as drift:
        send_auth.begin_authorized_report_execution(project_id=7, local_task_id="tsk-demo", model_call_id=41, expected_data_scope_hash=scope)
    assert code(drift) == "REPORT_SEND_AUTHORIZATION_INVALID"
