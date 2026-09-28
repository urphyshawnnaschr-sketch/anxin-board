from __future__ import annotations

from copy import deepcopy
import inspect
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import report_generation_preparation as subject  # noqa: E402
from app.model_provider_contract import ProviderCapability  # noqa: E402


TASK = {
    "id": 31,
    "project_id": 7,
    "local_task_id": "task-real-1",
    "evidence_snapshot_id": 19,
    "task_type": "daily_report_generate",
    "state": "queued",
    "identity_hash": "a" * 64,
}
CAPABILITY = ProviderCapability(
    provider="deepseek",
    model_id="deepseek-flash",
    model_version="DeepSeek-V4.1-Flash",
    task_type="daily_report_generate",
    output_schema_version="daily-report/1.0",
    context_window_tokens=1_000_000,
    max_output_tokens=384_000,
)
CALL = {
    "model_call_id": 41,
    "project_id": 7,
    "local_task_id": "task-real-1",
    "snapshot_id": 19,
    "task_type": "daily_report_generate",
    "provider": "deepseek",
    "model_id": "deepseek-flash",
    "model_version": "DeepSeek-V4.1-Flash",
    "output_schema_version": "daily-report/1.0",
    "call_identity_hash": "b" * 64,
}
BUDGET = {
    "model_call_id": 41,
    "call_identity_hash": "b" * 64,
    "task_type": "daily_report_generate",
    "provider": "deepseek",
    "model_id": "deepseek-flash",
    "model_version": "DeepSeek-V4.1-Flash",
    "model_send_state": "not_admitted",
    "budget_profile_hash": "c" * 64,
}
MANIFEST = {
    "model_call_id": 41,
    "call_identity_hash": "b" * 64,
    "project_id": 7,
    "snapshot_id": 19,
    "task_type": "daily_report_generate",
    "provider": "deepseek",
    "model_id": "deepseek-flash",
    "model_version": "DeepSeek-V4.1-Flash",
    "budget_profile_hash": "c" * 64,
    "final_context_manifest_hash": "d" * 64,
    "framed_payload_hash": "e" * 64,
    "final_manifest_state": "finalized_local_metadata",
    "gateway_send_state": "not_evaluated",
    "final_request_fit_state": "not_evaluated",
    "local_request_readiness_state": "ready_for_gateway_evaluation",
    "context_admission_state": "all_targets_admitted",
    "budget_fit_state": "fit_by_conservative_upper_bound",
    "admitted_target_count": 4,
    "denied_target_count": 0,
    "unsupported_context_sources": [],
}


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def _install(monkeypatch, *, task=None, call=None, budget=None, manifest=None):
    from app import report_generation_batches
    monkeypatch.setattr(report_generation_batches, "request_budget_exceeded", lambda m, _: m["local_request_readiness_state"] == "blocked_context_budget")
    task = deepcopy(task or TASK)
    call = deepcopy(call or CALL)
    budget = deepcopy(budget or BUDGET)
    manifest = deepcopy(manifest or MANIFEST)
    seen = {"task": 0, "capability": 0, "prepare": 0, "budget": 0, "manifest": 0}
    args = {}

    class StubAdapter:
        provider_id = CAPABILITY.provider

        def get_capability(self, *, task_type: str, output_schema_version: str):
            seen["capability"] += 1
            assert task_type == CAPABILITY.task_type
            assert output_schema_version == CAPABILITY.output_schema_version
            return CAPABILITY

    adapter = StubAdapter()

    def get_task(**kwargs):
        seen["task"] += 1
        args["task"] = kwargs
        return deepcopy(task)

    def prepare(**kwargs):
        seen["prepare"] += 1
        args["prepare"] = kwargs
        return deepcopy(call)

    def build_budget(**kwargs):
        seen["budget"] += 1
        args["budget"] = kwargs
        return deepcopy(budget)

    def build_manifest(**kwargs):
        seen["manifest"] += 1
        args["manifest"] = kwargs
        return deepcopy(manifest)

    monkeypatch.setattr(subject, "get_report_generation_task", get_task)
    monkeypatch.setattr(
        subject.model_provider_runtime,
        "resolve_default_model_provider_adapter",
        lambda: adapter,
    )
    monkeypatch.setattr(subject.model_call_ledger, "prepare_model_call", prepare)
    monkeypatch.setattr(subject.model_budget_profiles, "build_model_budget_profile", build_budget)
    monkeypatch.setattr(subject.final_context_manifest, "build_final_context_manifest", build_manifest)
    return seen, args, adapter


