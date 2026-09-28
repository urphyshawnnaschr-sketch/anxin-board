"""Regression for deterministic report-output contract failures."""
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import model_execution_results as results
from app import model_provider_gateway as gateway
from app import report_generation_batches as batches
from app import report_generation_execution as execution
from app.ai_contracts import ValidationIssue


def test_output_invalid_exposes_only_safe_issue_identity():
    exc = results._output_invalid((
        ValidationIssue(
            code="AI_EVIDENCE_UNKNOWN",
            path="$.result.feature_progress[0].evidence_ids[0]",
            message="must never be exposed: raw-provider-body",
            severity="error",
        ),
    ))
    assert exc.detail["code"] == "MODEL_EXECUTION_RESULT_OUTPUT_INVALID"
    assert exc.detail["stage"] == "output_contract"
    assert exc.detail["cause_code"] == "AI_OUTPUT_CONTRACT_INVALID"
    assert exc.detail["validation_issues"] == [{
        "code": "AI_EVIDENCE_UNKNOWN",
        "path": "$.result.feature_progress[0].evidence_ids[0]",
    }]
    assert "raw-provider-body" not in str(exc.detail)


def _run_execution_failure(monkeypatch, raised):
    task = {
        "project_id": 7,
        "local_task_id": "task-output-contract",
        "state": "queued",
        "evidence_snapshot_id": 3,
        "task_type": "daily_report_generate",
    }
    call = {
        "model_call_id": 11,
        "project_id": 7,
        "local_task_id": task["local_task_id"],
        "snapshot_id": 3,
        "task_type": "daily_report_generate",
        "preparation_state": "prepared",
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "output_schema_version": "daily-report/1.0",
        "call_identity_hash": "a" * 64,
    }
    transitions = []

    monkeypatch.setattr(execution, "_load_bound_subject", lambda **_: (dict(task), dict(call)))
    monkeypatch.setattr(batches, "get_plan", lambda _call_id: None)
    monkeypatch.setattr(
        execution.report_generation_preparation,
        "get_report_generation_budget_record",
        lambda **_: {
            "provider": call["provider"],
            "model_id": call["model_id"],
            "model_version": call["model_version"],
        },
    )
    monkeypatch.setattr(
        execution.model_execution,
        "build_ready_model_execution_preflight",
        lambda **_: {
            "call_identity_hash": call["call_identity_hash"],
            "provider": call["provider"],
            "model_id": call["model_id"],
            "model_version": call["model_version"],
            "task_type": call["task_type"],
            "output_schema_version": call["output_schema_version"],
        },
    )
    monkeypatch.setattr(execution, "_read_result_if_available", lambda _call_id: None)

    def transition(**kwargs):
        transitions.append(kwargs["new_state"])
        return {**task, "state": kwargs["new_state"]}

    monkeypatch.setattr(execution, "transition_report_generation_task", transition)
    def execute_after_send_boundary(**kwargs):
        kwargs["before_provider_send"]()
        raise raised

    monkeypatch.setattr(
        execution.model_execution,
        "execute_model_call",
        execute_after_send_boundary,
    )
    with pytest.raises(type(raised)):
        execution.execute_prepared_report_generation(
            project_id=7,
            local_task_id=task["local_task_id"],
            model_call_id=11,
        )
    return transitions


