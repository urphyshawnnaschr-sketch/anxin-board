from __future__ import annotations

from copy import deepcopy
import inspect

import pytest
from fastapi import HTTPException

from app import report_generation_execution as subject


TASK = {
    "id": 31,
    "project_id": 7,
    "local_task_id": "task-real-1",
    "evidence_snapshot_id": 19,
    "task_type": "daily_report_generate",
    "state": "queued",
    "identity_hash": "a" * 64,
}
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
    "preparation_state": "prepared",
    "call_identity_hash": "b" * 64,
}
PREFLIGHT = {
    "model_call_id": 41,
    "call_identity_hash": "b" * 64,
    "provider": "deepseek",
    "model_id": "deepseek-flash",
    "model_version": "DeepSeek-V4.1-Flash",
    "task_type": "daily_report_generate",
    "output_schema_version": "daily-report/1.0",
}
BUDGET_RECORD = {
    "provider": "deepseek",
    "model_id": "deepseek-flash",
    "model_version": "DeepSeek-V4.1-Flash",
    "budget": "deterministic",
}
RESULT = {
    "model_result_id": 51,
    "model_call_id": 41,
    "project_id": 7,
    "snapshot_id": 19,
    "local_task_id": "task-real-1",
    "task_type": "daily_report_generate",
    "call_identity_hash": "b" * 64,
}
REPORT = {"report_version": {"report_version_id": 61}}


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "MODEL_EXECUTION_RESULT_NOT_FOUND", "message": "missing"},
    )


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def _install(
    monkeypatch,
    *,
    initial_state="queued",
    result_sequence=None,
    preflight=None,
    execute=None,
    materialize=None,
    call=None,
    budget_record=None,
):
    task_state = {"value": initial_state}
    calls = []
    result_sequence = list(result_sequence or [None])
    call = deepcopy(call or CALL)
    budget_record = deepcopy(budget_record or BUDGET_RECORD)

    def get_task(**kwargs):
        calls.append(("get_task", kwargs))
        return {**deepcopy(TASK), "state": task_state["value"]}

    def get_call(model_call_id):
        calls.append(("get_call", model_call_id))
        return deepcopy(call)

    def get_result(model_call_id):
        calls.append(("get_result", model_call_id))
        item = result_sequence.pop(0) if result_sequence else None
        if item is None:
            raise _not_found()
        if isinstance(item, Exception):
            raise item
        return deepcopy(item)

    def budget(*, provider):
        calls.append(("budget", {"provider": provider}))
        return deepcopy(budget_record)

    def ready(**kwargs):
        calls.append(("preflight", kwargs))
        if isinstance(preflight, Exception):
            raise preflight
        return deepcopy(preflight or PREFLIGHT)

    def run(**kwargs):
        # Mirror the real model_execution ordering: all pre-send readiness first, then
        # the durable send-boundary callback, then provider transport.
        ready_value = ready(
            model_call_id=kwargs["model_call_id"],
            budget_record=kwargs["budget_record"],
        )
        expected = {
            "call_identity_hash": call.get("call_identity_hash"),
            "provider": call.get("provider"),
            "model_id": call.get("model_id"),
            "model_version": call.get("model_version"),
            "task_type": call.get("task_type"),
            "output_schema_version": call.get("output_schema_version"),
        }
        if any(ready_value.get(field) != value for field, value in expected.items()):
            raise subject._binding_invalid("Gateway ready preflight 与 durable Model Call identity 漂移。")
        callback = kwargs.get("before_provider_send")
        if callback is not None:
            callback()
        calls.append(("execute", kwargs))
        if isinstance(execute, Exception):
            raise execute
        return deepcopy(execute or RESULT)

    def materialize_report(**kwargs):
        calls.append(("materialize", kwargs))
        if isinstance(materialize, Exception):
            raise materialize
        return deepcopy(materialize or REPORT)

    def transition(**kwargs):
        calls.append(("transition", kwargs.copy()))
        assert task_state["value"] == kwargs["expected_state"]
        task_state["value"] = kwargs["new_state"]
        return {**deepcopy(TASK), "state": task_state["value"]}

    monkeypatch.setattr(subject, "get_report_generation_task", get_task)
    monkeypatch.setattr(subject, "get_model_call", get_call)
    monkeypatch.setattr(subject.model_execution_results, "get_model_execution_result_for_call", get_result)
    monkeypatch.setattr(subject.report_generation_preparation, "get_report_generation_budget_record", budget)
    monkeypatch.setattr(subject.model_execution, "build_ready_model_execution_preflight", ready)
    monkeypatch.setattr(subject.model_execution, "execute_model_call", run)
    monkeypatch.setattr(subject.report_review, "materialize_report_version", materialize_report)
    monkeypatch.setattr(subject, "transition_report_generation_task", transition)
    return task_state, calls