def test_happy_preparation_binds_task_call_budget_manifest_without_send(monkeypatch):
    seen, args, _adapter = _install(monkeypatch)

    result = subject.prepare_report_generation_model_call(
        project_id=7,
        local_task_id="task-real-1",
        preparation_authorized=True,
    )

    assert seen == {"task": 1, "capability": 1, "prepare": 1, "budget": 1, "manifest": 1}
    assert args["task"] == {"project_id": 7, "local_task_id": "task-real-1"}
    prepared = args["prepare"]
    assert prepared["local_task_id"] == TASK["local_task_id"]
    assert prepared["snapshot_id"] == TASK["evidence_snapshot_id"]
    assert prepared["task_type"] == "daily_report_generate"
    assert prepared["provider"] == "deepseek"
    assert prepared["model_id"] == "deepseek-flash"
    assert prepared["model_version"] == "DeepSeek-V4.1-Flash"
    assert prepared["call_prepare_key"] == f"report-generate:{TASK['identity_hash']}"
    assert prepared["data_sending_authorization"] == {
        "provider": "deepseek",
        "authorized": True,
        "valid": True,
    }
    assert prepared["qualification_record"]["qualification_status"] == "qualified"

    budget_record = args["budget"]["budget_record"]
    assert args["budget"]["model_call_id"] == CALL["model_call_id"]
    assert budget_record["counting_policy_version"] == subject.context_token_framing.COUNTING_POLICY_VERSION
    assert budget_record["context_window_tokens"] == CAPABILITY.context_window_tokens
    assert budget_record["max_output_tokens"] == CAPABILITY.max_output_tokens
    assert budget_record["budget_policy_version"] == "daily-report-deepseek-flash/1.0"
    assert budget_record["reserved_output_tokens"] <= budget_record["max_output_tokens"]
    assert args["manifest"] == {
        "model_call_id": CALL["model_call_id"],
        "budget_record": budget_record,
    }

    assert result == {
        "schema_version": "report_generation_preparation_v1",
        "project_id": 7,
        "report_generation_task_id": 31,
        "local_task_id": "task-real-1",
        "evidence_snapshot_id": 19,
        "task_identity_hash": "a" * 64,
        "task_state": "queued",
        "model_call_id": 41,
        "call_identity_hash": "b" * 64,
        "budget_profile_hash": "c" * 64,
        "final_context_manifest_hash": "d" * 64,
        "framed_payload_hash": "e" * 64,
        "local_request_readiness_state": "ready_for_gateway_evaluation",
        "context_admission_state": "all_targets_admitted",
        "budget_fit_state": "fit_by_conservative_upper_bound",
        "admitted_target_count": 4,
        "denied_target_count": 0,
        "unsupported_context_source_count": 0,
        "context_findings": [],
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "preparation_state": "prepared",
        "preparation_authorization_state": "recorded_assertion_only",
        "provider_send_state": "not_attempted",
        "next_gate": "exact_human_send_authorization_required",
    }


def test_findings_never_resolve_denied_bodies_and_preserve_safe_locations(monkeypatch):
    manifest = deepcopy(MANIFEST)
    manifest["manifest_core_hash"] = "f" * 64
    manifest.update(admitted_targets=[{"target": "git:1"}], denied_targets=[{
        "target": "git:2", "matched_rule_id": "SP01"
    }], denied_target_count=1)
    _install(monkeypatch, manifest=manifest)
    monkeypatch.setattr(subject.context_manifest, "build_context_manifest_core", lambda **kwargs: {
        "manifest_core_hash": "f" * 64, "items": [{"evidence_id": "git:2", "path": ".env"}]
    })
    monkeypatch.setattr(
        subject.context_redaction_runtime,
        "build_context_redaction_result",
        lambda **kwargs: {"redaction_state": "completed", "redaction_match_count": 1},
    )
    calls = []
    def diagnostics(**kwargs):
        calls.append(kwargs["target"])
        assert kwargs["target"] == "git:1"
        return [{"target": "git:1", "path": "src/style.scss", "line_start": 4,
                 "line_end": 4, "rule_id": "R3", "risk_level": "warning",
                 "action": "isolated", "safe_snippet": "[REDACTED:CREDENTIAL]"}]
    monkeypatch.setattr(subject.context_redaction, "build_context_redaction_diagnostics", diagnostics)
    result = subject.prepare_report_generation_model_call(
        project_id=7, local_task_id="task-real-1", preparation_authorized=True)
    assert calls == ["git:1"]
    assert result["context_findings"][0]["action"] == "excluded"
    assert result["context_findings"][0]["path"] == ".env"
    assert result["context_findings"][1]["path"] == "src/style.scss"
    assert result["provider_send_state"] == "not_attempted"


def test_explicit_authorization_is_strict_and_stops_before_any_downstream(monkeypatch):
    seen, _, _ = _install(monkeypatch)
    for value in (False, None, 1, "true"):
        with pytest.raises(HTTPException) as exc:
            subject.prepare_report_generation_model_call(
                project_id=7,
                local_task_id="task-real-1",
                preparation_authorized=value,
            )
        assert _code(exc) == "REPORT_GENERATION_PREPARATION_AUTHORIZATION_REQUIRED"
    assert seen == {"task": 0, "capability": 0, "prepare": 0, "budget": 0, "manifest": 0}