def test_pre_send_failure_never_marks_task_running_or_unknown(monkeypatch):
    raised = HTTPException(
        status_code=409,
        detail={
            "code": "MODEL_EXECUTION_GATEWAY_NOT_READY",
            "stage": "current_authority",
            "cause_code": "MODEL_EXECUTION_CURRENT_AUTHORITY",
            "message": "pre-send unavailable",
        },
    )
    task = {
        "project_id": 7, "local_task_id": "task-pre-send", "state": "queued",
        "evidence_snapshot_id": 3, "task_type": "daily_report_generate",
    }
    call = {
        "model_call_id": 11, "project_id": 7, "local_task_id": task["local_task_id"],
        "snapshot_id": 3, "task_type": "daily_report_generate",
        "preparation_state": "prepared", "provider": "deepseek",
        "model_id": "deepseek-flash", "model_version": "DeepSeek-V4.1-Flash",
        "output_schema_version": "daily-report/1.0", "call_identity_hash": "a" * 64,
    }
    transitions = []
    monkeypatch.setattr(execution, "_load_bound_subject", lambda **_: (dict(task), dict(call)))
    monkeypatch.setattr(batches, "get_plan", lambda _call_id: None)
    monkeypatch.setattr(execution, "_read_result_if_available", lambda _call_id: None)
    monkeypatch.setattr(
        execution.report_generation_preparation, "get_report_generation_budget_record",
        lambda **_: {"provider": call["provider"], "model_id": call["model_id"], "model_version": call["model_version"]},
    )
    monkeypatch.setattr(
        execution, "transition_report_generation_task",
        lambda **kwargs: transitions.append(kwargs["new_state"]) or {**task, "state": kwargs["new_state"]},
    )
    monkeypatch.setattr(
        execution.model_execution, "execute_model_call",
        lambda **_: (_ for _ in ()).throw(raised),
    )
    with pytest.raises(HTTPException) as exc:
        execution.execute_prepared_report_generation(
            project_id=7, local_task_id=task["local_task_id"], model_call_id=11
        )
    assert exc.value.detail["code"] == "MODEL_EXECUTION_GATEWAY_NOT_READY"
    assert transitions == []


def test_output_invalid_failure_receipt_persists_bounded_provider_facts(tmp_path, monkeypatch):
    import json
    import sqlite3

    database = tmp_path / "failure-receipt.sqlite3"
    def connection():
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(results, "get_connection", connection)
    issue = ValidationIssue(
        code="AI_SCHEMA_INVALID", path="$.result.feature_progress[0].stage",
        message="raw body must not persist", severity="error",
    )
    call = {
        "model_call_id": 11, "project_id": 7, "snapshot_id": 3,
        "local_task_id": "task-output-contract", "task_type": "daily_report_generate",
        "call_identity_hash": "a" * 64, "provider": "deepseek",
        "model_id": "deepseek-flash", "model_version": "DeepSeek-V4.1-Flash",
    }
    receipt = {
        "provider": "deepseek", "provider_response_id": "resp-123",
        "actual_model": "deepseek-flash", "provider_runtime_fingerprint": "runtime-v1",
        "finish_reason": "stop", "prompt_tokens": 10, "completion_tokens": 4,
        "total_tokens": 14, "result": {"secret_body_marker": "do-not-store-raw"},
    }
    saved = results._record_output_failure_receipt(call=call, receipt=receipt, issues=(issue,))
    assert saved["failure_receipt_id"] > 0
    with connection() as conn:
        row = conn.execute("SELECT * FROM model_execution_failure_receipts").fetchone()
    assert row["provider_response_id"] == "resp-123"
    assert row["prompt_tokens"] == 10
    assert json.loads(row["validation_issues_json"]) == [{
        "code": "AI_SCHEMA_INVALID", "path": "$.result.feature_progress[0].stage"
    }]
    assert "do-not-store-raw" not in str(dict(row))


def test_deterministic_output_invalid_becomes_failed_not_unknown(monkeypatch):
    raised = results._output_invalid((
        ValidationIssue(
            code="AI_SCHEMA_INVALID",
            path="$.result.feature_progress[0].stage",
            message="static validator text",
            severity="error",
        ),
    ))
    assert _run_execution_failure(monkeypatch, raised) == ["running", "failed"]


def test_ambiguous_provider_failure_still_becomes_unknown(monkeypatch):
    assert _run_execution_failure(monkeypatch, RuntimeError("network outcome unknown")) == [
        "running", "unknown"
    ]


def test_prompt_binds_structural_example_to_exact_evidence_id():
    _descriptor, example, instruction = gateway._derive_json_material(
        "daily_report_generate", allowed_evidence_ids=["git_ev_123"]
    )
    assert "git_ev_123" in instruction
    assert '"evidence_ids":["string"]' not in instruction
    assert '"evidence_ids":["git_ev_123"]' in instruction
    assert "git_ev_123" in str(example)


def test_frontend_allows_explicit_restart_of_failed_chain():
    source = (
        Path(__file__).resolve().parents[2]
        / "apps" / "frontend" / "src" / "views" / "TaskExecutionView.vue"
    ).read_text(encoding="utf-8")
    assert "restartFailedAnalysis" in source
    assert "输出校验：" in source
    assert "validation_issues" in source