def test_happy_path_ready_then_running_then_execute_report_then_succeeded(monkeypatch):
    state, calls = _install(monkeypatch, result_sequence=[None])

    result = subject.execute_prepared_report_generation(
        project_id=7,
        local_task_id="task-real-1",
        model_call_id=41,
    )

    assert state["value"] == "succeeded"
    names = [name for name, _ in calls]
    assert names == [
        "get_task",
        "get_call",
        "get_result",
        "budget",
        "preflight",
        "transition",
        "execute",
        "materialize",
        "transition",
    ]
    assert calls[3][1] == {"provider": "deepseek"}
    assert calls[5][1]["expected_state"] == "queued"
    assert calls[5][1]["new_state"] == "running"
    assert calls[-1][1]["expected_state"] == "running"
    assert calls[-1][1]["new_state"] == "succeeded"
    assert result == {
        "schema_version": "report_generation_execution_lifecycle_v1",
        "project_id": 7,
        "local_task_id": "task-real-1",
        "model_call_id": 41,
        "model_result_id": 51,
        "report_version_id": 61,
        "task_state": "succeeded",
        "execution_source": "new_provider_execution",
        "provider_retry_state": "not_retried",
    }


def test_not_ready_leaves_task_queued_and_never_calls_execute(monkeypatch):
    failure = HTTPException(
        status_code=409,
        detail={"code": "MODEL_PROVIDER_AUTHORIZATION_NOT_CURRENT"},
    )
    state, calls = _install(monkeypatch, result_sequence=[None], preflight=failure)

    with pytest.raises(HTTPException) as exc:
        subject.execute_prepared_report_generation(
            project_id=7,
            local_task_id="task-real-1",
            model_call_id=41,
        )
    assert exc.value is failure
    assert state["value"] == "queued"
    assert "execute" not in [name for name, _ in calls]
    assert "transition" not in [name for name, _ in calls]


def test_existing_verified_result_is_finalized_without_gateway_or_provider(monkeypatch):
    state, calls = _install(monkeypatch, result_sequence=[RESULT])

    result = subject.execute_prepared_report_generation(
        project_id=7,
        local_task_id="task-real-1",
        model_call_id=41,
    )

    names = [name for name, _ in calls]
    assert "budget" not in names
    assert "preflight" not in names
    assert "execute" not in names
    assert state["value"] == "succeeded"
    assert result["execution_source"] == "existing_verified_result"


def test_existing_result_binding_drift_leaves_queued_and_never_enters_running(monkeypatch):
    state, calls = _install(
        monkeypatch,
        result_sequence=[{**RESULT, "snapshot_id": 999}],
    )

    with pytest.raises(HTTPException) as exc:
        subject.execute_prepared_report_generation(
            project_id=7,
            local_task_id="task-real-1",
            model_call_id=41,
        )

    assert _code(exc) == "REPORT_GENERATION_EXECUTION_BINDING_INVALID"
    assert state["value"] == "queued"
    names = [name for name, _ in calls]
    assert "transition" not in names
    assert "preflight" not in names
    assert "execute" not in names
    assert "materialize" not in names


def test_budget_provider_identity_drift_fails_before_gateway_or_running(monkeypatch):
    state, calls = _install(
        monkeypatch,
        result_sequence=[None],
        budget_record={**BUDGET_RECORD, "model_id": "other-model"},
    )

    with pytest.raises(HTTPException) as exc:
        subject.execute_prepared_report_generation(
            project_id=7,
            local_task_id="task-real-1",
            model_call_id=41,
        )

    assert _code(exc) == "REPORT_GENERATION_EXECUTION_BINDING_INVALID"
    assert state["value"] == "queued"
    names = [name for name, _ in calls]
    assert names == ["get_task", "get_call", "get_result", "budget"]