def test_only_queued_daily_generate_task_can_prepare(monkeypatch):
    for bad in (
        {**TASK, "state": "running"},
        {**TASK, "state": "succeeded"},
        {**TASK, "state": "failed"},
        {**TASK, "state": "unknown"},
        {**TASK, "task_type": "daily_report_regenerate"},
    ):
        seen, _, _ = _install(monkeypatch, task=bad)
        with pytest.raises(HTTPException) as exc:
            subject.prepare_report_generation_model_call(
                project_id=7,
                local_task_id="task-real-1",
                preparation_authorized=True,
            )
        assert _code(exc) == "REPORT_GENERATION_PREPARATION_TASK_NOT_QUEUED"
        assert seen["prepare"] == 0
        assert seen["budget"] == 0
        assert seen["manifest"] == 0


def test_cross_binding_drift_fails_closed(monkeypatch):
    seen, _, _ = _install(monkeypatch, call={**CALL, "snapshot_id": 999})
    with pytest.raises(HTTPException) as exc:
        subject.prepare_report_generation_model_call(
            project_id=7,
            local_task_id="task-real-1",
            preparation_authorized=True,
        )
    assert _code(exc) == "REPORT_GENERATION_PREPARATION_BINDING_INVALID"
    assert seen == {"task": 1, "capability": 1, "prepare": 1, "budget": 1, "manifest": 1}


def test_local_sensitive_path_block_is_visible_and_never_becomes_send_gate(monkeypatch):
    manifest = {
        **MANIFEST,
        "local_request_readiness_state": "blocked_context_denied",
        "context_admission_state": "contains_denied_targets",
        "admitted_target_count": 3,
        "denied_target_count": 1,
    }
    _, _, _ = _install(monkeypatch, manifest=manifest)
    result = subject.prepare_report_generation_model_call(
        project_id=7,
        local_task_id="task-real-1",
        preparation_authorized=True,
    )
    assert result["local_request_readiness_state"] == "blocked_context_denied"
    assert result["denied_target_count"] == 1
    assert result["provider_send_state"] == "not_attempted"
    assert result["next_gate"] == "local_context_denied"


def test_local_budget_overflow_prepares_batches_without_sending(monkeypatch):
    manifest = {
        **MANIFEST,
        "local_request_readiness_state": "blocked_context_budget",
        "budget_fit_state": "not_fit_by_conservative_upper_bound",
    }
    _, _, _ = _install(monkeypatch, manifest=manifest)
    from app import report_generation_batches
    plan = {"can_resume": True, "batch_count": 2}
    monkeypatch.setattr(report_generation_batches, "prepare_plan", lambda **_: plan)
    monkeypatch.setattr(report_generation_batches, "summary", lambda value: value)
    result = subject.prepare_report_generation_model_call(
        project_id=7,
        local_task_id="task-real-1",
        preparation_authorized=True,
    )
    assert result["local_request_readiness_state"] == "ready_for_gateway_evaluation"
    assert result["provider_send_state"] == "not_attempted"
    assert result["next_gate"] == "exact_human_send_authorization_required"
    assert result["batch_plan"]["batch_count"] == 2


def test_manifest_hash_or_identity_drift_fails_closed(monkeypatch):
    for manifest in (
        {**MANIFEST, "model_call_id": 999},
        {**MANIFEST, "final_context_manifest_hash": "bad"},
        {**MANIFEST, "local_request_readiness_state": "invented"},
        {**MANIFEST, "unsupported_context_sources": "not-a-list"},
    ):
        _, _, _ = _install(monkeypatch, manifest=manifest)
        with pytest.raises(HTTPException) as exc:
            subject.prepare_report_generation_model_call(
                project_id=7,
                local_task_id="task-real-1",
                preparation_authorized=True,
            )
        assert _code(exc) == "REPORT_GENERATION_PREPARATION_BINDING_INVALID"


def test_budget_record_is_detached_and_does_not_grant_send(monkeypatch):
    class StubAdapter:
        provider_id = CAPABILITY.provider

        def get_capability(self, *, task_type: str, output_schema_version: str):
            assert task_type == CAPABILITY.task_type
            assert output_schema_version == CAPABILITY.output_schema_version
            return CAPABILITY

    monkeypatch.setattr(
        subject.model_provider_runtime,
        "resolve_default_model_provider_adapter",
        lambda: StubAdapter(),
    )
    first = subject.get_report_generation_budget_record()
    first["reserved_output_tokens"] = 1
    second = subject.get_report_generation_budget_record()
    assert second["reserved_output_tokens"] == 64_000
    assert "model_send_state" not in second


def test_source_has_no_provider_execution_credential_or_task_transition_authority():
    source = inspect.getsource(subject)
    for forbidden in (
        "deepseek_transport",
        "deepseek_execution",
        "deepseek_current_authority",
        "send_deepseek",
        "DEEPSEEK_API_KEY",
        "os.environ",
        "httpx",
        "transition_report_generation_task",
        "materialize_report_version",
        "record_model_execution_result",
    ):
        assert forbidden not in source
    assert "resolve_default_model_provider_adapter" in source
