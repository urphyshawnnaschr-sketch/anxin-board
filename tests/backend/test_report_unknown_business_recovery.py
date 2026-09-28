"""Regression for low-friction Human recovery from historical UNKNOWN report tasks."""
from pathlib import Path
import inspect
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import report_generation_recovery_api as api
from app import main

TASK = {
    "project_id": 7,
    "local_task_id": "old-unknown",
    "evidence_snapshot_id": 3,
    "task_type": "daily_report_generate",
    "state": "unknown",
}


@pytest.fixture
def seams(monkeypatch):
    monkeypatch.setattr(api, "require_local_write_request", lambda *_a, **_k: None)
    monkeypatch.setattr(api, "get_report_generation_task", lambda **_k: dict(TASK))
    transitions = []

    def transition(**kwargs):
        transitions.append(kwargs)
        return {**TASK, "state": kwargs["new_state"]}

    monkeypatch.setattr(api, "transition_report_generation_task", transition)
    return transitions


def _call(human_confirmed=True):
    return api.void_unknown_report_generation_task_http(
        project_id=7,
        local_task_id="old-unknown",
        payload=api.VoidUnknownReportGenerationTaskRequest(
            human_confirmed=human_confirmed
        ),
        request=object(),
    )


def test_unknown_recovery_requires_explicit_button_action(seams):
    with pytest.raises(HTTPException) as exc:
        _call(False)
    assert exc.value.detail["code"] == "REPORT_GENERATION_TASK_UNKNOWN_VOID_CONFIRMATION_REQUIRED"
    assert seams == []


def test_unknown_recovery_releases_chain_without_provider_retry(seams):
    result = _call(True)
    assert result["state"] == "voided"
    assert result["provider_retry_state"] == "not_retried"
    assert result["abandonment_state"] == "human_confirmed_unknown_voided"
    assert seams == [{
        "project_id": 7,
        "local_task_id": "old-unknown",
        "expected_state": "unknown",
        "new_state": "voided",
    }]


@pytest.mark.parametrize("state", ["queued", "running", "failed", "succeeded", "voided"])
def test_recovery_only_changes_unknown_state(seams, monkeypatch, state):
    monkeypatch.setattr(
        api, "get_report_generation_task", lambda **_k: {**TASK, "state": state}
    )
    with pytest.raises(HTTPException) as exc:
        _call(True)
    assert exc.value.detail["code"] == "REPORT_GENERATION_TASK_UNKNOWN_VOID_NOT_ALLOWED"
    assert seams == []


def test_recovery_api_has_no_provider_transport_or_credentials():
    source = inspect.getsource(api)
    for forbidden in (
        "execute_model_call",
        "model_provider_gateway",
        "deepseek_transport",
        "DEEPSEEK_API_KEY",
        "httpx",
        "requests",
    ):
        assert forbidden not in source


def test_recovery_route_is_separate_from_frozen_task_api_contract():
    routes = {
        (route.path, frozenset(route.methods or set()))
        for route in main.app.routes
    }
    assert (
        "/api/projects/{project_id}/report-generation-recovery/void-unknown/{local_task_id:path}",
        frozenset({"POST"}),
    ) in routes


def test_frontend_exposes_single_click_unknown_escape_hatch():
    root = Path(__file__).resolve().parents[2]
    api_source = (root / "apps/frontend/src/api/taskExecution.js").read_text(encoding="utf-8")
    view_source = (root / "apps/frontend/src/views/TaskExecutionView.vue").read_text(encoding="utf-8")
    assert "report-generation-recovery/void-unknown" in api_source
    assert "{ guarded: true }" in api_source
    assert "voidUnknownAnalysis" in view_source
    assert "放弃不确定任务并重新开始" in view_source
    assert "window?.confirm" not in view_source