def test_missing_durable_provider_identity_fails_before_budget_or_gateway(monkeypatch):
    state, calls = _install(
        monkeypatch,
        call={**CALL, "provider": ""},
    )

    with pytest.raises(HTTPException) as exc:
        subject.execute_prepared_report_generation(
            project_id=7,
            local_task_id="task-real-1",
            model_call_id=41,
        )

    assert _code(exc) == "REPORT_GENERATION_EXECUTION_BINDING_INVALID"
    assert state["value"] == "queued"
    names = [name for name, _ in calls]
    assert names == ["get_task", "get_call"]


def test_preflight_provider_switch_fails_before_running_or_transport(monkeypatch):
    state, calls = _install(
        monkeypatch,
        result_sequence=[None],
        preflight={**PREFLIGHT, "provider": "other-provider"},
    )

    with pytest.raises(HTTPException) as exc:
        subject.execute_prepared_report_generation(
            project_id=7,
            local_task_id="task-real-1",
            model_call_id=41,
        )

    assert _code(exc) == "REPORT_GENERATION_EXECUTION_BINDING_INVALID"
    assert state["value"] == "queued"
    names = [name for name, _ in calls]
    assert "execute" not in names
    assert "transition" not in names


def test_execution_failure_without_durable_result_becomes_unknown_no_retry(monkeypatch):
    failure = HTTPException(status_code=502, detail={"code": "MODEL_PROVIDER_TRANSPORT_ERROR"})
    state, calls = _install(
        monkeypatch,
        result_sequence=[None, None],
        execute=failure,
    )

    with pytest.raises(HTTPException) as exc:
        subject.execute_prepared_report_generation(
            project_id=7,
            local_task_id="task-real-1",
            model_call_id=41,
        )
    assert exc.value is failure
    assert state["value"] == "unknown"
    transitions = [payload for name, payload in calls if name == "transition"]
    assert transitions == [
        {
            "project_id": 7,
            "local_task_id": "task-real-1",
            "expected_state": "queued",
            "new_state": "running",
        },
        {
            "project_id": 7,
            "local_task_id": "task-real-1",
            "expected_state": "running",
            "new_state": "unknown",
        },
    ]
    assert [name for name, _ in calls].count("execute") == 1


def test_execution_exception_with_durable_result_recovers_without_resend(monkeypatch):
    failure = RuntimeError("post-ledger surface failure")
    state, calls = _install(
        monkeypatch,
        result_sequence=[None, RESULT],
        execute=failure,
    )

    result = subject.execute_prepared_report_generation(
        project_id=7,
        local_task_id="task-real-1",
        model_call_id=41,
    )

    assert state["value"] == "succeeded"
    assert result["execution_source"] == "recovered_verified_result"
    assert [name for name, _ in calls].count("execute") == 1


def test_execution_exception_with_foreign_durable_result_becomes_unknown(monkeypatch):
    failure = RuntimeError("post-send surface failure")
    state, calls = _install(
        monkeypatch,
        result_sequence=[None, {**RESULT, "local_task_id": "foreign-task"}],
        execute=failure,
    )

    with pytest.raises(RuntimeError) as exc:
        subject.execute_prepared_report_generation(
            project_id=7,
            local_task_id="task-real-1",
            model_call_id=41,
        )

    assert exc.value is failure
    assert state["value"] == "unknown"
    transitions = [payload for name, payload in calls if name == "transition"]
    assert [item["new_state"] for item in transitions] == ["running", "unknown"]
    names = [name for name, _ in calls]
    assert names.count("execute") == 1
    assert "materialize" not in names


def test_report_materialization_failure_keeps_running_for_existing_result_recovery(monkeypatch):
    failure = HTTPException(status_code=409, detail={"code": "REPORT_REVIEW_SOURCE_NOT_REVIEWABLE"})
    state, calls = _install(
        monkeypatch,
        result_sequence=[None],
        materialize=failure,
    )

    with pytest.raises(HTTPException) as exc:
        subject.execute_prepared_report_generation(
            project_id=7,
            local_task_id="task-real-1",
            model_call_id=41,
        )
    assert _code(exc) == "REPORT_GENERATION_EXECUTION_FINALIZATION_PENDING"
    assert state["value"] == "running"
    transitions = [payload for name, payload in calls if name == "transition"]
    assert len(transitions) == 1
    assert transitions[0]["new_state"] == "running"


def test_invalid_materialized_report_identity_never_marks_task_succeeded(monkeypatch):
    state, calls = _install(
        monkeypatch,
        result_sequence=[None],
        materialize={"report_version": {"report_version_id": None}},
    )

    with pytest.raises(HTTPException) as exc:
        subject.execute_prepared_report_generation(
            project_id=7,
            local_task_id="task-real-1",
            model_call_id=41,
        )
    assert _code(exc) == "REPORT_GENERATION_EXECUTION_FINALIZATION_PENDING"
    assert state["value"] == "running"
    transitions = [payload for name, payload in calls if name == "transition"]
    assert transitions == [
        {
            "project_id": 7,
            "local_task_id": "task-real-1",
            "expected_state": "queued",
            "new_state": "running",
        }
    ]


def test_finalize_running_from_existing_result_never_calls_gateway_or_provider(monkeypatch):
    state, calls = _install(
        monkeypatch,
        initial_state="running",
        result_sequence=[RESULT],
    )

    result = subject.finalize_report_generation_from_existing_result(
        project_id=7,
        local_task_id="task-real-1",
        model_call_id=41,
    )

    names = [name for name, _ in calls]
    assert "budget" not in names
    assert "preflight" not in names
    assert "execute" not in names
    assert state["value"] == "succeeded"
    assert result["execution_source"] == "existing_verified_result"


def test_finalize_queued_with_foreign_result_never_consumes_running(monkeypatch):
    state, calls = _install(
        monkeypatch,
        initial_state="queued",
        result_sequence=[{**RESULT, "call_identity_hash": "c" * 64}],
    )

    with pytest.raises(HTTPException) as exc:
        subject.finalize_report_generation_from_existing_result(
            project_id=7,
            local_task_id="task-real-1",
            model_call_id=41,
        )

    assert _code(exc) == "REPORT_GENERATION_EXECUTION_BINDING_INVALID"
    assert state["value"] == "queued"
    names = [name for name, _ in calls]
    assert "transition" not in names
    assert "preflight" not in names
    assert "execute" not in names
    assert "materialize" not in names


def test_finalize_without_result_does_not_change_state_or_send(monkeypatch):
    state, calls = _install(monkeypatch, initial_state="running", result_sequence=[None])
    with pytest.raises(HTTPException) as exc:
        subject.finalize_report_generation_from_existing_result(
            project_id=7,
            local_task_id="task-real-1",
            model_call_id=41,
        )
    assert _code(exc) == "REPORT_GENERATION_EXECUTION_RESULT_UNAVAILABLE"
    assert state["value"] == "running"
    names = [name for name, _ in calls]
    assert "preflight" not in names and "execute" not in names and "transition" not in names


def test_model_call_binding_drift_fails_before_ready_or_send(monkeypatch):
    state, calls = _install(monkeypatch, call={**CALL, "snapshot_id": 999})
    with pytest.raises(HTTPException) as exc:
        subject.execute_prepared_report_generation(
            project_id=7,
            local_task_id="task-real-1",
            model_call_id=41,
        )
    assert _code(exc) == "REPORT_GENERATION_EXECUTION_BINDING_INVALID"
    assert state["value"] == "queued"
    names = [name for name, _ in calls]
    assert "preflight" not in names and "execute" not in names and "transition" not in names


def test_source_owns_sequence_only_not_provider_authority_or_transport():
    source = inspect.getsource(subject)
    for forbidden in (
        "deepseek_execution",
        "deepseek_transport",
        "deepseek_current_authority",
        "DEEPSEEK_API_KEY",
        "os.environ",
        "httpx",
        "send_deepseek_v4_flash",
        "record_model_execution_result",
        "validate_ai_response",
    ):
        assert forbidden not in source
    assert source.count("execute_model_call(") == 1
